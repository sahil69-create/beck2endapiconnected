"""
Configuration module.

Loads all configuration from environment variables (via a local .env file
during development, or real environment variables in production/Vercel).

IMPORTANT: Never hardcode credentials here. Nothing in this file should
contain a real API key, secret, or token.
"""

import os
from functools import lru_cache
from typing import List

from dotenv import load_dotenv

# Load .env file if present (no-op in environments where env vars are
# already injected, e.g. Vercel project settings).
load_dotenv()


def _split_csv(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class Settings:
    """Application settings, populated exclusively from environment variables."""

    def __init__(self) -> None:
        # --- Groww API credentials (never log or return these) ---
        self.GROWW_API_KEY: str = os.getenv("GROWW_API_KEY", "")
        self.GROWW_API_SECRET: str = os.getenv("GROWW_API_SECRET", "")
        self.ACCESS_TOKEN: str = os.getenv("ACCESS_TOKEN", "")

        # --- Groww API base URL (overridable for sandbox/testing) ---
        self.GROWW_API_BASE_URL: str = os.getenv(
            "GROWW_API_BASE_URL", "https://api.groww.in"
        )

        # --- CORS: comma separated list of allowed origins, e.g.
        # "https://myportfolio.vercel.app,https://myportfolio.com"
        self.ALLOWED_ORIGINS: List[str] = _split_csv(
            os.getenv("ALLOWED_ORIGINS", "")
        )

        # --- HTTP client behaviour ---
        self.REQUEST_TIMEOUT_SECONDS: float = float(
            os.getenv("REQUEST_TIMEOUT_SECONDS", "10")
        )
        self.MAX_RETRIES: int = int(os.getenv("MAX_RETRIES", "3"))
        self.RETRY_BACKOFF_SECONDS: float = float(
            os.getenv("RETRY_BACKOFF_SECONDS", "0.5")
        )

        # --- Misc ---
        self.ENVIRONMENT: str = os.getenv("ENVIRONMENT", "development")
        self.LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

    def validate(self) -> List[str]:
        """Return a list of human-readable problems with the current config.

        Does not raise so that /health can still respond and report what's
        missing, rather than the whole app failing to boot.
        """
        problems: List[str] = []
        if not self.GROWW_API_KEY:
            problems.append("GROWW_API_KEY is not set")
        if not self.GROWW_API_SECRET:
            problems.append("GROWW_API_SECRET is not set")
        if not self.ACCESS_TOKEN:
            problems.append("ACCESS_TOKEN is not set")
        if not self.ALLOWED_ORIGINS:
            problems.append(
                "ALLOWED_ORIGINS is not set - CORS will block all browser requests"
            )
        return problems


@lru_cache
def get_settings() -> Settings:
    """Cached settings accessor so we parse env vars once per process."""
    return Settings()
