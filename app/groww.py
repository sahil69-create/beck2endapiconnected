"""
Reusable Groww API client.

Wraps Groww's REST API (https://groww.in/trade-api/docs/curl) with:
  - Bearer token authentication (using a pre-generated ACCESS_TOKEN, or
    auto-generating one from GROWW_API_KEY + GROWW_API_SECRET)
  - Retry with backoff for transient failures
  - Explicit handling of 401 / 403 / 429 / 5xx
  - Timeouts
  - Structured logging (never logs secrets)

Reference endpoints used (verify against current Groww docs before relying
on this in production, as third-party APIs evolve):
  GET  /v1/holdings/user
  GET  /v1/positions/user
  GET  /v1/order/list
  GET  /v1/live-data/ltp
  POST /v1/token/api/access   (only used if no ACCESS_TOKEN is supplied)
"""

from __future__ import annotations

import hashlib
import time
from typing import Any, Dict, List, Optional

import httpx

from .config import get_settings
from .utils import logger, redact


class GrowwAPIError(Exception):
    """Raised for any non-2xx response from Groww, after retries are exhausted."""

    def __init__(self, status_code: int, message: str, code: Optional[str] = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f"[{status_code}] {code or ''} {message}".strip())


class GrowwAuthError(GrowwAPIError):
    """401 Unauthorized - credentials missing, invalid, or expired."""


class GrowwForbiddenError(GrowwAPIError):
    """403 Forbidden - authenticated but not permitted to perform this action."""


class GrowwRateLimitError(GrowwAPIError):
    """429 Too Many Requests."""


class GrowwServerError(GrowwAPIError):
    """5xx - problem on Groww's side."""


