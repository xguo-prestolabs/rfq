#!/usr/bin/env python3
"""
Test script for Deribit API integration (auth + quote endpoints).

Uses config.json (client_id, client_secret) for test.deribit.com.
Requires the FastAPI app to be running, e.g.:
  uv run uvicorn app:app --host 0.0.0.0 --port 8000

Usage:
  uv run python test_deribit_api.py [--base-url http://localhost:8000]
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import urllib.request
import urllib.error
import urllib.parse


def load_config(config_path: Path) -> Dict[str, Any]:
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def request(
    method: str,
    url: str,
    *,
    data: Optional[Dict[str, Any]] = None,
    params: Optional[Dict[str, Any]] = None,
) -> Tuple[int, Any]:
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}" if "?" not in url else f"{url}&{qs}"
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}
    except urllib.error.URLError as e:
        print(f"  URL error: {e.reason}")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Deribit API endpoints")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8000",
        help="App base URL (default: http://localhost:8000)",
    )
    parser.add_argument(
        "--config",
        default="config.json",
        type=Path,
        help="Path to config.json (default: config.json)",
    )
    args = parser.parse_args()
    base = args.base_url.rstrip("/")

    if not args.config.exists():
        print(f"Config not found: {args.config}")
        return 1
    config = load_config(args.config)
    client_id = config.get("client_id")
    client_secret = config.get("client_secret")
    if not client_id or not client_secret:
        print("config.json must contain client_id and client_secret")
        return 1

    failed = 0

    # --- Configure ---
    print("1. POST /configure (testnet=True)")
    status, body = request(
        "POST",
        f"{base}/configure",
        params={
            "client_id": client_id,
            "client_secret": client_secret,
            "testnet": "true",
        },
    )
    if status != 200:
        print(f"   FAIL status={status} body={body}")
        failed += 1
    else:
        print(f"   OK {body}")

    # --- ensure_authenticated + call_deribit_api (via GET /quotes) ---
    print("2. GET /quotes (exercises ensure_authenticated, call_deribit_api)")
    status, body = request("GET", f"{base}/quotes")
    if status != 200:
        print(f"   FAIL status={status} body={body}")
        failed += 1
    else:
        count = body.get("count", 0)
        print(f"   OK count={count} quotes={body.get('quotes', [])[:2]}...")

    # --- quotes/add (may fail if no active block RFQ) ---
    print("3. POST /quotes/add")
    status, body = request(
        "POST",
        f"{base}/quotes/add",
        data={
            "block_rfq_id": "test-rfq-from-script",
            "price": 50000.0,
            "amount": 1.0,
        },
    )
    if status not in (200, 400, 404):
        print(f"   FAIL status={status} body={body}")
        failed += 1
    else:
        print(f"   OK (status={status}) {body if status == 200 else body.get('detail', body)}")
    quote_id = body.get("quote_id") if status == 200 else None

    # --- GET /quotes again ---
    print("4. GET /quotes")
    status, body = request("GET", f"{base}/quotes")
    if status != 200:
        print(f"   FAIL status={status} body={body}")
        failed += 1
    else:
        print(f"   OK count={body.get('count', 0)}")

    # --- quotes/edit (only if we have a quote_id) ---
    if quote_id:
        print("5. POST /quotes/edit")
        status, body = request(
            "POST",
            f"{base}/quotes/edit",
            data={"quote_id": quote_id, "price": 51000.0, "amount": 2.0},
        )
        if status not in (200, 400, 404):
            print(f"   FAIL status={status} body={body}")
            failed += 1
        else:
            print(f"   OK (status={status})")
    else:
        print("5. POST /quotes/edit (skipped, no quote_id)")

    # --- quotes/cancel (only if we have a quote_id) ---
    if quote_id:
        print("6. POST /quotes/cancel")
        status, body = request(
            "POST",
            f"{base}/quotes/cancel",
            data={"quote_id": quote_id},
        )
        if status not in (200, 400, 404):
            print(f"   FAIL status={status} body={body}")
            failed += 1
        else:
            print(f"   OK (status={status})")
    else:
        print("6. POST /quotes/cancel (skipped, no quote_id)")

    # --- quotes/cancel_all ---
    print("7. POST /quotes/cancel_all")
    status, body = request("POST", f"{base}/quotes/cancel_all", data={})
    if status != 200:
        print(f"   FAIL status={status} body={body}")
        failed += 1
    else:
        print(f"   OK {body}")

    # --- GET /quotes/{quote_id} (expect 404 for unknown id) ---
    print("8. GET /quotes/unknown-quote-id (expect 404)")
    status, body = request("GET", f"{base}/quotes/unknown-quote-id")
    if status != 404:
        print(f"   FAIL expected 404, got status={status} body={body}")
        failed += 1
    else:
        print(f"   OK 404 as expected")

    print()
    if failed:
        print(f"Total: {failed} step(s) failed")
        return 1
    print("All steps completed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
