# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import asyncio
import websockets
import logging
import json
import time
import urllib.parse

from absl import app, flags, logging
from typing import Set

from ws_base import Message, MsgType, parse_config
from latency_monitor import get_latency_monitor, record_message_latency

"""
Interception/filtering is done here by the WsServer.
WsServer also knows how to parse subscription messages.
"""


class WsServer:
    def __init__(
        self,
        config: dict,
        out_queue: asyncio.Queue,
        in_queue: asyncio.Queue,
    ):
        """
        Initialize WebSocket server.

        Config fields:
            - host: str
            - port: int
            - ping_interval: int
        """
        self.ready_clients: Set[websockets.WebSocketServerProtocol] = set()
        self.out_queue = out_queue
        self.in_queue = in_queue
        self.config = config
        self.verbose_log = config.get("verbose_log", False)
        self.latency_monitor = get_latency_monitor()

    def login_response(self, msg) -> str:
        raise NotImplementedError(f"Subclass must implement this method: {msg}")

    def heartbeat_response(self, msg) -> str:
        raise NotImplementedError(f"Subclass must implement this method: {msg}")

    def get_message_type(self, msg) -> MsgType:
        raise NotImplementedError(f"Subclass must implement this method: {msg}")

    async def accept(self, websocket):
        logging.info(f"New client connected: {websocket.remote_address}")
        msg = await websocket.recv()
        msg_type = self.get_message_type(msg)
        if msg_type == MsgType.LOGIN:
            await websocket.send(self.login_response(msg))
            self.ready_clients.add(websocket)
            logging.info(f"Client {websocket.remote_address} logged in")
            await self.handle_message(websocket)
        else:
            logging.error(f"Invalid login message: {msg}")
            await websocket.close()

    async def handle_message(self, websocket):
        # must handle all the exceptions and errors
        logging.info(f"Handling message from client {websocket.remote_address}")
        try:
            async for message in websocket:
                logging.info(
                    f"Received message from client {websocket.remote_address}: {message}"
                )
                msg_type = self.get_message_type(message)
                match msg_type:
                    case MsgType.LOGIN:
                        logging.error(
                            f"Login message should be handled by login_response: {message}"
                        )
                        await websocket.send(self.login_response(message))
                        # do not forward
                        continue
                    case MsgType.SUBSCRIBE:
                        message = Message(MsgType.SUBSCRIBE, message, queue_entry_time=time.time_ns())
                    case MsgType.DATA:
                        message = Message(MsgType.DATA, message, queue_entry_time=time.time_ns())
                    case MsgType.HEARTBEAT:
                        message = Message(MsgType.HEARTBEAT, message, queue_entry_time=time.time_ns())
                        await websocket.send(self.heartbeat_response(message))
                        continue
                    case MsgType.SERVER_COMMNAD:
                        self.handle_server_command(message)
                        continue
                    case MsgType.CLIENT_COMMAND:
                        message = Message(MsgType.CLIENT_COMMAND, message, queue_entry_time=time.time_ns())
                    case MsgType.INGORE:
                        # do not forward
                        continue
                    case _:
                        logging.error(
                            f"Invalid message type: {msg_type}"
                        )  # should never happen
                        continue
                await self.out_queue.put(message)
        except websockets.exceptions.ConnectionClosed:
            logging.info(f"Client disconnected: {websocket.remote_address}")
        except Exception as e:
            logging.error(f"Error with client {websocket.remote_address}: {e}")
        finally:
            logging.info(f"Client {websocket.remote_address} disconnected")
            self.ready_clients.discard(websocket)

    async def publish(self):
        while True:
            msg: Message = await self.in_queue.get()

            # Record when we start processing (after dequeuing)
            msg.process_time = time.time_ns()

            # For HFT: parallel broadcast to minimize forward latency
            if not self.ready_clients:
                msg.discard_time = time.time_ns()
                continue

            # Create broadcast tasks for all clients simultaneously
            tasks = []
            clients_snapshot = self.ready_clients.copy()
            msg.client_count = len(clients_snapshot)
            msg.broadcast_start_time = time.time_ns()

            for ws in clients_snapshot:
                task = asyncio.create_task(self._send_to_client_safe(ws, msg.message))
                tasks.append(task)

            # Wait for all broadcasts to complete
            results = await asyncio.gather(*tasks, return_exceptions=True)
            msg.broadcast_complete_time = time.time_ns()

            # Clean up disconnected clients
            for ws, result in zip(clients_snapshot, results):
                if isinstance(result, Exception):
                    if isinstance(result, websockets.exceptions.ConnectionClosed):
                        logging.info(f"Client disconnected: {ws.remote_address}")
                    else:
                        logging.error(f"Error sending to client {ws.remote_address}: {result}")
                    self.ready_clients.discard(ws)

            msg.discard_time = time.time_ns()

            # Record latency for HFT monitoring
            record_message_latency(msg)

    async def _send_to_client_safe(self, ws, message):
        """Send message to a single client with exception handling"""
        await ws.send(message)

    def get_latency_stats(self) -> dict:
        """Get current latency statistics for monitoring"""
        stats = self.latency_monitor.get_latency_report()
        return {
            "server_type": "websocket_server",
            "active_clients": len(self.ready_clients),
            "latency_stats": stats
        }

    async def ping(self):
        if self.config.get("ping_interval") is None:
            logging.warning("Ping interval not set, skipping ping")
            return
        ping_interval = self.config["ping_interval"]
        while True:
            await asyncio.sleep(ping_interval)
            await self.in_queue.put(Message(MsgType.DATA, "pong"))

    async def run(self):
        parsed_url = urllib.parse.urlparse(self.config["server"])
        host = parsed_url.hostname
        port = parsed_url.port
        if port is None:
            port = 80 if parsed_url.scheme == "ws" else 443

        asyncio.create_task(self.publish())
        asyncio.create_task(self.ping())
        try:
            async with websockets.serve(
                self.accept,
                host,
                port,
                ping_interval=20,
                ping_timeout=10,
            ):
                logging.info("WS server running on ws://%s", self.config["server"])
                await asyncio.Future()
        except KeyboardInterrupt:
            logging.info("Server stopped by user")
        except Exception as e:
            logging.error(f"Server error: {e}")
        finally:
            await self.cleanup()

    async def cleanup(self):
        logging.info(f"Closing {len(self.ready_clients)} client connections...")
        for client in self.ready_clients:
            try:
                await client.close()
            except:
                pass
        self.ready_clients.clear()


