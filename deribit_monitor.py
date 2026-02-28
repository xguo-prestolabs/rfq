import asyncio
import websockets
import time
import json
import subprocess

import subprocess
import time
import sys
from absl import app, flags, logging


services = [
    "futures_feed.service",
    "futures_ws.service",
    "futures_wsapi.service",
    "options_feed.service",
    "options_ws.service",
    "options_wsapi.service",
]


DERIBIT_URLS = [
#    "deribit-options-feed.colo",
    "deribit-options-ws.colo",
    "deribit-options-wsapi.colo",
#    "deribit-futures-feed.colo",
    "deribit-futures-ws.colo",
    "deribit-futures-wsapi.colo",
]


def _log_task_result(task: asyncio.Task):
    name = task.get_name()
    try:
        task.result()
    except asyncio.CancelledError:
        logging.info("Task %s cancelled", name)
    except Exception as e:
        logging.exception("Task %s failed", name, exc_info=e)



def run(cmd):
    subprocess.run(cmd, check=True)

def start(service):
    run(["systemctl", "--user", "enable", service])
    run(["systemctl", "--user", "start", service])
    time.sleep(1)

def stop(service):
    run(["systemctl", "--user", "stop", service])
    run(["systemctl", "--user", "disable", service])

def restart(service):
    stop(service)
    start(service)


class DeribitWsClient:
    def __init__(self, out_queue: asyncio.Queue):
        self.out_queue = out_queue
        self.last_time = time.time_ns()

    async def connect(self, uri):
        uri = f"ws://{uri}:8022/api/v2"
        async with websockets.connect(uri) as websocket:
            logging.info(f"Connected to {uri}")
            async for message in websocket:
                msg = {
                    "ts": time.time_ns(),
                    "uri": uri,
                    "message": message,
                }
                await self.out_queue.put(msg)


class Writer:
    def __init__(self, out_queue: asyncio.Queue):
        self.out_queue = out_queue
        self.last_time = time.time_ns()

    async def run(self):
        output_file = f"deribit_ws_messages_{time.time_ns()}.txt"
        logging.info(f"Writing to {output_file}")
        with open(output_file, "a", encoding="utf-8") as out_file:
            while True:
                msg = await self.out_queue.get()
                try:
                    json_message = json.loads(msg["message"])
                    msg["message"] = json_message
                    jrpc_id = str(json_message.get("id", ""))
                    if jrpc_id.startswith("ps"):
                        self.last_time = time.time_ns()
                except Exception as e:
                    logging.error(f"Error parsing message: {msg}")
                    logging.error(f"Error parsing message: {e}")

                out_file.write(json.dumps(msg) + "\n")
                out_file.flush()
                if (time.time_ns() - self.last_time) / 1e9 > 60:
                    for service in services:
                        restart(service)
                        time.sleep(1)
                    time.sleep(20)
                    sys.exit(10)


async def main_async():
    queue = asyncio.Queue()
    tasks = []
    for uri in DERIBIT_URLS:
        client = DeribitWsClient(queue)
        task = asyncio.create_task(client.connect(uri), name=f"deribit_ws_client_{uri}")
        task.add_done_callback(_log_task_result)
        tasks.append(task)

    writer = Writer(queue)
    task = asyncio.create_task(writer.run(), name="writer")
    task.add_done_callback(_log_task_result)
    tasks.append(task)

    await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
    logging.info("exit")



def main(_):
    asyncio.run(main_async())


if __name__ == "__main__":
    app.run(main)