class GrowwClient:
    """Thin, resilient wrapper around the Groww REST API."""

    API_VERSION = "1.0"

    def __init__(self) -> None:
        self.settings = get_settings()
        self.base_url = self.settings.GROWW_API_BASE_URL.rstrip("/")
        self._access_token: Optional[str] = self.settings.ACCESS_TOKEN or None
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=self.settings.REQUEST_TIMEOUT_SECONDS,
        )

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------
    def _generate_checksum(self, timestamp: str) -> str:
        """SHA-256(api_secret + timestamp), per Groww's checksum spec."""
        raw = f"{self.settings.GROWW_API_SECRET}{timestamp}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _ensure_access_token(self) -> str:
        """Return a usable access token, generating one via the API-key/secret
        approval flow if the caller hasn't supplied ACCESS_TOKEN directly.

        Note: the "approval" flow requires the API key to have been approved
        for API access in the Groww dashboard for the current day. If that
        approval hasn't been granted, Groww will reject this call with 401 -
        that's expected and not a bug in this client.
        """
        if self._access_token:
            return self._access_token

        if not (self.settings.GROWW_API_KEY and self.settings.GROWW_API_SECRET):
            raise GrowwAuthError(
                401,
                "No ACCESS_TOKEN configured and GROWW_API_KEY/GROWW_API_SECRET "
                "are missing - cannot authenticate with Groww.",
            )

        timestamp = str(int(time.time()))
        checksum = self._generate_checksum(timestamp)

        logger.info(
            "Requesting a new Groww access token (api_key=%s)",
            redact(self.settings.GROWW_API_KEY),
        )
        try:
            response = self._client.post(
                "/v1/token/api/access",
                headers={
                    "Authorization": f"Bearer {self.settings.GROWW_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "key_type": "approval",
                    "checksum": checksum,
                    "timestamp": timestamp,
                },
            )
        except httpx.TimeoutException as exc:
            raise GrowwAPIError(504, "Timed out generating Groww access token") from exc
        except httpx.RequestError as exc:
            raise GrowwAPIError(502, f"Network error generating access token: {exc}") from exc

        if response.status_code != 200:
            self._raise_for_status(response)

        data = response.json()
        token = data.get("token")
        if not token:
            raise GrowwAuthError(401, "Groww did not return a token in the response")

        self._access_token = token
        logger.info("Obtained new Groww access token, expiry=%s", data.get("expiry"))
        return token

    # ------------------------------------------------------------------
    # Core request machinery
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._ensure_access_token()}",
            "X-API-VERSION": self.API_VERSION,
        }

    def _raise_for_status(self, response: httpx.Response) -> None:
        """Translate a non-2xx Groww response into a typed exception."""
        status = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}

        error = body.get("error") or {}
        message = error.get("message") or response.text or "Unknown error"
        code = error.get("code")

        if status == 401:
            raise GrowwAuthError(status, message, code)
        if status == 403:
            raise GrowwForbiddenError(status, message, code)
        if status == 429:
            raise GrowwRateLimitError(status, message, code)
        if status >= 500:
            raise GrowwServerError(status, message, code)
        raise GrowwAPIError(status, message, code)

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Make a request to Groww with retry + backoff on transient errors.

        Retries on: network/timeout errors, 429, and 5xx.
        Does NOT retry on: 401, 403, or other 4xx (these won't succeed by retrying).
        """
        last_exc: Optional[Exception] = None

        for attempt in range(1, self.settings.MAX_RETRIES + 1):
            try:
                response = self._client.request(
                    method,
                    path,
                    params=params,
                    json=json_body,
                    headers=self._headers(),
                )
            except httpx.TimeoutException as exc:
                last_exc = exc
                logger.warning(
                    "Timeout calling Groww %s %s (attempt %d/%d)",
                    method, path, attempt, self.settings.MAX_RETRIES,
                )
            except httpx.RequestError as exc:
                last_exc = exc
                logger.warning(
                    "Network error calling Groww %s %s (attempt %d/%d): %s",
                    method, path, attempt, self.settings.MAX_RETRIES, exc,
                )
            else:
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("status") == "FAILURE":
                        # Groww returns 200 with status=FAILURE in some cases
                        self._raise_for_status(response)
                    return payload.get("payload", payload)

                if response.status_code in (429,) or response.status_code >= 500:
                    logger.warning(
                        "Groww %s %s returned %d (attempt %d/%d)",
                        method, path, response.status_code, attempt,
                        self.settings.MAX_RETRIES,
                    )
                    last_exc = None
                    if attempt == self.settings.MAX_RETRIES:
                        self._raise_for_status(response)
                else:
                    # 401/403/other 4xx - fail fast, no retry
                    self._raise_for_status(response)

            if attempt < self.settings.MAX_RETRIES:
                time.sleep(self.settings.RETRY_BACKOFF_SECONDS * attempt)

        if last_exc:
            raise GrowwAPIError(504, f"Groww request failed after retries: {last_exc}")
        raise GrowwAPIError(500, "Groww request failed after retries for an unknown reason")

    # ------------------------------------------------------------------
    # Public API methods
    # ------------------------------------------------------------------
    def get_holdings(self) -> List[Dict[str, Any]]:
        payload = self._request("GET", "/v1/holdings/user")
        return payload.get("holdings", [])

    def get_positions(self, segment: str = "CASH") -> List[Dict[str, Any]]:
        payload = self._request(
            "GET", "/v1/positions/user", params={"segment": segment}
        )
        return payload.get("positions", [])

    def get_order_list(
        self, segment: str = "CASH", page: int = 0, page_size: int = 100
    ) -> List[Dict[str, Any]]:
        payload = self._request(
            "GET",
            "/v1/order/list",
            params={"segment": segment, "page": page, "page_size": page_size},
        )
        return payload.get("order_list", [])

    def get_ltp(
        self, exchange_symbols: List[str], segment: str = "CASH"
    ) -> Dict[str, float]:
        """exchange_symbols look like 'NSE_RELIANCE', 'BSE_SENSEX'. Max 50 per call.

        Normalizes multiple Groww response shapes into a flat {symbol: ltp} dict:
          - {NSE_X: price, NSE_Y: price}                 (flat)
          - {"data": [{symbol, ltp}, ...]}                (list-of-records)
          - {"prices": {NSE_X: price, ...}}               (nested "prices")
          - {"market_data": [{exchange_symbol, price}...]}
          - Any list of {symbol, last_price|ltp|price} records
        """
        from .utils import safe_float

        if not exchange_symbols:
            return {}
        raw = self._request(
            "GET",
            "/v1/live-data/ltp",
            params={
                "segment": segment,
                "exchange_symbols": ",".join(exchange_symbols),
            },
        )
        result: Dict[str, float] = {}
        if isinstance(raw, dict):
            for _key in ("prices", "market_data", "payload"):
                if isinstance(raw.get(_key), dict):
                    raw = raw[_key]
                    break
            if isinstance(raw, dict):
                list_of_records = raw.get("data")
            else:
                list_of_records = None
        else:
            list_of_records = raw if isinstance(raw, list) else None

        if isinstance(raw, dict):
            for k, v in raw.items():
                if isinstance(v, (int, float, str)):
                    price = safe_float(v, None)
                    if price is not None:
                        result[str(k)] = price

        if list_of_records and isinstance(list_of_records, list):
            for rec in list_of_records:
                if not isinstance(rec, dict):
                    continue
                sym = (
                    rec.get("exchange_symbol")
                    or rec.get("symbol")
                    or rec.get("trading_symbol")
                )
                price = None
                for pk in ("ltp", "last_price", "last_traded_price", "price", "value"):
                    price = safe_float(rec.get(pk), None)
                    if price is not None:
                        break
                if sym and price is not None:
                    result[str(sym)] = price
        return result

    def get_quote(
        self, trading_symbol: str, exchange: str = "NSE", segment: str = "CASH"
    ) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/v1/live-data/quote",
            params={
                "exchange": exchange,
                "segment": segment,
                "trading_symbol": trading_symbol,
            },
        )


# Module-level singleton, created lazily on first use so import-time never
# fails just because env vars aren't set yet (important for /health).
_client: Optional[GrowwClient] = None


def get_groww_client() -> GrowwClient:
    global _client
    if _client is None:
        _client = GrowwClient()
    return _client
