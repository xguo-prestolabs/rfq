# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import enum
import json
import pathlib
from typing import Optional
from dataclasses import dataclass


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
