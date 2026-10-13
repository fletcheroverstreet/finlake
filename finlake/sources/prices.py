"""Prices and corporate actions.

Key decision: we store RAW (unadjusted) OHLCV in parquet and corporate actions
in SQLite, then compute adjusted prices at read time.

The tempting alternative — cache adj_close — is a trap. A split announced next
Tuesday changes every adjusted close going back to 1980. With raw + actions you
append one row and every historical query is instantly correct. With cached
adj_close you must refetch and rewrite the entire file for every ticker that
ever splits, and if you miss one you get a 2x error that looks like alpha.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from pathlib import Path

import pandas as pd

from .. import config, store
from ._provider import quiet_provider as _quiet_provider

RAW_COLS = ["date", "open", "high", "low", "close", "volume"]


def _path(ticker: str) -> Path:
    return config.PRICE_DIR / f"{ticker.upper()}.parquet"


# ---------------------------------------------------------------------------
# Latest quote
#
# THE PRICE THE HUB SHOWS AND THE PRICE THE CHART DRAWS MUST BE THE SAME
# NUMBER. They were not. The chart read this cache; the header read the close
# at the last FISCAL QUARTER END, because that is the only price
# `ratios.latest()` ever computes — correct for a point-in-time multiple,
# completely wrong as "the price". Microsoft's header read $373 (30 June) on a
# day the stock closed $509, and market cap, EV and every valuation multiple
# beside it were built on that same stale figure.
#
# So there is exactly one live-price accessor, it reads the same parquet the
# chart reads, and it returns the bar's DATE alongside the price so a stale
# cache is visible on screen instead of being presented as "now".
# ---------------------------------------------------------------------------

# (path, mtime, size) -> quote. Reading only the final row group makes a single
# quote cheap, but a universe sweep is still ~700 file opens; memoising on the
# file's own mtime means repeated sweeps inside one process are free AND cannot
# serve a stale value, because a rewritten parquet always changes its mtime.
_QUOTE_MEMO: dict[tuple[str, float, int], dict] = {}

_QUOTE_CACHE_COLUMNS = (
    "ticker", "bar_date", "close", "previous_close", "open", "high", "low",
    "volume", "source_mtime", "source_size", "updated_at",
)


def _quote_from_row(row, ticker: str) -> dict:
    close = row["close"]
    previous = row["previous_close"]
    return {
        "ticker": ticker.upper(),
        "price": close,
        "as_of": row["bar_date"],
        "previous_close": previous,
        "change": None if previous is None else close - previous,
        "change_pct": (None if not previous else close / previous - 1.0),
        "open": row["open"], "high": row["high"], "low": row["low"],
        "volume": row["volume"],
    }


def _read_quote_index(tickers) -> dict[str, dict]:
    """The stored index rows for these tickers, keyed by ticker.

    Freshness is NOT checked here — the caller compares each row's recorded
    mtime and size against the parquet on disk, which is what makes the index
    unable to serve a stale price.
    """
    try:
        from .. import store

        with store.session(read_only=True) as conn:
            placeholders = ",".join("?" * len(tickers))
            rows = conn.execute(
                f"SELECT * FROM quote_cache WHERE ticker IN ({placeholders})",
                [t.upper() for t in tickers],
            ).fetchall()
        return {r["ticker"]: dict(r) for r in rows}
    except Exception:
        return {}


def _write_quote_index(entries: list[tuple]) -> None:
    """Best-effort. A read-only cache still serves quotes, just not quickly."""
    if not entries:
        return
    try:
        from .. import store

        with store.session() as conn:
            store.upsert_many(conn, "quote_cache", _QUOTE_CACHE_COLUMNS,
                              entries, ignore_conflicts=False)
    except Exception:
        pass


def _index_entry(quote: dict, stat) -> tuple:
    return (
        quote["ticker"], quote["as_of"], quote["price"],
        quote["previous_close"], quote["open"], quote["high"], quote["low"],
        quote["volume"], stat.st_mtime, stat.st_size,
        dt.datetime.now().isoformat(timespec="seconds"),
    )


def last_quote(ticker: str, *, _stat=None, _index_row=None,
               _persist: bool = True) -> dict | None:
    """The most recent cached bar for one ticker, or None if uncached.

    Returns `price`, the `as_of` DATE of that bar, the previous close, and the
    change between them. The date is not decoration: a bar from three days ago
    is a perfectly good last close and a very bad "current price", and the only
    way a reader can tell the difference is if we say which day it is.
    """
    path = _path(ticker)
    stat = _stat
    if stat is None:
        try:
            stat = path.stat()
        except OSError:
            return None

    key = (str(path), stat.st_mtime, stat.st_size)
    hit = _QUOTE_MEMO.get(key)
    if hit is not None:
        return dict(hit)

    # An index row is usable only if the parquet has not been rewritten since
    # it was taken. That check is what keeps this a cache rather than a second,
    # divergent source of price.
    if (_index_row is not None
            and _index_row.get("source_mtime") == stat.st_mtime
            and _index_row.get("source_size") == stat.st_size
            and _index_row.get("close") is not None):
        quote = _quote_from_row(_index_row, ticker)
        _QUOTE_MEMO[key] = quote
        return dict(quote)

    try:
        import pyarrow.parquet as pq

        handle = pq.ParquetFile(path)
        if handle.num_row_groups == 0:
            return None
        # Only the last row group: a 25-year daily history is a few hundred KB
        # and there is no reason to decode all of it to read two numbers.
        table = handle.read_row_group(
            handle.num_row_groups - 1,
            columns=["date", "open", "high", "low", "close", "volume"])
        rows = table.to_pydict()
    except Exception:
        # Any parquet trouble at all: fall back to the full read rather than
        # reporting "no price" for a ticker whose file is merely unusual.
        try:
            frame = pd.read_parquet(path)
        except Exception:
            return None
        if frame.empty:
            return None
        rows = {c: frame[c].tolist() for c in frame.columns if c in RAW_COLS}

    dates = rows.get("date") or []
    closes = rows.get("close") or []
    if not dates or not closes:
        return None

    def _at(name: str, index: int):
        values = rows.get(name) or []
        try:
            value = values[index]
        except IndexError:
            return None
        return None if value is None or value != value else float(value)

    close = _at("close", -1)
    if close is None:
        return None
    previous = _at("close", -2)
    quote = {
        "ticker": ticker.upper(),
        "price": close,
        "as_of": str(dates[-1])[:10],
        "previous_close": previous,
        "change": None if previous is None else close - previous,
        # Guarded rather than assumed positive: a bad bar of 0.0 would make
        # this a ZeroDivisionError in the middle of a page render.
        "change_pct": (None if not previous else close / previous - 1.0),
        "open": _at("open", -1),
        "high": _at("high", -1),
        "low": _at("low", -1),
        "volume": _at("volume", -1),
    }
    _QUOTE_MEMO[key] = quote
    if len(_QUOTE_MEMO) > 4000:       # bounded: one entry per ticker per write
        _QUOTE_MEMO.clear()
    if _persist:
        _write_quote_index([_index_entry(quote, stat)])
    return dict(quote)


def last_quotes(tickers) -> dict[str, dict]:
    """`last_quote` for many tickers. Missing names are simply absent.

    Reads the whole index in one query, then confirms each row against its
    parquet's mtime and size before using it. A `stat` costs microseconds and
    opening a parquet costs milliseconds, which is the difference between a
    screener that draws instantly and one that stalls for seven seconds every
    time its cache expires.
    """
    symbols = [str(t).upper() for t in tickers]
    if not symbols:
        return {}

    index = _read_quote_index(symbols)
    out: dict[str, dict] = {}
    fresh_entries: list[tuple] = []

    for symbol in symbols:
        path = _path(symbol)
        try:
            stat = path.stat()
        except OSError:
            continue
        row = index.get(symbol)
        served_from_index = (
            row is not None
            and row.get("source_mtime") == stat.st_mtime
            and row.get("source_size") == stat.st_size
            and row.get("close") is not None)

        # `_persist=False`: writing one row per name across a 500-name sweep
        # is 500 transactions. They are collected and written together below.
        quote = last_quote(symbol, _stat=stat, _index_row=row, _persist=False)
        if quote is None:
            continue
        out[symbol] = quote
        if not served_from_index:
            fresh_entries.append(_index_entry(quote, stat))

    _write_quote_index(fresh_entries)
    return out


def load_prices(
    conn: sqlite3.Connection, ticker: str, *, start: str | None = None,
    force: bool = False,
) -> int:
    """Fetch raw daily bars + actions via yfinance. Incremental after day one.

    `start=None` (the default) pulls the ticker's FULL available history back
    to listing, rather than an arbitrary cutoff. Depth is close to free here —
    it is one request either way, and daily bars for even a 40-year history are
    a few hundred KB of parquet — while a cutoff quietly removes the 2000,
    2008-09, and 2020 drawdowns from every long-window calculation that reads
    this table.
    """
    import yfinance as yf

    config.ensure_dirs()
    ticker = ticker.upper()
    path = _path(ticker)

    existing = pd.read_parquet(path) if path.exists() and not force else None
    if existing is not None and len(existing):
        last = pd.to_datetime(existing["date"]).max().date()
        start = (last - dt.timedelta(days=5)).isoformat()  # overlap for safety

    tk = yf.Ticker(ticker)
    # period="max" and start= are mutually exclusive in yfinance: passing both
    # makes start win silently. Only one is ever sent.
    kwargs = {"start": start} if start else {"period": "max"}
    # The provider logs a line per unservable symbol, and the universe holds a
    # handful of warrants and preferred classes it has no history for
    # (NEE-PW, OXY-WT, WRB-PF). Every quote sweep would reprint them, which on
    # a three-minute tier is a daemon log made almost entirely of the same
    # four names. The failure is already handled — an empty frame returns 0 —
    # so this suppresses the narration, not the outcome.
    with _quiet_provider():
        hist = tk.history(auto_adjust=False, actions=True, **kwargs)
    if hist is None or hist.empty:
        return 0

    return _write_history(conn, ticker, hist, existing)


def _write_history(conn: sqlite3.Connection, ticker: str, hist: pd.DataFrame,
                   existing: pd.DataFrame | None) -> int:
    """Normalise one provider history frame into actions + the parquet cache.

    Split out of `load_prices` so the batched sweep writes through exactly the
    same code. Two loaders with two copies of this normalisation is how the
    fast path and the slow path end up disagreeing about what a bar is.
    """
    if hist is None or hist.empty:
        return 0

    ticker = ticker.upper()
    path = _path(ticker)

    hist = hist.reset_index()
    hist.columns = [str(c).lower().replace(" ", "_") for c in hist.columns]
    if "date" not in hist.columns:
        # A batched download indexes on 'Datetime' for intraday intervals and
        # on 'Date' for daily ones; after lowercasing, take whichever arrived.
        for candidate in ("datetime", "index"):
            if candidate in hist.columns:
                hist = hist.rename(columns={candidate: "date"})
                break
    if "date" not in hist.columns or "close" not in hist.columns:
        return 0

    hist["date"] = pd.to_datetime(hist["date"], utc=True, errors="coerce")
    hist = hist[hist["date"].notna()]
    if hist.empty:
        return 0
    hist["date"] = hist["date"].dt.tz_localize(None).dt.normalize()

    # --- corporate actions -> SQLite ------------------------------------
    actions = []
    if "stock_splits" in hist:
        for _, r in hist[hist["stock_splits"].fillna(0) > 0].iterrows():
            actions.append((ticker, r["date"].date().isoformat(), "split",
                            float(r["stock_splits"])))
    if "dividends" in hist:
        for _, r in hist[hist["dividends"].fillna(0) > 0].iterrows():
            actions.append((ticker, r["date"].date().isoformat(), "dividend",
                            float(r["dividends"])))
    store.upsert_many(conn, "corp_actions",
                      ("ticker", "date", "kind", "value"), actions)
    conn.commit()

    # --- raw bars -> parquet ---------------------------------------------
    for col in ("open", "high", "low", "volume"):
        if col not in hist.columns:
            hist[col] = float("nan")
    bars = hist[["date", "open", "high", "low", "close", "volume"]].copy()
    bars["date"] = bars["date"].dt.date.astype(str)
    # A bar with no close is not a bar. The provider emits these for the
    # current session before the first print, and letting one through makes
    # `last_quote` report today's price as missing on a name that traded fine.
    bars = bars[pd.to_numeric(bars["close"], errors="coerce").notna()]
    if bars.empty:
        return 0

    if existing is not None and len(existing):
        bars = pd.concat([existing, bars], ignore_index=True)
    bars = (bars.drop_duplicates(subset="date", keep="last")
                .sort_values("date").reset_index(drop=True))
    bars.to_parquet(path, index=False)

    # Refresh the quote index in the same breath as the file it indexes.
    #
    # Without this, every quote sweep invalidates all ~500 index rows at once
    # (each records the mtime the parquet had before the sweep), and the next
    # screener load pays the full seven-second parquet walk to rebuild them —
    # so a FASTER refresh tier would have made the UI slower.
    #
    # Built from `bars`, which is already in memory, rather than by re-reading
    # the file we have just written. No memo invalidation is needed: the memo
    # keys on mtime, so its old entries are simply unreachable.
    try:
        tail = bars.tail(2)
        closes = pd.to_numeric(tail["close"], errors="coerce").tolist()
        last = tail.iloc[-1]
        stat = path.stat()
        _write_quote_index([(
            ticker, str(last["date"])[:10], float(closes[-1]),
            float(closes[-2]) if len(closes) > 1 else None,
            _as_float(last.get("open")), _as_float(last.get("high")),
            _as_float(last.get("low")), _as_float(last.get("volume")),
            stat.st_mtime, stat.st_size,
            dt.datetime.now().isoformat(timespec="seconds"),
        )])
    except Exception:
        pass    # the index is a cache; failing to update it costs only speed
    return len(bars)


def _as_float(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return None if out != out else out


# How many symbols go into one batched request. The provider accepts far more
# than this in a single call, but a chunk that fails takes every symbol in it
# down with it, so the chunk size is really a blast radius.
QUOTE_CHUNK = 40


def load_prices_many(
    conn: sqlite3.Connection, tickers, *, chunk_size: int = QUOTE_CHUNK,
) -> tuple[dict[str, int], list[str]]:
    """Top up the cached bars for many tickers in a handful of requests.

    THIS IS WHAT MAKES A FAST QUOTE SWEEP POSSIBLE. `load_prices` issues one
    request per name; 600 of those serially is minutes of wall clock and 600
    chances to be rate-limited, which is why the refresh tier that claimed to
    be batched could never actually keep up with its own 15-minute interval.
    The provider takes many symbols per request, so this asks for the tail —
    only what has happened since each name's last cached bar — in chunks.

    Returns `(bars_written_per_ticker, needs_full_history)`. Names with no
    cached history at all are NOT handled here: they need a full
    back-to-listing pull, which is a different and much larger request. They
    come back in the second element so the caller can fall back to
    `load_prices` for them at whatever pace it likes.
    """
    import yfinance as yf

    config.ensure_dirs()
    symbols = [str(t).upper() for t in tickers]
    out: dict[str, int] = {}

    existing_by_ticker: dict[str, pd.DataFrame] = {}
    needs_full: list[str] = []
    for symbol in symbols:
        path = _path(symbol)
        if not path.exists():
            needs_full.append(symbol)
            out[symbol] = 0
            continue
        try:
            frame = pd.read_parquet(path)
        except Exception:
            needs_full.append(symbol)
            out[symbol] = 0
            continue
        if frame.empty:
            needs_full.append(symbol)
            out[symbol] = 0
            continue
        existing_by_ticker[symbol] = frame

    incremental = sorted(existing_by_ticker)
    for i in range(0, len(incremental), chunk_size):
        chunk = incremental[i:i + chunk_size]
        # One `start` for the whole chunk: the oldest last-bar in it, minus a
        # few days of overlap so a late-corrected bar is picked up rather than
        # frozen at whatever first arrived.
        oldest = min(
            pd.to_datetime(existing_by_ticker[s]["date"]).max().date()
            for s in chunk)
        start = (oldest - dt.timedelta(days=5)).isoformat()
        try:
            with _quiet_provider():
                frame = yf.download(
                    chunk, start=start, auto_adjust=False, actions=True,
                    group_by="ticker", progress=False, threads=True,
                    repair=False, rounding=False)
        except Exception:
            frame = None
        if frame is None or frame.empty:
            for symbol in chunk:
                out[symbol] = 0
            continue

        for symbol in chunk:
            try:
                # A one-symbol chunk comes back with flat columns; anything
                # larger is a (symbol, field) MultiIndex.
                if isinstance(frame.columns, pd.MultiIndex):
                    if symbol not in frame.columns.get_level_values(0):
                        out[symbol] = 0
                        continue
                    part = frame[symbol]
                else:
                    part = frame
                part = part.dropna(how="all")
                out[symbol] = _write_history(
                    conn, symbol, part, existing_by_ticker[symbol])
            except Exception:
                out[symbol] = 0

    return out, needs_full


# ---------------------------------------------------------------------------
# Back-filling names with no cached history
# ---------------------------------------------------------------------------
# How long a symbol the provider has no history for is left alone.
#
# THE QUEUE WAS JAMMED, PERMANENTLY. A name with no parquet needs a full
# back-to-listing pull, which cannot be batched, so the quote tier drains a
# few per pass. But a symbol the provider simply HAS no history for — a
# warrant, a preferred series, a name delisted years ago — writes no parquet,
# so it was still missing on the next pass, and the pass after that. On this
# cache fourteen symbols were in that state and the twelve-name budget was
# spent entirely on them: about 5,760 full-history requests a day, every one
# of them already known to return nothing, against a source that responds to
# volume by blocking the IP.
#
# The second half is worse than the waste. The backfill list is in ticker
# order, so those twelve permanently occupied the front of the queue and a
# genuine new listing sorting after them would NEVER have been fetched. It
# would simply have had no price series, with nothing anywhere reporting why.
#
# A day, not a week: a symbol that has just started trading, or one whose
# history was briefly unavailable, comes back on its own by tomorrow. The cost
# of being wrong is one day of a missing series; the cost of not bounding it
# at all is the whole queue.
BACKFILL_RETRY_HOURS = 24


def _backfill_suppressed(conn: sqlite3.Connection, ticker: str) -> bool:
    """Whether this symbol was recently confirmed to have no history."""
    row = conn.execute(
        "SELECT fetched_at, status FROM fetch_log WHERE resource = ?",
        (f"prices:{ticker.upper()}",),
    ).fetchone()
    if row is None or row["status"] != "empty":
        return False
    try:
        when = dt.datetime.fromisoformat(row["fetched_at"])
    except (TypeError, ValueError):
        return False
    return (dt.datetime.now() - when) < dt.timedelta(hours=BACKFILL_RETRY_HOURS)


def _record_backfill(conn: sqlite3.Connection, ticker: str, status: str,
                     note: str) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
        (f"prices:{ticker.upper()}", dt.datetime.now().isoformat(), status, note),
    )
    conn.commit()


def backfill_missing(conn: sqlite3.Connection, tickers: list[str], *,
                     limit: int, provider_healthy: bool) -> dict[str, int]:
    """Pull full history for up to `limit` names that have none cached.

    `provider_healthy` says whether the provider answered for anything else on
    this pass. It is the difference between "this symbol has no history" and
    "nothing has any history right now because the source is down" — and
    without it, one outage would mark the entire universe as historyless and
    stop refreshing all of it. A symbol is only ever written off while the
    provider is demonstrably answering.

    Returns counts: loaded, empty (written off for now), skipped (still inside
    a previous write-off's window).
    """
    counts = {"loaded": 0, "empty": 0, "skipped": 0}
    queue = []
    for ticker in tickers:
        if _backfill_suppressed(conn, ticker):
            counts["skipped"] += 1
            continue
        queue.append(ticker)

    for ticker in queue[:limit]:
        try:
            written = load_prices(conn, ticker)
        except Exception as exc:
            # A raised error is not evidence about the symbol — it is evidence
            # about the request. Never written off on this path.
            _record_backfill(conn, ticker, "failed", str(exc)[:200])
            continue
        if written:
            counts["loaded"] += 1
            _record_backfill(conn, ticker, "ok", f"{written} bars")
        elif provider_healthy:
            counts["empty"] += 1
            _record_backfill(
                conn, ticker, "empty",
                f"no history at the provider; not retried for "
                f"{BACKFILL_RETRY_HOURS}h")
    return counts


def get_prices(
    conn: sqlite3.Connection, ticker: str, *, start: str | None = None,
    end: str | None = None, adjust: str = "split",
) -> pd.DataFrame:
    """Read bars from cache.

    WHAT IS ACTUALLY STORED, which is not what this module originally assumed.

    The design note at the top of this file says raw, unadjusted OHLCV is
    cached. That is what we *ask* for — `history(auto_adjust=False)` — but it
    is not what the provider returns: `auto_adjust` controls DIVIDEND
    adjustment only. Splits are always applied, retroactively, to the whole
    series. So the cached `close` column is **split-adjusted and
    dividend-unadjusted**.

    That mismatch was live and expensive. `adjust="split"` applied the split
    factor a second time, so every bar before a split was divided by that
    ratio again: NVDA's 2024-05-21 close read $95.39 in the cache and came
    back as $9.54. It corrupted historical price charts, every historical
    valuation multiple, VWAP (and through it the buyback-timing metric), and
    the 12-1 momentum return whenever its 252-day window spanned a split.

    The modes now describe what they actually do:

      'split'     -> as cached. Splits are ALREADY applied by the provider,
                     so this is a no-op rather than a second adjustment.
                     Comparable across time; what a chart should show.
      'total'     -> split-adjusted plus dividends reinvested. A return
                     series. Dividends genuinely are not pre-applied.
      'as_traded' -> undoes the provider's split adjustment to recover the
                     price actually printed on the tape that day. This is
                     what pairs with an as-reported share count: multiplying
                     a split-adjusted price by a pre-split share count
                     understates market cap by the split ratio.
      'none'      -> kept as an alias of 'split' for callers that predate
                     this distinction. It never meant "as traded".

    Applying `end` BEFORE adjusting is deliberate: adjustment factors are
    computed only from actions at or before `end`, so a series pulled with
    end='2019-03-14' is not retroactively rescaled by a 2020 split.
    """
    path = _path(ticker.upper())
    if not path.exists():
        raise FileNotFoundError(f"No cached prices for {ticker}. Run load_prices.")

    df = pd.read_parquet(path)
    if start:
        df = df[df["date"] >= start]
    if end:
        df = df[df["date"] <= end]
    df = df.sort_values("date").reset_index(drop=True)
    if df.empty:
        return df

    horizon = end or df["date"].iloc[-1]
    acts = conn.execute(
        "SELECT date, kind, value FROM corp_actions WHERE ticker=? AND date<=? "
        "ORDER BY date",
        (ticker.upper(), horizon),
    ).fetchall()

    if adjust in ("split", "none"):
        # Already split-adjusted at the source; nothing to do. Mirrored into
        # adj_* so every caller can read the same column names regardless of
        # mode instead of branching on it.
        out = df.copy()
        for col in ("open", "high", "low", "close"):
            out[f"adj_{col}"] = out[col]
        out["adj_volume"] = out["volume"]
        return out

    if adjust == "as_traded":
        # Undo the provider's split adjustment: multiply every bar before a
        # split by that split's ratio, so the series shows what actually
        # printed. Volume moves the opposite way.
        factor = pd.Series(1.0, index=df.index)
        dates = df["date"].values
        for a in reversed(acts):
            if a["kind"] != "split":
                continue
            idx = dates.searchsorted(a["date"])
            ratio = float(a["value"])
            if 0 < idx <= len(df) and ratio > 0:
                factor.iloc[:idx] *= ratio
        out = df.copy()
        for col in ("open", "high", "low", "close"):
            out[f"adj_{col}"] = out[col] * factor
        out["adj_volume"] = out["volume"] / factor.replace(0, pd.NA)
        return out

    if adjust != "total":
        raise ValueError(
            f"unknown adjust={adjust!r}; use 'split', 'total', 'as_traded', "
            f"or 'none'")

    # adjust='total': the cached series is already split-adjusted, so ONLY
    # dividends are applied here. Applying splits again is exactly the bug
    # this rewrite exists to remove.
    #
    # Volume is left alone. A dividend changes no share count, and the split
    # adjustment volume would need is already baked into the cached figures.
    price_factor = pd.Series(1.0, index=df.index)
    dates = df["date"].values
    for a in reversed(acts):
        if a["kind"] != "dividend":
            continue
        idx = dates.searchsorted(a["date"])  # first bar on/after the action
        if idx <= 0 or idx > len(df):
            continue
        prev_close = df["close"].iloc[idx - 1]
        if prev_close > 0:
            price_factor.iloc[:idx] *= 1.0 - float(a["value"]) / prev_close

    out = df.copy()
    for col in ("open", "high", "low", "close"):
        out[f"adj_{col}"] = out[col] * price_factor
    out["adj_volume"] = out["volume"]
    return out


def vwap(
    conn: sqlite3.Connection, ticker: str, *, start: str, end: str,
    adjust: str = "split", price: str = "typical",
) -> float | None:
    """Dollar-weighted average price over [start, end].

        VWAP = sum(price_i * volume_i) / sum(volume_i)

    across the daily bars in the window. Added for the lodestar buyback-
    timing bucket (a company's own dollar-weighted repurchase price vs. the
    market's VWAP over the same window), but this is generic price math any
    tool would need, not a scoring-specific calculation.

    `price` selects the per-bar price:
      'typical' -> (high + low + close) / 3, the conventional VWAP input
      'close'   -> closing price only

    `adjust` is passed straight to get_prices — 'split' (the default) keeps
    the window comparable across a split without pulling dividends into a
    price-level comparison; use 'none' for raw prices or 'total' if a
    dividend-inclusive comparison is actually what's wanted.

    Returns None if there are no cached bars in the window (window predates
    the cache, or the ticker was never built with --prices).
    """
    df = get_prices(conn, ticker, start=start, end=end, adjust=adjust)
    if df.empty:
        return None

    prefix = "adj_" if adjust != "none" else ""
    if price == "typical":
        p = (df[f"{prefix}high"] + df[f"{prefix}low"] + df[f"{prefix}close"]) / 3.0
    elif price == "close":
        p = df[f"{prefix}close"]
    else:
        raise ValueError(f"unknown price basis {price!r}; use 'typical' or 'close'")

    vol = df[f"{prefix}volume"] if adjust != "none" else df["volume"]
    total_vol = float(vol.sum())
    if total_vol <= 0:
        return None
    return float((p * vol).sum() / total_vol)
