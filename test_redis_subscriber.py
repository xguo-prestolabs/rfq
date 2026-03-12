#!/usr/bin/env python3
"""
Test script for RedisSubscriber.

Subscribes to testnet Redis (6380) and waits for up to 2 minutes.
If an RFQ message is received, the test passes.

Usage:
    uv run python test_redis_subscriber.py
"""

import asyncio
import sys
import time

from mmbot import RedisSubscriber, log

REDIS_URL = "redis://localhost:6380"
CHANNEL = "rfq_updates"
TIMEOUT_SECONDS = 120


class TestHandler:
    """Simple handler that records received events."""

    def __init__(self):
        self.rfqs_received: list[int] = []
        self.updates_received: list[int] = []
        self.quotes_received: list[int] = []
        self.trades_received: list[int] = []
        self.first_rfq_event = asyncio.Event()

    async def on_new_rfq(self, rfq_id: int, data: dict) -> None:
        log.info(f"[TEST] on_new_rfq: rfq_id={rfq_id} state={data.get('state')}")
        self.rfqs_received.append(rfq_id)
        self.first_rfq_event.set()

    async def on_rfq_update(self, rfq_id: int, data: dict) -> None:
        log.info(f"[TEST] on_rfq_update: rfq_id={rfq_id} state={data.get('state')}")
        self.updates_received.append(rfq_id)
        self.first_rfq_event.set()

    async def on_quote_update(self, quote_id: int) -> None:
        log.info(f"[TEST] on_quote_update: quote_id={quote_id}")
        self.quotes_received.append(quote_id)

    async def on_trade(self, rfq_id: int) -> None:
        log.info(f"[TEST] on_trade: rfq_id={rfq_id}")
        self.trades_received.append(rfq_id)


async def run_test():
    handler = TestHandler()
    subscriber = RedisSubscriber(REDIS_URL, CHANNEL, handler)

    log.info(f"Connecting to {REDIS_URL}, channel={CHANNEL}")
    await subscriber.start()

    # Run subscriber in background
    sub_task = asyncio.create_task(subscriber.run())

    log.info(f"Waiting up to {TIMEOUT_SECONDS}s for an RFQ event...")
    start_time = time.time()

    try:
        # Wait for first RFQ event or timeout
        await asyncio.wait_for(handler.first_rfq_event.wait(), timeout=TIMEOUT_SECONDS)
        elapsed = time.time() - start_time
        log.info(f"SUCCESS: Received RFQ event after {elapsed:.1f}s")
        log.info(f"  New RFQs: {handler.rfqs_received}")
        log.info(f"  RFQ updates: {handler.updates_received}")
        log.info(f"  Quote updates: {handler.quotes_received}")
        log.info(f"  Trades: {handler.trades_received}")
        return True

    except asyncio.TimeoutError:
        log.warning(f"TIMEOUT: No RFQ events received in {TIMEOUT_SECONDS}s")
        return False

    finally:
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass
        await subscriber.stop()


def main():
    success = asyncio.run(run_test())
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
