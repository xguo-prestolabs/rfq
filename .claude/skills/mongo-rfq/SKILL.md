---
name: mongo-rfq
description: Query RFQ messages from MongoDB using query_mongo.py
allowed-tools: Bash
---

Query RFQ messages from all 4 channels stored in MongoDB's `messages` collection using query_mongo.py.

## MongoDB Channels

All messages are stored in a single collection. The channel is at `message.params.channel`:

| Channel pattern | Alias | Content |
|----------------|-------|---------|
| `block_rfq.maker.*` (excl. quotes) | `maker` | Maker-side RFQ state (new/open/filled/expired/cancelled) |
| `block_rfq.maker.quotes.*` | `quotes` | Our submitted quote snapshots |
| `block_rfq.taker.*` | `taker` | Taker-side RFQ state |
| `block_rfq.trades.*` | `trades` | Trade execution data |

## Steps

1. Show document counts per channel:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py --count
```

2. For each channel, get the latest messages:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py -c maker -n 3
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py -c quotes -n 3
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py -c taker -n 3
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py -c trades -n 3
```

3. For testnet data, add `--testnet`:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py --count --testnet
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py -c maker -n 3 --testnet
```

4. If `$ARGUMENTS` is provided, pass it as extra flags to query_mongo.py (e.g. `--testnet`, `-c trades -n 10`):

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python query_mongo.py $ARGUMENTS
```

5. Display results in a readable format, summarizing the RFQ data found across all channels.

## CLI Reference

```
uv run python query_mongo.py [--testnet] [--count] [-c CHANNEL] [-n LIMIT] [config_path]
```

- `--count`: show per-channel document counts
- `-c` / `--channel`: filter by alias (maker/taker/trades/quotes) or raw channel substring
- `-n` / `--limit`: max documents (default 20)
- `--testnet`: use testnet config/collection
