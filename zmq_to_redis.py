# Copyright (c) 2026 Presto Labs Pte. Ltd.
# Author: xguo

import asyncio
import json
import zmq.asyncio
import redis.asyncio as redis

ZMQ_URL = "tcp://localhost:40000"
REDIS_URL = "redis://localhost:6379"


async def run() -> None:
    redis_client = await redis.from_url(REDIS_URL, decode_responses=True)
    print(f"Connected to Redis: {await redis_client.ping()}")

    context = zmq.asyncio.Context()
    socket = context.socket(zmq.SUB)
    socket.connect(ZMQ_URL)
    socket.setsockopt_string(zmq.SUBSCRIBE, "")
    print(f"ZMQ subscriber connected to {ZMQ_URL}")

    try:
        while True:
            msg = await socket.recv_string()
            try:
                data = json.loads(msg)
                msg_type = data.get("msg_type")
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
                print(f"Invalid JSON: {e}")
            except Exception as e:
                print(f"Error processing message: {e}")
    finally:
        socket.close()
        context.term()
        await redis_client.close()
        print("ZMQ subscriber stopped.")


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
