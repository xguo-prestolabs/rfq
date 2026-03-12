# Copyright (c) 2026 Presto Labs Pte. Ltd.
# Author: xguo

import asyncio
import json
import logging
import os
import time

import zmq.asyncio
import redis.asyncio as redis

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=__import__("sys").stderr)
log = logging.getLogger(__name__)

ZMQ_URL = "tcp://localhost:40000"
REDIS_URL = "redis://localhost:6380"

# Redis port layout:
#   6379 (production) — C++ binary writes price/greeks data directly here (C++ binary keeps the old zmq publish interface in 40000 for now)
#   6380 (testnet)    — this script bridges ZMQ → Redis for testnet use
#
# Data flow:
#   Production: C++ binary → Redis 6379 → app.py / ws_client_redis.py
#   Testnet:    C++ binary → ZMQ :40000 → zmq_to_redis.py → Redis 6380 → app.py

# InfluxDB 2.x config from environment
INFLUXDB_URL = os.environ.get("INFLUXDB_URL", "")
INFLUXDB_TOKEN = os.environ.get("INFLUXDB_TOKEN", "")
INFLUXDB_ORG = os.environ.get("INFLUXDB_ORG", "")
INFLUXDB_BUCKET = os.environ.get("INFLUXDB_BUCKET", "")
STATS_INTERVAL = 10  # seconds

# Let hardcode the values for now, will be moved to environment variables later.
INFLUXDB_ORG = "hft_order_1"
INFLUXDB_BUCKET = "options_rfq"
INFLUXDB_URL = "http://1.tcp.jp.ngrok.io:24840"
INFLUXDB_TOKEN = "OBweXiRWw_4IgNFFxSAQsCJ7itk91_EBgL5LmoCBnsB4SSOgOqE85djasJjQbmHW9JLR01PCzSKPbLJKEErDuA=="


class Stats:
    """Accumulates ZMQ message counts and bytes per msg_type."""

    def __init__(self):
        self.reset()

    def reset(self):
        self._start = time.monotonic()
        self._counts = {}   # msg_type -> count
        self._bytes = {}    # msg_type -> total bytes

    def record(self, msg_type: str, nbytes: int):
        self._counts[msg_type] = self._counts.get(msg_type, 0) + 1
        self._bytes[msg_type] = self._bytes.get(msg_type, 0) + nbytes

    def snapshot(self):
        """Return (elapsed_seconds, {msg_type: (count, bytes)}) and reset."""
        elapsed = time.monotonic() - self._start
        data = {k: (self._counts[k], self._bytes[k]) for k in self._counts}
        self.reset()
        return elapsed, data


async def _push_influxdb(stats: Stats):
    """Periodically flush stats to InfluxDB 2.x."""
    if not all([INFLUXDB_URL, INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET]):
        log.warning("InfluxDB not configured, stats push disabled")
        return

    from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync

    log.info(f"InfluxDB stats push enabled: {INFLUXDB_URL} org={INFLUXDB_ORG} bucket={INFLUXDB_BUCKET}")

    while True:
        await asyncio.sleep(STATS_INTERVAL)
        elapsed, data = stats.snapshot()
        if not data:
            continue

        try:
            async with InfluxDBClientAsync(
                url=INFLUXDB_URL, token=INFLUXDB_TOKEN, org=INFLUXDB_ORG
            ) as client:
                write_api = client.write_api()
                records = []
                total_count = 0
                total_bytes = 0
                for msg_type, (count, nbytes) in data.items():
                    total_count += count
                    total_bytes += nbytes
                    records.append({
                        "measurement": "zmq_stats",
                        "tags": {"msg_type": msg_type},
                        "fields": {
                            "msg_count": count,
                            "msg_bytes": nbytes,
                            "msg_rate": round(count / elapsed, 2),
                            "byte_rate": round(nbytes / elapsed, 2),
                        },
                    })
                # Also write an aggregate record
                records.append({
                    "measurement": "zmq_stats",
                    "tags": {"msg_type": "ALL"},
                    "fields": {
                        "msg_count": total_count,
                        "msg_bytes": total_bytes,
                        "msg_rate": round(total_count / elapsed, 2),
                        "byte_rate": round(total_bytes / elapsed, 2),
                    },
                })
                await write_api.write(bucket=INFLUXDB_BUCKET, record=records)
        except Exception as e:
            log.error(f"InfluxDB write error: {e}", exc_info=True)


async def run() -> None:
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    log.info(f"Connected to Redis: {await redis_client.ping()}")

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    log.info(f"ZMQ subscriber connected to {ZMQ_URL}")

    stats = Stats()
    influx_task = asyncio.create_task(_push_influxdb(stats))

    try:
        while True:
            msg = await socket.recv_string()
            msg_bytes = len(msg.encode("utf-8"))
            try:
                data = json.loads(msg)
                msg_type = data.get("msg_type", "unknown")
                stats.record(msg_type, msg_bytes)

                if msg_type == "fair_price":
                    native_product = data["native_product"]
                    await redis_client.set(
                        f"price:{native_product}", json.dumps(data)
                    )
                elif msg_type == "greeks":
                    native_product = data["native_product"]
                    await redis_client.set(
                        f"greeks:{native_product}", json.dumps(data)
                    )
                elif msg_type == "total_greeks":
                    native_product = data["native_product"]
                    await redis_client.set(
                        f"total_greeks:{native_product}", json.dumps(data)
                    )
            except json.JSONDecodeError as e:
                stats.record("_json_error", msg_bytes)
                log.warning(f"Invalid JSON: {e}")
            except Exception as e:
                log.error(f"Error processing message: {e}")
    finally:
        influx_task.cancel()
        try:
            await influx_task
        except asyncio.CancelledError:
            pass
        socket.close()
        context.term()
        await redis_client.close()
        log.info("ZMQ subscriber stopped.")


def main() -> None:
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(run())
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
