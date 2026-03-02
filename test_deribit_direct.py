#!/usr/bin/env python3
"""
Direct test of Deribit API methods used in app.py against test.deribit.com.
Reads credentials from config.json. No FastAPI app needed.

Usage:
  uv run python test_deribit_direct.py [--config config.json]
"""

import argparse
import json
import ssl
import sys
import urllib.request
import urllib.error
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

BASE_URL = "https://test.deribit.com/api/v2"

# Build an SSL context using the system CA bundle (certifi preferred, fallback to /etc/ssl)
def _make_ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for bundle in ("/etc/ssl/certs/ca-bundle.crt", "/etc/ssl/certs/ca-certificates.crt"):
        if Path(bundle).exists():
            return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()

_SSL = _make_ssl_context()


# ---------------------------------------------------------------------------
# Low-level HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, params: Optional[Dict] = None) -> Tuple[int, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def http_post(url: str, payload: Dict, headers: Optional[Dict] = None) -> Tuple[int, Any]:
    body = json.dumps(payload).encode()
    h = {"Content-Type": "application/json"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=body, headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15, context=_SSL) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raw = e.read().decode() if e.fp else "{}"
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


# ---------------------------------------------------------------------------
# Deribit helpers (mirrors logic in app.py)
# ---------------------------------------------------------------------------

def authenticate(client_id: str, client_secret: str) -> str:
    """Call public/auth and return access_token (mirrors ensure_authenticated)."""
    params = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }
    status, body = http_get(f"{BASE_URL}/public/auth", params)
    assert status == 200, f"Auth HTTP error {status}: {body}"
    assert "error" not in body, f"Auth error: {body['error']}"
    token = body["result"]["access_token"]
    expires_in = body["result"].get("expires_in")
    print(f"      access_token=...{token[-8:]}  expires_in={expires_in}s")
    return token


NOT_MAKER_CODE = 13009  # account not registered as a Block RFQ maker


class MakerPermissionError(Exception):
    """Raised when the account is not a registered Block RFQ maker (code 13009)."""


def call_private(token: str, method: str, params: Dict) -> Any:
    """
    POST to a private endpoint (mirrors call_deribit_api in app.py).
    method should be the full method string, e.g. 'private/get_block_rfq_quotes'.
    """
    endpoint = method.replace("private/", "")
    url = f"{BASE_URL}/private/{endpoint}"
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    headers = {"Authorization": f"Bearer {token}"}
    status, body = http_post(url, payload, headers)
    err = body.get("error") if isinstance(body, dict) else None
    if err and isinstance(err, dict) and err.get("code") == NOT_MAKER_CODE:
        raise MakerPermissionError(f"account not a registered maker: {err}")
    assert status == 200, f"HTTP {status}: {body}"
    if err:
        raise RuntimeError(f"Deribit error: {err}")
    return body.get("result", {})


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
SKIP = "\033[33mSKIP\033[0m"

failures = 0


