#!/usr/bin/env python3
"""
RFQ Market-Maker Bot (mmbot)

A simple automated RFQ quoter that:
1. Subscribes to ZMQ for real-time fair prices from C++ binary
2. Listens for new RFQs via Redis pub/sub (`rfq_updates` channel)
3. Submits two-sided quotes (buy + sell) with a configurable edge
4. Edits quotes when fair prices change beyond a threshold
5. Cleans up quotes when RFQs expire or quotes are filled/cancelled

Usage:
    uv run python mmbot.py --config config/mmbot.json
"""

import argparse
import asyncio
import json
import logging
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import aiohttp
import redis.asyncio as aioredis
import zmq.asyncio
from pydantic import BaseModel, Field

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("mmbot")

TICK = 0.0001  # option tick size
MIN_PRICE = TICK  # Deribit requires positive price


# ─── Config ────────────────────────────────────────────────────────────────


class MMBotConfig(BaseModel):
    """Configuration for the RFQ Market-Maker Bot."""

    api_url: str = Field(
        default="http://localhost:8000",
        description="app.py base URL",
    )
    redis_url: str = Field(
        default="redis://localhost:6379",
        description="Redis connection URL",
    )
    zmq_url: str = Field(
        default="tcp://localhost:40000",
        description="ZMQ PUB socket URL for price/greeks data from C++ binary",
    )
    edge_ticks: int = Field(
        default=3,
        ge=0,
        description="Edge in ticks for quoting (bid/ask spread from fair)",
    )
    threshold_ticks: int = Field(
        default=1,
        ge=1,
        description="Min price change in ticks to trigger quote edit",
    )
    cooldown: float = Field(
        default=1.0,
        ge=0,
        description="Min seconds between edits per quote",
    )
    amount_frac: float = Field(
        default=1.0,
        gt=0,
        le=1,
        description="Fraction of RFQ amount to quote (0 < frac <= 1)",
    )
    dry_run: bool = Field(
        default=False,
        description="Log actions without sending to Deribit",
    )


def round_to_tick(price: float, tick: float = TICK) -> str:
    """Round to nearest tick and return as string to avoid IEEE 754 issues."""
    decimals = max(0, -math.floor(math.log10(tick)))
    return f"{max(MIN_PRICE, round(price / tick) * tick):.{decimals}f}"


# ─── Redis Subscriber ──────────────────────────────────────────────────────


class RFQHandler(Protocol):
    """Protocol for handling RFQ pub/sub events."""

    async def on_new_rfq(self, rfq_id: int, data: dict) -> None:
        """Called when a new open RFQ is received."""
        ...

    async def on_rfq_update(self, rfq_id: int, data: dict) -> None:
        """Called when an existing RFQ state changes."""
        ...

    async def on_quote_update(self, quote_id: int) -> None:
        """Called when a quote state changes."""
        ...

    async def on_trade(self, rfq_id: int) -> None:
        """Called when a trade occurs on an RFQ."""
        ...


class RedisSubscriber:
    """Subscribes to Redis pub/sub and dispatches events to a handler."""

    def __init__(self, redis_url: str, channel: str, handler: RFQHandler):
        self.redis_url = redis_url
        self.channel = channel
        self.handler = handler
        self._redis: aioredis.Redis | None = None

    async def start(self) -> None:
        """Connect to Redis and start the subscription loop."""
        self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        log.info(f"RedisSubscriber connected to {self.redis_url}")

    async def stop(self) -> None:
        """Close the Redis connection."""
        if self._redis:
            await self._redis.close()
            self._redis = None

    async def run(self) -> None:
        """Subscribe and dispatch messages to handler. Call start() first."""
        if not self._redis:
            raise RuntimeError("RedisSubscriber not started. Call start() first.")

        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.channel)
        log.info(f"Subscribed to {self.channel}")

        try:
            async for message in pubsub.listen():
                if message["type"] != "message":
                    continue
                try:
                    payload = json.loads(message["data"])
                    await self._dispatch(payload)
                except Exception as e:
                    log.error(f"Error processing pub/sub message: {e}", exc_info=True)
        except asyncio.CancelledError:
            pass
        finally:
            await pubsub.unsubscribe(self.channel)

    async def _dispatch(self, payload: dict) -> None:
        """Route a message to the appropriate handler method."""
        msg_type = payload.get("type")
        msg_id = payload.get("id")

        if msg_type == "rfq":
            data = payload.get("data", {})
            state = data.get("state", "")
            if state == "open":
                await self.handler.on_new_rfq(msg_id, data)
            else:
                await self.handler.on_rfq_update(msg_id, data)

        elif msg_type == "rfq_quote":
            await self.handler.on_quote_update(msg_id)

        elif msg_type == "rfq_trade":
            await self.handler.on_trade(msg_id)


