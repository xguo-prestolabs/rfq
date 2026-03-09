---
name: redis-rfq
description: Query all RFQ-related values from Redis using redis_cli.py
allowed-tools: Bash
---

Query RFQ messages from all 4 Redis channels using the project's redis_cli.py tool.

## Redis Key Patterns

The ws_client_redis.py writer produces 4 key families:

| Pattern | Source channel | Content |
|---------|---------------|---------|
| `rfq:<block_rfq_id>` | block_rfq.maker.{new,cancel,edit} | Maker-side RFQ state (open/expired) |
| `rfq_quote:<block_rfq_quote_id>` | block_rfq.maker.quotes.* | Quote snapshot (our submitted quotes) |
| `rfq_taker:<block_rfq_id>` | block_rfq.taker.* | Taker-side RFQ state |
| `rfq_trade:<block_rfq_id>` | block_rfq.trades.* | Trade execution data |

## Steps

1. Count keys in each channel:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq
for p in 'rfq:*' 'rfq_quote:*' 'rfq_taker:*' 'rfq_trade:*'; do
  echo -n "$p  "; uv run python redis_cli.py KEYS "$p" 2>/dev/null | wc -l
done
```

2. For each channel, list keys sorted by ID (descending) and GET the latest 5 values:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq
for p in 'rfq' 'rfq_quote' 'rfq_taker' 'rfq_trade'; do
  echo "=== $p ===";
  keys=$(uv run python redis_cli.py KEYS "${p}:*" 2>/dev/null | tr -d '"' | sed 's/^[0-9]*) //' | sort -t: -k2 -n -r | head -5);
  for k in $keys; do
    echo "--- $k ---"; uv run python redis_cli.py GET "$k" 2>/dev/null;
  done;
  echo;
done
```

3. If `$ARGUMENTS` is provided, use it as an additional filter or key pattern instead of the defaults:

```bash
cd /remote/iosg/home-2/xguo/workspace/rfq && uv run python redis_cli.py KEYS '$ARGUMENTS'
```

4. Display results in a readable table, summarizing the latest messages from each channel.
