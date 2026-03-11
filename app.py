import argparse
import asyncio
from collections import defaultdict, deque
import json
import os
import time
import redis.asyncio as redis
import aiohttp
import uuid
import uvicorn

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


# Pydantic models for request validation
class QuoteRequest(BaseModel):
    block_rfq_id: int
    amount: float
    direction: str  # maker's direction: "buy" or "sell"
    legs: List[Dict[str, Any]]  # per-leg: instrument_name, direction, ratio, price
    price: Optional[float] = None  # optional aggregate price (for future spreads)
    hedge: Optional[Dict[str, Any]] = None  # optional hedge leg


class EditQuoteRequest(BaseModel):
    quote_id: int  # block_rfq_quote_id on Deribit
    amount: float
    legs: List[Dict[str, Any]]  # per-leg: instrument_name, direction, ratio, price
    price: Optional[float] = None  # optional aggregate price
    hedge: Optional[Dict[str, Any]] = None  # optional hedge leg


class CancelQuoteRequest(BaseModel):
    quote_id: int  # block_rfq_quote_id on Deribit


class CancelAllQuotesRequest(BaseModel):
    currency: Optional[str] = None


# Deribit API configuration (should be loaded from config in production)
class DeribitConfig:
    def __init__(self):
        self.client_id: Optional[str] = None
        self.client_secret: Optional[str] = None
        self.access_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.token_expiry: Optional[int] = None
        self.base_url = "https://www.deribit.com/api/v2"  # Change to test.deribit.com for testnet
        self.testnet: bool = False

deribit_config = DeribitConfig()

# ── WebSocket clients for live updates ───────────────────────────────────────
_ws_clients: set = set()
_pubsub_connected: bool = False   # True while broadcaster is subscribed to rfq_updates
_pubsub_last_msg_at: Optional[float] = None  # time.time() of last received pub/sub message

# ── Query timing stats ────────────────────────────────────────────────────────
class _OpStats:
    __slots__ = ("count", "total_ms", "min_ms", "max_ms", "recent")
    def __init__(self):
        self.count    = 0
        self.total_ms = 0.0
        self.min_ms   = float("inf")
        self.max_ms   = 0.0
        self.recent: deque = deque(maxlen=20)

    def record(self, ms: float):
        self.count    += 1
        self.total_ms += ms
        if ms < self.min_ms: self.min_ms = ms
        if ms > self.max_ms: self.max_ms = ms
        self.recent.append(round(ms, 1))

    @property
    def avg_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

_stats: dict = defaultdict(_OpStats)


