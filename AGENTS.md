# AGENTS.md

Guidance for human and AI agents working in this repository.

## 1) Project snapshot


### System overview

The system is a **multi-component RFQ (Request for Quote) pipeline**. Only part of this repo is in active use; the rest is legacy or unused.

**In-repo components (what matters for agents):**

| Component | Role | Status |
|-----------|------|--------|
| `app.py` | Subscribes to ZMQ from the C++ binary, stores JSON in **Redis**, exposes a **REST API** for the UI. | **Active** — main backend to improve. |
| `block_rfq_ui.py` | Streamlit GUI; polls app’s API for RFQ requests, greeks, positions, fair prices; displays blocks per RFQ; user can submit/cancel/edit quotes. | **Active** — main UI to improve. |
| `ws_client.py` | Subscribes to **Deribit RFQ channels**, persists messages to **MongoDB**. Only **DeribitWsClient** (and the subscribe → MongoDB path) is used. | **Active** for RFQ ingestion. |
| `ws_server.py`, `ws_portal.py` | WebSocket server/portal. | **Not used** currently. |
| OkexWsClient (in ws_client or related code) | Alternative exchange client. | **Not used**; only Deribit is used. |

**External components (outside this repo):**

- **C++ binary**: Connects to Deribit (futures/options feed, auth, order/trade/fill/account). Computes greeks (delta, gamma, vega), fair prices for options; **publishes JSON over ZMQ**.
- **Redis**: Used by `app.py` for storing data from the C++ binary and for API-served state (e.g. recent RFQ requests, greeks, positions, fair prices).
- **MongoDB**: Used by `ws_client.py` to store RFQ-related messages from Deribit.

**Data flow (high level):**

1. **C++ binary** → ZMQ (JSON) → **app.py** → Redis; **app.py** → REST API ← **block_rfq_ui.py** (polling).
2. **Deribit** (RFQ channels) → **ws_client.py** (DeribitWsClient) → MongoDB. RFQ data in MongoDB may be consumed or mirrored elsewhere; app/UI get RFQ info via the API that app.py serves.
3. User uses **block_rfq_ui.py** to see RFQs and pre-filled inputs (from RFQ + fair price/greeks from C++), then **submit**, **cancel**, or **edit** quotes. A **cancel-all** button is not implemented yet.

**Run-order / dependencies:**

To run the full pipeline you need (agents assume these are started manually when relevant):

1. C++ binary (feeds + orders, greeks, fair prices, ZMQ publisher).
2. MongoDB and Redis (storage).
3. `app.py` (central API and ZMQ→Redis bridge).
4. `block_rfq_ui.py` (GUI; refreshes every few seconds).
5. `ws_client.py` (Deribit RFQ → MongoDB).

Agents should focus improvements on **app.py** and **block_rfq_ui.py**; the C++ binary, MongoDB, Redis, and ws_client are launched manually.


This repository is a Python service/tooling codebase centered on:

- WebSocket client/server components (`ws_client.py`, `ws_server.py`, `ws_portal.py`)
- A FastAPI application (`app.py`) that exposes market/RFQ endpoints
- A Streamlit UI script (`block_rfq_ui.py`)
- JSON config files in `config/`

Dependency management is defined in `pyproject.toml` and `uv.lock` (use `uv`).

## 2) Environment and setup

This repo uses **`~/venv`** as the project virtual environment. From the repository root:

```bash
# Optional: load UV_PROJECT_ENVIRONMENT so uv uses ~/venv (otherwise uv uses .venv)
source .env
uv sync
```

If you prefer to use the default `.venv`, omit `source .env`. If `uv` is unavailable, install it first and then run `uv sync`.

## 3) Common run commands

Examples (adjust config paths as needed):

```bash
# FastAPI app
uv run uvicorn app:app --host 0.0.0.0 --port 8000

# WebSocket client (default config in code points to mongodb variant)
uv run python ws_client.py --config config/ws_client_config_mongodb.json

# WebSocket server / portal need explicit valid config files
uv run python ws_server.py --config <path-to-server-config.json>
uv run python ws_portal.py --config <path-to-portal-config.json>
```

## 4) Validation expectations

There is no formal test suite checked in today. Before submitting changes:

1. Run syntax checks:

```bash
uv run python -m py_compile app.py ws_base.py ws_client.py ws_server.py ws_portal.py block_rfq_ui.py
```

2. Run a focused smoke test for the component you changed (API endpoint, websocket flow, or UI path).
3. Prefer small, scoped changes and avoid unrelated refactors.

## 5) Coding guardrails

- Keep changes minimal and localized to the requested behavior.
- Preserve async behavior; do not introduce blocking operations in async paths.
- Do not hardcode secrets or credentials; prefer config/env-driven values.
- Reuse existing message/data shapes unless a schema update is explicitly required.
- If adding dependencies, use the package manager and update lockfiles accordingly.

## 6) Git workflow for agents

- Work on the current feature branch only.
- Commit in small, logical units with descriptive messages.
- Ensure code compiles/runs for touched paths before pushing.
- Do not rewrite history unless explicitly requested.

