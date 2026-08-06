# Stock Portfolio Dashboard — Backend

A FastAPI backend that proxies the [Groww Trading API](https://groww.in/trade-api/docs) to
power a personal stock portfolio dashboard. Built for Python 3.12.

> **Security note:** this backend never hardcodes credentials. It reads
> `GROWW_API_KEY`, `GROWW_API_SECRET`, and `ACCESS_TOKEN` from environment
> variables only (a local `.env` file in development, or your platform's
> environment variable settings in production). The API secret is never
> sent to, or readable by, the frontend — it only ever leaves this backend
> process when calling `https://api.groww.in` directly.

## Project structure

```
backend/
├── app/
│   ├── main.py       # FastAPI app, CORS, exception handlers
│   ├── config.py     # Env-var driven settings
│   ├── groww.py       # Groww API client: auth, retries, error handling
│   ├── routes.py      # All REST endpoints
│   ├── models.py      # Pydantic request/response schemas
│   └── utils.py       # Logging, safe parsing, allocation math
├── .env.example
├── requirements.txt
├── vercel.json
└── README.md
```

## Endpoints

| Method | Path                        | Description                                          |
| ------ | --------------------------- | ----------------------------------------------------- |
| GET    | `/health`                   | Config/health check (never leaks secret values)        |
| GET    | `/portfolio`                | Invested vs current amount, total P&L                  |
| GET    | `/holdings`                 | Demat holdings enriched with live price + P&L          |
| GET    | `/positions`                | Open positions (CASH segment by default)                |
| GET    | `/orders`                   | Today's order book                                      |
| GET    | `/watchlist`                | Live prices for symbols in `WATCHLIST_SYMBOLS`          |
| GET    | `/market-price/{symbol}`    | Live quote for one symbol, e.g. `/market-price/RELIANCE` |
| GET    | `/dashboard`                | Aggregated view (see below)                             |

`/dashboard` response includes: current portfolio value, total P&L, invested
amount, current amount, top gainers/losers, and portfolio allocation by
holding. Two fields are intentionally conservative because Groww's API
doesn't provide the data needed to compute them accurately:

- **`todays_pnl`** — Groww's holdings/LTP endpoints don't return a
  previous-close per holding, so this is `0` unless you extend
  `_fetch_holdings_enriched()` in `routes.py` to call `get_ohlc()` per symbol.
- **`sector_allocation`** — Groww doesn't return sector metadata for
  holdings. This stays empty unless you supply your own symbol→sector
  mapping.

`/watchlist` is similarly a documented approximation: Groww's public API has
no "fetch my saved watchlist" endpoint, so this route works off a symbol
list you configure yourself via `WATCHLIST_SYMBOLS` in `.env`.

## Setup

1. **Install Python 3.12** and create a virtual environment:
   ```bash
   python3.12 -m venv venv
   source venv/bin/activate    # Windows: venv\Scripts\activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Create your `.env` file:**
   ```bash
   cp .env.example .env
   ```
   Then edit `.env` and paste in your **own** credentials:
   - Either `ACCESS_TOKEN` (generated daily from your Groww account's
     Trading APIs page), **or**
   - `GROWW_API_KEY` + `GROWW_API_SECRET` (the backend will exchange these
     for an access token automatically using Groww's checksum-based
     "approval" flow — see `app/groww.py`).

   Also set `ALLOWED_ORIGINS` to your actual frontend URL(s).

   **Never commit `.env` to git.** `.env` should already be in your
   `.gitignore` — double check before your first commit.

4. **Run locally:**
   ```bash
   uvicorn app.main:app --reload
   ```
   Visit `http://localhost:8000/health` and `http://localhost:8000/docs`
   (interactive Swagger UI).

## How authentication works

Groww supports three ways to authenticate (see their
[docs](https://groww.in/trade-api/docs/curl)):

1. **Access Token** — generated manually from your Groww account, expires
   daily at 6:00 AM IST.
2. **API Key + Secret ("approval" flow)** — this backend computes a
   SHA-256 checksum of `secret + timestamp` and exchanges it for a fresh
   access token on demand. Requires you to approve API access for the
   day on Groww's dashboard.
3. **API Key + TOTP** — not wired up here, but `app/groww.py` is
   structured so you can add it alongside the approval flow if needed.

The backend prefers `ACCESS_TOKEN` if set, and falls back to generating one
from `GROWW_API_KEY`/`GROWW_API_SECRET` otherwise.

## Error handling

`app/groww.py` distinguishes:

- **401 Unauthorized** → credentials missing/invalid/expired. No retry.
- **403 Forbidden** → authenticated but not permitted. No retry.
- **429 Rate limited** → retried with backoff, then surfaced as HTTP 429.
- **5xx server errors** → retried with backoff, then surfaced as HTTP 502.
- **Network/timeout errors** → retried with backoff.

All Groww error responses are translated into a consistent JSON error shape
by the exception handlers in `app/main.py`:
```json
{ "error": "rate_limited", "detail": "...", "status_code": 429 }
```

Groww's own rate limits (per their docs, subject to change):

| Type        | Per second | Per minute |
| ----------- | ---------- | ---------- |
| Orders      | 10         | 250        |
| Live Data   | 10         | 300        |
| Non-trading (holdings, positions, order list) | 20 | 500 |

## CORS

Configured in `app/main.py` via `ALLOWED_ORIGINS` — only the origins you
list there can call this API from a browser. There is no wildcard `*`
fallback; if `ALLOWED_ORIGINS` is empty, no browser origin will be allowed
(server-to-server/API clients are unaffected).

## Deployment (Vercel)

This repo is Vercel-ready via `vercel.json`, which points Vercel's Python
runtime at `app/main.py`.

1. **Push this `backend/` folder to a GitHub repository.**

2. **Import the repo in Vercel** (vercel.com → New Project → your repo).
   - If `backend/` is a subfolder of a larger repo, set Vercel's
     **Root Directory** to `backend`.

3. **Set environment variables** in Vercel's Project Settings →
   Environment Variables (never in vercel.json, never in code):
   - `ACCESS_TOKEN` or (`GROWW_API_KEY` + `GROWW_API_SECRET`)
   - `ALLOWED_ORIGINS` (your deployed frontend URL)
   - `WATCHLIST_SYMBOLS` (optional)
   - Any of the HTTP-tuning vars from `.env.example` you want to override

4. **Deploy.** Vercel installs `requirements.txt` and serves the FastAPI
   `app` object as a single serverless function.

5. **Verify:** hit `https://<your-project>.vercel.app/health` — it should
   report `"status": "ok"` with no `issues` once env vars are set correctly.

### Notes on Vercel + daily access tokens

If you use a manually-generated `ACCESS_TOKEN` (Option A), remember it
expires daily — you'll need to update the Vercel environment variable
each day, or switch to the API key/secret approval flow (Option B), which
this backend can refresh automatically as long as you keep approving API
access on Groww's side for the day.

## Extending this backend

- **Sector allocation:** add a `SYMBOL_SECTOR_MAP` (or a small JSON/CSV
  file) and join it against holdings in `routes.py`.
- **Today's P&L:** call `client.get_ohlc()` (add this method to
  `GrowwClient`, mirroring `get_ltp`) per holding symbol and diff against
  `previous_close`.
- **Caching:** consider caching `/v1/live-data/ltp` responses for a few
  seconds if you expect frequent dashboard polling, to stay well under
  Groww's rate limits.
