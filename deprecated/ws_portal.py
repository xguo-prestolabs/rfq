# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import asyncio
import json
import logging

from absl import app, flags
from ws_server import WsServer, create_ws_server
from ws_client import WsClientPool
from ws_base import parse_config


async def async_main(config):
    out_queue = asyncio.Queue()
    in_queue = asyncio.Queue()
    server: WsServer = create_ws_server(config["ws_server"], out_queue, in_queue)
    client_pool: WsClientPool = WsClientPool(
        config["ws_client_pool"], out_queue, in_queue
    )

    tasks = [
        asyncio.create_task(server.run(), name="ws_server"),
        asyncio.create_task(client_pool.run(), name="ws_client_pool"),
    ]

    await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    for task in tasks:
        if task.done() and task.exception():
            raise task.exception()
    raise RuntimeError("Connection closed")


async def monitor_queue(q: asyncio.Queue, interval=60):
    while True:
        size = q.qsize()
        maxsize = q.maxsize
        await asyncio.sleep(interval)


def main(_):
    FLAGS = flags.FLAGS
    config = parse_config(FLAGS.config)
    logging.info(f"Config:\n{json.dumps(config, indent=2)}")

    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        print("Quitting...")


if __name__ == "__main__":
    flags.DEFINE_string("config", "config/ws_portal_config.json", "config file")
    app.run(main)