async def _pubsub_broadcaster(redis_url: str):
    """Subscribe to Redis rfq_updates and fan-out messages to all browser WebSocket clients.
    Reconnects automatically if the Redis connection drops (e.g. SSH tunnel flap)."""
    global _pubsub_connected, _pubsub_last_msg_at, _ws_clients
    delay = 1
    while True:
        pubsub_conn = None
        try:
            pubsub_conn = await redis.from_url(redis_url, decode_responses=True)
            pubsub = pubsub_conn.pubsub()
            await pubsub.subscribe("rfq_updates")
            _pubsub_connected = True
            print("Pub/sub broadcaster subscribed to rfq_updates")
            delay = 1  # reset backoff after successful connect
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                payload = message["data"]
                _pubsub_last_msg_at = time.time()
                try:
                    meta = json.loads(payload)
                    # Filter out messages from the wrong environment
                    msg_testnet = meta.get("testnet")
                    if msg_testnet is not None and msg_testnet != deribit_config.testnet:
                        continue
                    print(f"[pubsub] rfq_updates → type={meta.get('type')} id={meta.get('id')} state={meta.get('data', {}).get('state', '—')} clients={len(_ws_clients)}")
                except Exception:
                    print(f"[pubsub] rfq_updates → (unparseable) clients={len(_ws_clients)}")
                dead = set()
                for ws in list(_ws_clients):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.add(ws)
                _ws_clients -= dead
        except asyncio.CancelledError:
            _pubsub_connected = False
            return
        except Exception as e:
            _pubsub_connected = False
            print(f"[pubsub] connection lost: {e} — reconnecting in {delay}s")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)
        finally:
            if pubsub_conn:
                try:
                    await pubsub_conn.aclose()
                except Exception:
                    pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load Deribit credentials at startup (env vars take priority over config file)
    client_id = os.environ.get("DERIBIT_CLIENT_ID")
    client_secret = os.environ.get("DERIBIT_CLIENT_SECRET")
    testnet = os.environ.get("DERIBIT_TESTNET") == "1"

    config_path = os.environ.get("DERIBIT_CONFIG")
    if config_path:
        with open(config_path) as f:
            cfg = json.load(f)
        client_id = client_id or cfg.get("client_id")
        client_secret = client_secret or cfg.get("client_secret")
        testnet = testnet or cfg.get("testnet", False)
        print(f"Loaded Deribit config from {config_path}")

    if client_id and client_secret:
        deribit_config.client_id = client_id
        deribit_config.client_secret = client_secret
        deribit_config.base_url = (
            "https://test.deribit.com/api/v2" if testnet
            else "https://www.deribit.com/api/v2"
        )
        deribit_config.testnet = testnet
        print(f"Deribit credentials configured at startup (testnet={testnet})")

    # Startup: Connect to Redis (ZMQ is handled by zmq_to_redis.py)
    app.state.redis_client = await redis.from_url(
        "redis://localhost:6379", decode_responses=True
    )
    print(f"Connected to Redis: {await app.state.redis_client.ping()}")

    app.state.http_session = aiohttp.ClientSession()
    print("HTTP session created for Deribit API")

    app.state.broadcaster = asyncio.create_task(
        _pubsub_broadcaster("redis://localhost:6379")
    )
    print("Redis pub/sub broadcaster started")

    yield

    # Shutdown
    app.state.broadcaster.cancel()
    try:
        await app.state.broadcaster
    except asyncio.CancelledError:
        pass
    print("Broadcaster stopped")
    await app.state.redis_client.close()
    print("Redis connection closed")
    await app.state.http_session.close()
    print("HTTP session closed")


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_ui():
    return FileResponse("block_rfq_ui.html")


