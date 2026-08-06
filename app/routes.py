"""
API route handlers.

Each route:
  1. Calls the Groww wrapper (app.groww) to fetch raw data.
  2. Maps that raw data onto our own Pydantic response models (app.models),
     so the frontend contract stays stable even if Groww's schema shifts.
  3. Lets GrowwAPIError subclasses propagate up to the exception handlers
     registered in main.py, which convert them into proper HTTP responses.
"""

import os
from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter

from .config import get_settings
from .groww import get_groww_client
from .models import (
    AllocationSlice,
    DashboardResponse,
    HealthResponse,
    Holding,
    HoldingsResponse,
    MarketPriceResponse,
    MoverItem,
    Order,
    OrdersResponse,
    PortfolioResponse,
    Position,
    PositionsResponse,
    WatchlistItem,
    WatchlistResponse,
)
from .utils import compute_allocation, logger, pnl_percent, safe_float

router = APIRouter()


# ----------------------------------------------------------------------
# Health
# ----------------------------------------------------------------------
@router.get("/health", response_model=HealthResponse, tags=["Meta"])
def health() -> HealthResponse:
    settings = get_settings()
    issues = settings.validate()
    return HealthResponse(
        status="ok" if not issues else "degraded",
        environment=settings.ENVIRONMENT,
        timestamp=datetime.now(timezone.utc),
        issues=issues,
    )


# ----------------------------------------------------------------------
# Holdings
# ----------------------------------------------------------------------
def _fetch_holdings_enriched() -> List[Holding]:
    """Fetch holdings from Groww and enrich each with live price + P&L."""
    client = get_groww_client()
    raw_holdings = client.get_holdings()

    if not raw_holdings:
        return []

    exchange_symbols = [f"NSE_{h['trading_symbol']}" for h in raw_holdings]
    ltp_map = {}
    try:
        ltp_map = client.get_ltp(exchange_symbols)
    except Exception as exc:  # noqa: BLE001 - degrade gracefully, holdings still useful without LTP
        logger.warning("Failed to fetch LTPs for holdings: %s", exc)

    holdings: List[Holding] = []
    for h in raw_holdings:
        symbol = h["trading_symbol"]
        quantity = safe_float(h.get("quantity"))
        avg_price = safe_float(h.get("average_price"))
        invested_value = round(quantity * avg_price, 2)

        ltp = ltp_map.get(f"NSE_{symbol}")
        current_value = round(quantity * ltp, 2) if ltp is not None else None
        pnl = round(current_value - invested_value, 2) if current_value is not None else None

        holdings.append(
            Holding(
                symbol=symbol,
                quantity=quantity,
                average_price=avg_price,
                last_traded_price=ltp,
                invested_value=invested_value,
                current_value=current_value,
                pnl=pnl,
                pnl_percent=pnl_percent(invested_value, current_value)
                if current_value is not None
                else None,
                exchange="NSE",
            )
        )
    return holdings


@router.get("/holdings", response_model=HoldingsResponse, tags=["Portfolio"])
def holdings() -> HoldingsResponse:
    items = _fetch_holdings_enriched()
    return HoldingsResponse(holdings=items, count=len(items))


# ----------------------------------------------------------------------
# Positions
# ----------------------------------------------------------------------
@router.get("/positions", response_model=PositionsResponse, tags=["Portfolio"])
def positions() -> PositionsResponse:
    client = get_groww_client()
    raw_positions = client.get_positions()

    items = [
        Position(
            symbol=p["trading_symbol"],
            quantity=safe_float(p.get("quantity")),
            average_price=safe_float(p.get("net_price")),
            pnl=safe_float(p.get("realised_pnl")),
            product=p.get("product"),
            exchange=p.get("exchange"),
        )
        for p in raw_positions
    ]
    return PositionsResponse(positions=items, count=len(items))


# ----------------------------------------------------------------------
# Orders
# ----------------------------------------------------------------------
@router.get("/orders", response_model=OrdersResponse, tags=["Trading"])
def orders() -> OrdersResponse:
    client = get_groww_client()
    raw_orders = client.get_order_list()

    items = [
        Order(
            order_id=o["groww_order_id"],
            symbol=o["trading_symbol"],
            order_type=o.get("order_type"),
            transaction_type=o.get("transaction_type"),
            quantity=safe_float(o.get("quantity")),
            price=safe_float(o.get("price")),
            status=o.get("order_status"),
            order_time=o.get("created_at"),
        )
        for o in raw_orders
    ]
    return OrdersResponse(orders=items, count=len(items))


