"""
Standalone Deribit Block RFQ WebSocket client.

Subscribes to Deribit block_rfq channels and writes every subscription message to:
  - MongoDB    (if mongodb.enabled in config)
  - Redis      (if redis.enabled in config)
  - Local file (if local_file.enabled in config)

Reconnects automatically on any error or disconnection.

Redis key layout
----------------
  rfq:<block_rfq_id>        block_rfq.maker.*  (non-quotes) — latest RFQ state
  rfq_trade:<block_rfq_id>  block_rfq.trades.* — latest trade for that RFQ
  rfq_quote:<quote_id>      block_rfq.maker.quotes.* — latest quote snapshot

All Redis values are JSON strings of the params.data field.
The most-recent message always overwrites the previous one (SET, not a list).

Config format
-------------
{
  "server":       "wss://www.deribit.com/ws/api/v2",
  "ping_interval": 60,
  "scope":        "session:cpp_og",
  "trade_key":    "trade_key.json"   // path string OR inline {"access_key":..,"secret_key":..}
  "subscription": {
    "method": "public/subscribe",
    "params": { "channels": [ "block_rfq.maker.BTC", ... ] }
  },
  "mongodb": {
    "enabled":    true,
    "uri":        "mongodb://...",
    "database":   "deribit_block_rfq",
    "collection": "messages"
  },
  "redis": {
    "enabled": true,
    "url":     "redis://localhost:6379"
  },
  "local_file": {
    "enabled": true,
    "path":    "rfq_messages.jsonl"   // optional; auto-generated name if omitted
  }
}

Run
---
  uv run python ws_client_redis.py --config config/ws_client_config_mongodb_redis.json
"""

import argparse
import asyncio
import json
import logging
import pathlib
import ssl
import time
from typing import Dict, Optional

import certifi
import redis.asyncio as aioredis
import websockets
from pymongo import MongoClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _expand_trade_key(config: dict) -> dict:
    """Recursively expand trade_key path strings to dicts."""
    result = config.copy()
    for k, v in config.items():
        if k == "trade_key" and isinstance(v, str):
            result[k] = json.loads(pathlib.Path(v).read_text())
        elif isinstance(v, dict):
            result[k] = _expand_trade_key(v)
    return result


def load_config(path: str) -> dict:
    config = json.loads(pathlib.Path(path).read_text())
    return _expand_trade_key(config)


# ---------------------------------------------------------------------------
# Local file writer
# ---------------------------------------------------------------------------