@app.get("/health")
async def health(request: Request):
    """Liveness check: Redis ping + pub/sub broadcaster status."""
    try:
        await request.app.state.redis_client.ping()
        redis_ok = True
    except Exception:
        redis_ok = False
    last_msg_age = round(time.time() - _pubsub_last_msg_at) if _pubsub_last_msg_at else None
    return {
        "redis": "ok" if redis_ok else "error",
        "pubsub": "ok" if _pubsub_connected else "error",
        "pubsub_last_msg_ago_s": last_msg_age,
        "ws_clients": len(_ws_clients),
        "testnet": deribit_config.testnet,
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time RFQ push updates (Redis pub/sub fan-out)."""
    await websocket.accept()
    _ws_clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep connection alive; ignore client messages
    except WebSocketDisconnect:
        pass
    finally:
        _ws_clients.discard(websocket)


@app.get("/price/{product_name}")
async def get_price(product_name: str, request: Request):
    """Get price for a product by native_product name"""
    t0 = time.perf_counter()
    price_data = await request.app.state.redis_client.get(f"price:{product_name}")
    _stats["GET /price/{product}"].record((time.perf_counter() - t0) * 1000)
    if not price_data:
        return {"product": product_name, "price": None, "ts": None}
    data = json.loads(price_data)
    return {
        "product": product_name,
        "price": data.get("strat_mid_price"),  # None if field absent in Redis data
        "ts": data.get("ts"),
    }


@app.get("/prices")
async def get_all_prices(request: Request):
    """Get all stored prices"""
    t0 = time.perf_counter()
    keys = []
    async for key in request.app.state.redis_client.scan_iter("price:*"):
        keys.append(key)

    prices = {}
    if keys:
        values = await request.app.state.redis_client.mget(keys)
        for key, value in zip(keys, values):
            if value:
                product_name = key.replace("price:", "")
                data = json.loads(value)
                prices[product_name] = {
                    "price": data.get("strat_mid_price"),
                    "ts": data.get("ts"),
                }
    _stats["GET /prices (SCAN+MGET)"].record((time.perf_counter() - t0) * 1000)
    return {"count": len(prices), "prices": prices}

@app.get("/greeks/{product_name}")
async def get_greeks(product_name: str, request: Request):
    t0 = time.perf_counter()
    greeks = await request.app.state.redis_client.get(f"greeks:{product_name}")
    _stats["GET /greeks/{product}"].record((time.perf_counter() - t0) * 1000)
    data = {}
    if greeks:
        data = json.loads(greeks)
    return {
        "greeks": data
    }

@app.get("/greeks_list")
async def get_greeks_list(request: Request):
    t0 = time.perf_counter()
    keys = []
    async for key in request.app.state.redis_client.scan_iter("greeks:*"):
        keys.append(key)

    greeks = {}
    if keys:
        values = await request.app.state.redis_client.mget(keys)
        for key, value in zip(keys, values):
            if value:
                product_name = key.replace("greeks:", "")
                data = json.loads(value)
                greeks[product_name] = data
    _stats["GET /greeks_list (SCAN+MGET)"].record((time.perf_counter() - t0) * 1000)
    return {
        "greeks": greeks
    }

@app.get("/total_greeks")
async def get_total_greeks(request: Request):
    t0 = time.perf_counter()
    greeks = await request.app.state.redis_client.get("total_greeks:ALL")
    _stats["GET /total_greeks"].record((time.perf_counter() - t0) * 1000)
    if not greeks:
        return {"total_greeks": {}}
    data = json.loads(greeks)
    return {
        "total_greeks": data
    }

@app.get("/trades")
async def get_trades(request: Request):
    """Get recent RFQ trades from Redis (rfq_trade:* keys), sorted newest first."""
    t0 = time.perf_counter()
    keys = await request.app.state.redis_client.keys("rfq_trade:*")
    trades = []
    if keys:
        values = await request.app.state.redis_client.mget(*keys)
        for v in values:
            if v:
                try:
                    doc = json.loads(v)
                    trades.append(doc.get("message", {}).get("params", {}).get("data", doc))
                except Exception:
                    pass
    trades.sort(key=lambda t: t.get("timestamp", 0), reverse=True)
    _stats["GET /trades"].record((time.perf_counter() - t0) * 1000)
    return {"trades": trades[:20]}


@app.get("/rfqs")
async def get_rfqs(request: Request):
    """Get all RFQs from Redis (rfq:* keys)."""
    t0 = time.perf_counter()
    # KEYS is a single round-trip; safe here because rfq:* is a bounded set (~hundreds of keys).
    # scan_iter was previously used but caused excessive round-trips over the SSH tunnel (~7 RTTs
    # for 610 keys at the default batch size of 100), making this endpoint very slow.
    keys = await request.app.state.redis_client.keys("rfq:*")
    t1 = time.perf_counter()
    results = {}
    if keys:
        values = await request.app.state.redis_client.mget(keys)
        t2 = time.perf_counter()
        is_testnet = deribit_config.testnet
        for key, value in zip(keys, values):
            if value:
                doc = json.loads(value)
                # Filter out messages that don't match the current environment
                doc_testnet = doc.get("meta", {}).get("testnet")
                if doc_testnet is not None and doc_testnet != is_testnet:
                    continue
                rfq_id = int(key.split(":", 1)[1])
                results[rfq_id] = doc.get("message", {}).get("params", {}).get("data", doc)
    else:
        t2 = time.perf_counter()
    keys_ms, mget_ms = (t1-t0)*1000, (t2-t1)*1000
    _stats["GET /rfqs — KEYS rfq:*"].record(keys_ms)
    _stats["GET /rfqs — MGET"].record(mget_ms)
    _stats["GET /rfqs — total"].record(keys_ms + mget_ms)
    print(f"[Redis] KEYS rfq:* ({len(keys)} keys, {len(results)} matched) — {keys_ms:.1f} ms | MGET — {mget_ms:.1f} ms | total — {keys_ms+mget_ms:.1f} ms")
    return results


@app.get("/rfqs/taker")
async def get_rfqs_taker(request: Request):
    """Get all taker RFQs from Redis (rfq_taker:* keys)."""
    t0 = time.perf_counter()
    keys = await request.app.state.redis_client.keys("rfq_taker:*")
    results = {}
    if keys:
        values = await request.app.state.redis_client.mget(keys)
        for key, value in zip(keys, values):
            if value:
                rfq_id = int(key.split(":", 1)[1])
                doc = json.loads(value)
                results[rfq_id] = doc.get("message", {}).get("params", {}).get("data", doc)
    _stats["GET /rfqs/taker"].record((time.perf_counter() - t0) * 1000)
    return results


@app.get("/stats")
async def stats_page():
    """HTML dashboard showing per-endpoint query timing stats."""
    from fastapi.responses import HTMLResponse

    def row(name: str, s: _OpStats) -> str:
        recent = ", ".join(f"{v}" for v in s.recent)
        return (
            f"<tr>"
            f"<td>{name}</td>"
            f"<td>{s.count}</td>"
            f"<td>{s.avg_ms:.1f}</td>"
            f"<td>{s.min_ms:.1f}</td>"
            f"<td>{s.max_ms:.1f}</td>"
            f"<td class='recent'>{recent}</td>"
            f"</tr>"
        )

    rows = "".join(row(k, v) for k, v in sorted(_stats.items()))
    ws_count = len(_ws_clients)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="refresh" content="5">
<title>RFQ Stats</title>
<style>
  body {{ background:#0d1117; color:#e6edf3; font-family:monospace; padding:24px; }}
  h1   {{ font-size:18px; margin-bottom:4px; }}
  p    {{ color:#8b949e; font-size:12px; margin-bottom:16px; }}
  table {{ border-collapse:collapse; width:100%; font-size:13px; }}
  th   {{ background:#21262d; color:#8b949e; text-align:left; padding:6px 12px;
           font-size:11px; text-transform:uppercase; letter-spacing:.05em;
           border-bottom:1px solid #30363d; }}
  td   {{ padding:6px 12px; border-bottom:1px solid #21262d; }}
  tr:hover td {{ background:#161b22; }}
  .recent {{ color:#8b949e; font-size:11px; max-width:340px;
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .badge {{ display:inline-block; padding:2px 8px; border-radius:10px; font-size:11px;
             background:rgba(63,185,80,.15); color:#3fb950; }}
</style>
</head>
<body>
<h1>RFQ API — Query Stats</h1>
<p>Auto-refreshes every 5 s &nbsp;·&nbsp;
   <span class="badge">⬤ {ws_count} browser WS client{'s' if ws_count != 1 else ''}</span></p>
<table>
  <thead>
    <tr>
      <th>Operation</th><th>Count</th><th>Avg (ms)</th>
      <th>Min (ms)</th><th>Max (ms)</th><th>Last 20 samples (ms)</th>
    </tr>
  </thead>
  <tbody>{rows if rows else '<tr><td colspan="6" style="color:#8b949e">No data yet — make some requests.</td></tr>'}</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(html)


@app.get("/rfqs/{rfq_id}")
async def get_rfq_status(rfq_id: str, request: Request):
    """
    Get the current status/state of a specific RFQ

    Returns the RFQ details including its current state:
    - "open": Still accepting quotes
    - "filled": Quote was accepted, trade executed
    - "canceled": RFQ was canceled by taker
    - "expired": RFQ expired (time limit)
    """
    session = request.app.state.http_session

    try:
        # Try to get the RFQ from Deribit API
        # First try BTC
        try:
            btc_rfqs = await call_deribit_api(
                session,
                "private/get_block_rfqs",
                {"currency": "BTC"}
            )
            for rfq in btc_rfqs.get("block_rfqs", []):
                if rfq.get("block_rfq_id") == rfq_id:
                    return {
                        "status": "success",
                        "rfq_id": rfq_id,
                        "state": rfq.get("state"),
                        "rfq": rfq
                    }
        except Exception:
            pass

        # Try ETH
        eth_rfqs = await call_deribit_api(
            session,
            "private/get_block_rfqs",
            {"currency": "ETH"}
        )
        for rfq in eth_rfqs.get("block_rfqs", []):
            if rfq.get("block_rfq_id") == rfq_id:
                return {
                    "status": "success",
                    "rfq_id": rfq_id,
                    "state": rfq.get("state"),
                    "rfq": rfq
                }

        raise HTTPException(
            status_code=404,
            detail=f"RFQ {rfq_id} not found"
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get RFQ status: {str(e)}"
        )


# ============================================================================
# Deribit Authentication Helper Functions
# ============================================================================

async def ensure_authenticated(session: aiohttp.ClientSession) -> bool:
    """Ensure we have a valid access token, refresh if needed"""
    if not deribit_config.client_id or not deribit_config.client_secret:
        raise HTTPException(
            status_code=401,
            detail="Deribit credentials not configured. Start app with --config <path> or set DERIBIT_CONFIG env var."
        )

    # Check if token is still valid
    current_time = int(time.time() * 1000)
    if (deribit_config.access_token and
        deribit_config.token_expiry and
        current_time < deribit_config.token_expiry - 60000):  # 60s buffer
        return True

    # Need to authenticate or refresh
    print("Authenticating with Deribit...")

    params = {
        "grant_type": "client_credentials",
        "client_id": deribit_config.client_id,
        "client_secret": deribit_config.client_secret,
    }

    url = f"{deribit_config.base_url}/public/auth"

    async with session.get(url, params=params) as response:
        if response.status != 200:
            error_text = await response.text()
            raise HTTPException(
                status_code=response.status,
                detail=f"Deribit authentication failed: {error_text}"
            )

        result = await response.json()
        if "error" in result:
            raise HTTPException(
                status_code=400,
                detail=f"Deribit auth error: {result['error']}"
            )

        auth_result = result.get("result", {})
        deribit_config.access_token = auth_result.get("access_token")
        deribit_config.refresh_token = auth_result.get("refresh_token")
        deribit_config.token_expiry = current_time + (auth_result.get("expires_in", 0) * 1000)

        print(f"Authenticated successfully. Token expires in {auth_result.get('expires_in')} seconds")
        return True


async def call_deribit_api(
    session: aiohttp.ClientSession,
    method: str,
    params: Dict[str, Any]
) -> Dict[str, Any]:
    """Call a Deribit API method with automatic authentication"""
    await ensure_authenticated(session)

    request_id = str(uuid.uuid4())

    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params
    }

    headers = {
        "Authorization": f"Bearer {deribit_config.access_token}",
        "Content-Type": "application/json"
    }

    url = f"{deribit_config.base_url}/private/{method.replace('private/', '')}"

    print(f"Deribit API call: {method} params={json.dumps(params)}")
    async with session.post(url, json=payload, headers=headers) as response:
        result = await response.json()

        if "error" in result:
            error = result["error"]
            print(f"Deribit API error response: {json.dumps(result)}")
            raise HTTPException(
                status_code=400,
                detail=f"Deribit API error: {error.get('message', error)} (code={error.get('code')}, data={error.get('data')})"
            )

        return result.get("result", {})


# ============================================================================
# RFQ Quote Management Endpoints
# ============================================================================

@app.post("/quotes/add")
async def add_quote(quote: QuoteRequest, request: Request):
    """
    Add a quote to a Block RFQ as a maker

    Example request:
    {
        "block_rfq_id": "12345",
        "price": 50000.5,
        "amount": 1.5
    }

    NOTE (confirmed by Deribit tech support):
    Calling add_block_rfq_quote multiple times on the same block_rfq_id creates additional
    active quotes — previous quotes are NOT implicitly cancelled. Multiple simultaneous quotes
    from the same maker are allowed, subject to per-RFQ limits (error codes:
    too_many_quotes_per_block_rfq, too_many_quotes_per_block_rfq_side).
    To modify an existing quote use edit_block_rfq_quote; to remove one use
    cancel_block_rfq_quote or cancel_all_block_rfq_quotes.
    """
    session = request.app.state.http_session

    params = {
        "block_rfq_id": quote.block_rfq_id,
        "amount": quote.amount,
        "direction": quote.direction,
        "legs": quote.legs,
    }
    if quote.price is not None:
        params["price"] = quote.price
    if quote.hedge is not None:
        params["hedge"] = quote.hedge

    redis_client = request.app.state.redis_client
    ts = time.time_ns()
    try:
        result = await call_deribit_api(
            session,
            "private/add_block_rfq_quote",
            params
        )

        print(f"Quote added successfully: {result}")

        # Store in Redis for 48 hours
        qid = result.get("block_rfq_quote_id") or result.get("quote_id")
        if qid:
            record = {"timestamp": ts, "action": "submit", "status": "success", "params": params, "result": result}
            await redis_client.set(f"rfq_user:submit:{qid}", json.dumps(record, default=str), ex=48 * 3600)

        return {
            "status": "success",
            "quote_id": result.get("quote_id"),
            "block_rfq_id": quote.block_rfq_id,
            "price": quote.price,
            "amount": quote.amount,
            "result": result
        }

    except HTTPException as e:
        record = {"timestamp": ts, "action": "submit", "status": "error", "params": params, "error": e.detail}
        await redis_client.set(f"rfq_user:submit:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise e
    except Exception as e:
        record = {"timestamp": ts, "action": "submit", "status": "error", "params": params, "error": str(e)}
        await redis_client.set(f"rfq_user:submit:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to add quote: {str(e)}"
        )


@app.post("/quotes/cancel")
async def cancel_quote(cancel_req: CancelQuoteRequest, request: Request):
    """
    Cancel a previously submitted quote

    Example request:
    {
        "quote_id": "67890"
    }
    """
    session = request.app.state.http_session

    params = {
        "block_rfq_quote_id": cancel_req.quote_id
    }

    redis_client = request.app.state.redis_client
    ts = time.time_ns()
    try:
        result = await call_deribit_api(
            session,
            "private/cancel_block_rfq_quote",
            params
        )

        print(f"Quote canceled successfully: {result}")

        # Store in Redis for 48 hours
        record = {"timestamp": ts, "action": "cancel", "status": "success", "params": params, "result": result}
        await redis_client.set(f"rfq_user:cancel:{cancel_req.quote_id}", json.dumps(record, default=str), ex=48 * 3600)

        return {
            "status": "success",
            "quote_id": cancel_req.quote_id,
            "result": result
        }

    except HTTPException as e:
        record = {"timestamp": ts, "action": "cancel", "status": "error", "params": params, "error": e.detail}
        await redis_client.set(f"rfq_user:cancel:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise e
    except Exception as e:
        record = {"timestamp": ts, "action": "cancel", "status": "error", "params": params, "error": str(e)}
        await redis_client.set(f"rfq_user:cancel:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cancel quote: {str(e)}"
        )


@app.post("/quotes/edit")
async def edit_quote(edit_req: EditQuoteRequest, request: Request):
    """
    Edit a previously submitted quote

    Example request:
    {
        "quote_id": "67890",
        "price": 51000.0,
        "amount": 2.0
    }
    """
    session = request.app.state.http_session

    params = {
        "block_rfq_quote_id": edit_req.quote_id,
        "amount": edit_req.amount,
        "legs": edit_req.legs,
    }
    if edit_req.price is not None:
        params["price"] = edit_req.price
    if edit_req.hedge is not None:
        params["hedge"] = edit_req.hedge

    redis_client = request.app.state.redis_client
    ts = time.time_ns()
    try:
        result = await call_deribit_api(
            session,
            "private/edit_block_rfq_quote",
            params
        )

        print(f"Quote edited successfully: {result}")

        # Store in Redis for 48 hours
        record = {"timestamp": ts, "action": "edit", "status": "success", "params": params, "result": result}
        await redis_client.set(f"rfq_user:edit:{edit_req.quote_id}", json.dumps(record, default=str), ex=48 * 3600)

        return {
            "status": "success",
            "quote_id": edit_req.quote_id,
            "price": edit_req.price,
            "amount": edit_req.amount,
            "result": result
        }

    except HTTPException as e:
        record = {"timestamp": ts, "action": "edit", "status": "error", "params": params, "error": e.detail}
        await redis_client.set(f"rfq_user:edit:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise e
    except Exception as e:
        record = {"timestamp": ts, "action": "edit", "status": "error", "params": params, "error": str(e)}
        await redis_client.set(f"rfq_user:edit:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to edit quote: {str(e)}"
        )


@app.post("/quotes/cancel_all")
async def cancel_all_quotes(cancel_all_req: CancelAllQuotesRequest, request: Request):
    """
    Cancel all active quotes for a currency (or all currencies)

    Example requests:
    - Cancel all BTC quotes: {"currency": "BTC"}
    - Cancel all quotes: {}
    """
    session = request.app.state.http_session

    params = {}
    if cancel_all_req.currency:
        params["currency"] = cancel_all_req.currency

    try:
        result = await call_deribit_api(
            session,
            "private/cancel_all_block_rfq_quotes",
            params
        )

        print(f"All quotes canceled successfully: {result}")

        # Store in Redis for 48 hours
        redis_client = request.app.state.redis_client
        ts = time.time_ns()
        currency = cancel_all_req.currency or "all"
        record = {"timestamp": ts, "action": "cancel_all", "currency": currency, "params": params, "result": result}
        await redis_client.set(f"rfq_user:cancel_all:{ts}", json.dumps(record, default=str), ex=48 * 3600)

        return {
            "status": "success",
            "currency": currency,
            "result": result
        }

    except HTTPException as e:
        redis_client = request.app.state.redis_client
        ts = time.time_ns()
        currency = cancel_all_req.currency or "all"
        record = {"timestamp": ts, "action": "cancel_all", "status": "error", "currency": currency, "params": params, "error": e.detail}
        await redis_client.set(f"rfq_user:cancel_all:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise e
    except Exception as e:
        redis_client = request.app.state.redis_client
        ts = time.time_ns()
        currency = cancel_all_req.currency or "all"
        record = {"timestamp": ts, "action": "cancel_all", "status": "error", "currency": currency, "params": params, "error": str(e)}
        await redis_client.set(f"rfq_user:cancel_all:err:{ts}", json.dumps(record, default=str), ex=48 * 3600)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cancel all quotes: {str(e)}"
        )


@app.get("/quotes")
async def get_my_quotes(request: Request):
    """
    Get all quotes submitted by this maker
    """
    session = request.app.state.http_session

    try:
        # Get BTC quotes
        btc_result = await call_deribit_api(
            session,
            "private/get_block_rfq_quotes",
            {"currency": "BTC"}
        )

        # Get ETH quotes
        eth_result = await call_deribit_api(
            session,
            "private/get_block_rfq_quotes",
            {"currency": "ETH"}
        )

        # Deduplicate — Deribit may return the same quote under multiple currencies
        seen = set()
        all_quotes = []
        for q in btc_result + eth_result:
            qid = q.get("block_rfq_quote_id")
            if qid and qid in seen:
                continue
            seen.add(qid)
            all_quotes.append(q)

        return {
            "status": "success",
            "count": len(all_quotes),
            "quotes": all_quotes
        }

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get quotes: {str(e)}"
        )


@app.get("/quotes/{quote_id}")
async def get_quote_by_id(quote_id: str, request: Request):
    """
    Get details of a specific quote by ID
    """
    session = request.app.state.http_session

    try:
        # Try BTC first
        try:
            btc_quotes = await call_deribit_api(
                session,
                "private/get_block_rfq_quotes",
                {"currency": "BTC"}
            )
            for quote in btc_quotes:
                if quote.get("quote_id") == quote_id:
                    return {
                        "status": "success",
                        "quote": quote
                    }
        except Exception:
            pass

        # Try ETH
        eth_quotes = await call_deribit_api(
            session,
            "private/get_block_rfq_quotes",
            {"currency": "ETH"}
        )
        for quote in eth_quotes:
            if quote.get("quote_id") == quote_id:
                return {
                    "status": "success",
                    "quote": quote
                }

        raise HTTPException(
            status_code=404,
            detail=f"Quote {quote_id} not found"
        )

    except HTTPException as e:
        raise e
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to get quote: {str(e)}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RFQ market-maker API")
    parser.add_argument("--config", default=None, help="Path to JSON config with client_id/client_secret")
    parser.add_argument("--testnet", action="store_true", help="Use Deribit testnet instead of mainnet")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--root-path", default="",
        help="ASGI root_path when served under an nginx subpath, e.g. /rfq"
    )
    args = parser.parse_args()

    if args.config:
        os.environ["DERIBIT_CONFIG"] = args.config
    if args.testnet:
        os.environ["DERIBIT_TESTNET"] = "1"

    uvicorn.run(
        "app:app",
        host="0.0.0.0",
        port=args.port,
        root_path=args.root_path,
        # Trust X-Forwarded-For / X-Forwarded-Proto from nginx
        proxy_headers=True,
        forwarded_allow_ips="*",
        reload=True,
    )
