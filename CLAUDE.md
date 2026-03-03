# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Multi-component **RFQ (Request for Quote) pipeline** for Deribit block RFQ market-making. The system connects to Deribit's exchange, receives block RFQ notifications, computes fair prices/greeks via a C++ binary, and lets users submit/edit/cancel quotes via a Streamlit UI.

## Environment & Setup

Uses `uv` for dependency management with Python ≥ 3.6.3. The `.env` file optionally sets `UV_PROJECT_ENVIRONMENT=~/venv`; without it, `uv` defaults to `.venv`.

```bash
source .env   # optional: use ~/venv instead of .venv
uv sync
```

## Run Commands

```bash
# FastAPI backend (central API + Redis bridge)
uv run uvicorn app:app --host 0.0.0.0 --port 8000

# ZMQ→Redis bridge (needed if running separately from app.py)
uv run python zmq_to_redis.py

# Deribit RFQ WebSocket client → MongoDB
uv run python ws_client.py --config config/ws_client_config_mongodb.json

# Streamlit UI (connects to app at 127.0.0.1:8000)
uv run streamlit run block_rfq_ui.py

# API smoke tests (app must be running; uses config.json for testnet credentials)
uv run uvicorn app:app --host 0.0.0.0 --port 8000  # Terminal 1
uv run python test_deribit_api.py [--base-url http://localhost:8000] [--config config.json]  # Terminal 2
```

## Syntax Validation

```bash
uv run python -m py_compile app.py ws_base.py ws_client.py ws_server.py ws_portal.py block_rfq_ui.py test_deribit_api.py
```

There is no formal test suite; validate changed components with targeted smoke tests.

## Architecture

### Data Flow

```
C++ binary → ZMQ (tcp://localhost:40000)
    → zmq_to_redis.py → Redis (localhost:6379)
        → app.py reads Redis → REST API (port 8000)
            → block_rfq_ui.py polls API

Deribit WS (wss://www.deribit.com/ws/api/v2)
    → ws_client.py (DeribitWsClient) → MongoDB
        → app.py /rfqs reads MongoDB → REST API
```

### Active Components

| File | Role |
|------|------|
| `app.py` | FastAPI app. Reads Redis for prices/greeks, reads MongoDB for RFQs, proxies quote actions to Deribit REST API. Deribit credentials configured at runtime via `POST /configure`. |
| `block_rfq_ui.py` | Streamlit GUI polling `127.0.0.1:8000`. Shows RFQs with fair prices/greeks prefilled; submit/edit/cancel buttons per RFQ. |
| `ws_client.py` | `DeribitWsClient` subscribes to `block_rfq.maker.*` / `block_rfq.taker.*` / `block_rfq.trades.*` channels and writes to MongoDB. |
| `zmq_to_redis.py` | Bridges ZMQ PUB from C++ binary to Redis keys: `price:<product>`, `greeks:<product>`, `total_greeks:ALL`. |
| `ws_base.py` | Shared `Message`/`MsgType` dataclasses, `parse_config()` (expands `trade_key` path→JSON), and `Writer` (fallback file sink). |

### Inactive / Not Used

- `ws_server.py`, `ws_portal.py`: WebSocket server/portal — not in current pipeline.
- `OkexWsClient` in `ws_client.py`: alternative exchange client — not used.

### Key Patterns

- **`app.py` Deribit auth**: `deribit_config` is a module-level singleton. Credentials are set via `POST /configure`. `ensure_authenticated()` caches the bearer token with a 60s expiry buffer; `call_deribit_api()` wraps all Deribit REST calls using JSON-RPC 2.0 over HTTP POST.
- **`ws_client.py` request-response**: `DeribitWsClient._send_jsonrpc()` registers a `Future` in `pending_requests`, the `_message_loop` routes responses by `id`, and login/subscribe/check_auth all use this pattern before the subscription message loop begins.
- **Config `trade_key`**: Can be a file path string (expanded by `_exapnd_trade_key`) or an inline dict. Config is JSON; `ws_client_config_mongodb.json` is the active config for `ws_client.py`.
- **MongoDB connection**: Used in both `app.py` (sync `pymongo`) and `ws_client.py` (sync MongoClient in async context); connection URI in `config/ws_client_config_mongodb.json` and hardcoded in `app.py`.
- **Redis keys**: `price:<instrument_name>`, `greeks:<instrument_name>`, `total_greeks:ALL` — all stored as JSON strings by `zmq_to_redis.py`.

## Coding Guardrails

- Preserve async behavior — do not introduce blocking calls in async paths (particularly in `app.py`).
- Keep changes minimal and localized. Avoid unrelated refactors.
- Reuse existing message/data shapes unless a schema change is explicitly required.
- If adding dependencies, use `uv add <pkg>` to update `pyproject.toml` and `uv.lock`.
- External services (C++ binary, Redis, MongoDB) are assumed to be running; agents do not manage them.
