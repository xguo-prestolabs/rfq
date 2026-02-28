# Copyright (c) 2025 Presto Labs Pte. Ltd.
# Author: xguo

import streamlit as st
import time
import json
from datetime import datetime
import pandas as pd
from absl import logging
import requests

logging.set_verbosity(logging.INFO)
logging.set_stderrthreshold(logging.INFO)

st.set_page_config(
    page_title="Deribit Block RFQ - Market Maker",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.title("Deribit Block RFQ Market Maker")
st.markdown("---")


# Sidebar
with st.sidebar:
    st.markdown("---")
    st.header("Total Greeks")

    #for tenor in ["day", "next_day", "week", "next_week", "month", "next_month"]:
    data = requests.get(f"http://127.0.0.1:8000/total_greeks").json()
    data = data.get("total_greeks", {})
    data.pop('ts')
    data.pop('trade_key')
    data.pop('msg_type')
    data.pop('native_product')
    #data = [{"tenor": "day", "inventory": 100}, {"tenor": "next_day", "inventory": 100}, {"tenor": "week", "inventory": 100}, {"tenor": "next_week", "inventory": 100}, {"tenor": "month", "inventory": 100}, {"tenor": "next_month", "inventory": 100}]
    df = pd.DataFrame(list(data.items()))
    df.columns = ["field", "value"]
    df.set_index('field', inplace=True)
    st.dataframe(df)

# Tabs
tab1, tab2, tab3 = st.tabs(["📋 RFQs", "💬 Quotes", "📈 Trades"])

with tab1:
    st.header("Active RFQs")
    try:
        rfqs = requests.get("http://127.0.0.1:8000/rfqs").json()
    except Exception as e:
        st.error(f"Error getting RFQs: {e}")
        rfqs = []

    if not rfqs:
        st.info("No RFQs yet")
    else:
        for rfq_id, rfq in sorted(rfqs.items(), reverse=True):
            print(rfq_id, rfq)
            with st.expander(f"RFQ #{rfq_id}", expanded=True):
                col1, col2 = st.columns([2, 1])

                with col1:
                    st.write(f"**State:** {rfq.get('state')}")
                    cts = datetime.fromtimestamp(rfq.get('creation_timestamp') / 1e3).isoformat()
                    ets = datetime.fromtimestamp(rfq.get('expiration_timestamp') / 1e3).isoformat()
                    st.write(f"**Created at:** {cts}")
                    st.write(f"**Expires at:** {ets}")
                    st.write()
                    legs = rfq.get("legs", [])
                    total_fair_price = 0
                    total_delta_position = 0
                    total_gamma_position = 0
                    total_delta_position = 0

                    for leg in legs:
                        product = leg["instrument_name"]

                        resp = requests.get(f"http://127.0.0.1:8000/price/{product}")
                        resp.raise_for_status()
                        data = resp.json()
                        leg["fair_price"] = data["price"] or 0
                        sign = 1 if leg["direction"] == "buy" else -1
                        leg["ratio"] = leg["ratio"] * sign

                        resp = requests.get(f"http://127.0.0.1:8000/greeks/{product}")
                        resp.raise_for_status()
                        data = resp.json()["greeks"]
                        leg["delta"] = data.get("delta", 0)
                        leg["gamma"] = data.get("gamma", 0)
                        leg["vega"] = data.get("vega", 0)

                    combo = {}
                    combo['instrument_name'] = rfq.get("combo_id")
                    combo['direction'] = 'N/A'
                    combo['ratio'] = 1
                    combo['delta'] = 0
                    combo['gamma'] = 0
                    combo['vega'] = 0
                    combo['fair_price'] = 0
                    for leg in legs:
                        ratio = leg["ratio"]
                        combo["delta"] += leg["delta"] * ratio
                        combo["gamma"] += leg["gamma"] * ratio
                        combo["vega"] += leg["vega"] * ratio
                        combo["fair_price"] += leg["fair_price"] * ratio

                    legs.append(combo)
                    if legs:
                        st.dataframe(pd.DataFrame(legs))

                with col2:
                    st.subheader("Add Quote")
                    price_key = f"p_{rfq_id}"
                    amount_key = f"a_{rfq_id}"

                    if price_key not in st.session_state:
                        st.session_state[price_key] = legs[-1]["fair_price"]

                    if amount_key not in st.session_state:
                        st.session_state[amount_key] = rfq.get("amount")
                    price = st.number_input("Price", step=0.00000001, format="%.8f", key=price_key)
                    amount = st.number_input("Amount", step=0.01, format="%.2f", key=amount_key)

                    # Create two columns for horizontal button layout
                    col1, col2, col3 = st.columns(3)

                    with col1:
                        if st.button("Submit", key=f"s_{rfq_id}"):
                            params = {"rfq_id": rfq_id, "price": price, "amount": amount}
                            resp = requests.post(f"http://127.0.0.1:8000/add_quote", json=params)
                            resp.raise_for_status()
                            data = resp.json()
                            print(json.dumps(data, indent=4))
                            st.success(f"Quote: {amount} @ {price}")

                    with col2:
                        if st.button("Edit", key=f"c_{rfq_id}"):
                            params = {"quote_id": quote_id}
                            resp = requests.post(f"http://127.0.0.1:8000/edit_quote", json=params)
                            resp.raise_for_status()
                            data = resp.json()
                            print(json.dumps(data, indent=4))
                            st.success(f"Canceled: {amount} @ {price}")

                    with col3:
                        if st.button("Cancel", key=f"e_{rfq_id}"):
                            params = {"quote_id": quote_id}
                            resp = requests.post(f"http://127.0.0.1:8000/cancel_quote", json=params)
                            resp.raise_for_status()
                            data = resp.json()
                            print(json.dumps(data, indent=4))
                            st.success(f"Canceled: {amount} @ {price}")

with tab2:
    st.header("My Quotes")
    resp = requests.get("http://127.0.0.1:8000/quotes").json()
    my_quotes = resp.get("quotes", [])
    if my_quotes:
        df = pd.DataFrame(list(my_quotes.values()))
        st.dataframe(df)
    else:
        st.info("No quotes")

with tab3:
    st.header("Trades")
    resp = requests.get("http://127.0.0.1:8000/trades").json()
    trades = resp.get("trades", [])
    if trades:
        df = pd.DataFrame(trades)
        st.dataframe(df)
    else:
        st.info("No trades")


# Auto-refresh
st.markdown("---")
col1, col2 = st.columns([3, 1])
with col1:
    if st.checkbox("Auto-refresh (5s)", value=False):
        time.sleep(5)
        st.rerun()
with col2:
    if st.button("🔄 Refresh Now"):
        st.rerun()

# Display last refresh time
st.caption(f"Last refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
