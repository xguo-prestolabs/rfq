# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import enum
import json
import time
import pathlib
from typing import Optional
from dataclasses import dataclass, asdict


class MsgType(enum.IntEnum):
    LOGIN = 1
    SUBSCRIBE = 2
    DATA = 3
    HEARTBEAT = 4
    INGORE = 5
    SERVER_COMMNAD = 6
    CLIENT_COMMAND = 7


@dataclass
class Message:
    msg_type: MsgType
    message: str
    fetch_time: Optional[int] = None
    process_time: Optional[int] = None
    discard_time: Optional[int] = None
    # HFT latency tracking
    queue_entry_time: Optional[int] = None  # When message enters input queue
    broadcast_start_time: Optional[int] = None  # When broadcast begins
    broadcast_complete_time: Optional[int] = None  # When all clients receive message
    client_count: Optional[int] = None  # Number of clients message was sent to
    
    def to_dict(self) -> dict:
        d = asdict(self)
        try:
            message = json.loads(self.message)
            d["message"] = message
        except Exception as e:
            pass
        return d
    
    def to_json(self) -> str:
        return json.dumps(self.to_dict())



def _exapnd_trade_key(config: dict) -> dict:
    new_config = config.copy()
    for k, v in config.items():
        if k == "trade_key" and isinstance(v, str):
            content = pathlib.Path(v).read_text()
            new_config[k] = json.loads(content)
        elif isinstance(v, dict):
            new_config[k] = _exapnd_trade_key(v)
        else:
            pass
    return new_config


def parse_config(config_path: pathlib.Path) -> dict:
    content = pathlib.Path(config_path).read_text()
    config = json.loads(content)
    return _exapnd_trade_key(config)


class Writer:
    def __init__(self):
        self._path = f"messages_{time.time_ns()}.txt"
        self._fp = open(self._path, "a", encoding="utf-8")

    def write(self, message: Message):
        self._fp.write(message.to_json())
        self._fp.write("\n")
        self._fp.flush()

    def close(self):
        self._fp.close()