# ----------------------------------------------------------------------
# Watchlist
#
# NOTE: Groww's public Trading API does not currently expose a "get my
# saved watchlist" endpoint. This route instead uses a symbol list you
# configure yourself (WATCHLIST_SYMBOLS in .env, comma-separated NSE
# trading symbols) and enriches it with live price data. If Groww adds a
# native watchlist endpoint later, swap the symbol source here.
# ----------------------------------------------------------------------
@router.get("/watchlist", response_model=WatchlistResponse, tags=["Portfolio"])
def watchlist() -> WatchlistResponse:
    symbols = [s.strip() for s in os.getenv("WATCHLIST_SYMBOLS", "").split(",") if s.strip()]

    if not symbols:
        return WatchlistResponse(watchlist=[], count=0)

    client = get_groww_client()
    exchange_symbols = [f"NSE_{s}" for s in symbols]
    ltp_map = client.get_ltp(exchange_symbols)

    items = [
        WatchlistItem(
            symbol=symbol,
            last_traded_price=ltp_map.get(f"NSE_{symbol}"),
        )
        for symbol in symbols
    ]
    return WatchlistResponse(watchlist=items, count=len(items))


# ----------------------------------------------------------------------
# Market price for a single symbol
# ----------------------------------------------------------------------
@router.get("/market-price/{symbol}", response_model=MarketPriceResponse, tags=["Market Data"])
def market_price(symbol: str, exchange: str = "NSE") -> MarketPriceResponse:
    client = get_groww_client()
    quote = client.get_quote(trading_symbol=symbol.upper(), exchange=exchange)

    last_price = quote.get("last_price")
    ohlc = quote.get("ohlc")
    previous_close = ohlc.get("close") if isinstance(ohlc, dict) else None

    return MarketPriceResponse(
        symbol=symbol.upper(),
        last_traded_price=safe_float(last_price) if last_price is not None else None,
        previous_close=safe_float(previous_close) if previous_close is not None else None,
        change=safe_float(quote.get("day_change")),
        change_percent=safe_float(quote.get("day_change_perc")),
        raw=quote,
    )


# ----------------------------------------------------------------------
# Portfolio summary
# ----------------------------------------------------------------------
@router.get("/portfolio", response_model=PortfolioResponse, tags=["Portfolio"])
def portfolio() -> PortfolioResponse:
    holdings_items = _fetch_holdings_enriched()
    client = get_groww_client()
    positions_raw = client.get_positions()

    invested = sum(h.invested_value for h in holdings_items)
    current = sum(h.current_value for h in holdings_items if h.current_value is not None)
    total_pnl = round(current - invested, 2)

    return PortfolioResponse(
        invested_amount=round(invested, 2),
        current_amount=round(current, 2),
        total_pnl=total_pnl,
        total_pnl_percent=pnl_percent(invested, current),
        holdings_count=len(holdings_items),
        positions_count=len(positions_raw),
    )


# ----------------------------------------------------------------------
# Dashboard (aggregate view)
# ----------------------------------------------------------------------
@router.get("/dashboard", response_model=DashboardResponse, tags=["Dashboard"])
def dashboard() -> DashboardResponse:
    holdings_items = _fetch_holdings_enriched()

    invested = sum(h.invested_value for h in holdings_items)
    current = sum(h.current_value for h in holdings_items if h.current_value is not None)
    total_pnl = round(current - invested, 2)

    # "Today's" P&L requires each holding's previous close, which Groww's
    # /v1/holdings/user and /v1/live-data/ltp responses don't include (ltp
    # only gives the current price). Getting a true day P&L would mean an
    # extra /v1/live-data/ohlc or /quote call per symbol. Left at 0 here to
    # avoid silently returning a wrong number - wire up get_ohlc() per
    # holding if you want this populated.
    todays_pnl = 0.0

    movers_source = [
        h for h in holdings_items if h.pnl_percent is not None
    ]
    sorted_by_pnl = sorted(movers_source, key=lambda h: h.pnl_percent, reverse=True)
    top_gainers = [
        MoverItem(symbol=h.symbol, pnl_percent=h.pnl_percent, pnl=h.pnl)
        for h in sorted_by_pnl[:5]
        if h.pnl_percent > 0
    ]
    top_losers = [
        MoverItem(symbol=h.symbol, pnl_percent=h.pnl_percent, pnl=h.pnl)
        for h in sorted(movers_source, key=lambda h: h.pnl_percent)[:5]
        if h.pnl_percent < 0
    ]

    allocation_raw = [
        {"label": h.symbol, "value": h.current_value or h.invested_value}
        for h in holdings_items
    ]
    allocation_slices = compute_allocation(allocation_raw, "value", "label")
    portfolio_allocation = [AllocationSlice(**a) for a in allocation_slices]

    # Sector allocation requires sector metadata Groww's holdings/LTP APIs
    # don't provide - left empty unless you enrich holdings with your own
    # symbol->sector mapping.
    sector_allocation: List[AllocationSlice] = []

    return DashboardResponse(
        current_portfolio_value=round(current, 2),
        todays_pnl=round(todays_pnl, 2),
        todays_pnl_percent=pnl_percent(current - todays_pnl, current) if current else 0.0,
        total_pnl=total_pnl,
        total_pnl_percent=pnl_percent(invested, current),
        invested_amount=round(invested, 2),
        current_amount=round(current, 2),
        top_gainers=top_gainers,
        top_losers=top_losers,
        portfolio_allocation=portfolio_allocation,
        sector_allocation=sector_allocation,
        generated_at=datetime.now(timezone.utc),
    )
