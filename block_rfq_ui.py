# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import json
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from absl import logging

logging.set_verbosity(logging.INFO)
logging.set_stderrthreshold(logging.INFO)

BASE_URL = "http://127.0.0.1:8000"
CONFIG_PATH = Path(__file__).parent / "config.json"

st.set_page_config(
    page_title="Deribit Block RFQ - Market Maker",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.title("Deribit Block RFQ Market Maker")
st.markdown("---")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def api_get(path: str):
    return requests.get(f"{BASE_URL}{path}", timeout=10)


def api_post(path: str, body: dict = None, params: dict = None):
    return requests.post(f"{BASE_URL}{path}", json=body, params=params, timeout=10)


def show_error(label: str, resp: requests.Response):
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        detail = resp.text
    st.error(f"{label}: {detail}")


# ---------------------------------------------------------------------------
# Sidebar — Configuration & Greeks
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ Deribit Configuration")

    # Auto-load credentials from config.json
    default_id, default_secret = "", ""
    if CONFIG_PATH.exists():
        try:
            cfg = json.loads(CONFIG_PATH.read_text())
            default_id = cfg.get("client_id", "")
            default_secret = cfg.get("client_secret", "")
        except Exception:
            pass

    client_id = st.text_input("Client ID", value=default_id)
    client_secret = st.text_input("Client Secret", value=default_secret, type="password")
    testnet = st.checkbox("Use Testnet (test.deribit.com)", value=True)

    if st.button("🔑 Configure", use_container_width=True):
        try:
            resp = api_post(
                "/configure",
                params={"client_id": client_id, "client_secret": client_secret, "testnet": str(testnet).lower()},
            )
            if resp.status_code == 200:
                st.success(resp.json().get("message", "Configured"))
                st.session_state["configured"] = True
            else:
                show_error("Configure failed", resp)
        except Exception as e:
            st.error(f"Configure error: {e}")

    if st.session_state.get("configured"):
        st.success("✅ Connected")
    else:
        st.warning("Not configured — click Configure to authenticate")

    st.markdown("---")

    # Cancel All Quotes
    st.header("🚨 Cancel All Quotes")
    cancel_currency = st.selectbox("Currency", ["All", "BTC", "ETH"])
    if st.button("Cancel All Quotes", use_container_width=True, type="primary"):
        try:
            body = {} if cancel_currency == "All" else {"currency": cancel_currency}
            resp = api_post("/quotes/cancel_all", body=body)
            if resp.status_code == 200:
                data = resp.json()
                st.success(f"Cancelled {data.get('result', 0)} quotes")
            else:
                show_error("Cancel all failed", resp)
        except Exception as e:
            st.error(f"Error: {e}")

    st.markdown("---")

    # Total Greeks
    st.header("📊 Total Greeks")
    try:
        data = api_get("/total_greeks").json()
        greeks = data.get("total_greeks", {})
        for key in ("ts", "trade_key", "msg_type", "native_product"):
            greeks.pop(key, None)
        df = pd.DataFrame(list(greeks.items()), columns=["field", "value"])
        df.set_index("field", inplace=True)
        st.dataframe(df, use_container_width=True)
    except Exception as e:
        st.warning(f"Greeks unavailable: {e}")


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------

tab1, tab2 = st.tabs(["📋 RFQs", "💬 My Quotes"])


# ── Tab 1: RFQs ──────────────────────────────────────────────────────────────

with tab1:
    st.header("Active RFQs")

    try:
        rfqs = api_get("/rfqs").json()
    except Exception as e:
        st.error(f"Error fetching RFQs: {e}")
        rfqs = {}

    if not rfqs:
        st.info("No active RFQs")
    else:
        for rfq_id, rfq in sorted(rfqs.items(), reverse=True):
            state = rfq.get("state", "unknown")
            state_icon = {"open": "🟢", "filled": "✅", "canceled": "❌", "expired": "⏰"}.get(state, "⚪")
            header = f"{state_icon} RFQ #{rfq_id}  —  {state.upper()}"

            with st.expander(header, expanded=(state == "open")):
                col_info, col_quote = st.columns([2, 1])

                with col_info:
                    cts = datetime.fromtimestamp(rfq.get("creation_timestamp", 0) / 1e3).strftime("%Y-%m-%d %H:%M:%S")
                    ets = datetime.fromtimestamp(rfq.get("expiration_timestamp", 0) / 1e3).strftime("%Y-%m-%d %H:%M:%S")
                    st.write(f"**Created:** {cts}  |  **Expires:** {ets}")
                    st.write(f"**Amount:** {rfq.get('amount')}  |  **Taker Rating:** {rfq.get('taker_rating', 'N/A')}")

                    # Build legs display with fair prices and greeks
                    legs = [dict(leg) for leg in rfq.get("legs", [])]  # shallow copy
                    for leg in legs:
                        product = leg["instrument_name"]
                        try:
                            price_data = api_get(f"/price/{product}").json()
                            leg["fair_price"] = price_data.get("price") or 0
                        except Exception:
                            leg["fair_price"] = 0
                        try:
                            greeks_data = api_get(f"/greeks/{product}").json().get("greeks", {})
                            leg["delta"] = greeks_data.get("delta", 0)
                            leg["gamma"] = greeks_data.get("gamma", 0)
                            leg["vega"] = greeks_data.get("vega", 0)
                        except Exception:
                            leg["delta"] = leg["gamma"] = leg["vega"] = 0

                    # Combo row (signed by direction)
                    combo = {"instrument_name": rfq.get("combo_id", "COMBO"), "direction": "—",
                             "ratio": "—", "delta": 0.0, "gamma": 0.0, "vega": 0.0, "fair_price": 0.0}
                    for leg in legs:
                        sign = 1 if leg["direction"] == "buy" else -1
                        ratio = leg["ratio"] * sign
                        combo["delta"] += leg["delta"] * ratio
                        combo["gamma"] += leg["gamma"] * ratio
                        combo["vega"] += leg["vega"] * ratio
                        combo["fair_price"] += leg["fair_price"] * ratio

                    df_legs = pd.DataFrame(legs + [combo])
                    st.dataframe(df_legs, use_container_width=True, hide_index=True)

                    # Hedge info
                    hedge = rfq.get("hedge")
                    if hedge:
                        st.caption(
                            f"**Hedge:** {hedge['instrument_name']}  {hedge['direction'].upper()}"
                            f"  qty={hedge['amount']}  @  {hedge['price']}"
                        )

                with col_quote:
                    st.subheader("Quote")

                    # Session-state keys for this RFQ
                    sk_quote_id   = f"quote_id_{rfq_id}"
                    sk_price      = f"price_{rfq_id}"
                    sk_amount     = f"amount_{rfq_id}"
                    sk_direction  = f"direction_{rfq_id}"
                    sk_leg_prices = [f"leg_price_{rfq_id}_{i}" for i in range(len(legs))]

                    # Initialise defaults from fair price / RFQ amount
                    combo_fair = combo["fair_price"]
                    if sk_amount not in st.session_state:
                        st.session_state[sk_amount] = float(rfq.get("amount", 1.0))
                    if sk_direction not in st.session_state:
                        first_leg_dir = rfq.get("legs", [{}])[0].get("direction", "buy")
                        st.session_state[sk_direction] = "sell" if first_leg_dir == "buy" else "buy"
                    for i, sk in enumerate(sk_leg_prices):
                        if sk not in st.session_state:
                            st.session_state[sk] = float(legs[i].get("fair_price", 0.0))

                    # Inputs
                    direction = st.selectbox(
                        "Direction (maker)",
                        ["sell", "buy"],
                        index=0 if st.session_state[sk_direction] == "sell" else 1,
                        key=sk_direction,
                    )
                    amount = st.number_input("Amount", min_value=0.0, step=0.1, format="%.4f", key=sk_amount)

                    leg_prices = []
                    for i, leg in enumerate(legs):
                        p = st.number_input(
                            f"Price — {leg['instrument_name']}",
                            min_value=0.0,
                            step=0.0001,
                            format="%.8f",
                            key=sk_leg_prices[i],
                        )
                        leg_prices.append(p)

                    # Build the legs payload
                    def build_legs(prices):
                        return [
                            {
                                "instrument_name": legs[i]["instrument_name"],
                                "direction": legs[i]["direction"],
                                "ratio": legs[i]["ratio"],
                                "price": prices[i],
                            }
                            for i in range(len(legs))
                        ]

                    existing_quote_id = st.session_state.get(sk_quote_id)

                    btn_col1, btn_col2, btn_col3 = st.columns(3)

                    # Submit (add new quote)
                    with btn_col1:
                        if st.button("Submit", key=f"submit_{rfq_id}", use_container_width=True):
                            body = {
                                "block_rfq_id": int(rfq_id),
                                "amount": amount,
                                "direction": direction,
                                "legs": build_legs(leg_prices),
                            }
                            # Include hedge if RFQ has one
                            if hedge:
                                body["hedge"] = {
                                    "instrument_name": hedge["instrument_name"],
                                    "direction": hedge["direction"],
                                    "price": hedge["price"],
                                    "amount": hedge["amount"],
                                }
                            try:
                                resp = api_post("/quotes/add", body=body)
                                if resp.status_code == 200:
                                    data = resp.json()
                                    qid = data.get("result", {}).get("block_rfq_quote_id")
                                    st.session_state[sk_quote_id] = qid
                                    st.success(f"Submitted! quote_id={qid}")
                                else:
                                    show_error("Submit failed", resp)
                            except Exception as e:
                                st.error(f"Error: {e}")

                    # Edit (requires existing quote)
                    with btn_col2:
                        edit_disabled = existing_quote_id is None
                        if st.button("Edit", key=f"edit_{rfq_id}", disabled=edit_disabled, use_container_width=True):
                            body = {
                                "quote_id": existing_quote_id,
                                "amount": amount,
                                "legs": build_legs(leg_prices),
                            }
                            if hedge:
                                body["hedge"] = {
                                    "instrument_name": hedge["instrument_name"],
                                    "direction": hedge["direction"],
                                    "price": hedge["price"],
                                    "amount": hedge["amount"],
                                }
                            try:
                                resp = api_post("/quotes/edit", body=body)
                                if resp.status_code == 200:
                                    st.success(f"Updated quote {existing_quote_id}")
                                else:
                                    show_error("Edit failed", resp)
                            except Exception as e:
                                st.error(f"Error: {e}")

                    # Cancel (requires existing quote)
                    with btn_col3:
                        cancel_disabled = existing_quote_id is None
                        if st.button("Cancel", key=f"cancel_{rfq_id}", disabled=cancel_disabled, use_container_width=True):
                            try:
                                resp = api_post("/quotes/cancel", body={"quote_id": existing_quote_id})
                                if resp.status_code == 200:
                                    st.success(f"Cancelled quote {existing_quote_id}")
                                    st.session_state.pop(sk_quote_id, None)
                                else:
                                    show_error("Cancel failed", resp)
                            except Exception as e:
                                st.error(f"Error: {e}")

                    if existing_quote_id:
                        st.caption(f"Active quote ID: `{existing_quote_id}`")


# ── Tab 2: My Quotes ──────────────────────────────────────────────────────────

with tab2:
    st.header("My Open Quotes")
    try:
        resp = api_get("/quotes")
        if resp.status_code == 200:
            data = resp.json()
            quotes = data.get("quotes", [])
            if quotes:
                df = pd.DataFrame(quotes)
                st.dataframe(df, use_container_width=True, hide_index=True)

                # Per-quote cancel buttons
                st.subheader("Cancel a specific quote")
                qid_input = st.number_input("Quote ID to cancel", min_value=0, step=1, format="%d")
                if st.button("Cancel Quote"):
                    try:
                        r = api_post("/quotes/cancel", body={"quote_id": int(qid_input)})
                        if r.status_code == 200:
                            st.success(f"Cancelled quote {qid_input}")
                        else:
                            show_error("Cancel failed", r)
                    except Exception as e:
                        st.error(f"Error: {e}")
            else:
                st.info("No open quotes")
        else:
            show_error("Failed to fetch quotes", resp)
    except Exception as e:
        st.error(f"Error fetching quotes: {e}")


# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------

st.markdown("---")
col_refresh, col_btn = st.columns([3, 1])
with col_refresh:
    if st.checkbox("Auto-refresh (5s)", value=False):
        time.sleep(5)
        st.rerun()
with col_btn:
    if st.button("🔄 Refresh Now"):
        st.rerun()

st.caption(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