class DeribitWsServer(WsServer):
    def __init__(self, config: dict, out_queue: asyncio.Queue, in_queue: asyncio.Queue):
        super().__init__(config, out_queue, in_queue)

    def try_copy_id(self, msg, msg_to_copy):
        try:
            json_msg = json.loads(msg_to_copy)
            msg["id"] = json_msg["id"]
        except:
            pass

    def login_response(self, login_msg) -> str:
        msg = {
            "jsonrpc": "2.0",
            "id": "auth12345",
            "result": {
                "enabled_features": [],
                "access_token": "access_token",
                "expires_in": 900,
                "refresh_token": "refresh_token",
                "scope": "some_scrope",
                "sid": "83535.PEATOebJ.cpp_og",
                "token_type": "bearer",
            },
            "usIn": time.time_ns() // 1000,
            "usOut": time.time_ns() // 1000,
            "usDiff": 12,
            "testnet": False,
        }
        self.try_copy_id(msg, login_msg)
        return json.dumps(msg)

    def heartbeat_response(self, heartbeat_msg) -> str:
        msg = {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"version": "1.2.26"},
            "usIn": 1765877414852182,
            "usOut": 1765877414852233,
            "usDiff": 51,
            "testnet": False,
        }
        self.try_copy_id(msg, heartbeat_msg)
        return json.dumps(msg)

    def get_message_type(self, msg) -> MsgType:
        if len(msg) == 4:
            method = msg.lower()
            if method == "ping":
                return MsgType.HEARTBEAT
            elif method == "auth":
                return MsgType.LOGIN
            else:
                pass

        try:
            method = json.loads(msg).get("method")
        except:
            return MsgType.INGORE

        if method[0] == "/":
            method = method[1:]

        match method:
            case "public/auth":
                return MsgType.LOGIN
            case (
                "public/subscribe"
                | "public/unsubscribe"
                | "private/subscribe"
                | "private/unsubscribe"
            ):
                return MsgType.SUBSCRIBE
            case "public/set_heartbeat" | "public/test":
                return MsgType.HEARTBEAT
            case _:
                return MsgType.DATA


class OkexWsServer(WsServer):
    def __init__(self, config: dict, out_queue: asyncio.Queue, in_queue: asyncio.Queue):
        super().__init__(config, out_queue, in_queue)

    def login_response(self, login_msg) -> str:
        login_msg = json.loads(login_msg)
        login_succ = {"event": "login", "msg": "", "code": "0", "connId": "a81267c7"}
        return json.dumps(login_succ)

    def heartbeat_response(self, heartbeat_msg) -> str:
        return "pong"

    def get_message_type(self, msg) -> MsgType:
        if len(msg) == 4 and msg == "ping":
            return MsgType.HEARTBEAT

        try:
            op = json.loads(msg).get("op")
        except:
            return MsgType.INGORE

        match op:
            case "login":
                return MsgType.LOGIN
            case "subscribe" | "unsubscribe":
                return MsgType.SUBSCRIBE
            case _:
                return MsgType.DATA


def create_ws_server(
    config: dict, out_queue: asyncio.Queue, in_queue: asyncio.Queue
) -> WsServer:
    if config["server_type"] == "deribit":
        return DeribitWsServer(config, out_queue, in_queue)
    elif config["server_type"] == "okex":
        return OkexWsServer(config, out_queue, in_queue)
    else:
        raise ValueError("Invalid server type: {}".format(config["server_type"]))


async def async_main(config):
    logging.info(f"Starting WS server with config:\n{json.dumps(config, indent=2)}")
    out_queue = asyncio.Queue()
    in_queue = asyncio.Queue()
    server = create_ws_server(config, out_queue, in_queue)
    await server.run()


def main(_):
    FLAGS = flags.FLAGS
    config = parse_config(FLAGS.config)

    try:
        asyncio.run(async_main(config))
    except KeyboardInterrupt:
        print("Quitting...")


if __name__ == "__main__":
    flags.DEFINE_string("config", "config/ws_server_config.json", "config file")
    app.run(main)
