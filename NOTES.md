check websocket portal service if anything seems wrong.

## Real-time UI updates (WebSocket + Redis pub/sub)

The HTML UI (`block_rfq_ui.html`) receives live RFQ state changes via a WebSocket
connection instead of relying solely on HTTP polling.

### Data flow

```
Deribit WS → ws_client_redis.py → Redis SET + PUBLISH rfq_updates
                                        ↓
                                   app.py _pubsub_broadcaster
                                   (dedicated Redis SUBSCRIBE connection)
                                        ↓
                                   @app.websocket("/ws")  fan-out to browsers
                                        ↓
                                   block_rfq_ui.html  handleLiveUpdate()
```

### Components

| Component | Role |
|-----------|------|
| `ws_client_redis.py` `_write_redis()` | After each `SET rfq:<id>`, calls `PUBLISH rfq_updates <json>`. The JSON payload includes `type`, `id`, and the full Deribit `data` dict so the browser can update without an extra REST round-trip. |
| `app.py` `_pubsub_broadcaster()` | Background `asyncio.Task` started in lifespan. Uses a **separate** Redis connection for `SUBSCRIBE` (Redis pub/sub requires a dedicated connection — the same connection cannot also do GET/SET while subscribed). Fan-outs each message to all entries in `_ws_clients`. Dead clients are silently removed. |
| `app.py` `@app.websocket("/ws")` | Accepts browser connections, adds the `WebSocket` object to the module-level `_ws_clients` set, and loops on `receive_text()` to keep the connection alive until the client disconnects. |
| `block_rfq_ui.html` `connectLiveUpdates()` | Opens `ws://<host>/ws` on page load. Reconnects with exponential backoff (1 s base, 30 s cap) on close or error. Updates the `⬤` status indicator (green / yellow / red). |
| `block_rfq_ui.html` `handleLiveUpdate()` | Parses the incoming JSON. For **existing** cards: swaps the state badge `outerHTML` in-place, leaving price inputs untouched. For **new** RFQs not yet in the cache: inserts a placeholder card at the top, enriches it (price/greeks REST calls), then replaces the placeholder with the full card. |

### Why a dedicated Redis connection for pub/sub

Redis enters a special "subscriber" mode when `SUBSCRIBE` is called on a connection.
In that mode the connection can only send `SUBSCRIBE`/`UNSUBSCRIBE`/`PING` — not
`GET`/`SET`. `app.py` therefore creates `pubsub_conn` separately from
`app.state.redis_client` (which handles all normal key reads).

### Progressive rendering and Promise batching

On initial page load `loadRFQs()` uses `Promise.all` to parallelise price/greeks
queries across legs and across cards:

1. Placeholder skeleton cards are rendered immediately (page is never blank).
2. Open RFQs — all enriched in parallel (`Promise.all(openIds.map(renderOne))`).
3. Expired/cancelled/filled RFQs — processed in **batches of 5** to bound the
   number of concurrent HTTP requests while still being faster than fully sequential.
   Each batch is a `Promise.all` of 5; batches are awaited sequentially in a `for`
   loop. Cards appear batch-by-batch as their queries complete.

## Dollar Greeks in the UI

### Per-leg table (RFQ card)

Raw per-unit greeks from `/greeks/{product}` (Black-Scholes output) are converted to dollar
greeks in the browser before display. The index price comes from `rfq.index_prices` (e.g.
`{"btc_usd": 72525.92}`).

| Greek | Conversion |
|-------|-----------|
| Dollar Delta | `delta × index_price` |
| Dollar Gamma | `gamma × index_price² × 0.01` |
| Dollar Vega  | `vega` (already in dollar terms — `blackVega` output for a BTC option with BTC notional is in USD/1% vol) |

Implemented in `toDollarGreeks(leg, idx)` called inside `combo(rfq)`, which mutates
each leg in-place before `rfqCard` renders the rows.

### Position Dollar Greeks (sidebar)

Values come from `GET /total_greeks` → Redis key `total_greeks:ALL`, written by the C++
binary. The C++ code already computes dollar-denominated totals:

- `delta_position_total` = Σ(delta_i × pos_i × multiplier_i) × perp_price
- `gamma_position_total` = Σ(opt_gamma_i × F_i² × 0.01 × pos_i × multiplier_i)
- `vega_postition_total` = Σ(vega_i × pos_i × multiplier_i)  ← note: typo in C++ key name

### Testnet indicator

`GET /health` now returns `"testnet": bool`. `pollHealth()` sets the page `<h1>` to
`"Deribit Block RFQ [TEST]"` when testnet is true, `"Deribit Block RFQ"` otherwise.

## TODO

- [ ] Implement Redis writer in C++ so the C++ binary writes RFQ data directly to Redis,
      removing the Python WebSocket client from the critical path for latency-sensitive writes.
