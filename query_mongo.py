#!/usr/bin/env python3
"""Query recent MongoDB documents from ws_client config. Debug/dev only."""

import json
import sys
from pathlib import Path

from pymongo import MongoClient


def main() -> None:
    config_path = Path("config/ws_client_config_mongodb.json")
    if len(sys.argv) > 1:
        config_path = Path(sys.argv[1])
    limit = 20
    if len(sys.argv) > 2:
        limit = int(sys.argv[2])

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
