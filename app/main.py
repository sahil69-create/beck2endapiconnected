"""
FastAPI application entrypoint.

Run locally with:
    uvicorn app.main:app --reload

On Vercel, this `app` object is imported directly by the Python runtime
(see vercel.json).
"""

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import get_settings
from .groww import (
    GrowwAPIError,
    GrowwAuthError,
    GrowwForbiddenError,
    GrowwRateLimitError,
    GrowwServerError,
)
from .routes import router
from .utils import logger

settings = get_settings()

app = FastAPI(
    title="Stock Portfolio Dashboard API",
    description="Personal backend that proxies the Groww Trading API for a portfolio dashboard.",
    version="1.0.0",
)

# ----------------------------------------------------------------------
# CORS - locked to your frontend origin(s) only.
# Set ALLOWED_ORIGINS in .env, e.g.:
#   ALLOWED_ORIGINS=https://your-frontend.vercel.app,http://localhost:3000
# ----------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS or [],
    allow_credentials=True,
    allow_methods=["GET"],
    allow_headers=["*"],
)

app.include_router(router)


# ----------------------------------------------------------------------
# Startup checks - log (not raise) so the app still boots and /health can
# report what's missing, rather than crashing the whole process.
# ----------------------------------------------------------------------
@app.on_event("startup")
def on_startup() -> None:
    issues = settings.validate()
    if issues:
        logger.warning("Starting with configuration issues: %s", "; ".join(issues))
    else:
        logger.info("Configuration OK. Environment=%s", settings.ENVIRONMENT)


# ----------------------------------------------------------------------
# Exception handlers - translate our typed Groww errors into clean JSON
# HTTP responses instead of leaking stack traces or raw Groww payloads.
# ----------------------------------------------------------------------
@app.exception_handler(GrowwAuthError)
async def handle_groww_auth_error(request: Request, exc: GrowwAuthError):
    logger.error("Groww auth error on %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=401,
        content={
            "error": "unauthorized",
            "detail": "Groww rejected the request - check ACCESS_TOKEN / API key & secret.",
            "status_code": 401,
        },
    )


@app.exception_handler(GrowwForbiddenError)
async def handle_groww_forbidden_error(request: Request, exc: GrowwForbiddenError):
    logger.error("Groww forbidden error on %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=403,
        content={
            "error": "forbidden",
            "detail": exc.message or "Not authorised to perform this operation.",
            "status_code": 403,
        },
    )


@app.exception_handler(GrowwRateLimitError)
async def handle_groww_rate_limit_error(request: Request, exc: GrowwRateLimitError):
    logger.warning("Groww rate limit hit on %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=429,
        content={
            "error": "rate_limited",
            "detail": "Groww API rate limit exceeded. Please slow down and retry shortly.",
            "status_code": 429,
        },
    )


@app.exception_handler(GrowwServerError)
async def handle_groww_server_error(request: Request, exc: GrowwServerError):
    logger.error("Groww server error on %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=502,
        content={
            "error": "upstream_error",
            "detail": "Groww API is currently having issues. Please try again later.",
            "status_code": 502,
        },
    )


@app.exception_handler(GrowwAPIError)
async def handle_groww_api_error(request: Request, exc: GrowwAPIError):
    logger.error("Groww API error on %s: %s", request.url.path, exc.message)
    return JSONResponse(
        status_code=400,
        content={
            "error": "groww_api_error",
            "detail": exc.message,
            "status_code": 400,
        },
    )


@app.exception_handler(Exception)
async def handle_unexpected_error(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "error": "internal_server_error",
            "detail": "An unexpected error occurred.",
            "status_code": 500,
        },
    )
