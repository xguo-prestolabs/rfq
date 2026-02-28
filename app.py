import asyncio
import json
import zmq
import time
import zmq.asyncio
import redis.asyncio as redis
import aiohttp
import uuid

from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from pymongo import MongoClient
from bson import json_util
from pydantic import BaseModel
from typing import Optional, List, Dict, Any


def get_mongo_connection(app: FastAPI):
    mongo_uri = "mongodb://options:options@1.tcp.ngrok.io:25657/?authSource=admin"
    mongo_client = MongoClient(mongo_uri)
    db = mongo_client["deribit_block_rfq"]
    mongo_collection = db["messages"]
    app.state.mongo_client = mongo_client
    app.state.mongo_collection = mongo_collection


# Pydantic models for request validation
class QuoteRequest(BaseModel):
    block_rfq_id: str
    price: float
    amount: float


class EditQuoteRequest(BaseModel):
    quote_id: str
    price: float
    amount: float


class CancelQuoteRequest(BaseModel):
    quote_id: str


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

deribit_config = DeribitConfig()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Connect to Redis and start ZMQ listener
    app.state.redis_client = await redis.from_url(
        "redis://localhost:6379", decode_responses=True
    )
    print(f"Connected to Redis: {await app.state.redis_client.ping()}")

    # Create aiohttp session for Deribit API calls
    app.state.http_session = aiohttp.ClientSession()
    print("HTTP session created for Deribit API")

    app.state.zmq_task = asyncio.create_task(zmq_listener(app))
    get_mongo_connection(app)

    yield

    # Shutdown: Cancel ZMQ listener and close Redis
    app.state.zmq_task.cancel()
    try:
        await app.state.zmq_task
    except asyncio.CancelledError:
        pass

    await app.state.redis_client.close()
    print("Redis connection closed")
    await app.state.mongo_client.close()
    print("MongoDB connection closed")
    await app.state.http_session.close()
    print("HTTP session closed")


app = FastAPI(lifespan=lifespan)


async def zmq_listener(app: FastAPI):
    """Background task to receive ZMQ messages and update Redis"""
    context = zmq.asyncio.Context()
    socket = context.socket(zmq.SUB)
    zmq_url = "tcp://localhost:40000"
    socket.connect(zmq_url)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    print(f"ZMQ Subscriber listening on {zmq_url}")

    try:
        while True:
            msg = await socket.recv_string()
            try:
                data = json.loads(msg)
                if data.get("msg_type") == "fair_price":
                    native_product = data["native_product"]
                    await app.state.redis_client.set(
                        f"price:{native_product}", json.dumps(data)
                    )
                elif data.get("msg_type") == "greeks":
                    native_product = data["native_product"]
                    await app.state.redis_client.set(
                        f"greeks:{native_product}", json.dumps(data)
                    )
                elif data.get("msg_type") == "total_greeks":
                    native_product = data["native_product"]
                    await app.state.redis_client.set(
                        f"total_greeks:{native_product}", json.dumps(data)
                    )
                else:
                    pass
            except json.JSONDecodeError as e:
                print(f"Failed to parse message as JSON: {e}")
            except Exception as e:
                print(f"Error processing message: {e}")

    except asyncio.CancelledError:
        print("ZMQ listener cancelled")
        socket.close()
        context.term()
        raise


@app.get("/price/{product_name}")
async def get_price(product_name: str, request: Request):
    """Get price for a product by native_product name"""
    price_data = await request.app.state.redis_client.get(f"price:{product_name}")
    data = {}
    if price_data:
        data = json.loads(price_data)
    return {
        "product": product_name,
        "price": data.get("strat_mid_price"),
        "ts": data.get("ts"),
    }


@app.get("/prices")
async def get_all_prices(request: Request):
    """Get all stored prices"""
    # Get all price keys
    keys = []
    async for key in request.app.state.redis_client.scan_iter("price:*"):
        keys.append(key)

    prices = {}
    if keys:
        # Batch get all values
        values = await request.app.state.redis_client.mget(keys)
        for key, value in zip(keys, values):
            if value:
                product_name = key.replace("price:", "")
                data = json.loads(value)
                prices[product_name] = {
                    "price": data.get("strat_mid_price"),
                    "ts": data.get("ts"),
                }

    return {"count": len(prices), "prices": prices}

@app.get("/greeks/{product_name}")
async def get_greeks(product_name: str, request: Request):
    greeks = await request.app.state.redis_client.get(f"greeks:{product_name}")
    data = {}
    if greeks:
        data = json.loads(greeks)
    return {
        "greeks": data
    }

@app.get("/greeks_list")
async def get_greeks_list(request: Request):
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
    return {
        "greeks": greeks
    }

@app.get("/total_greeks")
async def get_total_greeks(request: Request):
    greeks = await request.app.state.redis_client.get("total_greeks:ALL")
    data = json.loads(greeks)
    return {
        "total_greeks": data
    }

