#!/usr/bin/env python3
"""Query recent MongoDB documents from ws_client config. Debug/dev only."""

import argparse
import json
import sys
from pathlib import Path

from pymongo import MongoClient


def main() -> None:
    parser = argparse.ArgumentParser(description="Query recent MongoDB documents.")
    parser.add_argument("--testnet", action="store_true", help="Use testnet config")
    parser.add_argument("config", nargs="?", help="Path to config JSON")
    parser.add_argument("limit", nargs="?", type=int, default=20, help="Max documents")
    args = parser.parse_args()

    if args.config:
        config_path = Path(args.config)
    elif args.testnet:
        config_path = Path("config/ws_client_config_testnet_mongodb.json")
    else:
        config_path = Path("config/ws_client_config_mongodb.json")
    limit = args.limit

    with open(config_path) as f:
        config = json.load(f)
    mongo = config.get("mongodb", {})
    if not mongo.get("enabled", False):
        print("mongodb.enabled is false in config", file=sys.stderr)
        sys.exit(1)

    client = MongoClient(mongo["uri"])
    coll = client[mongo["database"]][mongo["collection"]]
    cursor = coll.find().sort("fetch_time", -1).limit(limit)
    for doc in cursor:
        doc["_id"] = str(doc["_id"])
        print(json.dumps(doc, default=str, indent=2))
        print("---")


if __name__ == "__main__":
    main()
