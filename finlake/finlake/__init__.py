"""finlake — a point-in-time financial data layer.

One append-only fact table with a `filed` column serves both live screening
and honest backtesting. See store.py for the design note.
"""

from .api import (
    fundamentals, macro, news, news_signal, prices, quote, quotes,
    refresh_once, refresh_status, restatements, universe, universe_status,
    vwap,
)
from .ratios import for_ticker as ratio_history
from .ratios import latest as ratios_latest
from .ratios import live_valuation
from .statements import (
    balance_sheet, cash_flow, income_statement, periods, statement,
)
from .store import init_db

__version__ = "0.13.0"
__all__ = [
    # Raw facts
    "fundamentals", "prices", "vwap", "macro", "universe", "restatements",
    # Live quotes — the same cached series `prices()` returns, so a header
    # and a chart drawn from these can never disagree.
    "quote", "quotes",
    # News
    "news", "news_signal",
    # Keeping the cache current
    "refresh_once", "refresh_status", "universe_status",
    # Assembled statements
    "income_statement", "balance_sheet", "cash_flow", "statement", "periods",
    # Derived ratios
    "ratios_latest", "ratio_history", "live_valuation",
    "init_db",
]