@app.get("/rfqs")
async def get_rfqs(request: Request):
    """Get all RFQs"""
    # Get all RFQ keys
    mongo_collection = request.app.state.mongo_collection
    nanos_per_hour = 3600 * 1_000_000_000
    ten_hours_ago_ns = time.time_ns() - (10 * nanos_per_hour)
    query = {"fetch_time": {"$gte": ten_hours_ago_ns}}
    cursor = mongo_collection.find(query).sort("fetch_time", -1)
    results = {}
    for doc in cursor:
        doc_json = json.loads(json_util.dumps(doc))
        msg_json = doc_json.get("message")
        if msg_json.get("method") == "subscription":
            params = msg_json.get("params", {})
            channel = params.get("channel", "")
            data = params.get("data")

            # Process Block RFQ notifications
            if "block_rfq.maker." in channel and "quotes" not in channel:
                if isinstance(data, dict):
                    rfq_id = data.get("block_rfq_id")
                    if rfq_id:
                        results[rfq_id] = data
    return results


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
            for rfq in btc_rfqs:
                if rfq.get("block_rfq_id") == rfq_id:
                    return {
                        "status": "success",
                        "rfq_id": rfq_id,
                        "state": rfq.get("state"),
                        "rfq": rfq
                    }
        except:
            pass

        # Try ETH
        eth_rfqs = await call_deribit_api(
            session,
            "private/get_block_rfqs",
            {"currency": "ETH"}
        )
        for rfq in eth_rfqs:
            if rfq.get("block_rfq_id") == rfq_id:
                return {
                    "status": "success",
                    "rfq_id": rfq_id,
                    "state": rfq.get("state"),
                    "rfq": rfq
                }

        # If not found in API, try MongoDB
        mongo_collection = request.app.state.mongo_collection
        nanos_per_hour = 3600 * 1_000_000_000
        ten_hours_ago_ns = time.time_ns() - (10 * nanos_per_hour)
        query = {"fetch_time": {"$gte": ten_hours_ago_ns}}
        cursor = mongo_collection.find(query).sort("fetch_time", -1)

        for doc in cursor:
            doc_json = json.loads(json_util.dumps(doc))
            msg_json = doc_json.get("message")
            if msg_json.get("method") == "subscription":
                params = msg_json.get("params", {})
                channel = params.get("channel", "")
                data = params.get("data")

                if "block_rfq.maker." in channel and "quotes" not in channel:
                    if isinstance(data, dict) and data.get("block_rfq_id") == rfq_id:
                        return {
                            "status": "success",
                            "rfq_id": rfq_id,
                            "state": data.get("state"),
                            "rfq": data,
                            "source": "mongodb_cache"
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
            detail="Deribit credentials not configured. Use /configure endpoint first."
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

    async with session.post(url, json=payload, headers=headers) as response:
        result = await response.json()

        if "error" in result:
            error = result["error"]
            raise HTTPException(
                status_code=400,
                detail=f"Deribit API error: {error.get('message', error)}"
            )

        return result.get("result", {})


# ============================================================================
# Configuration Endpoint
# ============================================================================

@app.post("/configure")
async def configure_deribit(
    client_id: str,
    client_secret: str,
    testnet: bool = True
):
    """Configure Deribit API credentials"""
    deribit_config.client_id = client_id
    deribit_config.client_secret = client_secret
    deribit_config.base_url = (
        "https://test.deribit.com/api/v2" if testnet
        else "https://www.deribit.com/api/v2"
    )

    # Clear existing tokens
    deribit_config.access_token = None
    deribit_config.refresh_token = None
    deribit_config.token_expiry = None

    return {
        "status": "configured",
        "testnet": testnet,
        "message": "Deribit credentials configured successfully"
    }


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
    """
    session = request.app.state.http_session

    params = {
        "block_rfq_id": quote.block_rfq_id,
        "price": quote.price,
        "amount": quote.amount
    }

    try:
        result = await call_deribit_api(
            session,
            "private/add_block_rfq_quote",
            params
        )

        print(f"Quote added successfully: {result}")

        return {
            "status": "success",
            "quote_id": result.get("quote_id"),
            "block_rfq_id": quote.block_rfq_id,
            "price": quote.price,
            "amount": quote.amount,
            "result": result
        }

    except HTTPException as e:
        raise e
    except Exception as e:
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
        "quote_id": cancel_req.quote_id
    }

    try:
        result = await call_deribit_api(
            session,
            "private/cancel_block_rfq_quote",
            params
        )

        print(f"Quote canceled successfully: {result}")

        return {
            "status": "success",
            "quote_id": cancel_req.quote_id,
            "result": result
        }

    except HTTPException as e:
        raise e
    except Exception as e:
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
        "quote_id": edit_req.quote_id,
        "price": edit_req.price,
        "amount": edit_req.amount
    }

    try:
        result = await call_deribit_api(
            session,
            "private/edit_block_rfq_quote",
            params
        )

        print(f"Quote edited successfully: {result}")

        return {
            "status": "success",
            "quote_id": edit_req.quote_id,
            "price": edit_req.price,
            "amount": edit_req.amount,
            "result": result
        }

    except HTTPException as e:
        raise e
    except Exception as e:
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

        return {
            "status": "success",
            "currency": cancel_all_req.currency or "all",
            "result": result
        }

    except HTTPException as e:
        raise e
    except Exception as e:
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

        all_quotes = btc_result + eth_result

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
        except:
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
