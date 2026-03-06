# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import asyncio
import websockets
import logging
import json
import time
import hmac
import hashlib
import base64
import ssl
import certifi
from pymongo import MongoClient

from absl import app, flags, logging
from typing import Set, Optional, Dict
from websockets.legacy.protocol import WebSocketCommonProtocol
from ws_base import Message, MsgType, parse_config, Writer


class WsClient:
    def __init__(
        self,
        config: dict,
        in_queue: asyncio.Queue,
    ):
        self.config = config
        self.in_queue = in_queue
        self.subscription_msg = config.get("subscription", None)
        self.writer = Writer()

        # For request-response pattern (used by DeribitWsClient)
        self.pending_requests: Dict[str, asyncio.Future] = {}
        self.mongo_client = None
        self.mongo_db = None
        self.mongo_collection = None

        if config.get("mongodb", {}).get("enabled", False):
            mongo_config = config["mongodb"]
            mongo_uri = mongo_config.get("uri", "mongodb://localhost:27017/")
            self.mongo_client = MongoClient(mongo_uri)
            self.mongo_db = self.mongo_client[
                mongo_config.get("database", "deribit_block_rfq")
            ]
            self.mongo_collection = self.mongo_db[
                mongo_config.get("collection", "messages")
            ]
            logging.info(f"MongoDB connection established: {mongo_uri}")

    async def login(self, ws: WebSocketCommonProtocol):
        raise NotImplementedError("Subclasses must implement this method")

    async def send(self, message: str):
        raise NotImplementedError("Subclasses must implement this method")

    async def subscribe(self, ws: WebSocketCommonProtocol):
        raise NotImplementedError("Subclasses must implement this method")

    async def check_auth(self, ws: WebSocketCommonProtocol):
        raise NotImplementedError("Subclasses must implement this method")

    async def keepalive(self):
        raise NotImplementedError("Subclasses must implement this method")

    def need_publish(self, message: str) -> bool:
        raise NotImplementedError("Subclasses must implement this method")

    def close(self):
        if self.mongo_client is not None:
            try:
                self.mongo_client.close()
                logging.info("MongoDB connection closed")
            except Exception as e:
                logging.error(f"Error closing MongoDB connection: {e}")

    async def connect(self):
        url = self.config["server"]
        use_ssl = url.startswith("wss://")
        ssl_context = None
        if use_ssl:
            ssl_context = ssl.create_default_context()
            ssl_context.load_verify_locations(certifi.where())

        # All steps below may fail and throw exeception.
        ws = await websockets.connect(
            url,
            ping_interval=20,
            close_timeout=5,
            max_queue=1024,
            compression=None,  # no compression
            ssl=ssl_context,
        )
        logging.info(f"Connected to WebSocket: {url}")

        self.conn = ws

        # Start message loop as a background task FIRST
        # so it can route responses for login/subscribe
        message_loop_task = asyncio.create_task(
            self._message_loop(ws), name="message_loop"
        )

        # Give the loop a moment to start
        await asyncio.sleep(0.1)

        # Now we can do login/subscribe which will use request-response pattern
        await self.login(ws)
        logging.info(f"Login success")

        await self.check_auth(ws)
        logging.info(f"Auth check success")

        if self.subscription_msg:
            await self.subscribe(ws)
            logging.info(f"Subscription success")

        # Start health check task
        health_check_task = asyncio.create_task(
            self._periodic_health_check(), name="health_check"
        )

        try:
            # Wait for message loop to complete (it runs until connection closes)
            await message_loop_task
        except Exception as e:
            logging.error(f"Message loop failed: {e}")
            raise  # Re-raise to trigger reconnection
        finally:
            # Cancel tasks
            health_check_task.cancel()
            message_loop_task.cancel()
            try:
                await health_check_task
            except asyncio.CancelledError:
                pass
            try:
                await message_loop_task
            except asyncio.CancelledError:
                pass

            self.conn = None
            logging.info("Connection closed")

    async def _message_loop(self, ws: WebSocketCommonProtocol):
        """Message loop that reads from WebSocket and routes messages"""
        try:
            async for raw_message in ws:
                # Parse message to check if it's a response to a pending request
                try:
                    parsed = json.loads(raw_message)
                    message_id = parsed.get("id")

                    # Check if this is a response to a pending request
                    if message_id and message_id in self.pending_requests:
                        future = self.pending_requests[message_id]
                        if not future.done():
                            future.set_result(parsed)
                        continue  # Don't process this message further
                except (json.JSONDecodeError, KeyError):
                    pass  # Not a JSON-RPC response, process as normal message

                # Create Message object for normal messages
                message = Message(
                    msg_type=MsgType.DATA,
                    fetch_time=time.time_ns(),
                    message=raw_message,
                )

                if self.mongo_collection is not None:
                    try:
                        self.mongo_collection.insert_one(message.to_dict())
                        logging.info(f"Saved to MongoDB: {message.to_json()}")
                    except Exception as e:
                        logging.error(f"Failed to save to MongoDB: {e}")
                else:
                    self.writer.write(message)

                if self.need_publish(message.message) and self.mongo_collection is None:
                    await self.in_queue.put(message)
                else:
                    logging.info(f"Ignoring message: {message.to_json()}")
        except Exception as e:
            logging.error(f"Message loop error: {e}")
            raise

    async def _periodic_health_check(self):
        interval = self.config.get("ping_interval", 60)
        while True:
            await self.keepalive()
            await asyncio.sleep(interval)

    async def run(self):
        while True:
            try:
                await self.connect()
                # If connect() completes without exception, connection closed gracefully
                # Exit the loop instead of reconnecting
                logging.info("Connection closed gracefully, exiting...")
                break
            except Exception as e:
                logging.error(f"Connection error: {e}")
                logging.info("Reconnecting in 5 seconds...")
                await asyncio.sleep(5)


