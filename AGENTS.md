# AGENTS.md

Guidance for human and AI agents working in this repository.

## 1) Project snapshot

This repository is a Python service/tooling codebase centered on:

- WebSocket client/server components (`ws_client.py`, `ws_server.py`, `ws_portal.py`)
- A FastAPI application (`app.py`) that exposes market/RFQ endpoints
- A Streamlit UI script (`block_rfq_ui.py`)
- JSON config files in `config/`

Dependency management is defined in `pyproject.toml` and `uv.lock` (use `uv`).

## 2) Environment and setup

From the repository root:

```bash
uv sync
```

If `uv` is unavailable in your environment, install it first and then run `uv sync`.

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

