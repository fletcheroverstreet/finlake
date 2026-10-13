"""FRED macro series, vintage-aware.

Most people call the FRED observations endpoint with default parameters, get
the fully-revised series, and quietly build lookahead bias into every macro
signal they test. Q1 2020 GDP was first printed at -4.8%; the number you see
today is -5.1%, and neither is what a trader had on April 29, 2020 at 8:29am.

The fix is one parameter. `realtime_start=1776-07-04&realtime_end=9999-12-31`
switches FRED into ALFRED mode and returns every vintage of every observation,
each stamped with the date it went public. That stamp is our PIT key.
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from .. import config, store
from ..http_client import get_json

# Useful defaults for a macro sensitivity layer.
DEFAULT_SERIES = [
    # Rates and the yield curve
    "DGS3MO",      # 3M Treasury
    "DGS2",        # 2Y Treasury
    "DGS10",       # 10Y Treasury
    "DGS30",       # 30Y Treasury
    "T10Y2Y",      # 10Y-2Y spread — the recession bellwether
    "T10Y3M",      # 10Y-3M spread
    "FEDFUNDS",    # Fed funds
    "MORTGAGE30US",  # 30Y mortgage
    # Inflation
    "CPIAUCSL",    # CPI, all urban
    "CPILFESL",    # Core CPI
    "PCEPILFE",    # Core PCE — the Fed's actual target
    # Activity and labour
    "UNRATE",      # Unemployment
    "PAYEMS",      # Nonfarm payrolls
    "GDPC1",       # Real GDP
    "INDPRO",      # Industrial production
    "HOUST",       # Housing starts
    "RSAFS",       # Retail sales
    "UMCSENT",     # Consumer sentiment
    # Markets and risk
    "VIXCLS",      # VIX
    "BAMLH0A0HYM2",  # High-yield OAS — credit stress
    "DCOILWTICO",  # WTI crude
    "DTWEXBGS",    # Trade-weighted dollar
]

# Series that are NEVER revised, so there is nothing to keep vintages of.
#
# This is a correctness distinction, not an optimization. A Treasury yield or
# a VIX close is a market observation: it is printed once and never restated.
# An economic statistic is an estimate that gets revised for years — Q1 2020
# real GDP was first published at -4.8% and is -5.1% today.
#
# Requesting every vintage of a daily market series also fails outright:
# DGS10 has ~16,000 daily observations since 1962, and asking ALFRED for all
# vintages of all of them exceeds FRED's response limit and returns HTTP 400.
# The fix and the correct model are the same thing.
NON_REVISED = {
    "DGS3MO", "DGS2", "DGS10", "DGS30", "T10Y2Y", "T10Y3M", "MORTGAGE30US",
    "VIXCLS", "BAMLH0A0HYM2", "DCOILWTICO", "DTWEXBGS", "FEDFUNDS",
}


def load_series(
    conn: sqlite3.Connection, series_id: str, *, vintages: bool | None = None
) -> int:
    """One FRED series into the store.

    `vintages=None` (the default) decides per series: revised statistics get
    the full ALFRED vintage history, market observations that are never
    revised get a single pass. See NON_REVISED for why that is a modelling
    decision rather than a shortcut.
    """
    if not config.FRED_API_KEY:
        raise RuntimeError("Set FRED_API_KEY (free at fred.stlouisfed.org).")
    if vintages is None:
        vintages = series_id not in NON_REVISED

    def fetch(with_vintages: bool):
        params = {
            "series_id": series_id,
            "api_key": config.FRED_API_KEY,
            "file_type": "json",
        }
        if with_vintages:
            # The magic pair. Without these you get final revisions only.
            params["realtime_start"] = "1776-07-04"
            params["realtime_end"] = "9999-12-31"
        return get_json(
            f"{config.FRED_BASE}/series/observations",
            params=params,
            rate=config.FRED_RATE_LIMIT,
            max_age_days=config.STALE_AFTER_DAYS["macro"],
        )

    try:
        payload = fetch(vintages)
    except Exception:
        if not vintages:
            raise
        # A long daily series can exceed FRED's response limit in vintage
        # mode. Falling back to the current vintage is strictly better than
        # having no series at all — and it is only reachable for series not
        # already known to be non-revised, so the loss is bounded.
        payload = fetch(False)

    rows = []
    for o in payload.get("observations", []):
        raw = o.get("value")
        if raw in (None, ".", ""):
            continue  # FRED uses '.' for missing
        rows.append((
            series_id,
            o["date"],
            o.get("realtime_start") or o["date"],
            float(raw),
        ))

    n = store.upsert_many(
        conn, "macro",
        ("series_id", "obs_date", "realtime_start", "value"), rows,
    )

    meta = get_json(
        f"{config.FRED_BASE}/series",
        params={"series_id": series_id, "api_key": config.FRED_API_KEY,
                "file_type": "json"},
        rate=config.FRED_RATE_LIMIT, max_age_days=30,
    )
    s = (meta.get("seriess") or [{}])[0]
    conn.execute(
        "INSERT OR REPLACE INTO macro_meta VALUES (?,?,?,?,?)",
        (series_id, s.get("title"), s.get("units"), s.get("frequency"),
         dt.datetime.now().isoformat()),
    )
    conn.commit()
    return n


def load_default_series(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for sid in DEFAULT_SERIES:
        try:
            out[sid] = load_series(conn, sid)
        except Exception as exc:  # one bad series shouldn't kill the build
            out[sid] = 0
            print(f"  ! {sid}: {exc}")
    return out
