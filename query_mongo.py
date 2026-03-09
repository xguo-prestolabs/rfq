#!/usr/bin/env python3
"""Query recent MongoDB documents from ws_client config. Debug/dev only."""

import argparse
import json
import sys
from pathlib import Path

from pymongo import MongoClient


CHANNEL_ALIASES = {
    "maker":  "block_rfq.maker.",
    "taker":  "block_rfq.taker.",
    "trades": "block_rfq.trades.",
    "quotes": "block_rfq.maker.quotes.",
}

# Channel field lives in different places depending on writer version:
#   ws_client_redis.py (new): top-level "channel" field
#   ws_client.py (old):       "message.params.channel"
CHANNEL_FIELDS = ["channel", "message.params.channel"]


def _channel_query(pattern: str) -> dict:
    """Build a $or query matching the channel pattern in either field location."""
    conditions = []
    for field in CHANNEL_FIELDS:
        conditions.append({field: {"$regex": pattern}})
    return {"$or": conditions}


def _channel_query_maker(pattern: str, exclude: str) -> dict:
    """Maker channel: match pattern but exclude quotes sub-channel."""
    conditions = []
    for field in CHANNEL_FIELDS:
        conditions.append({
            "$and": [
                {field: {"$regex": pattern}},
                {field: {"$not": {"$regex": exclude}}},
            ]
        })
    return {"$or": conditions}


def main() -> None:
    parser = argparse.ArgumentParser(description="Query recent MongoDB documents.")
    parser.add_argument("--testnet", action="store_true", help="Use testnet config")
    parser.add_argument("--channel", "-c", help="Filter by channel substring or alias (maker/taker/trades/quotes)")
    parser.add_argument("--limit", "-n", type=int, default=20, help="Max documents (default 20)")
    parser.add_argument("--count", action="store_true", help="Print document counts per channel instead of documents")
    parser.add_argument("config", nargs="?", help="Path to config JSON")
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
    elif args.testnet:
        config_path = Path("config/ws_client_config_testnet_mongodb.json")
    else:
        config_path = Path("config/ws_client_config_mongodb.json")

    with open(config_path) as f:
        config = json.load(f)
    mongo = config.get("mongodb", {})
    if not mongo.get("enabled", False):
        print("mongodb.enabled is false in config", file=sys.stderr)
        sys.exit(1)

    client = MongoClient(mongo["uri"])
    coll = client[mongo["database"]][mongo["collection"]]

    if args.count:
        # Unify both channel field locations into a single "ch" field for grouping
        pipeline = [
            {"$addFields": {
                "_ch": {
                    "$ifNull": ["$channel", {"$ifNull": ["$message.params.channel", None]}]
                }
            }},
            {"$group": {"_id": "$_ch", "count": {"$sum": 1}}},
            {"$sort": {"count": -1}},
        ]
        for row in coll.aggregate(pipeline):
            ch = row["_id"] or "(no channel)"
            print(f"{ch:45s} {row['count']:>8d}")
        return

    query = {}
    if args.channel:
        ch = CHANNEL_ALIASES.get(args.channel, args.channel)
        if ch == "block_rfq.maker.":
            query = _channel_query_maker(
                r"^block_rfq\.maker\.",
                r"^block_rfq\.maker\.quotes\.",
            )
        else:
            query = _channel_query(ch)

    cursor = coll.find(query).sort("fetch_time", -1).limit(args.limit)
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        print(json.dumps(doc, default=str, indent=2))
        print("---")


if __name__ == "__main__":
    main()