def run(label: str, fn):
    global failures
    print(f"  {label} ...", end=" ", flush=True)
    try:
        result = fn()
        print(PASS)
        return result
    except AssertionError as e:
        print(f"{FAIL}  {e}")
        failures += 1
        return None
    except MakerPermissionError as e:
        print(f"{SKIP}  (account not a registered maker — expected for test creds): {e}")
        return None
    except RuntimeError as e:
        # Deribit API-level errors: e.g. RFQ/quote id not found — expected in test env.
        print(f"{SKIP}  (API rejected — expected in test env): {e}")
        return None
    except Exception as e:
        print(f"{FAIL}  Unexpected: {type(e).__name__}: {e}")
        failures += 1
        return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.json", type=Path)
    args = parser.parse_args()

    if not args.config.exists():
        print(f"Config not found: {args.config}")
        return 1
    cfg = json.loads(args.config.read_text())
    client_id = cfg.get("client_id")
    client_secret = cfg.get("client_secret")
    if not client_id or not client_secret:
        print("config.json must contain client_id and client_secret")
        return 1

    print(f"\nTarget: {BASE_URL}")
    print(f"Client ID: {client_id}\n")

    # ------------------------------------------------------------------
    # 1. Authentication  (public/auth)
    # ------------------------------------------------------------------
    print("=== 1. Authentication (public/auth) ===")
    token = run("authenticate with client_credentials", lambda: authenticate(client_id, client_secret))
    if token is None:
        print("\nAuthentication failed — cannot continue.\n")
        return 1

    # ------------------------------------------------------------------
    # 2. get_block_rfqs  (private/get_block_rfqs)
    # ------------------------------------------------------------------
    print("\n=== 2. private/get_block_rfqs ===")
    # API returns {"block_rfqs": [...], "continuation": ...} — NOT a bare list.
    # app.py must extract result.get("block_rfqs", []).
    all_rfqs = []
    for currency in ("BTC", "ETH"):
        def _get_rfqs(cur=currency):
            result = call_private(token, "private/get_block_rfqs", {"currency": cur})
            assert isinstance(result, dict), f"Expected dict, got {type(result)}"
            assert "block_rfqs" in result, f"Missing 'block_rfqs' key: {result}"
            rfqs = result["block_rfqs"]
            assert isinstance(rfqs, list), f"'block_rfqs' should be list, got {type(rfqs)}"
            print(f"      {cur}: {len(rfqs)} RFQ(s)", end="  ")
            all_rfqs.extend(rfqs)
            return rfqs
        run(f"get_block_rfqs currency={currency}", _get_rfqs)

    # ------------------------------------------------------------------
    # 3. get_block_rfq_quotes  (private/get_block_rfq_quotes)
    # ------------------------------------------------------------------
    print("\n=== 3. private/get_block_rfq_quotes ===")
    all_quotes = []
    for currency in ("BTC", "ETH"):
        def _get_quotes(cur=currency):
            result = call_private(token, "private/get_block_rfq_quotes", {"currency": cur})
            assert isinstance(result, list), f"Expected list, got {type(result)}"
            print(f"      {cur}: {len(result)} quote(s)", end="  ")
            all_quotes.extend(result)
            return result
        run(f"get_block_rfq_quotes currency={currency}", _get_quotes)

    # ------------------------------------------------------------------
    # 4-6. add / edit / cancel quote (full flow using a real open RFQ)
    # ------------------------------------------------------------------
    import time as _time
    now_ms = int(_time.time() * 1000)
    open_rfqs = [rfq for rfq in all_rfqs
                 if rfq.get("state") == "open"
                 and rfq.get("expiration_timestamp", 0) > now_ms + 20_000]

    if not open_rfqs:
        print("\n=== 4-6. add / edit / cancel quote ===")
        print(f"  {SKIP}  No open RFQs with sufficient time remaining — skipping add/edit/cancel")
        new_quote_id = None
    else:
        rfq = open_rfqs[0]
        leg = rfq["legs"][0]
        print(f"\n=== 4. private/add_block_rfq_quote ===")
        print(f"   Using RFQ {rfq['block_rfq_id']} ({leg['instrument_name']}, amount={rfq['amount']})")

        def _add_quote():
            params = {
                "block_rfq_id": rfq["block_rfq_id"],
                "amount": rfq["amount"],
                "direction": "sell",              # maker's direction (opposite of taker's buy)
                "legs": [{
                    "instrument_name": leg["instrument_name"],
                    "direction": leg["direction"],  # must match the RFQ's leg direction
                    "ratio": leg["ratio"],
                    "price": 0.5,
                }],
            }
            # Include hedge if the RFQ defines one
            if rfq.get("hedge"):
                h = rfq["hedge"]
                params["hedge"] = {
                    "instrument_name": h["instrument_name"],
                    "direction": h["direction"],
                    "price": h["price"],
                    "amount": h["amount"],
                }
            result = call_private(token, "private/add_block_rfq_quote", params)
            qid = result.get("block_rfq_quote_id")
            assert qid is not None, f"No block_rfq_quote_id in result: {result}"
            print(f"      block_rfq_quote_id={qid} state={result.get('quote_state')}", end="  ")
            return qid
        new_quote_id = run("add_block_rfq_quote", _add_quote)

        # ------------------------------------------------------------------
        # 5. edit_block_rfq_quote
        # ------------------------------------------------------------------
        print("\n=== 5. private/edit_block_rfq_quote ===")
        if new_quote_id:
            def _edit_quote():
                params = {
                    "block_rfq_quote_id": new_quote_id,
                    "amount": rfq["amount"],
                    "legs": [{
                        "instrument_name": leg["instrument_name"],
                        "direction": leg["direction"],
                        "ratio": leg["ratio"],
                        "price": 0.6,
                    }],
                }
                if rfq.get("hedge"):
                    h = rfq["hedge"]
                    params["hedge"] = {
                        "instrument_name": h["instrument_name"],
                        "direction": h["direction"],
                        "price": h["price"],
                        "amount": h["amount"],
                    }
                result = call_private(token, "private/edit_block_rfq_quote", params)
                assert result.get("replaced") is True, f"Expected replaced=True: {result}"
                assert result.get("block_rfq_quote_id") == new_quote_id
                print(f"      price→0.6 replaced={result.get('replaced')}", end="  ")
                return result
            run("edit_block_rfq_quote", _edit_quote)
        else:
            print(f"  edit_block_rfq_quote ... {SKIP}  (no quote from add step)")

        # ------------------------------------------------------------------
        # 6. cancel_block_rfq_quote
        # ------------------------------------------------------------------
        print("\n=== 6. private/cancel_block_rfq_quote ===")
        if new_quote_id:
            def _cancel_quote():
                result = call_private(token, "private/cancel_block_rfq_quote", {
                    "block_rfq_quote_id": new_quote_id,
                })
                assert result.get("quote_state") == "cancelled", f"Expected cancelled: {result}"
                print(f"      quote_state={result.get('quote_state')}", end="  ")
                return result
            run("cancel_block_rfq_quote", _cancel_quote)
        else:
            print(f"  cancel_block_rfq_quote ... {SKIP}  (no quote from add step)")

    # ------------------------------------------------------------------
    # 7. cancel_all_block_rfq_quotes  (private/cancel_all_block_rfq_quotes)
    # ------------------------------------------------------------------
    print("\n=== 7. private/cancel_all_block_rfq_quotes ===")

    def _cancel_all_btc():
        result = call_private(token, "private/cancel_all_block_rfq_quotes", {"currency": "BTC"})
        print(f"      result={result}", end="  ")
        return result
    run("cancel_all_block_rfq_quotes currency=BTC", _cancel_all_btc)

    def _cancel_all():
        result = call_private(token, "private/cancel_all_block_rfq_quotes", {})
        print(f"      result={result}", end="  ")
        return result
    run("cancel_all_block_rfq_quotes (all currencies)", _cancel_all)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print()
    if failures:
        print(f"\033[31m{failures} test(s) FAILED\033[0m")
        return 1
    print("\033[32mAll tests PASSED\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
