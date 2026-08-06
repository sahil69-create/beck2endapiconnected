"""
Pydantic models describing request/response shapes for this API.

These models describe OUR API's contract to the frontend. They are
intentionally decoupled from Groww's raw response shape (see groww.py),
so that if Groww changes their API, only the mapping code needs updating,
not every consumer of this backend.
"""

from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field 


class HealthResponse(BaseModel):
    status: str = Field(..., description="'ok' or 'degraded'")
    environment: str
    timestamp: datetime
    issues: List[str] = Field(
        default_factory=list,
        description="Configuration problems, e.g. missing env vars",
    )


class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    status_code: int


class Holding(BaseModel):
    symbol: str
    company_name: Optional[str] = None
    quantity: float
    average_price: float
    last_traded_price: Optional[float] = None
    invested_value: float
    current_value: Optional[float] = None
    pnl: Optional[float] = None
    pnl_percent: Optional[float] = None
    sector: Optional[str] = None
    exchange: Optional[str] = None


class HoldingsResponse(BaseModel):
    holdings: List[Holding]
    count: int


class Position(BaseModel):
    symbol: str
    quantity: float
    average_price: float
    last_traded_price: Optional[float] = None
    pnl: Optional[float] = None
    product: Optional[str] = None
    exchange: Optional[str] = None


class PositionsResponse(BaseModel):
    positions: List[Position]
    count: int


class Order(BaseModel):
    order_id: str
    symbol: str
    order_type: Optional[str] = None
    transaction_type: Optional[str] = None
    quantity: float
    price: Optional[float] = None
    status: Optional[str] = None
    order_time: Optional[str] = None


class OrdersResponse(BaseModel):
    orders: List[Order]
    count: int


class WatchlistItem(BaseModel):
    symbol: str
    company_name: Optional[str] = None
    last_traded_price: Optional[float] = None
    change_percent: Optional[float] = None


class WatchlistResponse(BaseModel):
    watchlist: List[WatchlistItem]
    count: int


class MarketPriceResponse(BaseModel):
    symbol: str
    last_traded_price: Optional[float] = None
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    previous_close: Optional[float] = None
    change: Optional[float] = None
    change_percent: Optional[float] = None
    raw: Optional[Dict[str, Any]] = Field(
        default=None, description="Raw passthrough fields from Groww, if any"
    )


class PortfolioResponse(BaseModel):
    invested_amount: float
    current_amount: float
    total_pnl: float
    total_pnl_percent: float
    holdings_count: int
    positions_count: int


class AllocationSlice(BaseModel):
    label: str
    value: float
    percent: float


class MoverItem(BaseModel):
    symbol: str
    company_name: Optional[str] = None
    pnl_percent: float
    pnl: float


class DashboardResponse(BaseModel):
    current_portfolio_value: float
    todays_pnl: float
    todays_pnl_percent: float
    total_pnl: float
    total_pnl_percent: float
    invested_amount: float
    current_amount: float
    top_gainers: List[MoverItem]
    top_losers: List[MoverItem]
    portfolio_allocation: List[AllocationSlice]
    sector_allocation: List[AllocationSlice] = Field(default_factory=list)
    generated_at: datetime
