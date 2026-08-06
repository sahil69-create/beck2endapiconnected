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
from typing import Any, Dict, List, Optional

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
def _norm_exchange(exchange_raw: Any) -> str:
    """Normalize an exchange label like 'NSE', 'NSE_EQ', 'BSE_EQ', 'BSE', '' -> 'NSE'|'BSE'."""
    raw = str(exchange_raw or "").strip().upper()
    if "BSE" in raw:
        return "BSE"
    if raw and raw != "NAN":
        return "NSE"  # treat anything non-empty (incl. "NSE") as NSE
    return "NSE"


def _lookup_ltp(ltp_map: Dict[str, float], symbol: str, exchange: str) -> Optional[float]:
    """Try multiple key formats to find an LTP in the map."""
    if not symbol or not ltp_map:
        return None
    candidates = [
        f"{exchange}_{symbol}",
        f"{exchange}_{symbol.upper()}",
        f"NSE_{symbol}",
        f"BSE_{symbol}",
        symbol,
        symbol.upper(),
    ]
    for key in candidates:
        price = ltp_map.get(key)
        if price is not None and price > 0:
            return float(price)
    return None


def _fetch_holdings_enriched() -> List[Holding]:
    """Fetch holdings from Groww and enrich each with live price + P&L.

    For each holding we:
      1. Read exchange + trading_symbol + quantity + average_price from raw record.
      2. First try to pull LTP / current_price / last_traded_price directly from
         the raw holding record (Groww's /holdings/user often embeds live data).
      3. Fall back to a bulk /live-data/ltp call using the correct exchange.
      4. Only then fall back to avg_price so current ≈ invested (better than 0).
    """
    client = get_groww_client()
    raw_holdings = client.get_holdings()

    if not raw_holdings:
        return []

    # --- Pass 1: Read raw holding fields, collect those still needing LTP ---
    per_symbol_exchange: List[tuple] = []
    prefilled: Dict[str, float] = {}
    for h in raw_holdings:
        symbol = str(h.get("trading_symbol") or h.get("symbol") or "").strip()
        if not symbol:
            continue
        exchange = _norm_exchange(h.get("exchange"))
        in_record_price: Optional[float] = None
        for key in (
            "last_traded_price",
            "ltp",
            "current_price",
            "market_price",
            "price",
        ):
            if h.get(key) not in (None, ""):
                cand = safe_float(h.get(key), None)
                if cand and cand > 0:
                    in_record_price = cand
                    break
        # Sometimes "current_value" is set but per-unit price isn't — derive it.
        if in_record_price is None:
            qty = safe_float(h.get("quantity"))
            cv = safe_float(h.get("current_value"), None)
            if qty and cv and cv > 0:
                in_record_price = round(cv / qty, 4)
        if in_record_price:
            prefilled[f"{exchange}_{symbol}"] = in_record_price
        else:
            per_symbol_exchange.append((symbol, exchange))

    # --- Pass 2: Bulk LTP call for symbols that didn't have an inline price ---
    ltp_map: Dict[str, float] = dict(prefilled)
    if per_symbol_exchange:
        exchange_symbols = [f"{ex}_{sym}" for sym, ex in per_symbol_exchange]
        try:
            batch = client.get_ltp(exchange_symbols)
            if batch:
                ltp_map.update(batch)
        except Exception as exc:  # noqa: BLE001 - degrade gracefully
            logger.warning("Failed to fetch LTPs batch for %d holdings: %s",
                           len(exchange_symbols), exc)

    # --- Pass 3: Build enriched holdings ---
    holdings: List[Holding] = []
    for h in raw_holdings:
        symbol = str(h.get("trading_symbol") or h.get("symbol") or "").strip()
        if not symbol:
            continue
        exchange = _norm_exchange(h.get("exchange"))
        quantity = safe_float(h.get("quantity"))
        avg_price = safe_float(h.get("average_price"))
        if avg_price <= 0 and quantity > 0:
            iv = safe_float(h.get("invested_value"), None)
            if iv and iv > 0:
                avg_price = round(iv / quantity, 4)
        invested_value = round(quantity * avg_price, 2)

        ltp = _lookup_ltp(ltp_map, symbol, exchange)
        if ltp is None or ltp <= 0:
            # Last resort: match current_value/quantity or fall back to avg_price.
            qty = quantity if quantity else 1
            cv = safe_float(h.get("current_value"), None)
            if cv and cv > 0 and qty:
                ltp = round(cv / qty, 4)
            else:
                ltp = avg_price if avg_price > 0 else None

        current_value = round(quantity * ltp, 2) if ltp is not None else invested_value
        pnl = round(current_value - invested_value, 2)

        # Try to pull a human-readable company name from raw holding fields.
        company_name = (
            h.get("company_name")
            or h.get("script_name")
            or h.get("scrip_name")
            or h.get("name")
            or h.get("display_name")
            or None
        )
        if company_name:
            company_name = str(company_name).strip() or None

        holdings.append(
            Holding(
                symbol=symbol,
                company_name=company_name,
                quantity=quantity,
                average_price=avg_price,
                last_traded_price=ltp,
                invested_value=invested_value,
                current_value=current_value,
                pnl=pnl,
                pnl_percent=pnl_percent(invested_value, current_value),
                sector=(h.get("sector") or None),
                exchange=exchange,
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
    ltp_map: Dict[str, float] = {}

    if raw_positions:
        per_symbol_exchange = []
        for p in raw_positions:
            sym = str(p.get("trading_symbol") or p.get("symbol") or "").strip()
            if not sym:
                continue
            ex = _norm_exchange(p.get("exchange"))
            in_record_price = None
            for key in ("last_traded_price", "ltp", "current_price", "market_price", "price"):
                cand = safe_float(p.get(key), None)
                if cand and cand > 0:
                    in_record_price = cand
                    break
            if in_record_price:
                ltp_map[f"{ex}_{sym}"] = in_record_price
            else:
                per_symbol_exchange.append((sym, ex))
        if per_symbol_exchange:
            try:
                batch = client.get_ltp([f"{ex}_{sym}" for sym, ex in per_symbol_exchange])
                if batch:
                    ltp_map.update(batch)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to fetch LTPs for positions: %s", exc)

    items = []
    for p in raw_positions:
        symbol = str(p.get("trading_symbol") or p.get("symbol") or "").strip()
        if not symbol:
            continue
        ex = _norm_exchange(p.get("exchange"))
        ltp = _lookup_ltp(ltp_map, symbol, ex)
        if ltp is None or ltp <= 0:
            for key in ("last_traded_price", "ltp", "current_price", "price"):
                cand = safe_float(p.get(key), None)
                if cand and cand > 0:
                    ltp = cand
                    break
        if ltp is None or ltp <= 0:
            ltp = safe_float(p.get("net_price"), None)
            if not ltp:
                ltp = None
        items.append(
            Position(
                symbol=symbol,
                quantity=safe_float(p.get("quantity")),
                average_price=safe_float(p.get("net_price")),
                last_traded_price=ltp,
                pnl=safe_float(p.get("realised_pnl")),
                product=p.get("product"),
                exchange=ex,
            )
        )
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
#
# Each symbol can be prefixed with "BSE:" to force the BSE exchange.
# ----------------------------------------------------------------------
@router.get("/watchlist", response_model=WatchlistResponse, tags=["Portfolio"])
def watchlist() -> WatchlistResponse:
    raw_items = [s.strip() for s in os.getenv("WATCHLIST_SYMBOLS", "").split(",") if s.strip()]

    if not raw_items:
        return WatchlistResponse(watchlist=[], count=0)

    client = get_groww_client()

    parsed = []
    for item in raw_items:
        item_upper = item.upper()
        if item_upper.startswith("BSE:"):
            parsed.append((item[4:].strip(), "BSE"))
        elif item_upper.startswith("NSE:"):
            parsed.append((item[4:].strip(), "NSE"))
        else:
            parsed.append((item, "NSE"))

    exchange_symbols = [f"{ex}_{sym}" for sym, ex in parsed]
    ltp_map: Dict[str, float] = {}
    try:
        ltp_map = client.get_ltp(exchange_symbols)
    except Exception as exc:  # noqa: BLE001
        logger.warning("watchlist LTP batch failed: %s", exc)

    items = []
    for symbol, ex in parsed:
        ltp = _lookup_ltp(ltp_map, symbol, ex)
        change_percent = None
        if isinstance(ltp, (list, tuple)) and len(ltp) >= 2:
            change_percent = safe_float(ltp[1], None)
            ltp = safe_float(ltp[0], None)
        if ltp is None or change_percent is None:
            try:
                q = client.get_quote(trading_symbol=symbol, exchange=ex)
                if ltp is None:
                    ltp = safe_float(q.get("last_price"), None)
                if change_percent is None:
                    change_percent = safe_float(q.get("day_change_perc"), None)
                    if change_percent is None:
                        day_change = safe_float(q.get("day_change"), None)
                        if day_change is not None and ltp:
                            prev_close = ltp - day_change
                            if prev_close:
                                change_percent = round((day_change / prev_close) * 100, 2)
            except Exception as exc:  # noqa: BLE001
                logger.warning("watchlist quote failed for %s:%s: %s", ex, symbol, exc)
        items.append(
            WatchlistItem(
                symbol=symbol,
                last_traded_price=ltp,
                change_percent=change_percent,
            )
        )
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

    invested = sum(float(h.invested_value or 0.0) for h in holdings_items)
    current = sum(float(h.current_value or h.invested_value or 0.0) for h in holdings_items)
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
def _resolve_prev_close(holding: Holding, raw_holding: Any, client) -> Optional[float]:
    """Return yesterday's close for a single holding.

    Tries, in order:
      1. Fields already present on the raw holding record (yesterday_close,
         previous_close, prev_close, yesterday_price, day_before_price).
      2. A /v1/live-data/quote call that returns ohlc.close = previous close.
    """
    from .utils import safe_float as sf

    if isinstance(raw_holding, dict):
        for key in (
            "yesterday_close",
            "previous_close",
            "prev_close",
            "previous_day_close",
            "yesterday_price",
            "prev_price",
            "close",
            "day_close",
        ):
            cand = sf(raw_holding.get(key), None)
            if cand and cand > 0:
                return cand
        # Sometimes day_change is present; derive prev_close = ltp - day_change
        ltp_raw = holding.last_traded_price
        if ltp_raw and ltp_raw > 0:
            for dk in ("day_change", "change", "today_change", "change_amount"):
                dc = sf(raw_holding.get(dk), None)
                if dc is not None:
                    cand = ltp_raw - dc
                    if cand > 0:
                        return cand
    # Fallback: quote call (cheapest single-call source of prev_close)
    try:
        ex = holding.exchange or "NSE"
        q = client.get_quote(trading_symbol=holding.symbol, exchange=ex)
        ohlc = q.get("ohlc") if isinstance(q, dict) else None
        if isinstance(ohlc, dict):
            cand = sf(ohlc.get("close"), None)
            if cand and cand > 0:
                return cand
        # Some schemas place previous_close at the root
        if isinstance(q, dict):
            for pk in ("previous_close", "prev_close", "yesterday_close", "last_day_close"):
                cand = sf(q.get(pk), None)
                if cand and cand > 0:
                    return cand
    except Exception as exc:  # noqa: BLE001
        logger.warning("prev_close quote failed for %s:%s: %s",
                       holding.exchange, holding.symbol, exc)
    return None


@router.get("/dashboard", response_model=DashboardResponse, tags=["Dashboard"])
def dashboard() -> DashboardResponse:
    client = get_groww_client()
    raw_holdings = client.get_holdings()
    holdings_items = _fetch_holdings_enriched()

    # Build a raw-holding lookup keyed by (exchange, symbol) for prev_close resolution.
    raw_by_key: Dict[tuple, Any] = {}
    if isinstance(raw_holdings, list):
        for rh in raw_holdings:
            sym = str(rh.get("trading_symbol") or rh.get("symbol") or "").strip()
            ex = _norm_exchange(rh.get("exchange"))
            if sym:
                raw_by_key[(ex, sym)] = rh

    invested = sum(float(h.invested_value or 0.0) for h in holdings_items)
    current = sum(float(h.current_value or h.invested_value or 0.0) for h in holdings_items)
    total_pnl = round(current - invested, 2)

    # Today's P&L: sum over holdings of (ltp - prev_close) * qty
    todays_pnl = 0.0
    yesterdays_value = 0.0
    for h in holdings_items:
        if h.last_traded_price is None or h.quantity is None:
            continue
        rh = raw_by_key.get(((h.exchange or "NSE"), h.symbol))
        prev_close = _resolve_prev_close(h, rh, client)
        if prev_close and prev_close > 0:
            today_pnl_for_h = round((h.last_traded_price - prev_close) * h.quantity, 2)
            todays_pnl += today_pnl_for_h
            yesterdays_value += prev_close * h.quantity
    todays_pnl = round(todays_pnl, 2)
    todays_base = yesterdays_value if yesterdays_value > 0 else (current - todays_pnl)

    movers_source = [
        h for h in holdings_items if h.pnl_percent is not None
    ]
    sorted_by_pnl = sorted(movers_source, key=lambda h: h.pnl_percent, reverse=True)
    top_gainers = [
        MoverItem(
            symbol=h.symbol,
            company_name=h.company_name,
            pnl_percent=h.pnl_percent,
            pnl=h.pnl,
        )
        for h in sorted_by_pnl[:5]
        if h.pnl_percent and h.pnl_percent > 0
    ]
    top_losers = [
        MoverItem(
            symbol=h.symbol,
            company_name=h.company_name,
            pnl_percent=h.pnl_percent,
            pnl=h.pnl,
        )
        for h in sorted(movers_source, key=lambda h: h.pnl_percent or 0)[:5]
        if h.pnl_percent and h.pnl_percent < 0
    ]

    allocation_raw = [
        {
            "label": h.company_name or h.symbol,
            "value": float(h.current_value or h.invested_value or 0.0),
        }
        for h in holdings_items
    ]
    allocation_slices = compute_allocation(allocation_raw, "value", "label")
    portfolio_allocation = [AllocationSlice(**a) for a in allocation_slices]

    sector_allocation: List[AllocationSlice] = []

    return DashboardResponse(
        current_portfolio_value=round(current, 2),
        todays_pnl=todays_pnl,
        todays_pnl_percent=pnl_percent(todays_base, todays_base + todays_pnl) if todays_base else 0.0,
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
