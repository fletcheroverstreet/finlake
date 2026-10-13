"""Configuration. Everything the rest of the package needs to find things."""

from __future__ import annotations

import os
from pathlib import Path


def _load_dotenv() -> None:
    """Read `.env` into the environment, without taking a dependency.

    `.env.example` has always told you to copy it to `.env` and put your
    credentials there — but nothing ever read the file. Every credential had
    to ALSO be exported into the shell, and the failure mode was silent: a
    FRED key sitting correctly in `.env` produced "Set FRED_API_KEY", and an
    unset SEC user-agent produced a 403 from the SEC rather than anything
    naming the cause.

    Real environment variables win, so an explicit export still overrides the
    file. Values may be quoted; `export ` prefixes and `#` comments are
    tolerated because people paste those in.
    """
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"):
        if not candidate.exists():
            continue
        try:
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                line = line.removeprefix("export ").strip()
                key, sep, value = line.partition("=")
                if not sep:
                    continue
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                # setdefault: a real environment variable beats the file.
                if key and value:
                    os.environ.setdefault(key, value)
        except OSError:
            continue
        break


_load_dotenv()

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
# Override with FINLAKE_HOME=/some/path if you want the cache somewhere else.
DATA_DIR = Path(os.environ.get("FINLAKE_HOME", Path.home() / ".finlake"))
DB_PATH = DATA_DIR / "finlake.db"
PRICE_DIR = DATA_DIR / "prices"          # one parquet file per ticker
RAW_DIR = DATA_DIR / "raw"               # raw API responses, gzipped

# ---------------------------------------------------------------------------
# SEC EDGAR
# ---------------------------------------------------------------------------
# The SEC *requires* a User-Agent with a real contact address. Requests without
# one get 403'd, and abusive clients get IP-banned. Set this before you fetch.
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT", "finlake/0.1 (set SEC_USER_AGENT env var to your email)"
)
SEC_RATE_LIMIT = 8.0        # requests/sec. SEC's stated ceiling is 10; leave headroom.
SEC_BASE = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"

# ---------------------------------------------------------------------------
# FRED / ALFRED
# ---------------------------------------------------------------------------
FRED_API_KEY = os.environ.get("FRED_API_KEY", "")
FRED_BASE = "https://api.stlouisfed.org/fred"
FRED_RATE_LIMIT = 8.0

# ---------------------------------------------------------------------------
# Freshness policy: how old a cached resource can be before we refetch.
# ---------------------------------------------------------------------------
STALE_AFTER_DAYS = {
    "company_tickers": 7,
    "companyfacts": 1,
    "submissions": 1,
    "prices": 1,
    "macro": 1,
}


def ensure_dirs() -> None:
    for d in (DATA_DIR, PRICE_DIR, RAW_DIR):
        d.mkdir(parents=True, exist_ok=True)
