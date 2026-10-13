"""The store.

Design note, because this is the decision everything else depends on:

`facts` is APPEND-ONLY and BITEMPORAL. Every row carries two dates:

    period_end  -- the period the number describes  ("valid time")
    filed       -- the date the number became public ("transaction time")

A restatement does not overwrite anything. It arrives as a new row with a new
`accn` and a later `filed`, sitting alongside the original. That is the whole
trick. "Latest known" and "as it looked on 2019-03-14" become the same query
with a different `filed <= ?` bound.

If you ever find yourself writing `UPDATE facts SET val = ...`, stop. You are
about to destroy the only thing that makes a backtest honest.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from contextlib import contextmanager
from typing import Iterable, Iterator, Sequence

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

-- ---------------------------------------------------------------- securities
CREATE TABLE IF NOT EXISTS securities (
    cik              INTEGER PRIMARY KEY,
    name             TEXT,
    sic              TEXT,
    sic_desc         TEXT,
    fiscal_year_end  TEXT,      -- e.g. '0930' for a Sept year-end
    first_filed      TEXT,      -- earliest filing: proxy for "this existed by"
    last_filed       TEXT,      -- latest filing: proxy for "still alive"
    updated_at       TEXT
);

-- Ticker <-> CIK is NOT stable. Tickers get reused after delisting, and
-- companies rename. Storing it with a validity window is the difference
-- between a correct 2015 screen and one that silently uses today's mapping.
CREATE TABLE IF NOT EXISTS ticker_map (
    ticker      TEXT NOT NULL,
    cik         INTEGER NOT NULL,
    exchange    TEXT,
    valid_from  TEXT NOT NULL,
    valid_to    TEXT,           -- NULL = still current
    -- 1 => valid_from was inferred from the company's first filing rather
    -- than observed by diffing two runs of the SEC ticker file. See
    -- sources.sec.backfill_ticker_validity for why that inference exists and
    -- exactly when it is and isn't applied. Never let an inferred window be
    -- mistaken for an observed one.
    valid_from_inferred INTEGER DEFAULT 0,
    -- WHERE THIS MAPPING CAME FROM. 'sec' (the default, and every row written
    -- before this column existed) means it was observed in the SEC's ticker
    -- file. 'name-search' means the ticker files do not contain it and it was
    -- resolved through EDGAR's company search instead.
    --
    -- The distinction is load-bearing, not documentation. `load_ticker_map`
    -- closes out every live mapping missing from the SEC file, on the theory
    -- that a disappearance means a delisting or a ticker change. A mapping
    -- that was never IN that file disappears from it on every single run — so
    -- AEP was resolved by name, written, and closed again by the next build,
    -- with the ticker resolving to nothing in between.
    source TEXT DEFAULT 'sec',
    PRIMARY KEY (ticker, cik, valid_from)
);
CREATE INDEX IF NOT EXISTS idx_tm_ticker ON ticker_map (ticker, valid_from);
CREATE INDEX IF NOT EXISTS idx_tm_cik    ON ticker_map (cik);

-- ------------------------------------------------------------------ universe
-- WHICH SYMBOLS THE REFRESH TIERS KEEP CURRENT. Deliberately not a claim
-- about index membership on a date -- `ticker_map` answers "what did this
-- ticker mean then", `pit.universe` answers "who was filing then", and this
-- answers the third, operational question: which instruments is this cache
-- being maintained for.
--
-- It exists because the refresh loop was answering that question with
-- `ticker_map`, which carries every symbol the SEC maps to a filer CIK. That
-- includes each filer's preferred series, baby bonds, warrants and notes, so
-- 209 of the 711 symbols being swept were instruments like ALL-PB, PSA-PJ,
-- AIZN and OXY-WT. The market source publishes no fundamentals for any of
-- them, so roughly a third of every sweep was spent collecting 404s on a
-- rate-limited connection -- and none of it could ever reach the hub, which
-- scores the constituent list.
--
-- Empty is a valid state and means "sweep everything with facts", which is
-- the behaviour that predates this table.
CREATE TABLE IF NOT EXISTS universe (
    ticker    TEXT PRIMARY KEY,
    cik       INTEGER,
    name      TEXT,
    source    TEXT,           -- the file that declared this membership
    added_at  TEXT
);

-- --------------------------------------------------------------------- facts
CREATE TABLE IF NOT EXISTS facts (
    cik           INTEGER NOT NULL,
    taxonomy      TEXT    NOT NULL,   -- us-gaap | dei | ifrs-full | srt
    tag           TEXT    NOT NULL,   -- Revenues, Assets, ...
    unit          TEXT    NOT NULL,   -- USD | shares | USD/shares
    period_start  TEXT,               -- NULL => instant fact (balance sheet)
    period_end    TEXT    NOT NULL,
    val           REAL    NOT NULL,
    fy            INTEGER,
    fp            TEXT,               -- Q1 Q2 Q3 FY
    form          TEXT,               -- 10-K 10-Q 10-K/A 8-K 20-F
    accn          TEXT    NOT NULL,   -- accession number = identifies the filing
    filed         TEXT    NOT NULL,   -- <<<< the point-in-time key
    frame         TEXT,
    PRIMARY KEY (cik, taxonomy, tag, unit, period_end, period_start, accn)
);
-- The index that makes as-of queries fast. Order matters: equality columns
-- first, then the range column.
CREATE INDEX IF NOT EXISTS idx_facts_pit
    ON facts (cik, tag, period_end, filed);
CREATE INDEX IF NOT EXISTS idx_facts_filed
    ON facts (filed);

-- ------------------------------------------------------------------- filings
-- Independent of facts: lets you answer "what had this company disclosed by
-- date D" even for forms that carry no XBRL (8-K, S-1, DEF 14A).
CREATE TABLE IF NOT EXISTS filings (
    accn         TEXT PRIMARY KEY,
    cik          INTEGER NOT NULL,
    form         TEXT,
    filed        TEXT,
    period       TEXT,
    primary_doc  TEXT
);
CREATE INDEX IF NOT EXISTS idx_filings_cik ON filings (cik, filed);

-- ------------------------------------------------------------- corp_actions
-- Stored RAW. Adjusted prices are computed at read time, never stored.
-- Reason: a split announced tomorrow changes every adjusted close in history.
-- If you cache adj_close you must rewrite the whole file. If you cache raw
-- close + an actions table, you append one row.
CREATE TABLE IF NOT EXISTS corp_actions (
    ticker  TEXT NOT NULL,
    date    TEXT NOT NULL,
    kind    TEXT NOT NULL,          -- 'split' | 'dividend'
    value   REAL NOT NULL,          -- split ratio (2.0 = 2-for-1) | cash/share
    PRIMARY KEY (ticker, date, kind)
);

-- --------------------------------------------------------------------- macro
-- FRED revises. GDP for Q1 published in April is not the number you see today.
-- ALFRED gives you the vintages; realtime_start is the PIT key.
CREATE TABLE IF NOT EXISTS macro (
    series_id       TEXT NOT NULL,
    obs_date        TEXT NOT NULL,   -- the period the number describes
    realtime_start  TEXT NOT NULL,   -- first date this value was public
    value           REAL,
    PRIMARY KEY (series_id, obs_date, realtime_start)
);
CREATE INDEX IF NOT EXISTS idx_macro_pit ON macro (series_id, realtime_start);

CREATE TABLE IF NOT EXISTS macro_meta (
    series_id  TEXT PRIMARY KEY,
    title      TEXT,
    units      TEXT,
    frequency  TEXT,
    updated_at TEXT
);

-- --------------------------------------------------------------- quote_cache
-- A DERIVED INDEX OF THE PRICE PARQUETS. Not a second source of truth: every
-- row records the mtime and size of the parquet it was read from, and a
-- reader that finds those changed re-reads the file. So this can be deleted
-- at any time and costs only speed, never correctness.
--
-- It exists because the screener needs the last bar for 500 names on every
-- page load, and opening 500 parquet files takes ~7 seconds. Two sources of
-- price that could disagree is the bug this whole area was fixed for, so the
-- staleness check is the point of the design rather than an optimisation on
-- top of it.
CREATE TABLE IF NOT EXISTS quote_cache (
    ticker        TEXT PRIMARY KEY,
    bar_date      TEXT,       -- the DATE of the bar, so staleness is visible
    close         REAL,
    previous_close REAL,
    open          REAL,
    high          REAL,
    low           REAL,
    volume        REAL,
    source_mtime  REAL,       -- of the parquet this was read from
    source_size   INTEGER,
    updated_at    TEXT
);

-- ----------------------------------------------------------------- bookkeeping
CREATE TABLE IF NOT EXISTS fetch_log (
    resource   TEXT PRIMARY KEY,
    fetched_at TEXT,
    status     TEXT,
    note       TEXT
);

-- ================================================================== market
-- Everything the SEC does not publish: live prices, analyst estimates,
-- short interest, ownership. Sourced from the market data provider, not from
-- filings.
--
-- EVERY table here is keyed on `as_of`, the date the value was CAPTURED.
-- That is what keeps this data point-in-time like the rest of finlake. An
-- estimate is not a fact about a period, it is a fact about what analysts
-- believed on a date, and it gets revised constantly. Storing only the
-- current value would mean a backtest run over last year silently uses this
-- morning's consensus -- the exact lookahead the `filed` column exists to
-- prevent on the filings side.
--
-- Snapshots accumulate ACROSS DAYS; within one day the newest capture wins.
-- `as_of` is a date, so an INSERT OR IGNORE meant the first capture of the
-- day was permanent and every later refresh that day was silently dropped --
-- the write reported success, and the price simply never moved. `captured_at`
-- records the wall-clock time of whichever capture is currently held, so
-- "how fresh is this" is answerable rather than assumed.

-- Daily quote + key statistics snapshot.
CREATE TABLE IF NOT EXISTS market_snapshot (
    ticker              TEXT NOT NULL,
    as_of               TEXT NOT NULL,   -- capture date  <<< the PIT key
    price               REAL,
    market_cap          REAL,
    enterprise_value    REAL,
    shares_outstanding  REAL,
    float_shares        REAL,
    beta                REAL,
    week52_high         REAL,
    week52_low          REAL,
    avg_volume          REAL,
    -- Provider-computed multiples. Kept ALONGSIDE finlake's own, never
    -- instead of them: they are the independent second opinion the data
    -- quality checks reconcile against. Where they disagree, that is a
    -- finding, not a reason to silently prefer one.
    trailing_pe         REAL,
    forward_pe          REAL,
    trailing_eps        REAL,
    forward_eps         REAL,           -- the input that makes forward P/E possible
    price_to_book       REAL,
    price_to_sales      REAL,
    dividend_yield      REAL,           -- a FRACTION, never a percentage
    payout_ratio        REAL,
    captured_at         TEXT,           -- wall clock of the held capture
    PRIMARY KEY (ticker, as_of)
);
CREATE INDEX IF NOT EXISTS idx_snap_asof ON market_snapshot (as_of);

-- Consensus estimates. `period` is provider-relative: 0q = current quarter,
-- +1q = next quarter, 0y = current fiscal year, +1y = next fiscal year.
CREATE TABLE IF NOT EXISTS estimates (
    ticker        TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    metric        TEXT NOT NULL,        -- 'eps' | 'revenue'
    period        TEXT NOT NULL,        -- '0q' | '+1q' | '0y' | '+1y'
    avg           REAL,
    low           REAL,
    high          REAL,
    n_analysts    INTEGER,
    year_ago      REAL,
    growth        REAL,
    PRIMARY KEY (ticker, as_of, metric, period)
);

-- How the consensus has MOVED. This is the real version of the estimate
-- revision signal that momentum currently approximates by diffing finlake's
-- own filed data -- see lodestar's `fundamental_revision`, documented as a
-- proxy because no consensus feed existed. One now does.
CREATE TABLE IF NOT EXISTS estimate_trend (
    ticker        TEXT NOT NULL,
    as_of         TEXT NOT NULL,
    period        TEXT NOT NULL,
    current       REAL,
    days_7        REAL,
    days_30       REAL,
    days_60       REAL,
    days_90       REAL,
    up_7          INTEGER,
    up_30         INTEGER,
    down_7        INTEGER,
    down_30       INTEGER,
    PRIMARY KEY (ticker, as_of, period)
);

CREATE TABLE IF NOT EXISTS analyst_targets (
    ticker              TEXT NOT NULL,
    as_of               TEXT NOT NULL,
    price_current       REAL,
    target_mean         REAL,
    target_median       REAL,
    target_high         REAL,
    target_low          REAL,
    n_analysts          INTEGER,
    recommendation_mean REAL,           -- 1 = strong buy ... 5 = strong sell
    recommendation_key  TEXT,
    PRIMARY KEY (ticker, as_of)
);

CREATE TABLE IF NOT EXISTS recommendations (
    ticker      TEXT NOT NULL,
    as_of       TEXT NOT NULL,
    period      TEXT NOT NULL,          -- '0m' = this month, '-1m' = last, ...
    strong_buy  INTEGER,
    buy         INTEGER,
    hold        INTEGER,
    sell        INTEGER,
    strong_sell INTEGER,
    PRIMARY KEY (ticker, as_of, period)
);

-- Actual vs. consensus, per reported quarter. Repeated beats, and the size
-- of the surprise, are both signals in their own right.
CREATE TABLE IF NOT EXISTS earnings_history (
    ticker        TEXT NOT NULL,
    quarter       TEXT NOT NULL,        -- quarter end
    eps_actual    REAL,
    eps_estimate  REAL,
    surprise_pct  REAL,
    as_of         TEXT,
    PRIMARY KEY (ticker, quarter)
);

CREATE TABLE IF NOT EXISTS short_interest (
    ticker              TEXT NOT NULL,
    as_of               TEXT NOT NULL,
    shares_short        REAL,
    shares_short_prior  REAL,
    short_ratio         REAL,           -- days to cover
    short_pct_float     REAL,
    PRIMARY KEY (ticker, as_of)
);

CREATE TABLE IF NOT EXISTS ownership (
    ticker           TEXT NOT NULL,
    as_of            TEXT NOT NULL,
    pct_insiders     REAL,
    pct_institutions REAL,
    n_institutions   INTEGER,
    PRIMARY KEY (ticker, as_of)
);

-- Slow-changing descriptive data. Overwritten rather than snapshotted: a
-- company's sector is not a time series worth keeping vintages of, and this
-- is what feeds the industry/peer-group mapping.
CREATE TABLE IF NOT EXISTS profile (
    ticker      TEXT PRIMARY KEY,
    cik         INTEGER,
    name        TEXT,
    sector      TEXT,
    industry    TEXT,
    country     TEXT,
    exchange    TEXT,
    employees   INTEGER,
    website     TEXT,
    summary     TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker      TEXT NOT NULL,
    event_date  TEXT NOT NULL,
    kind        TEXT,                   -- 'earnings' | 'ex_dividend' | ...
    as_of       TEXT,
    PRIMARY KEY (ticker, event_date, kind)
);

-- =================================================================== news
-- Articles are stored ONCE and linked to tickers many-to-many.
--
-- Both halves of that matter. The same story appears on five feeds within
-- minutes -- a Reuters piece syndicates to Yahoo, Google News, and two
-- aggregators -- and counting it five times would turn "widely syndicated"
-- into "five independent pieces of bad news", which is precisely the signal
-- the news bucket is trying to measure. Deduping on normalized title is what
-- prevents that. And one article legitimately concerns several tickers (a
-- merger has two sides), so the link table is a real relationship rather
-- than a denormalization.
CREATE TABLE IF NOT EXISTS news_articles (
    id            TEXT PRIMARY KEY,   -- hash of the normalized title
    url           TEXT,
    source        TEXT,               -- the feed we got it from
    publisher     TEXT,               -- who actually wrote it, when known
    title         TEXT NOT NULL,
    summary       TEXT,
    published_at  TEXT NOT NULL,      -- <<< the PIT key for news
    fetched_at    TEXT NOT NULL,
    event_class   TEXT,               -- earnings | guidance | ma | legal | ...
    -- NULL means no sentiment vocabulary was found, which is NOT the same as
    -- a neutral 0.0. See lexicon.score().
    sentiment     REAL,
    pos_count     INTEGER,
    neg_count     INTEGER,
    word_count    INTEGER,
    source_weight REAL                 -- reliability of the source
);
CREATE INDEX IF NOT EXISTS idx_news_published ON news_articles (published_at);

CREATE TABLE IF NOT EXISTS news_tickers (
    id      TEXT NOT NULL,
    ticker  TEXT NOT NULL,
    PRIMARY KEY (id, ticker)
);
CREATE INDEX IF NOT EXISTS idx_news_ticker ON news_tickers (ticker);
"""


