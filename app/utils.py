"""
Shared utilities: logging setup and small helper functions.
"""

import logging
import sys
from typing import Any, Optional

from .config import get_settings


def setup_logging() -> logging.Logger:
    """Configure and return the application's root logger.

    Logs go to stdout so they show up correctly in Vercel's function logs.
    Credentials are never logged - see redact() below, and callers must
    make sure they never pass secrets into log messages.
    """
    settings = get_settings()
    logger = logging.getLogger("portfolio_backend")

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)

    logger.setLevel(settings.LOG_LEVEL.upper())
    logger.propagate = False
    return logger


logger = setup_logging()


def redact(value: Optional[str], keep: int = 4) -> str:
    """Redact a secret for safe logging, e.g. 'abcd1234' -> 'abcd***'.

    Only ever use this if you truly need to reference a credential in logs
    (e.g. to confirm which key is configured) - prefer not logging it at all.
    """
    if not value:
        return "<empty>"
    if len(value) <= keep:
        return "*" * len(value)
    return f"{value[:keep]}{'*' * max(3, len(value) - keep)}"


def safe_float(value: Any, default: float = 0.0) -> float:
    """Best-effort conversion to float, falling back to `default`."""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pnl_percent(invested: float, current: float) -> float:
    """Percent change of current relative to invested, safe against div-by-zero."""
    if not invested:
        return 0.0
    return round(((current - invested) / invested) * 100, 2)


def compute_allocation(items: list, value_key: str, label_key: str) -> list:
    """Turn a list of dicts into allocation slices (label, value, percent).

    `items` entries missing value_key are treated as 0.
    """
    total = sum(safe_float(item.get(value_key)) for item in items)
    slices = []
    for item in items:
        value = safe_float(item.get(value_key))
        percent = round((value / total) * 100, 2) if total else 0.0
        slices.append(
            {
                "label": item.get(label_key) or "Unknown",
                "value": round(value, 2),
                "percent": percent,
            }
        )
    return slices
