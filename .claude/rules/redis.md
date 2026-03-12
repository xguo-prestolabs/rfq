---
paths:
  - "**/*.py"
  - "config/**/*.json"
  - "docker-compose.yml"
---

# Redis Instances

Two Redis instances, both accessed via localhost (SSH tunnel to remote host):

| Name | Port | Contents | Writers |
|------|------|----------|---------|
| **redis** (mainnet) | 6379 | `price:*`, `greeks:*`, `total_greeks:ALL`, `rfq:*`, `rfq_quote:*`, `rfq_taker:*`, `rfq_trade:*`, `rfq_user:*` | C++ pricer (direct), `ws_client_redis` (mainnet) |
| **redis** (testnet) | 6380 | `price:*`, `greeks:*`, `total_greeks:ALL` + testnet RFQ keys | `zmq_to_redis` (subscribes to pricer ZMQ), `ws_client_redis_testnet` |

- C++ pricer writes directly to port 6379 and also publishes via ZMQ (tcp://localhost:40000)
- `zmq_to_redis.py` subscribes to ZMQ and writes the same pricer data to port 6380 for testnet use
- `app.py` connects to port 6379 in prod mode, port 6380 in testnet mode
- Mainnet and testnet RFQ messages are distinguished by `meta.testnet` flag

# Redis CLI

Always use `uv run python redis_cli.py` (not `redis-cli`):

```bash
uv run python redis_cli.py KEYS 'rfq:*'           # port 6379 (default)
uv run python redis_cli.py -p 6380 KEYS 'price:*'  # port 6380
uv run python redis_cli.py GET 'rfq:12345'
```

# Key Families

| Pattern | TTL | Source channel | Content |
|---------|-----|---------------|---------|
| `rfq:<block_rfq_id>` | none (cleanup task) | `block_rfq.maker.*` | Maker-side RFQ state |
| `rfq_quote:<block_rfq_quote_id>` | 4h | `block_rfq.maker.quotes.*` | Quote snapshots |
| `rfq_taker:<block_rfq_id>` | 4h | `block_rfq.taker.*` | Taker-side RFQ state |
| `rfq_trade:<block_rfq_id>` | 4h | `block_rfq.trades.*` | Trade execution data |
| `rfq_user:<action>:<ts>` | 48h | app.py endpoints | Our quote actions (submit/cancel/edit/cancel_all) |
| `rfq_user:<action>:err:<ts>` | 48h | app.py endpoints | Failed quote actions |

# Pub/Sub

- Channel `rfq_updates`: published by `ws_client_redis.py` on every RFQ/quote/trade/taker update
- `app.py` subscribes and broadcasts to WebSocket clients, filtering by `testnet` flag