def connect(read_only: bool = False) -> sqlite3.Connection:
    config.ensure_dirs()
    if read_only and config.DB_PATH.exists():
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added to existing tables after their first release. `CREATE TABLE IF
# NOT EXISTS` does nothing to a table that already exists, so a new column in
# SCHEMA above never reaches a cache that has already been built — and the
# first write naming it fails with "no such column" on a database that looks
# perfectly healthy.
MIGRATIONS: list[tuple[str, str, str]] = [
    ("market_snapshot", "captured_at", "TEXT"),
    # Existing rows default to 'sec', which is what they are: every mapping
    # written before this column existed came from the SEC ticker file.
    ("ticker_map", "source", "TEXT DEFAULT 'sec'"),
]


def _apply_migrations(conn: sqlite3.Connection) -> None:
    for table, column, decl in MIGRATIONS:
        try:
            existing = {r[1] for r in conn.execute(
                f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if not existing or column in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        except sqlite3.OperationalError:
            pass    # a concurrent process added it first; nothing to do


def init_db(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    conn = conn or connect()
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        if own:
            conn.close()


@contextmanager
def session(read_only: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(read_only=read_only)
    try:
        yield conn
        if not read_only:
            conn.commit()
    finally:
        conn.close()


def upsert_many(
    conn: sqlite3.Connection,
    table: str,
    columns: Sequence[str],
    rows: Iterable[Sequence],
    *,
    ignore_conflicts: bool = True,
) -> int:
    """Bulk insert. `INSERT OR IGNORE` is correct for `facts`: a row keyed by
    accn is immutable by definition, so a conflict means we already have it."""
    verb = "INSERT OR IGNORE" if ignore_conflicts else "INSERT OR REPLACE"
    placeholders = ",".join("?" * len(columns))
    sql = f"{verb} INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
    rows = list(rows)
    if not rows:
        return 0
    conn.executemany(sql, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# The declared universe. See the `universe` table's comment for why it exists.
# ---------------------------------------------------------------------------
def read_universe_file(path) -> list[tuple[str, int | None, str | None]]:
    """(ticker, cik, name) from a constituents CSV.

    Lives here rather than in the builder because the builder is not the only
    reader any more: `python -m finlake universe --file` declares the same
    list without rebuilding, so both need to agree on exactly what the file
    says. Two parsers is how a universe file comes to mean one thing to the
    build and another to the refresh loop.

    `#` lines are skipped: the universe builder writes its provenance and its
    survivorship warning into the file itself, and that belongs with the data
    rather than only in whatever documentation happens to be nearby.

    THE NAME COLUMN IS NOT DECORATION. The SEC's ticker files are a
    convenience index and genuinely miss real companies — AEP is in neither of
    them — so a ticker that fails to resolve is looked up by company name
    against EDGAR's search, which matches on names and finds nothing for
    "AEP".
    """
    import csv

    with open(path, encoding="utf-8") as fh:
        reader = csv.DictReader(line for line in fh if not line.startswith("#"))
        out = []
        for row in reader:
            ticker = (row.get("ticker") or "").strip().upper()
            if not ticker:
                continue
            raw_cik = (row.get("cik") or "").strip()
            try:
                cik = int(raw_cik) if raw_cik else None
            except ValueError:
                cik = None
            out.append((ticker, cik, (row.get("name") or "").strip() or None))
    return out


def declare_universe(conn: sqlite3.Connection, rows: Iterable[Sequence],
                     *, source: str) -> int:
    """Replace the declared universe with `rows` of (ticker, cik, name).

    REPLACES rather than merges. A constituent list is a statement about the
    whole set, so a name dropped from the file has to stop being swept —
    merging would mean the universe could only ever grow, and a company that
    left the index would be refreshed forever with nothing displaying it.

    Written in one transaction so a crash mid-write cannot leave a partial
    universe that looks complete.
    """
    stamp = dt.date.today().isoformat()
    prepared = [
        (str(t).upper().strip(), int(cik) if cik not in (None, "") else None,
         name, source, stamp)
        for t, cik, name in rows if str(t).strip()
    ]
    if not prepared:
        return 0
    conn.execute("DELETE FROM universe")
    conn.executemany(
        "INSERT OR REPLACE INTO universe (ticker, cik, name, source, added_at) "
        "VALUES (?,?,?,?,?)", prepared)
    conn.commit()
    return len(prepared)


def universe_members(conn: sqlite3.Connection) -> list[str]:
    """The declared universe, or [] if none has been declared.

    Never raises on a cache built before the table existed: an empty list is
    the honest answer there, and the caller's fallback is the behaviour that
    cache was already getting.
    """
    try:
        return [r[0] for r in conn.execute(
            "SELECT ticker FROM universe ORDER BY ticker")]
    except sqlite3.Error:
        return []


def universe_source(conn: sqlite3.Connection) -> str | None:
    """Where the declared universe came from, for reporting it on screen."""
    try:
        row = conn.execute(
            "SELECT source FROM universe WHERE source IS NOT NULL LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None
