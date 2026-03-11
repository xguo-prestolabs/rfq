"""
Standalone Deribit Block RFQ WebSocket client.

Subscribes to Deribit block_rfq channels and writes every subscription message to:
  - MongoDB    (if mongodb.enabled in config)
  - Redis      (if redis.enabled in config)
  - Local file (if local_file.enabled in config)

Robustness features
-------------------
  - Exponential backoff on reconnect (RECONNECT_BASE → RECONNECT_CAP seconds),
    reset to base after a stable connection (>= STABLE_THRESHOLD seconds).
  - Watchdog task: declares the connection dead if no frame is received from the
    server for WATCHDOG_TIMEOUT seconds.  Catches silent TCP drops that the
    websockets library would otherwise not detect.
  - Deribit heartbeat protocol: calls public/set_heartbeat after subscribing so
    Deribit sends periodic test_request probes; the message loop responds with
    public/test immediately.  Without this Deribit closes the connection after
    ~60 s of silence.
  - Keepalive task: independently verifies auth every KEEPALIVE_INTERVAL seconds
    as a belt-and-suspenders check on top of the heartbeat protocol.
  - Both keepalive and watchdog run concurrently with the message loop; any task
    failing (or completing) immediately cancels the others and triggers reconnect.
  - Connection-setup timeout: the TCP connect + login + subscribe sequence must
    finish within SETUP_TIMEOUT seconds, preventing hangs on a bad network.

Redis key layout
----------------
  rfq:<block_rfq_id>        block_rfq.maker.*  (non-quotes) — latest RFQ state
  rfq_trade:<block_rfq_id>  block_rfq.trades.* — latest trade for that RFQ
  rfq_quote:<quote_id>      block_rfq.maker.quotes.* — latest quote snapshot

All Redis values are JSON strings of params.data.  Most-recent message wins (SET).

Config format
-------------
{
  "server":       "wss://www.deribit.com/ws/api/v2",
  "scope":        "session:cpp_og",
  "trade_key":    "trade_key.json",   // file path OR inline {"access_key":..,"secret_key":..}
  "subscription": {
    "method": "public/subscribe",
    "params": { "channels": ["block_rfq.maker.BTC", ...] }
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
    "path":    "rfq_messages.jsonl"   // optional; auto-named if omitted
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
import subprocess
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


def _get_git_version() -> str:
    # Try git command first
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        ).strip()
    except Exception:
        pass

    # Fallback to VERSION file
    version_file = pathlib.Path(__file__).parent / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()

    return "unknown"


_META = {"program": "ws_client_redis", "version": _get_git_version()}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _expand_trade_key(config: dict) -> dict:
    """Recursively expand trade_key path strings to inline dicts."""
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
        log.info(f"Local file writer: {self._path}")

    def write(self, doc: dict):
        self._fp.write(json.dumps(doc, default=str) + "\n")
        self._fp.flush()

    def close(self):
        self._fp.close()


# ---------------------------------------------------------------------------
# Sentinel exception for the watchdog
# ---------------------------------------------------------------------------

class WatchdogTimeout(Exception):
    pass


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class DeribitRfqClient:
    # ── Tunable constants ────────────────────────────────────────────────────
    SETUP_TIMEOUT      = 30   # seconds: TCP connect + login + subscribe must finish
    REQUEST_TIMEOUT    = 10   # seconds: per JSON-RPC request
    WATCHDOG_TIMEOUT   = 90   # seconds: silence from server → dead connection
    HEARTBEAT_INTERVAL = 30   # seconds: ask Deribit to probe us at this rate
    KEEPALIVE_INTERVAL = 120  # seconds: independent auth-check period
    RECONNECT_BASE     = 2    # seconds: initial reconnect wait
    RECONNECT_CAP      = 60   # seconds: maximum reconnect wait
    STABLE_THRESHOLD   = 60   # seconds: uptime needed to reset backoff to base
    # ─────────────────────────────────────────────────────────────────────────

    def __init__(self, config: dict):
        self.config = config
        self._req_counter = 0
        self._id_prefix   = f"rfq_{id(self)}"
        self._pending: Dict[str, asyncio.Future] = {}
        self._last_msg_at: float = 0.0   # monotonic timestamp of last received frame
        self._meta = {**_META, "testnet": "test.deribit.com" in config.get("server", "")}

        # MongoDB (sync pymongo — blocking calls are acceptable here because they
        # run in the message loop which has no other concurrent awaits at that point)
        self._mongo_col = None
        mongo_cfg = config.get("mongodb", {})
        if mongo_cfg.get("enabled"):
            mc = MongoClient(mongo_cfg["uri"])
            db = mc[mongo_cfg.get("database", "deribit_block_rfq")]
            self._mongo_col = db[mongo_cfg.get("collection", "messages")]
            log.info(f"MongoDB connected: {mongo_cfg['uri']}")

        # Redis — async client, created fresh each session inside _connect()
        self._redis: Optional[aioredis.Redis] = None
        redis_cfg = config.get("redis", {})
        self._redis_url = (
            redis_cfg.get("url", "redis://localhost:6379")
            if redis_cfg.get("enabled") else None
        )

        # Local file
        self._file: Optional[FileWriter] = None
        file_cfg = config.get("local_file", {})
        if file_cfg.get("enabled"):
            self._file = FileWriter(file_cfg.get("path"))

    # ── Public entry point ───────────────────────────────────────────────────

    async def run(self):
        """
        Connect → receive → reconnect, forever.

        Backoff doubles from RECONNECT_BASE up to RECONNECT_CAP on each
        consecutive failure.  Resets to RECONNECT_BASE after the connection
        was stable for at least STABLE_THRESHOLD seconds.
        """
        delay   = self.RECONNECT_BASE
        attempt = 0

        while True:
            attempt += 1
            t_start = time.monotonic()
            log.info(f"--- Connection attempt #{attempt} ---")

            try:
                await self._connect()
                # _connect() only returns normally if the WebSocket closed without
                # raising — still treat it as a disconnect and reconnect.
                log.info("Connection ended — reconnecting")
            except asyncio.CancelledError:
                log.info("Cancelled — shutting down")
                return
            except Exception as e:
                log.error(f"Connection lost: {type(e).__name__}: {e}")

            # Reset backoff if this session was stable long enough
            elapsed = time.monotonic() - t_start
            if elapsed >= self.STABLE_THRESHOLD:
                log.info(f"Session was stable for {elapsed:.0f}s — resetting backoff")
                delay = self.RECONNECT_BASE

            log.info(f"Reconnecting in {delay}s (next: attempt #{attempt + 1})")
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.RECONNECT_CAP)

    # ── Single connection session ─────────────────────────────────────────────

    async def _connect(self):
        """Run one full WebSocket session: connect → setup → steady-state → cleanup."""
        url = self.config["server"]

        ssl_ctx = None
        if url.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.load_verify_locations(certifi.where())

        # Open Redis for this session
        if self._redis_url:
            self._redis = await aioredis.from_url(self._redis_url, decode_responses=True)
            log.info(f"Redis connected: {self._redis_url}")

        try:
            # Enforce a hard timeout on the TCP handshake
            ws = await asyncio.wait_for(
                websockets.connect(
                    url,
                    ping_interval=None,  # We use Deribit's heartbeat; disable lib pings
                    close_timeout=5,
                    max_queue=1024,
                    compression=None,
                    ssl=ssl_ctx,
                ),
                timeout=self.SETUP_TIMEOUT,
            )
            log.info(f"WebSocket connected: {url}")
        except asyncio.TimeoutError:
            raise ConnectionError(f"WebSocket connect timed out after {self.SETUP_TIMEOUT}s")

        self._last_msg_at = time.monotonic()

        try:
            # Start the message loop first so JSON-RPC responses during setup are routed
            loop_task = asyncio.create_task(self._message_loop(ws), name="msg_loop")
            await asyncio.sleep(0.1)  # yield so loop_task starts reading

            # All setup steps must finish within SETUP_TIMEOUT seconds total
            try:
                await asyncio.wait_for(self._setup(ws), timeout=self.SETUP_TIMEOUT)
            except asyncio.TimeoutError:
                raise ConnectionError(f"Setup timed out after {self.SETUP_TIMEOUT}s")

            log.info("Setup complete — entering steady state")

            keepalive_task = asyncio.create_task(self._keepalive(ws), name="keepalive")
            watchdog_task  = asyncio.create_task(self._watchdog(),     name="watchdog")

            # Run until any task finishes or raises — whichever comes first
            done, pending = await asyncio.wait(
                {loop_task, keepalive_task, watchdog_task},
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel the survivors
            for task in pending:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

            # Re-raise the first exception so run() can log it and reconnect
            for task in done:
                if not task.cancelled() and task.exception():
                    raise task.exception()

            # All tasks completed without exception (message loop closed cleanly)
            raise ConnectionError("WebSocket closed cleanly — reconnecting")

        finally:
            try:
                await ws.close()
            except Exception:
                pass
            if self._redis:
                await self._redis.aclose()
                self._redis = None
                log.info("Redis connection closed")

    # ── JSON-RPC request/response ─────────────────────────────────────────────

    def _next_id(self) -> str:
        self._req_counter += 1
        return f"{self._id_prefix}_{self._req_counter}"

    async def _send_request(self, ws, method: str, params: dict = None) -> dict:
        req_id  = self._next_id()
        payload = {
            "jsonrpc": "2.0",
            "id":      req_id,
            "method":  method,
            "params":  params or {},
        }
        future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future
        try:
            log.info(f"→ {method}  {json.dumps(params)}")
            await ws.send(json.dumps(payload))
            return await asyncio.wait_for(future, timeout=self.REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            raise ConnectionError(f"Request '{method}' timed out after {self.REQUEST_TIMEOUT}s")
        finally:
            self._pending.pop(req_id, None)

    # ── Setup sequence ────────────────────────────────────────────────────────

    async def _setup(self, ws):
        """Authenticate, subscribe, and configure Deribit heartbeats."""
        # 1. Authenticate
        trade_key = self.config["trade_key"]
        resp = await self._send_request(ws, "public/auth", {
            "grant_type":    "client_credentials",
            "client_id":     trade_key["access_key"],
            "client_secret": trade_key["secret_key"],
            "scope":         self.config.get("scope", "session:cpp_og"),
        })
        if not resp.get("result", {}).get("access_token"):
            raise RuntimeError(f"Login failed: {resp}")
        log.info("Login success")

        # 2. Verify auth
        resp = await self._send_request(ws, "private/get_account_summary", {"currency": "BTC"})
        if resp.get("error"):
            raise RuntimeError(f"Auth check failed: {resp['error']}")
        log.info("Auth check success")

        # 3. Subscribe to channels
        sub  = self.config.get("subscription", {})
        resp = await self._send_request(ws, sub.get("method", "public/subscribe"), sub.get("params", {}))
        log.info(f"Subscribed: {resp}")

        # 4. Ask Deribit to send heartbeat test_request probes every HEARTBEAT_INTERVAL
        #    seconds.  We must respond with public/test or Deribit closes the connection.
        #    This is the primary mechanism for detecting a dead connection on the server
        #    side; our watchdog handles the client side.
        resp = await self._send_request(ws, "public/set_heartbeat", {"interval": self.HEARTBEAT_INTERVAL})
        log.info(f"Heartbeat set: interval={self.HEARTBEAT_INTERVAL}s  result={resp}")

    # ── Background tasks ──────────────────────────────────────────────────────

    async def _keepalive(self, ws):
        """
        Independently verify auth every KEEPALIVE_INTERVAL seconds.
        Belt-and-suspenders on top of the Deribit heartbeat mechanism.
        Any failure raises and triggers reconnection via asyncio.wait.
        """
        while True:
            await asyncio.sleep(self.KEEPALIVE_INTERVAL)
            log.info("Keepalive: verifying auth…")
            resp = await self._send_request(ws, "private/get_account_summary", {"currency": "BTC"})
            if resp.get("error"):
                raise RuntimeError(f"Keepalive auth check failed: {resp['error']}")
            log.info("Keepalive: auth OK")

    async def _watchdog(self):
        """
        Raise WatchdogTimeout if no frame is received from the server for
        WATCHDOG_TIMEOUT seconds.  Catches silent TCP drops where the OS
        hasn't sent a FIN/RST and the websockets library stays blocked in recv().
        """
        while True:
            await asyncio.sleep(10)
            silence = time.monotonic() - self._last_msg_at
            if silence > self.WATCHDOG_TIMEOUT:
                raise WatchdogTimeout(
                    f"No frame received for {silence:.0f}s "
                    f"(limit {self.WATCHDOG_TIMEOUT}s) — declaring connection dead"
                )

    # ── Message loop ──────────────────────────────────────────────────────────

    async def _message_loop(self, ws):
        """
        Read all incoming WebSocket frames and classify them:

          1. JSON-RPC responses  → resolve the waiting Future in _pending
          2. Heartbeat probes    → respond with public/test immediately (no Future)
          3. Subscription data   → persist to all enabled storage sinks
        """
        try:
            async for raw in ws:
                self._last_msg_at = time.monotonic()  # reset watchdog on every frame

                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    log.warning(f"Non-JSON frame ignored: {raw[:120]}")
                    continue

                # 1. JSON-RPC response to one of our requests
                req_id = parsed.get("id")
                if req_id and req_id in self._pending:
                    future = self._pending[req_id]
                    if not future.done():
                        future.set_result(parsed)
                    continue

                # 2. Deribit heartbeat test_request — must respond or server disconnects
                if parsed.get("method") == "heartbeat":
                    if parsed.get("params", {}).get("type") == "test_request":
                        await ws.send(json.dumps({
                            "jsonrpc": "2.0",
                            "method":  "public/test",
                            "params":  {},
                        }))
                        log.info("Heartbeat probe → responded with public/test")
                    continue

                # 3. Subscription data — write to all enabled sinks
                channel = parsed.get("params", {}).get("channel", "")
                doc = {"fetch_time": time.time_ns(), "channel": channel, "meta": self._meta, "message": parsed}
                await self._write_redis(doc)
                self._write_file(doc)
                self._write_mongo(doc)

        except Exception as e:
            log.error(f"Message loop error: {e}")
            raise

    # ── Storage sinks ─────────────────────────────────────────────────────────

    def _write_mongo(self, doc: dict):
        if self._mongo_col is None:
            return
        try:
            self._mongo_col.insert_one(doc)
            log.info(f"[MongoDB] stored fetch_time={doc['fetch_time']}")
        except Exception as e:
            log.error(f"[MongoDB] write failed: {e}")

    async def _write_redis(self, doc: dict):
        """
        Persist latest state per entity.  Channel routing order matters:
        block_rfq.maker.quotes.* must be checked before block_rfq.maker.*
        because the latter is a substring of the former.

          block_rfq.maker.quotes.*  →  rfq_quote:<block_rfq_quote_id>
          block_rfq.maker.*         →  rfq:<block_rfq_id>
          block_rfq.taker.*         →  rfq_taker:<block_rfq_id>
          block_rfq.trades.*        →  rfq_trade:<block_rfq_id>

        Redis values use the same doc format as MongoDB/file:
          {"fetch_time": <ns>, "channel": <str>, "message": <parsed>}
        """
        if self._redis is None:
            return
        try:
            parsed = doc["message"]
            if parsed.get("method") != "subscription":
                return
            channel = doc["channel"]
            data    = parsed.get("params", {}).get("data")
            if data is None:
                return

            doc_json = json.dumps({k: v for k, v in doc.items() if k != "_id"})

            TTL_OTHER = 4 * 3600  # 4 hours for non-rfq: channels

            if "block_rfq.maker.quotes." in channel:
                # data is a list of quote objects
                quotes = data if isinstance(data, list) else [data]
                for q in quotes:
                    qid = q.get("block_rfq_quote_id")
                    if qid:
                        await self._redis.set(f"rfq_quote:{qid}", doc_json, ex=TTL_OTHER)
                        await self._redis.publish("rfq_updates", json.dumps({"type": "rfq_quote", "id": qid}))
                        log.info(f"[Redis] rfq_quote:{qid}")

            elif "block_rfq.maker." in channel:
                if not isinstance(data, dict):
                    return
                rfq_id = data.get("block_rfq_id")
                if rfq_id:
                    await self._redis.set(f"rfq:{rfq_id}", doc_json)
                    await self._redis.publish("rfq_updates", json.dumps({"type": "rfq", "id": rfq_id, "data": data}))
                    log.info(f"[Redis] rfq:{rfq_id}  state={data.get('state')}")
                    await self._cleanup_rfq_keys()

            elif "block_rfq.taker." in channel:
                if not isinstance(data, dict):
                    return
                rfq_id = data.get("block_rfq_id")
                if rfq_id:
                    await self._redis.set(f"rfq_taker:{rfq_id}", doc_json, ex=TTL_OTHER)
                    await self._redis.publish("rfq_updates", json.dumps({"type": "rfq_taker", "id": rfq_id, "data": data}))
                    log.info(f"[Redis] rfq_taker:{rfq_id}  state={data.get('state')}")

            elif "block_rfq.trades." in channel:
                if not isinstance(data, dict):
                    return
                rfq_id = data.get("id") or data.get("block_rfq_id")
                if rfq_id:
                    await self._redis.set(f"rfq_trade:{rfq_id}", doc_json, ex=TTL_OTHER)
                    await self._redis.publish("rfq_updates", json.dumps({"type": "rfq_trade", "id": rfq_id}))
                    log.info(f"[Redis] rfq_trade:{rfq_id}")

        except Exception as e:
            log.error(f"[Redis] write failed: {e}")

    async def _cleanup_rfq_keys(self):
        """Remove outdated rfq:* keys, keeping:
        1. All open RFQs.
        2. Completed (non-open) RFQs within 60 minutes.
        3. At least 10 keys total (pad with older RFQs if needed).
        """
        try:
            keys = await self._redis.keys("rfq:*")
            if not keys:
                return

            # Fetch all values in one round-trip
            values = await self._redis.mget(keys)

            now_ns = time.time_ns()
            sixty_min_ns = 60 * 60 * 10**9
            entries = []  # (key, fetch_time_ns, state)
            for k, v in zip(keys, values):
                if v is None:
                    continue
                try:
                    obj = json.loads(v)
                    ft = obj.get("fetch_time", 0)
                    state = obj.get("message", {}).get("params", {}).get("data", {}).get("state", "")
                    entries.append((k, ft, state))
                except (json.JSONDecodeError, TypeError):
                    continue

            # Sort by fetch_time descending (newest first)
            entries.sort(key=lambda e: e[1], reverse=True)

            keep = set()
            rest = []  # non-open entries outside 60 min window, newest first
            for k, ft, state in entries:
                if state == "open":
                    keep.add(k)
                elif (now_ns - ft) <= sixty_min_ns:
                    keep.add(k)
                else:
                    rest.append(k)

            # Ensure at least 10 keys
            min_total = 10
            if len(keep) < min_total:
                for k in rest[:min_total - len(keep)]:
                    keep.add(k)

            to_delete = [k for k, _, _ in entries if k not in keep]
            if to_delete:
                await self._redis.delete(*to_delete)
                log.info(f"[Redis] rfq cleanup: deleted {len(to_delete)}, kept {len(keep)}")

        except Exception as e:
            log.error(f"[Redis] rfq cleanup failed: {e}")

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
    args   = parser.parse_args()
    config = load_config(args.config)
    log.info(f"Config loaded from {args.config}")
    try:
        asyncio.run(DeribitRfqClient(config).run())
    except KeyboardInterrupt:
        log.info("Stopped by user")


if __name__ == "__main__":
    main()
