#!/usr/bin/env python3
"""
Test script for ZmqSubscriber.

Subscribes to ZMQ (tcp://localhost:40000) and waits for up to 2 minutes.
If a price update is received, the test passes.

Usage:
    uv run python test_zmq_subscriber.py
"""

import asyncio
import sys
import time

from mmbot import ZmqSubscriber, log

ZMQ_URL = "tcp://localhost:40000"
TIMEOUT_SECONDS = 120


class TestHandler:
    """Simple handler that records received price updates."""

    def __init__(self):
        self.prices_received: dict[str, float] = {}
        self.update_count: int = 0
        self.first_price_event = asyncio.Event()

    async def on_price(self, instrument: str, price: float) -> None:
        log.info(f"[TEST] on_price: {instrument} = {price}")
        self.prices_received[instrument] = price
        self.update_count += 1
        self.first_price_event.set()


async def run_test():
    handler = TestHandler()
    subscriber = ZmqSubscriber(ZMQ_URL, handler)

    log.info(f"Connecting to {ZMQ_URL}")
    await subscriber.start()

    # Run subscriber in background
    sub_task = asyncio.create_task(subscriber.run())

    log.info(f"Waiting up to {TIMEOUT_SECONDS}s for a price event...")
    start_time = time.time()

    try:
        # Wait for first price event or timeout
        await asyncio.wait_for(handler.first_price_event.wait(), timeout=TIMEOUT_SECONDS)
        elapsed = time.time() - start_time
        log.info(f"SUCCESS: Received price event after {elapsed:.1f}s")
        log.info(f"  Total updates: {handler.update_count}")
        log.info(f"  Instruments: {list(handler.prices_received.keys())[:10]}...")
        return True

    except asyncio.TimeoutError:
        log.warning(f"TIMEOUT: No price events received in {TIMEOUT_SECONDS}s")
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
