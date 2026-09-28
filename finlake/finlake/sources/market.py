"""Market data: everything the SEC does not publish.

Filings tell you what a company earned. They say nothing about what it is
worth today, what analysts expect next year, how much of the float is sold
short, or who owns it. That data comes from the market, and this module is
where it enters finlake.

**Forward P/E lives or dies here.** The SEC publishes no estimates of any
kind, so a forward multiple is impossible from filings alone. `forward_eps`
in `market_snapshot` is the single field that makes it computable, which is
why this module exists at all rather than being a nice-to-have.

Three things to know about the source.

*It is unofficial.* There is no contract and no SLA. Fields appear, vanish,
and change type between provider releases. Every read here goes through
`_num`/`_int`, every ticker is isolated so one bad response cannot stop a
build, and a missing field becomes NULL rather than an exception.

*It is rate-limited, invisibly.* Hammering it gets the IP blocked, which
takes prices down entirely rather than merely making them stale. Requests
are paced through the same token bucket the SEC calls use.

*It is a second opinion, not a correction.* The provider's own P/E and market
cap are stored next to finlake's rather than replacing them. Where the two
disagree, that is a finding for the data-quality layer to surface — silently
preferring one source is how a data layer stops being auditable.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from typing import Any, Iterable

from .. import config, store
from ..http_client import _limiter
from ._provider import quiet_provider

# Conservative. The provider publishes no limit, so this is set by what does
# not get blocked rather than by what is allowed: a full 600-name universe
# sweep at 2/s is about five minutes, which is well inside the 15-minute
# refresh cadence and leaves headroom for the per-ticker calls the UI makes.
MARKET_RATE_LIMIT = 2.0
MARKET_HOST = "query2.finance.yahoo.com"

# The provider-relative estimate horizons. Deliberately kept in provider terms
# rather than resolved to dates at ingest: "+1y" means the next fiscal year
# *as the provider understood it on the capture date*, and pinning that to a
# date here would bake in an interpretation that later becomes wrong.
ESTIMATE_PERIODS = ("0q", "+1q", "0y", "+1y")


def _today() -> str:
    return dt.date.today().isoformat()


def _num(value: Any) -> float | None:
    """A float, or None for anything that isn't cleanly one.

    Deliberately broad: the provider returns strings, empty strings, NaN,
    numpy scalars, and the literal 'Infinity' depending on the field and the
    day. All of them mean "no usable number" and must not reach the store as
    a value that looks real.
    """
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _int(value: Any) -> int | None:
    out = _num(value)
    return None if out is None else int(out)


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ticker(symbol: str):
    """A rate-limited provider handle."""
    import yfinance as yf

    _limiter(MARKET_HOST, MARKET_RATE_LIMIT).acquire()
    return yf.Ticker(symbol.upper())


def _rows_of(frame) -> Iterable[tuple[Any, dict]]:
    """(index, row-as-dict) pairs from a provider DataFrame, or nothing.

    The provider returns None, an empty frame, or raises, depending on the
    endpoint and whether the symbol has coverage. All three mean the same
    thing to a caller.
    """
    if frame is None:
        return []
    try:
        if getattr(frame, "empty", True):
            return []
        return list(frame.iterrows())
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Per-endpoint loaders. Each returns a row count and never raises.
# ---------------------------------------------------------------------------
def _dividend_yield(info: dict) -> float | None:
    """Dividend yield as a FRACTION, whatever shape the provider sent.

    The provider changed this field's units. `dividendYield` now comes back as
    a percentage — Coca-Cola arrives as 2.44 — while every yield finlake
    computes itself is a fraction, and the UI multiplies fractions by 100 to
    display them. Stored raw, Coca-Cola's 2.4% yield renders as 244%.

    `trailingAnnualDividendYield` is unambiguous (always a fraction) and is
    preferred where present. Otherwise the percentage form is converted, using
    a threshold rather than an unconditional divide: a yield genuinely below 1
    is far more likely to be a fraction the provider did not convert than a
    company paying under 1% — and dividing that by 100 again is the same class
    of error in the other direction.
    """
    trailing = _num(info.get("trailingAnnualDividendYield"))
    if trailing is not None and 0 <= trailing < 1:
        return trailing
    raw = _num(info.get("dividendYield"))
    if raw is None:
        return None
    return raw / 100.0 if raw > 1.0 else raw


def _load_snapshot(conn: sqlite3.Connection, ticker: str, info: dict,
                   as_of: str) -> int:
    """Quote, key statistics, short interest, and ownership.

    All four come out of the same single `info` response, so they are written
    together — splitting them into separate calls would quadruple the request
    count for no additional data.

    EVERY WRITE HERE REPLACES. The tables are keyed on (ticker, as_of) and
    `as_of` is a DATE, so with the default INSERT OR IGNORE the first capture
    of the day won and every later refresh that day was silently discarded —
    the rows were written, the counts came back non-zero, and the price never
    moved. Refreshing more often did nothing at all. Snapshots still
    accumulate across days, which is what makes this point-in-time; replacing
    within a day just means the newest capture of that day is the one kept.
    """
    store.upsert_many(
        conn, "market_snapshot",
        ("ticker", "as_of", "price", "market_cap", "enterprise_value",
         "shares_outstanding", "float_shares", "beta", "week52_high",
         "week52_low", "avg_volume", "trailing_pe", "forward_pe",
         "trailing_eps", "forward_eps", "price_to_book", "price_to_sales",
         "dividend_yield", "payout_ratio", "captured_at"),
        [(
            ticker, as_of,
            _num(info.get("currentPrice") or info.get("regularMarketPrice")),
            _num(info.get("marketCap")), _num(info.get("enterpriseValue")),
            _num(info.get("sharesOutstanding")), _num(info.get("floatShares")),
            _num(info.get("beta")), _num(info.get("fiftyTwoWeekHigh")),
            _num(info.get("fiftyTwoWeekLow")), _num(info.get("averageVolume")),
            _num(info.get("trailingPE")), _num(info.get("forwardPE")),
            _num(info.get("trailingEps")), _num(info.get("forwardEps")),
            _num(info.get("priceToBook")),
            _num(info.get("priceToSalesTrailing12Months")),
            _dividend_yield(info), _num(info.get("payoutRatio")),
            dt.datetime.now().isoformat(timespec="seconds"),
        )],
        ignore_conflicts=False,
    )

    store.upsert_many(
        conn, "short_interest",
        ("ticker", "as_of", "shares_short", "shares_short_prior",
         "short_ratio", "short_pct_float"),
        [(ticker, as_of, _num(info.get("sharesShort")),
          _num(info.get("sharesShortPriorMonth")), _num(info.get("shortRatio")),
          _num(info.get("shortPercentOfFloat")))],
        ignore_conflicts=False,
    )

    store.upsert_many(
        conn, "ownership",
        ("ticker", "as_of", "pct_insiders", "pct_institutions", "n_institutions"),
        [(ticker, as_of, _num(info.get("heldPercentInsiders")),
          _num(info.get("heldPercentInstitutions")), None)],
        ignore_conflicts=False,
    )

    # Profile is overwritten rather than snapshotted — a sector is not a time
    # series. REPLACE, not IGNORE, so a reclassification actually lands.
    store.upsert_many(
        conn, "profile",
        ("ticker", "cik", "name", "sector", "industry", "country", "exchange",
         "employees", "website", "summary", "updated_at"),
        [(ticker, None, _str(info.get("longName") or info.get("shortName")),
          _str(info.get("sector")), _str(info.get("industry")),
          _str(info.get("country")), _str(info.get("exchange")),
          _int(info.get("fullTimeEmployees")), _str(info.get("website")),
          _str(info.get("longBusinessSummary")), as_of)],
        ignore_conflicts=False,
    )
    return 1


def _load_estimates(conn: sqlite3.Connection, tk, ticker: str, as_of: str) -> int:
    """Consensus EPS and revenue estimates, and how they have been revised."""
    rows = []
    for metric, attr, year_ago_col in (
            ("eps", "earnings_estimate", "yearAgoEps"),
            ("revenue", "revenue_estimate", "yearAgoRevenue")):
        for period, row in _rows_of(getattr(tk, attr, None)):
            rows.append((
                ticker, as_of, metric, str(period), _num(row.get("avg")),
                _num(row.get("low")), _num(row.get("high")),
                _int(row.get("numberOfAnalysts")), _num(row.get(year_ago_col)),
                _num(row.get("growth")),
            ))
    store.upsert_many(
        conn, "estimates",
        ("ticker", "as_of", "metric", "period", "avg", "low", "high",
         "n_analysts", "year_ago", "growth"),
        rows, ignore_conflicts=False,
    )

    # Revision trend: the estimate as it stood 7/30/60/90 days ago, plus the
    # count of analysts revising each way. Two views of the same movement,
    # and the up/down counts are the "breadth" half that magnitude misses.
    trend = {str(p): r for p, r in _rows_of(getattr(tk, "eps_trend", None))}
    revs = {str(p): r for p, r in _rows_of(getattr(tk, "eps_revisions", None))}
    trend_rows = []
    for period in set(trend) | set(revs):
        t, v = trend.get(period, {}), revs.get(period, {})
        trend_rows.append((
            ticker, as_of, period, _num(t.get("current")),
            _num(t.get("7daysAgo")), _num(t.get("30daysAgo")),
            _num(t.get("60daysAgo")), _num(t.get("90daysAgo")),
            _int(v.get("upLast7days")), _int(v.get("upLast30days")),
            # Note the inconsistent capitalisation in the source field names
            # ("downLast7Days" vs "upLast7days") -- copied exactly on purpose.
            _int(v.get("downLast7Days")), _int(v.get("downLast30days")),
        ))
    store.upsert_many(
        conn, "estimate_trend",
        ("ticker", "as_of", "period", "current", "days_7", "days_30",
         "days_60", "days_90", "up_7", "up_30", "down_7", "down_30"),
        trend_rows, ignore_conflicts=False,
    )
    return len(rows) + len(trend_rows)


def _load_analyst(conn: sqlite3.Connection, tk, ticker: str, info: dict,
                  as_of: str) -> int:
    """Price targets and the buy/hold/sell distribution."""
    targets = {}
    try:
        targets = getattr(tk, "analyst_price_targets", None) or {}
    except Exception:
        targets = {}

    store.upsert_many(
        conn, "analyst_targets",
        ("ticker", "as_of", "price_current", "target_mean", "target_median",
         "target_high", "target_low", "n_analysts", "recommendation_mean",
         "recommendation_key"),
        [(ticker, as_of, _num(targets.get("current")),
          _num(targets.get("mean") or info.get("targetMeanPrice")),
          _num(targets.get("median") or info.get("targetMedianPrice")),
          _num(targets.get("high") or info.get("targetHighPrice")),
          _num(targets.get("low") or info.get("targetLowPrice")),
          _int(info.get("numberOfAnalystOpinions")),
          _num(info.get("recommendationMean")),
          _str(info.get("recommendationKey")))],
        ignore_conflicts=False,
    )

    rec_rows = []
    for _, row in _rows_of(getattr(tk, "recommendations", None)):
        period = _str(row.get("period"))
        if not period:
            continue
        rec_rows.append((ticker, as_of, period, _int(row.get("strongBuy")),
                         _int(row.get("buy")), _int(row.get("hold")),
                         _int(row.get("sell")), _int(row.get("strongSell"))))
    store.upsert_many(
        conn, "recommendations",
        ("ticker", "as_of", "period", "strong_buy", "buy", "hold", "sell",
         "strong_sell"),
        rec_rows, ignore_conflicts=False,
    )
    return 1 + len(rec_rows)


def _load_earnings_history(conn: sqlite3.Connection, tk, ticker: str,
                           as_of: str) -> int:
    """Actual vs. consensus EPS per reported quarter."""
    rows = []
    for quarter, row in _rows_of(getattr(tk, "earnings_history", None)):
        try:
            key = str(quarter.date()) if hasattr(quarter, "date") else str(quarter)[:10]
        except Exception:
            continue
        rows.append((ticker, key, _num(row.get("epsActual")),
                     _num(row.get("epsEstimate")),
                     _num(row.get("surprisePercent")), as_of))
    store.upsert_many(
        conn, "earnings_history",
        ("ticker", "quarter", "eps_actual", "eps_estimate", "surprise_pct",
         "as_of"),
        rows, ignore_conflicts=False,   # a restated actual should land
    )
    return len(rows)


def _load_calendar(conn: sqlite3.Connection, tk, ticker: str, as_of: str) -> int:
    """Upcoming earnings and ex-dividend dates."""
    try:
        cal = getattr(tk, "calendar", None) or {}
    except Exception:
        return 0
    if not isinstance(cal, dict):
        return 0

    rows = []
    for field, kind in (("Earnings Date", "earnings"),
                        ("Ex-Dividend Date", "ex_dividend"),
                        ("Dividend Date", "dividend_payable")):
        value = cal.get(field)
        for item in (value if isinstance(value, (list, tuple)) else [value]):
            if item is None:
                continue
            try:
                key = str(item.date()) if hasattr(item, "date") else str(item)[:10]
            except Exception:
                continue
            if len(key) == 10:
                rows.append((ticker, key, kind, as_of))
    store.upsert_many(
        conn, "earnings_calendar",
        ("ticker", "event_date", "kind", "as_of"), rows,
        ignore_conflicts=False)
    return len(rows)


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
def load_market_data(conn: sqlite3.Connection, ticker: str, *,
                     as_of: str | None = None) -> dict[str, int]:
    """Everything the market source has for one ticker, into the store.

    Returns per-section row counts. Never raises: a failure on one section is
    recorded and the rest still load, because a missing price target should
    not also cost you the estimates that came back fine.
    """
    ticker = ticker.upper()
    as_of = as_of or _today()
    counts: dict[str, int] = {}

    # The provider narrates every endpoint it has no data for, at ERROR, on a
    # logger with no handler — so Python's last-resort handler printed the raw
    # 404 body to the daemon's console four or five times per symbol. The
    # outcome is already recorded in `fetch_log` and summarised by the tier;
    # see sources/_provider.py for why this is scoped rather than global.
    with quiet_provider():
        try:
            tk = _ticker(ticker)
            info = tk.info or {}
        except Exception as exc:
            _log(conn, ticker, "failed", f"info: {exc}")
            return {"error": 0}

        if not info or not any(info.get(k) for k in
                               ("marketCap", "regularMarketPrice", "currentPrice")):
            # A response with no price and no market cap is the shape of a
            # delisted, suspended, or misspelled symbol. Recording it as
            # "empty" rather than "ok" keeps it visible in the build report
            # instead of looking like a successful load of nothing.
            _log(conn, ticker, "empty", "no quote fields in response")
            return {"empty": 0}

        for name, fn in (
            ("snapshot", lambda: _load_snapshot(conn, ticker, info, as_of)),
            ("estimates", lambda: _load_estimates(conn, tk, ticker, as_of)),
            ("analyst", lambda: _load_analyst(conn, tk, ticker, info, as_of)),
            ("earnings", lambda: _load_earnings_history(conn, tk, ticker, as_of)),
            ("calendar", lambda: _load_calendar(conn, tk, ticker, as_of)),
        ):
            try:
                counts[name] = fn()
            except Exception as exc:
                counts[name] = 0
                _log(conn, f"{ticker}:{name}", "failed", str(exc)[:200])

    conn.commit()
    _log(conn, ticker, "ok", ", ".join(f"{k}={v}" for k, v in counts.items()))
    return counts


def _log(conn: sqlite3.Connection, resource: str, status: str, note: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
        (f"market:{resource}", dt.datetime.now().isoformat(), status, note),
    )


def latest_snapshot(conn: sqlite3.Connection, ticker: str,
                    as_of: str | None = None) -> dict | None:
    """The newest snapshot at or before `as_of`.

    The `as_of` bound is the whole point of snapshotting: a query dated last
    month must see the estimates that existed last month, not today's.
    """
    row = conn.execute(
        "SELECT * FROM market_snapshot WHERE ticker = ? AND as_of <= ? "
        "ORDER BY as_of DESC LIMIT 1",
        (ticker.upper(), as_of or _today()),
    ).fetchone()
    return dict(row) if row else None


def forward_eps(conn: sqlite3.Connection, ticker: str,
                as_of: str | None = None) -> float | None:
    """Consensus next-twelve-month EPS — the input to forward P/E.

    Prefers the explicit `forwardEps` statistic; falls back to the next
    fiscal year's consensus estimate when that field is absent, which happens
    for names with thinner analyst coverage.
    """
    snap = latest_snapshot(conn, ticker, as_of)
    if snap and snap.get("forward_eps") is not None:
        return float(snap["forward_eps"])

    row = conn.execute(
        "SELECT avg FROM estimates WHERE ticker = ? AND metric = 'eps' "
        "AND period = '+1y' AND as_of <= ? AND avg IS NOT NULL "
        "ORDER BY as_of DESC LIMIT 1",
        (ticker.upper(), as_of or _today()),
    ).fetchone()
    return float(row["avg"]) if row else None


def sector_map(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """ticker -> (sector, industry), for peer-group construction."""
    return {
        r["ticker"]: (r["sector"], r["industry"])
        for r in conn.execute(
            "SELECT ticker, sector, industry FROM profile "
            "WHERE sector IS NOT NULL")
    }
