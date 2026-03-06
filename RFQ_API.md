# RFQ Market Maker API

FastAPI service for quoting on Deribit Block RFQs, backed by Redis for real-time pricing data from the C++ options_mm pricer.

## Architecture

```
C++ options_mm pricer ──(Redis SET)──► Redis ◄──(Redis SET)── ws_client_redis.py (Deribit WS)
                                        │                            │
                                        │  price:*, greeks:*,        │  rfq:*, rfq_trade:*,
                                        │  total_greeks:ALL          │  rfq_quote:*
                                        │                            │
                                        ▼                            │
                                    app.py (FastAPI) ◄───────────────┘
                                     │         │                (rfq_updates pub/sub)
                                     │         │
                              Deribit REST API  WebSocket (/ws)
                              (quote mgmt)      (live push to browser)
                                     │                │
                                     ▼                ▼
                                block_rfq_ui.html (browser dashboard)
```

**Data flow:**
- **Pricing:** C++ options_mm writes fair prices and greeks directly to Redis (`price:{instrument}`, `greeks:{instrument}`, `total_greeks:ALL`).
- **RFQ state:** `ws_client_redis.py` subscribes to Deribit `block_rfq.maker.*` WebSocket channels and writes RFQ state to Redis (`rfq:{id}`). It also publishes to the `rfq_updates` Redis pub/sub channel.
- **Quote actions:** `app.py` proxies quote submit/edit/cancel to Deribit's REST API with automatic authentication.
- **Browser push:** `app.py` subscribes to `rfq_updates` pub/sub and fans out messages to all connected browser WebSocket clients.

## Configuration

Deribit credentials are loaded at startup (env vars take priority over config file):

| Env var | Description |
|---------|-------------|
| `DERIBIT_CLIENT_ID` | Deribit API client ID |
| `DERIBIT_CLIENT_SECRET` | Deribit API client secret |
| `DERIBIT_TESTNET` | Set to `"1"` for testnet |
| `DERIBIT_CONFIG` | Path to JSON config file with `client_id`, `client_secret`, `testnet` fields |

## Running

```bash
# Docker
docker compose up --build

# Local
uv run uvicorn app:app --host 0.0.0.0 --port 8000

# With config
uv run python app.py --config config.json --testnet --port 8000
```

## Redis Key Layout

| Key pattern | Writer | Description |
|-------------|--------|-------------|
| `price:{instrument}` | C++ options_mm | Fair price with `strat_mid_price` and `ts` fields |
| `greeks:{instrument}` | C++ options_mm | Per-instrument greeks (`delta`, `gamma`, `vega`) |
| `total_greeks:ALL` | C++ options_mm | Aggregated portfolio greeks |
| `rfq:{block_rfq_id}` | ws_client_redis.py | Latest RFQ state from Deribit |
| `rfq_trade:{block_rfq_id}` | ws_client_redis.py | Trade execution data |
| `rfq_quote:{quote_id}` | ws_client_redis.py | Quote state snapshot |

## API Endpoints

### Pricing & Greeks (read from Redis)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/price/{product_name}` | Fair price for a single instrument |
| `GET` | `/prices` | All fair prices (bulk) |
| `GET` | `/greeks/{product_name}` | Greeks for a single instrument |
| `GET` | `/greeks_list` | All greeks (bulk) |
| `GET` | `/total_greeks` | Aggregated portfolio dollar greeks |

### RFQ Data (read from Redis)

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/rfqs` | All RFQs (keyed by `block_rfq_id`) |
| `GET` | `/rfqs/{rfq_id}` | Single RFQ status (falls back to Deribit API) |
| `GET` | `/trades` | Recent RFQ trades (newest first, max 20) |

### Quote Management (proxied to Deribit REST API)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/quotes/add` | Submit a new quote on an RFQ |
| `POST` | `/quotes/edit` | Edit an existing quote |
| `POST` | `/quotes/cancel` | Cancel a specific quote |
| `POST` | `/quotes/cancel_all` | Cancel all quotes (optionally by currency) |
| `GET` | `/quotes` | List all quotes submitted by this maker |
| `GET` | `/quotes/{quote_id}` | Get a specific quote's details |

### Infrastructure

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/` | Serves the browser UI (`block_rfq_ui.html`) |
| `GET` | `/health` | Redis + pub/sub status |
| `GET` | `/stats` | Per-endpoint latency dashboard (auto-refreshes) |
| `WS` | `/ws` | WebSocket for live RFQ state push updates |

## Deribit Block RFQ Flow

1. **Taker** creates an RFQ via `private/create_block_rfq` (instruments, amounts, sides).
2. **Makers** receive notifications via `block_rfq.maker.{currency}` WebSocket subscription.
3. **5-second grace period** after creation — taker cannot see quotes or trade during this window.
4. **Makers** submit quotes via `private/add_block_rfq_quote` (per-leg prices, amount, direction).
   - Calling add multiple times creates additional active quotes (previous quotes are NOT implicitly cancelled).
   - Use `edit_block_rfq_quote` to modify, `cancel_block_rfq_quote` to remove.
5. **Taker** reviews quotes via `private/get_block_rfq_quotes`.
6. **Taker** accepts via `private/accept_block_rfq` (fill_or_kill or good_til_cancelled).
7. **RFQ states:** `open` -> `filled`, `cancelled`, or `expired`.

Makers can subscribe to `block_rfq.maker.quotes.{currency}` for real-time quote state updates.

## Option Price Tick Size

Deribit requires option prices to be exact multiples of the tick size (`0.0001` for both BTC and ETH vanilla options). The UI rounds prices to tick before submission. Submitting unrounded prices results in Deribit error code `10043` (wrong_tick).
