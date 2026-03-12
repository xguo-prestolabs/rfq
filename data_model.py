"""
Pydantic data models for all Redis message types in the RFQ pipeline.

Redis key families and their sources:
─────────────────────────────────────────────────────────────────────────
  Key Pattern                          Source                     TTL
─────────────────────────────────────────────────────────────────────────
  rfq:<block_rfq_id>                   block_rfq.maker.*          none
  rfq_quote:<block_rfq_quote_id>       block_rfq.maker.quotes.*   4h
  rfq_taker:<block_rfq_id>             block_rfq.taker.*          4h
  rfq_trade:<block_rfq_id>             block_rfq.trades.*         4h
  rfq_user:submit:<id>                 app.py /quotes/add         48h
  rfq_user:cancel:<id>                 app.py /quotes/cancel      48h
  rfq_user:edit:<id>                   app.py /quotes/edit        48h
  rfq_user:cancel_all:<ts>             app.py /quotes/cancel_all  48h
  rfq_user:<action>:err:<ts>           app.py (on error)          48h
  price:<instrument_name>              C++ pricer (direct/ZMQ)    none
  greeks:<instrument_name>             C++ pricer (direct/ZMQ)    none
  total_greeks:ALL                     C++ pricer (direct/ZMQ)    none
─────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ═══════════════════════════════════════════════════════════════════════════
#  Common / shared models
# ═══════════════════════════════════════════════════════════════════════════


class Meta(BaseModel):
    """Writer metadata attached to every ws_client_redis message."""
    program: str
    version: str
    testnet: Optional[bool] = None


class Leg(BaseModel):
    """A single leg in an RFQ, quote, or trade."""
    instrument_name: str
    direction: Literal["buy", "sell"]
    ratio: int
    price: Optional[float] = None


class HedgeLeg(BaseModel):
    """Optional hedge leg (perpetual or future)."""
    instrument_name: str
    direction: Literal["buy", "sell"]
    price: float
    amount: float


class Trade(BaseModel):
    """A single fill within an RFQ trade."""
    price: float
    amount: float
    direction: Literal["buy", "sell"]


class IndexPrices(BaseModel):
    btc_usd: Optional[float] = None
    eth_usd: Optional[float] = None


# ═══════════════════════════════════════════════════════════════════════════
#  1. rfq:<block_rfq_id>  —  Maker-side RFQ state
#     Source: block_rfq.maker.{btc,eth}
# ═══════════════════════════════════════════════════════════════════════════


class RFQData(BaseModel):
    """Inner data payload of an RFQ notification (params.data)."""
    block_rfq_id: int
    state: Literal["open", "filled", "expired", "cancelled"]
    role: Literal["maker"]
    amount: float
    legs: list[Leg]
    combo_id: Optional[str] = None
    taker: Optional[str] = None
    taker_rating: Optional[str] = None
    disclosed: bool = False
    min_trade_amount: Optional[float] = None
    creation_timestamp: int
    expiration_timestamp: int
    timestamp: Optional[int] = None
    # Present when state == "filled"
    mark_price: Optional[float] = None
    trades: Optional[list[Trade]] = None
    index_prices: Optional[IndexPrices] = None
    # Hedge request from taker
    hedge: Optional[HedgeLeg] = None


class RFQParams(BaseModel):
    channel: str
    data: RFQData


class RFQMessage(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "subscription"
    params: RFQParams


class RFQDocument(BaseModel):
    """Top-level Redis document for rfq:<block_rfq_id>."""
    fetch_time: int
    channel: str
    meta: Meta
    message: RFQMessage


# ═══════════════════════════════════════════════════════════════════════════
#  2. rfq_quote:<block_rfq_quote_id>  —  Quote snapshots
#     Source: block_rfq.maker.quotes.{btc,eth}
# ═══════════════════════════════════════════════════════════════════════════


class QuoteData(BaseModel):
    """A single quote snapshot."""
    block_rfq_quote_id: int
    block_rfq_id: int
    quote_state: Literal["open", "filled", "cancelled", "expired"]
    direction: Literal["buy", "sell"]
    amount: float
    filled_amount: float = 0.0
    price: Optional[float] = None
    legs: list[Leg]
    hedge: Optional[HedgeLeg] = None
    replaced: bool = False
    execution_instruction: str = "any_part_of"
    creation_timestamp: int
    last_update_timestamp: int


class QuoteParams(BaseModel):
    channel: str
    data: list[QuoteData]  # Deribit sends quotes as a list


class QuoteMessage(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "subscription"
    params: QuoteParams


class QuoteDocument(BaseModel):
    """Top-level Redis document for rfq_quote:<block_rfq_quote_id>."""
    fetch_time: int
    channel: str
    meta: Meta
    message: QuoteMessage


# ═══════════════════════════════════════════════════════════════════════════
#  3. rfq_taker:<block_rfq_id>  —  Taker-side RFQ state
#     Source: block_rfq.taker.{btc,eth}
#     (Rarely seen — only when we are also the taker)
# ═══════════════════════════════════════════════════════════════════════════


class TakerRFQData(BaseModel):
    """Inner data for taker-side RFQ. Structure mirrors RFQData with role=taker."""
    block_rfq_id: int
    state: str
    role: Literal["taker"] = "taker"
    amount: float
    legs: list[Leg]
    combo_id: Optional[str] = None
    creation_timestamp: int
    expiration_timestamp: int


class TakerRFQParams(BaseModel):
    channel: str
    data: TakerRFQData


class TakerRFQMessage(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "subscription"
    params: TakerRFQParams


class TakerRFQDocument(BaseModel):
    """Top-level Redis document for rfq_taker:<block_rfq_id>."""
    fetch_time: int
    channel: str
    meta: Meta
    message: TakerRFQMessage


# ═══════════════════════════════════════════════════════════════════════════
#  4. rfq_trade:<block_rfq_id>  —  Trade execution data
#     Source: block_rfq.trades.{btc,eth}
# ═══════════════════════════════════════════════════════════════════════════


class TradeData(BaseModel):
    """Inner data for a block RFQ trade."""
    id: int  # this is the block_rfq_id
    timestamp: int
    direction: Literal["buy", "sell"]
    amount: float
    mark_price: Optional[float] = None
    trades: list[Trade]
    legs: list[Leg]
    combo_id: Optional[str] = None
    index_prices: Optional[IndexPrices] = None


class TradeParams(BaseModel):
    channel: str
    data: TradeData


class TradeMessage(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "subscription"
    params: TradeParams


class TradeDocument(BaseModel):
    """Top-level Redis document for rfq_trade:<block_rfq_id>."""
    fetch_time: int
    channel: str
    meta: Meta
    message: TradeMessage


# ═══════════════════════════════════════════════════════════════════════════
#  5. rfq_user:*  —  User quote actions (submitted via app.py)
# ═══════════════════════════════════════════════════════════════════════════


class SubmitParams(BaseModel):
    """Parameters sent to /quotes/add."""
    block_rfq_id: int
    amount: float
    direction: Literal["buy", "sell"]
    legs: list[Leg]
    hedge: Optional[HedgeLeg] = None
    price: Optional[float] = None


class SubmitResult(BaseModel):
    """Deribit response for a successful quote submission."""
    block_rfq_quote_id: int
    block_rfq_id: int
    quote_state: str
    direction: Literal["buy", "sell"]
    amount: float
    filled_amount: float = 0.0
    price: Optional[float] = None
    legs: list[Leg]
    hedge: Optional[HedgeLeg] = None
    replaced: bool = False
    execution_instruction: str = "any_part_of"
    creation_timestamp: int
    last_update_timestamp: int


class UserSubmitRecord(BaseModel):
    """rfq_user:submit:<quote_id> — successful submission."""
    timestamp: int
    action: Literal["submit"] = "submit"
    status: Optional[str] = None  # absent in older records
    params: SubmitParams
    result: SubmitResult


class UserSubmitErrorRecord(BaseModel):
    """rfq_user:submit:err:<ts> — failed submission."""
    timestamp: int
    action: Literal["submit"] = "submit"
    status: Literal["error"] = "error"
    params: SubmitParams
    error: str


class CancelParams(BaseModel):
    """Parameters sent to /quotes/cancel."""
    block_rfq_quote_id: int


class CancelResult(BaseModel):
    """Deribit response for a successful quote cancellation."""
    block_rfq_quote_id: int
    block_rfq_id: int
    quote_state: Literal["cancelled"]
    direction: Literal["buy", "sell"]
    amount: float
    filled_amount: float = 0.0
    price: Optional[float] = None
    legs: list[Leg]
    hedge: Optional[HedgeLeg] = None
    replaced: bool = False
    execution_instruction: str = "any_part_of"
    creation_timestamp: int
    last_update_timestamp: int


class UserCancelRecord(BaseModel):
    """rfq_user:cancel:<quote_id> — successful cancellation."""
    timestamp: int
    action: Literal["cancel"] = "cancel"
    status: Literal["success"] = "success"
    params: CancelParams
    result: CancelResult


class UserCancelErrorRecord(BaseModel):
    """rfq_user:cancel:err:<ts> — failed cancellation."""
    timestamp: int
    action: Literal["cancel"] = "cancel"
    status: Literal["error"] = "error"
    params: CancelParams
    error: str


class EditParams(BaseModel):
    """Parameters sent to /quotes/edit."""
    block_rfq_quote_id: int
    amount: float
    legs: list[Leg]
    price: Optional[float] = None
    hedge: Optional[HedgeLeg] = None


class UserEditRecord(BaseModel):
    """rfq_user:edit:<quote_id> — successful edit."""
    timestamp: int
    action: Literal["edit"] = "edit"
    status: Literal["success"] = "success"
    params: EditParams
    result: SubmitResult  # edit returns same shape as submit


class UserEditErrorRecord(BaseModel):
    """rfq_user:edit:err:<ts> — failed edit."""
    timestamp: int
    action: Literal["edit"] = "edit"
    status: Literal["error"] = "error"
    params: EditParams
    error: str


class CancelAllParams(BaseModel):
    """Parameters sent to /quotes/cancel_all."""
    currency: Optional[str] = None


class UserCancelAllRecord(BaseModel):
    """rfq_user:cancel_all:<ts> — successful cancel-all."""
    timestamp: int
    action: Literal["cancel_all"] = "cancel_all"
    currency: str = "all"
    params: CancelAllParams
    result: int  # number of quotes cancelled


class UserCancelAllErrorRecord(BaseModel):
    """rfq_user:cancel_all:err:<ts> — failed cancel-all."""
    timestamp: int
    action: Literal["cancel_all"] = "cancel_all"
    status: Literal["error"] = "error"
    currency: str = "all"
    params: CancelAllParams
    error: str


# ═══════════════════════════════════════════════════════════════════════════
#  6. price:<instrument_name>  —  Fair price from C++ pricer
# ═══════════════════════════════════════════════════════════════════════════


class FairPrice(BaseModel):
    """C++ pricer fair price output. Key: price:<native_product>."""
    msg_type: Literal["fair_price"] = "fair_price"
    native_product: str
    ts: int
    trade_key: str

    # Prices
    strat_mid_price: float
    buy_price: float
    sell_price: float
    mark_price: Optional[float] = None
    best_bid_price: Optional[float] = None
    best_bid_qty: Optional[float] = None
    best_ask_price: Optional[float] = None
    best_ask_qty: Optional[float] = None
    ref_price: Optional[float] = None
    perp_price: Optional[float] = None
    index: Optional[float] = None

    # Vol / greeks
    strat_mid_iv: Optional[float] = None
    mark_iv: Optional[float] = None
    delta: Optional[float] = None
    T: Optional[float] = Field(None, description="Time to expiry in years")

    # Sizing
    edge: Optional[float] = None
    spread_edge: Optional[float] = None
    order_size: Optional[float] = None


# ═══════════════════════════════════════════════════════════════════════════
#  7. greeks:<instrument_name>  —  Per-instrument greeks from C++ pricer
# ═══════════════════════════════════════════════════════════════════════════


class Greeks(BaseModel):
    """C++ pricer greeks output. Key: greeks:<native_product>.

    All values are dollar greeks per 1 coin of the underlying.
    """
    msg_type: Literal["greeks"] = "greeks"
    native_product: str
    ts: int
    trade_key: str

    delta: float              # dollar delta per coin
    gamma: float              # dollar gamma per coin
    vega: float               # dollar vega per coin
    delta_position: float     # delta * current position
    gamma_position: float     # gamma * current position
    vega_position: float      # vega * current position


# ═══════════════════════════════════════════════════════════════════════════
#  8. total_greeks:ALL  —  Aggregated portfolio greeks from C++ pricer
# ═══════════════════════════════════════════════════════════════════════════


class TotalGreeks(BaseModel):
    """Aggregate portfolio dollar greeks. Key: total_greeks:ALL.

    Note: vega field has a typo in the C++ source ("postition" → "position").
    """
    msg_type: Literal["total_greeks"] = "total_greeks"
    native_product: Literal["ALL"] = "ALL"
    trade_key: Literal["ALL"] = "ALL"
    ts: int

    delta_position_total: float
    gamma_position_total: float
    vega_postition_total: float  # typo preserved from C++ source


# ═══════════════════════════════════════════════════════════════════════════
#  9. Redis pub/sub: rfq_updates channel
# ═══════════════════════════════════════════════════════════════════════════


class PubSubRFQ(BaseModel):
    """Published when an RFQ is created or updated."""
    type: Literal["rfq"] = "rfq"
    id: int
    data: dict
    testnet: bool = False


class PubSubQuote(BaseModel):
    """Published when a quote state changes."""
    type: Literal["rfq_quote"] = "rfq_quote"
    id: int
    testnet: bool = False


class PubSubTaker(BaseModel):
    """Published when a taker RFQ state changes."""
    type: Literal["rfq_taker"] = "rfq_taker"
    id: int
    data: dict
    testnet: bool = False


class PubSubTrade(BaseModel):
    """Published when a trade occurs."""
    type: Literal["rfq_trade"] = "rfq_trade"
    id: int
    data: dict
    testnet: bool = False