class FileWriter:
    def __init__(self, path: Optional[str] = None):
        self._path = path or f"rfq_messages_{time.time_ns()}.jsonl"
        self._fp = open(self._path, "a", encoding="utf-8")
        log.info(f"Local file writer opened: {self._path}")

    def write(self, doc: dict):
        self._fp.write(json.dumps(doc, default=str))
        self._fp.write("\n")
        self._fp.flush()

    def close(self):
        self._fp.close()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DeribitRfqClient:
    """
    Standalone Deribit Block RFQ WebSocket client.

    Authentication flow (per connection):
      1. public/auth  (client_credentials)
      2. private/get_account_summary  (verify auth)
      3. public/subscribe  (block_rfq channels)
      4. periodic keepalive via private/get_account_summary

    Each subscription message is written to MongoDB, Redis, and/or a local file
    depending on which sinks are enabled in the config.
    """

    def __init__(self, config: dict):
        self.config = config
        self._request_counter = 0
        self._id_prefix = f"rfq_{id(self)}"
        self._pending: Dict[str, asyncio.Future] = {}

        # MongoDB (sync client — pymongo)
        self._mongo_col = None
        mongo_cfg = config.get("mongodb", {})
        if mongo_cfg.get("enabled"):
            mc = MongoClient(mongo_cfg["uri"])
            db = mc[mongo_cfg.get("database", "deribit_block_rfq")]
            self._mongo_col = db[mongo_cfg.get("collection", "messages")]
            log.info(f"MongoDB connected: {mongo_cfg['uri']}")

        # Redis (async — initialised per connection inside _connect)
        self._redis: Optional[aioredis.Redis] = None
        redis_cfg = config.get("redis", {})
        self._redis_url = redis_cfg.get("url", "redis://localhost:6379") if redis_cfg.get("enabled") else None

        # Local file
        self._file: Optional[FileWriter] = None
        file_cfg = config.get("local_file", {})
        if file_cfg.get("enabled"):
            self._file = FileWriter(file_cfg.get("path"))

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self):
        """Connect and receive messages, reconnecting forever on any error."""
        while True:
            try:
                await self._connect()
                log.info("Connection closed gracefully — stopping.")
                break
            except Exception as e:
                log.error(f"Connection error: {e}")
                log.info("Reconnecting in 5 seconds…")
                await asyncio.sleep(5)

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def _connect(self):
        url = self.config["server"]
        ssl_ctx = None
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.load_verify_locations(certifi.where())

        if self._redis_url:
            self._redis = await aioredis.from_url(self._redis_url, decode_responses=True)
            log.info(f"Redis connected: {self._redis_url}")

        try:
            async with websockets.connect(
                url,
                ping_interval=20,
                close_timeout=5,
                max_queue=1024,
                compression=None,
                ssl=ssl_ctx,
            ) as ws:
                log.info(f"WebSocket connected: {url}")

                # Start message loop first so request-response works during setup
                loop_task = asyncio.create_task(self._message_loop(ws), name="msg_loop")
                await asyncio.sleep(0.1)

                await self._login(ws)
                await self._check_auth(ws)
                await self._subscribe(ws)

                keepalive_task = asyncio.create_task(self._keepalive(ws), name="keepalive")
                try:
                    await loop_task  # runs until disconnected or error
                finally:
                    keepalive_task.cancel()
                    try:
                        await keepalive_task
                    except asyncio.CancelledError:
                        pass
        finally:
            if self._redis:
                await self._redis.aclose()
                self._redis = None
                log.info("Redis connection closed")

    # ------------------------------------------------------------------
    # JSON-RPC helpers
    # ------------------------------------------------------------------

    def _next_id(self) -> str:
        self._request_counter += 1
        return f"{self._id_prefix}_{self._request_counter}"

    async def _send_request(self, ws, method: str, params: dict = None, timeout: float = 10.0) -> dict:
        req_id = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params or {},
        }
        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[req_id] = future
        try:
            log.info(f"→ {method}  params={json.dumps(params)}")
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            log.error(f"Request {req_id} ({method}) timed out after {timeout}s")
            raise
        finally:
            self._pending.pop(req_id, None)

    # ------------------------------------------------------------------
    # Setup steps
    # ------------------------------------------------------------------

    async def _login(self, ws):
        trade_key = self.config["trade_key"]
        resp = await self._send_request(ws, "public/auth", {
            "grant_type":  "client_credentials",
            "client_id":   trade_key["access_key"],
            "client_secret": trade_key["secret_key"],
            "scope":       self.config.get("scope", "session:cpp_og"),
        })
        assert resp.get("result", {}).get("access_token"), f"Login failed: {resp}"
        log.info("Login success")

    async def _check_auth(self, ws):
        resp = await self._send_request(ws, "private/get_account_summary", {"currency": "BTC"})
        if resp.get("error"):
            raise RuntimeError(f"Auth check failed: {resp['error']}")
        log.info("Auth check success")

    async def _subscribe(self, ws):
        sub = self.config.get("subscription", {})
        resp = await self._send_request(ws, sub.get("method", "public/subscribe"), sub.get("params", {}))
        log.info(f"Subscribed. Result: {resp}")

    async def _keepalive(self, ws):
        interval = self.config.get("ping_interval", 60)
        while True:
            await asyncio.sleep(interval)
            try:
                await self._check_auth(ws)
            except Exception as e:
                log.error(f"Keepalive failed: {e}")
                raise

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _message_loop(self, ws):
        try:
            async for raw in ws:
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning(f"Non-JSON message ignored: {raw[:120]}")
                    continue

                # Route responses back to waiting _send_request callers
                req_id = parsed.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending[req_id]
                    if not future.done():
                        future.set_result(parsed)
                    continue

                # Subscription message — persist to all enabled sinks
                doc = {"fetch_time": time.time_ns(), "message": parsed}
                self._write_mongo(doc)
                await self._write_redis(parsed)
                self._write_file(doc)

        except Exception as e:
            log.error(f"Message loop error: {e}")
            raise

    # ------------------------------------------------------------------
    # Storage sinks
    # ------------------------------------------------------------------

    def _write_mongo(self, doc: dict):
        if self._mongo_col is None:
            return
        try:
            self._mongo_col.insert_one(doc)
            log.info(f"[MongoDB] stored fetch_time={doc['fetch_time']}")
        except Exception as e:
            log.error(f"[MongoDB] write failed: {e}")

    async def _write_redis(self, parsed: dict):
        """
        Store latest state per entity in Redis.

        Channel routing (quotes checked before generic maker because
        block_rfq.maker.quotes.* also matches "block_rfq.maker.*"):
          block_rfq.maker.quotes.*  →  rfq_quote:<block_rfq_quote_id>
          block_rfq.maker.*         →  rfq:<block_rfq_id>
          block_rfq.trades.*        →  rfq_trade:<block_rfq_id>
        """
        if self._redis is None:
            return
        try:
            if parsed.get("method") != "subscription":
                return
            channel = parsed.get("params", {}).get("channel", "")
            data = parsed.get("params", {}).get("data")
            if not isinstance(data, dict):
                return

            if "block_rfq.maker.quotes." in channel:
                quote_id = data.get("block_rfq_quote_id")
                if quote_id:
                    await self._redis.set(f"rfq_quote:{quote_id}", json.dumps(data))
                    log.info(f"[Redis] rfq_quote:{quote_id}")

            elif "block_rfq.maker." in channel:
                rfq_id = data.get("block_rfq_id")
                if rfq_id:
                    await self._redis.set(f"rfq:{rfq_id}", json.dumps(data))
                    log.info(f"[Redis] rfq:{rfq_id}  state={data.get('state')}")

            elif "block_rfq.trades." in channel:
                rfq_id = data.get("block_rfq_id")
                if rfq_id:
                    await self._redis.set(f"rfq_trade:{rfq_id}", json.dumps(data))
                    log.info(f"[Redis] rfq_trade:{rfq_id}")

        except Exception as e:
            log.error(f"[Redis] write failed: {e}")

    def _write_file(self, doc: dict):
        if self._file is None:
            return
        try:
            self._file.write(doc)
        except Exception as e:
            log.error(f"[File] write failed: {e}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Deribit Block RFQ WebSocket client")
    parser.add_argument(
        "--config",
        default="config/ws_client_config_mongodb_redis.json",
        help="Path to JSON config file",
    )
    args = parser.parse_args()
    config = load_config(args.config)
    log.info(f"Config loaded from {args.config}")
    try:
        asyncio.run(DeribitRfqClient(config).run())
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