# ─── Quote state ───────────────────────────────────────────────────────────


@dataclass
class QuoteState:
    block_rfq_id: int
    block_rfq_quote_id: int
    direction: str  # "buy" or "sell"
    amount: float
    legs: list  # [{instrument_name, direction, ratio, price}, ...]
    hedge: dict | None
    submitted_at: float
    last_edit_at: float | None = None


@dataclass
class RFQContext:
    """Tracks everything the bot knows about one RFQ."""
    rfq_id: int
    rfq_data: dict  # raw RFQ data from /rfqs
    buy_quote: QuoteState | None = None
    sell_quote: QuoteState | None = None
    last_prices: dict = field(default_factory=dict)  # instrument -> fair_price


# ─── Bot ───────────────────────────────────────────────────────────────────


class MMBot:
    def __init__(self, config: MMBotConfig):
        self.config = config
        self.api_url = config.api_url.rstrip("/")
        self.redis_url = config.redis_url
        self.zmq_url = config.zmq_url
        self.edge_ticks = config.edge_ticks
        self.threshold_ticks = config.threshold_ticks
        self.cooldown = config.cooldown
        self.amount_frac = config.amount_frac
        self.dry_run = config.dry_run

        self.contexts: dict[int, RFQContext] = {}  # rfq_id -> RFQContext
        self._session: aiohttp.ClientSession | None = None
        self._redis: aioredis.Redis | None = None

        # Price cache populated by ZMQ subscriber
        self._price_cache: dict[str, float] = {}  # instrument_name -> fair_price

    # ── HTTP helpers ──────────────────────────────────────────────────

    async def _get(self, path: str) -> dict | None:
        try:
            async with self._session.get(f"{self.api_url}{path}") as r:
                if r.status == 200:
                    return await r.json()
                log.warning(f"GET {path} → {r.status}")
        except Exception as e:
            log.error(f"GET {path} failed: {e}")
        return None

    async def _post(self, path: str, payload: dict) -> dict | None:
        try:
            async with self._session.post(f"{self.api_url}{path}", json=payload) as r:
                body = await r.json()
                if r.status == 200:
                    return body
                log.warning(f"POST {path} → {r.status}: {body}")
        except Exception as e:
            log.error(f"POST {path} failed: {e}")
        return None

    # ── Price cache (from ZMQ) ────────────────────────────────────────

    def _get_leg_prices(self, legs: list) -> dict:
        """Get fair prices for all legs from local cache. Returns {instrument_name: price_or_None}."""
        prices = {}
        for leg in legs:
            inst = leg["instrument_name"]
            prices[inst] = self._price_cache.get(inst)
        return prices

    async def _zmq_price_loop(self):
        """Subscribe to ZMQ and cache fair prices locally."""
        context = zmq.asyncio.Context()
        socket = context.socket(zmq.SUB)
        socket.connect(self.zmq_url)
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        log.info(f"ZMQ subscriber connected to {self.zmq_url}")

        try:
            while True:
                msg = await socket.recv_string()
                try:
                    data = json.loads(msg)
                    msg_type = data.get("msg_type")
                    if msg_type == "fair_price":
                        native_product = data.get("native_product")
                        price = data.get("strat_mid_price")
                        if native_product and price is not None:
                            self._price_cache[native_product] = price
                except json.JSONDecodeError:
                    pass
                except Exception as e:
                    log.error(f"ZMQ message error: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            socket.close()
            context.term()
            log.info("ZMQ subscriber stopped")

    # ── Quote logic ───────────────────────────────────────────────────

    def _build_legs(self, rfq_legs: list, prices: dict, direction: str) -> list | None:
        """Build quote legs with edge-adjusted prices.

        direction: "buy" or "sell" — the maker's side.
        For buy quotes, we subtract edge (we want a lower price).
        For sell quotes, we add edge (we want a higher price).
        """
        result = []
        for leg in rfq_legs:
            inst = leg["instrument_name"]
            fair = prices.get(inst)
            if fair is None:
                return None  # can't quote without fair price
            if direction == "buy":
                edged = fair - self.edge_ticks * TICK
            else:
                edged = fair + self.edge_ticks * TICK
            result.append({
                "instrument_name": inst,
                "direction": leg["direction"],
                "ratio": leg["ratio"],
                "price": round_to_tick(edged),
            })
        return result

    async def _submit_quote(self, ctx: RFQContext, direction: str, prices: dict) -> QuoteState | None:
        legs = self._build_legs(ctx.rfq_data["legs"], prices, direction)
        if legs is None:
            return None

        amount = ctx.rfq_data["amount"] * self.amount_frac
        payload = {
            "block_rfq_id": ctx.rfq_id,
            "amount": amount,
            "direction": direction,
            "legs": legs,
        }
        # Include hedge if present in RFQ
        hedge = ctx.rfq_data.get("hedge")
        if hedge:
            payload["hedge"] = hedge

        if self.dry_run:
            log.info(f"[DRY-RUN] SUBMIT {direction.upper()} rfq={ctx.rfq_id} legs={legs}")
            return None

        result = await self._post("/quotes/add", payload)
        if result and result.get("status") == "success":
            qid = result["result"]["block_rfq_quote_id"]
            qs = QuoteState(
                block_rfq_id=ctx.rfq_id,
                block_rfq_quote_id=qid,
                direction=direction,
                amount=amount,
                legs=legs,
                hedge=hedge,
                submitted_at=time.time(),
            )
            log.info(f"SUBMITTED {direction.upper()} rfq={ctx.rfq_id} quote={qid}")
            return qs
        return None

    async def _edit_quote(self, qs: QuoteState, new_legs: list) -> bool:
        payload = {
            "quote_id": qs.block_rfq_quote_id,
            "amount": qs.amount,
            "legs": new_legs,
        }
        if qs.hedge:
            payload["hedge"] = qs.hedge

        if self.dry_run:
            log.info(f"[DRY-RUN] EDIT {qs.direction.upper()} quote={qs.block_rfq_quote_id} legs={new_legs}")
            return True

        result = await self._post("/quotes/edit", payload)
        if result and result.get("status") == "success":
            qs.legs = new_legs
            qs.last_edit_at = time.time()
            log.info(f"EDITED {qs.direction.upper()} quote={qs.block_rfq_quote_id}")
            return True
        return False

    async def _cancel_quote(self, qs: QuoteState) -> bool:
        if self.dry_run:
            log.info(f"[DRY-RUN] CANCEL quote={qs.block_rfq_quote_id}")
            return True
        result = await self._post("/quotes/cancel", {"quote_id": qs.block_rfq_quote_id})
        if result and result.get("status") == "success":
            log.info(f"CANCELLED quote={qs.block_rfq_quote_id}")
            return True
        return False

    def _price_changed(self, old_legs: list, new_legs: list) -> bool:
        """Check if any leg price changed beyond threshold."""
        for old, new in zip(old_legs, new_legs):
            old_p = float(old["price"])
            new_p = float(new["price"])
            if abs(new_p - old_p) >= self.threshold_ticks * TICK - 1e-12:
                return True
        return False

    def _cooldown_ok(self, qs: QuoteState) -> bool:
        last = qs.last_edit_at or qs.submitted_at
        return (time.time() - last) >= self.cooldown

    # ── RFQ handling (RFQHandler protocol) ─────────────────────────────

    async def on_new_rfq(self, rfq_id: int, rfq_data: dict) -> None:
        """Quote a new RFQ with two-sided quotes."""
        if rfq_id in self.contexts:
            return  # already tracking

        # Only quote open RFQs
        if rfq_data.get("state") != "open":
            return

        # Check expiry — skip if less than 30s remaining
        exp_ts = rfq_data.get("expiration_timestamp", 0)
        if exp_ts and (exp_ts / 1000 - time.time()) < 30:
            log.info(f"Skipping rfq={rfq_id}: expires too soon")
            return

        ctx = RFQContext(rfq_id=rfq_id, rfq_data=rfq_data)
        self.contexts[rfq_id] = ctx

        prices = self._get_leg_prices(rfq_data["legs"])
        if any(p is None for p in prices.values()):
            log.warning(f"Missing prices for rfq={rfq_id}, skipping: {prices}")
            del self.contexts[rfq_id]
            return

        ctx.last_prices = prices

        # Submit buy and sell quotes
        ctx.buy_quote = await self._submit_quote(ctx, "buy", prices)
        ctx.sell_quote = await self._submit_quote(ctx, "sell", prices)

        if not ctx.buy_quote and not ctx.sell_quote:
            log.warning(f"Failed to submit any quotes for rfq={rfq_id}")
            del self.contexts[rfq_id]

    async def on_rfq_update(self, rfq_id: int, rfq_data: dict) -> None:
        """Handle RFQ state change (expired, cancelled, etc.)."""
        ctx = self.contexts.get(rfq_id)
        if not ctx:
            return
        state = rfq_data.get("state", "")
        if state != "open":
            log.info(f"RFQ {rfq_id} → {state}, removing context")
            del self.contexts[rfq_id]

    async def on_quote_update(self, quote_id: int) -> None:
        """Handle quote state change from rfq_quote pub/sub."""
        # Find the context that owns this quote
        for ctx in self.contexts.values():
            if ctx.buy_quote and ctx.buy_quote.block_rfq_quote_id == quote_id:
                # Check if quote is terminal
                data = await self._get(f"/quotes")
                if data:
                    for q in data.get("quotes", []):
                        if q.get("block_rfq_quote_id") == quote_id:
                            state = q.get("quote_state", "")
                            if state in ("filled", "cancelled", "expired"):
                                log.info(f"Buy quote {quote_id} → {state}")
                                ctx.buy_quote = None
                            break
                return
            if ctx.sell_quote and ctx.sell_quote.block_rfq_quote_id == quote_id:
                data = await self._get(f"/quotes")
                if data:
                    for q in data.get("quotes", []):
                        if q.get("block_rfq_quote_id") == quote_id:
                            state = q.get("quote_state", "")
                            if state in ("filled", "cancelled", "expired"):
                                log.info(f"Sell quote {quote_id} → {state}")
                                ctx.sell_quote = None
                            break
                return

    async def on_trade(self, rfq_id: int) -> None:
        """Handle trade event — remove RFQ context."""
        if rfq_id in self.contexts:
            log.info(f"Trade on rfq={rfq_id}, removing context")
            del self.contexts[rfq_id]

    # ── Price refresh loop ────────────────────────────────────────────

    async def _price_refresh_loop(self):
        """Periodically check prices and edit quotes if needed."""
        while True:
            await asyncio.sleep(1.0)
            for rfq_id, ctx in list(self.contexts.items()):
                # Check expiry
                exp_ts = ctx.rfq_data.get("expiration_timestamp", 0)
                if exp_ts and (exp_ts / 1000 - time.time()) < 5:
                    log.info(f"RFQ {rfq_id} expiring, removing context")
                    del self.contexts[rfq_id]
                    continue

                if not ctx.buy_quote and not ctx.sell_quote:
                    del self.contexts[rfq_id]
                    continue

                prices = self._get_leg_prices(ctx.rfq_data["legs"])
                if any(p is None for p in prices.values()):
                    continue

                # Check and edit buy quote
                if ctx.buy_quote and self._cooldown_ok(ctx.buy_quote):
                    new_legs = self._build_legs(ctx.rfq_data["legs"], prices, "buy")
                    if new_legs and self._price_changed(ctx.buy_quote.legs, new_legs):
                        await self._edit_quote(ctx.buy_quote, new_legs)

                # Check and edit sell quote
                if ctx.sell_quote and self._cooldown_ok(ctx.sell_quote):
                    new_legs = self._build_legs(ctx.rfq_data["legs"], prices, "sell")
                    if new_legs and self._price_changed(ctx.sell_quote.legs, new_legs):
                        await self._edit_quote(ctx.sell_quote, new_legs)

                ctx.last_prices = prices

    # ── Bootstrap: quote existing open RFQs ───────────────────────────

    async def _bootstrap(self):
        """On startup, fetch all open RFQs and quote them."""
        data = await self._get("/rfqs")
        if not data:
            log.warning("No RFQs found at startup")
            return
        open_rfqs = {int(k): v for k, v in data.items() if v.get("state") == "open"}
        log.info(f"Bootstrap: {len(open_rfqs)} open RFQs")
        for rfq_id, rfq_data in open_rfqs.items():
            await self.handle_new_rfq(rfq_id, rfq_data)

    # ── Main ──────────────────────────────────────────────────────────

    async def run(self):
        self._session = aiohttp.ClientSession()
        self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)

        log.info(f"mmbot started: api={self.api_url} redis={self.redis_url} "
                 f"zmq={self.zmq_url} edge={self.edge_ticks} threshold={self.threshold_ticks} "
                 f"cooldown={self.cooldown}s amount_frac={self.amount_frac} "
                 f"dry_run={self.dry_run}")

        # Start ZMQ price subscriber as a background task
        zmq_task = asyncio.create_task(self._zmq_price_loop())

        # Wait a moment for initial prices to populate
        await asyncio.sleep(1.0)
        log.info(f"Price cache initialized with {len(self._price_cache)} instruments")

        try:
            await self._bootstrap()
            await asyncio.gather(
                self._pubsub_loop(),
                self._price_refresh_loop(),
            )
        except asyncio.CancelledError:
            pass
        finally:
            log.info("Shutting down...")
            zmq_task.cancel()
            try:
                await zmq_task
            except asyncio.CancelledError:
                pass
            # Cancel all active quotes on shutdown
            for ctx in self.contexts.values():
                if ctx.buy_quote:
                    await self._cancel_quote(ctx.buy_quote)
                if ctx.sell_quote:
                    await self._cancel_quote(ctx.sell_quote)
            self.contexts.clear()
            await self._session.close()
            await self._redis.close()
            log.info("mmbot stopped")


def load_config(config_path: str) -> MMBotConfig:
    """Load and validate config from JSON file."""
    path = Path(config_path)
    if not path.exists():
        log.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    return MMBotConfig(**data)


def main():
    parser = argparse.ArgumentParser(description="RFQ Market-Maker Bot")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to JSON config file (e.g., config/mmbot.json)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    log.info(f"Loaded config from {args.config}")

    bot = MMBot(config)

    loop = asyncio.new_event_loop()
    task = loop.create_task(bot.run())
    try:
        loop.run_until_complete(task)
    except KeyboardInterrupt:
        task.cancel()
        try:
            loop.run_until_complete(task)
        except asyncio.CancelledError:
            pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