class DeribitWsClient(WsClient):
    def __init__(self, config: dict, out_queue: asyncio.Queue, in_queue: asyncio.Queue):
        super().__init__(config, in_queue)
        self.request_id = 0
        self.id_prefix = f"wps_deribit_{id(self)}"
        self.last_auth_response = None

    def _next_request_id(self) -> str:
        self.request_id += 1
        return f"{self.id_prefix}_{self.request_id}"

    async def _send_jsonrpc(
        self,
        ws: WebSocketCommonProtocol,
        method: str,
        params: dict = None,
        timeout: float = 10.0,
    ) -> dict:
        request_id = self._next_request_id()
        request = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        }

        # Create a Future to wait for the response
        future = asyncio.Future()
        self.pending_requests[request_id] = future

        try:
            logging.info(f"Sending request: {json.dumps(request)}")
            await ws.send(json.dumps(request))

            # Wait for the response with timeout
            result = await asyncio.wait_for(future, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            logging.error(f"Request {request_id} timed out after {timeout}s")
            raise
        finally:
            # Clean up the pending request
            self.pending_requests.pop(request_id, None)

    async def login(self, ws: WebSocketCommonProtocol):
        trade_key = self.config["trade_key"]
        params = {
            "grant_type": "client_credentials",
            "client_id": trade_key["access_key"],
            "client_secret": trade_key["secret_key"],
            "scope": self.config.get("scope", "session:cpp_og"),
        }
        response = await self._send_jsonrpc(ws, "public/auth", params)
        logging.info(f"Login response: {json.dumps(response, indent=2)}")
        self.last_auth_response = response
        assert response["result"]["access_token"], "Login failed"

    async def subscribe(self, ws: WebSocketCommonProtocol):
        result = await self._send_jsonrpc(ws, **self.subscription_msg)
        logging.info(f"Subscription done. Result: {result}")

    async def check_auth(self, ws: WebSocketCommonProtocol):
        method = "private/get_account_summary"
        params = {"currency": "BTC"}
        result = await self._send_jsonrpc(ws, method, params)
        logging.log_first_n(logging.INFO, f"Check auth response: {json.dumps(result, indent=2)}", 1)

        if result.get("error"):
            raise RuntimeError(f"Check auth failed: {result.get('error')}")

    def need_publish(self, message: str) -> bool:
        json_message = json.loads(message)
        return not json_message.get("id", "").startswith("wps_deribit_")

    async def keepalive(self):
        await self.check_auth(self.conn)


class OkexWsClient(WsClient):
    def __init__(self, config: dict, in_queue: asyncio.Queue):
        super().__init__(config, in_queue)
        self.start_time = time.time_ns()

    def _next_request_id(self) -> int:
        return time.time_ns() - self.start_time

    async def _send_okex_request(
        self, ws: WebSocketCommonProtocol, op: str, args: dict = None
    ) -> dict:
        request_id = self._next_request_id()
        request = {
            "op": op,
            "args": args or {},
            "id": request_id,
        }
        logging.info(f"Sending request: {json.dumps(request)}")
        await ws.send(json.dumps(request))

    async def login(self, ws: WebSocketCommonProtocol):
        ts = int(time.time())
        prehash = f"{ts}GET/users/self/verify"
        trade_key = self.config["trade_key"]
        secret_key = trade_key["secret_key"].encode()
        digest = hmac.new(secret_key, prehash.encode(), hashlib.sha256).digest()
        sign = base64.b64encode(digest).decode()

        args = [
            {
                "apiKey": trade_key["access_key"],
                "passphrase": trade_key["passphrase"],
                "timestamp": ts,
                "sign": sign,
            }
        ]
        await self._send_okex_request(ws, "login", args)
        raw = await ws.recv()

        try:
            msg = json.loads(raw)
        except Exception as e:
            raise RuntimeError(f"login parse failed: {e}")

        if not (
            isinstance(msg, dict)
            and msg.get("event") == "login"
            and str(msg.get("code", "0")) in ("0", "")
        ):
            raise RuntimeError(f"login failed: {msg}")

        logging.info(
            f'Login successful: {ws.remote_address}, {msg}, owner: {trade_key["owner"]}'
        )

    async def check_auth(self, ws: WebSocketCommonProtocol):
        pass

    async def subscribe(self, ws: WebSocketCommonProtocol):
        if self.config.get("subscription", None):
            args = self.config["subscription"]["args"]
            await self._send_okex_request(ws, "subscribe", args)
            logging.info(f"Subscription sent.")

    async def keepalive(self):
        await self.conn.send("ping")

    def need_publish(self, message: str) -> bool:
        return True


class WsClientPool:
    def __init__(self, config: dict, out_queue: asyncio.Queue, in_queue: asyncio.Queue):
        self.config = config
        self.out_queue = out_queue
        self.in_queue = in_queue
        self.clients_pool: Set[WsClient] = set()

    async def forward(self):
        try:
            while True:
                message = await self.out_queue.get()
                for client in self.clients_pool.copy():
                    try:
                        await client.send(message)
                    except Exception as e:
                        logging.error(f"Error sending to client {client}: {e}")
                        self.clients_pool.discard(client)
        except Exception as e:
            logging.error(f"Error forwarding message: {e}")
            return

    async def run(self):
        for n in range(self.config["num_workers"]):
            if self.config["client_type"] == "deribit":
                client = DeribitWsClient(self.config, self.in_queue)
            elif self.config["client_type"] == "okex":
                client = OkexWsClient(self.config, self.in_queue)
            else:
                raise ValueError(
                    "Invalid server type: {}".format(self.config["client_type"])
                )
            self.clients_pool.add(client)
            tasks = []
            task = asyncio.create_task(client.run(), name=f"ws_client_{n}")
            tasks.append(task)

        task = asyncio.create_task(self.forward(), name="forward")
        tasks.append(task)
        await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)


async def async_main(config):
    out_queue = asyncio.Queue()
    in_queue = asyncio.Queue()

    logging.info(f"Starting WS client with config:\n{json.dumps(config, indent=2)}")
    if config["client_type"] == "deribit":
        client = DeribitWsClient(config, out_queue, in_queue)
    elif config["client_type"] == "okex":
        client = OkexWsClient(config, out_queue, in_queue)
    else:
        raise ValueError("Invalid server type: {}".format(config["client_type"]))

    tasks = [
        asyncio.create_task(client.run(), name="ws_client"),
    ]

    await asyncio.gather(*tasks)
    while True:
        await asyncio.sleep(10)


def main(_):
    FLAGS = flags.FLAGS
    config = parse_config(FLAGS.config)

    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        print("Quitting...")


if __name__ == "__main__":
    flags.DEFINE_string("config", "config/ws_client_config_mongodb.json", "config file")
    app.run(main)
