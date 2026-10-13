"""Keeping the cache current.

    python -m finlake refresh --daemon      # run continuously
    python -m finlake refresh --once        # one pass of whatever is due
    python -m finlake refresh --status      # what is stale right now

TIERED BY HOW FAST EACH SOURCE ACTUALLY CHANGES. The cadences below differ by
three orders of magnitude, and pretending otherwise is not a neutral
simplification — it wastes requests on data that cannot have moved, and it
starves the data that has.

  quotes         3 min    prices move continuously, and this is the tier the
                          headline price, market cap and every valuation
                          multiple are read from
  news           5 min    RSS publishes continuously
  filings       10 min    EDGAR's feed is near-real-time; a 10-Q reaching the
                          hub minutes after publication is the "live" that
                          actually matters for fundamentals
  market         6 h      price targets, consensus estimates, 52-week range,
                          short interest and ownership — all from one `info`
                          response per name, so they share a tier whether or
                          not they each need it
  macro         daily     FRED publishes on its own calendar

THE QUOTE TIER IS BATCHED, AND UNTIL NOW ONLY CLAIMED TO BE. `task_quotes`
described itself as taking many symbols per request and then looped, one
request per name. Six hundred serial round trips do not fit in a 15-minute
window on a home connection, so the sweep ran continuously, never finished
cleanly, and the "15 minute" cadence was aspirational — while the same
serial traffic was the thing most likely to get the IP throttled. Batching
turns the sweep into ~15 requests, which is what makes three minutes both
possible and gentler than the fifteen it replaces.

WHY NOT FASTER STILL. The market source is unofficial and rate-limited
invisibly. Polling every name every minute gets the IP blocked, and a block
takes prices down *entirely* rather than merely making them stale — a strictly
worse outcome than the staleness it was trying to avoid.

WHY `market` IS NOT ON THE QUOTE TIER. It costs one full `info` request per
symbol and cannot be batched, so a universe sweep is ~500 requests however it
is scheduled. Price no longer depends on it: the headline quote comes from the
batched price cache, which is the same series the chart draws, so the two can
never disagree. What is left here — targets, consensus, 52-week range — moves
on an analyst's schedule, not a trader's.

CATCH-UP IS THE NORMAL CASE, not an edge case. This runs on a laptop that is
closed overnight and off for days. On start it works out what it missed from
`refresh_log` and runs those tasks in dependency order, so the hub opens on
current data instead of a stale run plus a queue of pending work.

Every task is failure-isolated: one dead feed costs that feed and never
stalls the others.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
import traceback
from dataclasses import dataclass
from typing import Callable

from . import config, store

# ---------------------------------------------------------------------------
# Cadence table. The single place these intervals are declared.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TaskResult:
    """A row count plus what the task wants said about the pass.

    THE NOTE IS THE REPLACEMENT FOR THE PROVIDER'S OWN NARRATION. The market
    source used to print a raw 404 body per symbol per endpoint, which was
    unreadable but did at least mean a source that had started refusing
    everything was obvious. Silencing that without putting anything in its
    place would trade noise for the far worse failure this codebase keeps
    running into: a number that is quietly wrong, or absent, and says nothing.

    So a tier reports its own coverage — "486 priced, 3 no coverage" — and the
    note lands in `refresh_log`, which the status table and the hub's health
    page already read.
    """

    rows: int
    note: str | None = None


@dataclass(frozen=True)
class Task:
    name: str
    interval_seconds: int
    fn: Callable[[sqlite3.Connection, list[str]], int | TaskResult]
    description: str
    # Tasks that must have run at least once before this one is useful.
    # On catch-up, a missing dependency runs first.
    depends_on: tuple[str, ...] = ()


MINUTE = 60
HOUR = 3600
DAY = 86400

SCHEMA = """
CREATE TABLE IF NOT EXISTS refresh_log (
    task        TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,              -- ok | failed | skipped
    rows        INTEGER,
    note        TEXT,
    PRIMARY KEY (task, started_at)
);
CREATE INDEX IF NOT EXISTS idx_refresh_task ON refresh_log (task, started_at);

-- Where a round-robin task got to. See SWEEP_CHUNK: a task that cannot
-- finish the universe inside its own interval takes a slice each pass and
-- resumes from here, instead of running long and starving every tier behind
-- it.
CREATE TABLE IF NOT EXISTS refresh_cursor (
    task     TEXT PRIMARY KEY,
    position INTEGER NOT NULL DEFAULT 0
);
"""

# How many tickers a round-robin task processes per pass.
#
# NEWS CANNOT SWEEP THE UNIVERSE IN FIVE MINUTES AND NEVER COULD. Four feeds
# per name, rate-limited to one request per second per host, is about four
# seconds a ticker — half an hour for 500 names against a five-minute
# interval. Because `run_once` walks the tiers in order, that half hour was
# spent with `filings` and `logos` sitting marked "due" behind it, which is
# what the status table was showing: quotes fresh, everything else 24 minutes
# late and waiting.
#
# Forty names is roughly two and a half minutes, so the pass returns well
# inside the interval and the whole universe still cycles about hourly —
# which is the right latency for news anyway. Nothing is skipped; the cursor
# just remembers where to resume.
SWEEP_CHUNK = 40


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def humanize(seconds: float | None) -> str:
    """A duration a person reads at a glance. "6h", not "360m"."""
    if seconds is None:
        return "—"
    seconds = float(seconds)
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 90 * MINUTE:
        return f"{seconds / MINUTE:.0f}m"
    if seconds < 48 * HOUR:
        return f"{seconds / HOUR:.0f}h"
    return f"{seconds / DAY:.0f}d"


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # The daemon is the one process guaranteed to run against an existing
    # cache, so it is where a column added after the cache was built has to be
    # applied. `CREATE TABLE IF NOT EXISTS` skips a table that already exists,
    # which means a new column never lands and the first write naming it fails
    # on a database that looks entirely healthy.
    store.init_db(conn)
    conn.commit()


def last_success(conn: sqlite3.Connection, task: str) -> dt.datetime | None:
    row = conn.execute(
        "SELECT MAX(finished_at) FROM refresh_log WHERE task = ? AND status = 'ok'",
        (task,),
    ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return dt.datetime.fromisoformat(row[0])
    except ValueError:
        return None


def seconds_since(conn: sqlite3.Connection, task: str) -> float | None:
    last = last_success(conn, task)
    if last is None:
        return None
    return (dt.datetime.now() - last).total_seconds()


def is_due(conn: sqlite3.Connection, task: Task) -> bool:
    age = seconds_since(conn, task.name)
    return age is None or age >= task.interval_seconds


# ---------------------------------------------------------------------------
# The tasks
# ---------------------------------------------------------------------------
def _universe_tickers(conn: sqlite3.Connection, limit: int | None = None) -> list[str]:
    """The symbols every tier sweeps.

    THE DECLARED UNIVERSE FIRST — `ticker_map` ONLY AS A FALLBACK, and that
    fallback was the bug. `ticker_map` holds every symbol the SEC maps to a
    filer CIK, which is not a list of companies: it is a list of INSTRUMENTS.
    Allstate contributes ALL and four preferred series; Public Storage
    contributes fifteen; Occidental contributes a warrant. On this cache that
    was 209 symbols out of 711.

    Every one of them was swept on all five tiers. The market source publishes
    no estimates, targets or earnings history for a preferred share, so each
    cost about five requests and returned five 404s — roughly a thousand
    wasted requests per sweep on a source that responds to volume by blocking
    the IP, which takes prices down entirely rather than merely making them
    stale. And none of it could ever surface: the hub scores the constituent
    list, so nothing downstream so much as looked at those rows.

    A cache with no declared universe keeps the old behaviour rather than
    refreshing nothing, so this cannot silently stop a build that predates the
    table. Declare one with `python -m finlake universe --file <csv>`.
    """
    declared = store.universe_members(conn)
    if not declared:
        rows = conn.execute(
            "SELECT DISTINCT ticker FROM ticker_map WHERE valid_to IS NULL "
            "AND cik IN (SELECT DISTINCT cik FROM facts) ORDER BY ticker"
        ).fetchall()
        declared = [r["ticker"] for r in rows]
    return declared[:limit] if limit else declared


# How many names with no cached history at all to back-fill per pass. A full
# back-to-listing pull is a large request and cannot be batched with the
# incremental tail, so they are drained a few at a time rather than allowed to
# turn one quote sweep into a twenty-minute build.
FULL_HISTORY_PER_PASS = 12


def task_quotes(conn: sqlite3.Connection, tickers: list[str]) -> TaskResult:
    """Latest bars for the universe, in a handful of batched requests.

    THE PRICE EVERY OTHER NUMBER HANGS OFF. The company header, market cap,
    enterprise value and every valuation multiple read the last bar this task
    wrote, and so does the price chart — one series, so a header and a chart
    drawn from it cannot disagree. Which is what they used to do: the header
    took its price from the last fiscal quarter end and read $373 for
    Microsoft on a day the chart's own last point was $509.

    `load_prices_many` asks for only the tail — what has happened since each
    name's last cached bar — for forty symbols at a time.
    """
    from .sources import prices as price_src

    written, needs_full = price_src.load_prices_many(conn, tickers)
    updated = sum(1 for count in written.values() if count)

    # Names with nothing cached need a full history, one request each. Capped
    # per pass so a first run drains over several minutes instead of blocking
    # every other tier behind it — and bounded by `backfill_missing`, so a
    # symbol the provider has no history for stops occupying the queue. See
    # BACKFILL_RETRY_HOURS for what that was costing.
    back = price_src.backfill_missing(
        conn, needs_full, limit=FULL_HISTORY_PER_PASS,
        provider_healthy=updated > 0)

    note = f"{updated} priced of {len(tickers)}"
    if back["loaded"]:
        note += f", {back['loaded']} back-filled"
    if back["empty"] or back["skipped"]:
        note += f", {back['empty'] + back['skipped']} no history"
    return TaskResult(updated + back["loaded"], note)


def next_slice(conn: sqlite3.Connection, task: str, tickers: list[str], *,
               size: int = SWEEP_CHUNK) -> list[str]:
    """The next `size` tickers for a round-robin task, and advance the cursor.

    Wraps at the end of the universe, so every name is reached in turn and no
    name is reached twice before the rest have had a turn.
    """
    if not tickers:
        return []
    row = conn.execute(
        "SELECT position FROM refresh_cursor WHERE task = ?", (task,)
    ).fetchone()
    start = int(row["position"]) % len(tickers) if row else 0

    # Wrap by walking from `start`, so a universe that shrank between passes
    # cannot produce an out-of-range slice.
    chunk = [tickers[(start + i) % len(tickers)]
             for i in range(min(size, len(tickers)))]
    conn.execute(
        "INSERT INTO refresh_cursor (task, position) VALUES (?, ?) "
        "ON CONFLICT(task) DO UPDATE SET position = excluded.position",
        (task, (start + len(chunk)) % len(tickers)))
    conn.commit()
    return chunk


def task_news(conn: sqlite3.Connection, tickers: list[str]) -> int:
    """Multi-source news for a SLICE of the universe — see SWEEP_CHUNK."""
    from .sources import news as news_src

    tickers = next_slice(conn, "news", tickers)
    new_articles = 0
    for ticker in tickers:
        cik = conn.execute(
            "SELECT cik FROM ticker_map WHERE ticker = ? AND valid_to IS NULL",
            (ticker,),
        ).fetchone()
        name = None
        if cik:
            row = conn.execute("SELECT name FROM securities WHERE cik = ?",
                               (cik["cik"],)).fetchone()
            name = row["name"] if row else None
        try:
            got = news_src.load_news(conn, ticker,
                                     cik=cik["cik"] if cik else None,
                                     company=name)
            new_articles += got.get("new", 0)
        except Exception:
            continue
    return new_articles


def task_filings(conn: sqlite3.Connection, tickers: list[str]) -> int:
    """Watch EDGAR for new filings by universe companies.

    The highest-value tier. A company files a 10-Q and the hub has it minutes
    later rather than at the next nightly rebuild — and because a filing is
    what changes fundamentals, this is the only tier where "live" means
    anything for the scoring side.
    """
    from .sources import sec

    tickers = next_slice(conn, "filings", tickers)
    changed = 0
    for ticker in tickers:
        row = conn.execute(
            "SELECT cik FROM ticker_map WHERE ticker = ? AND valid_to IS NULL",
            (ticker,),
        ).fetchone()
        if not row:
            continue
        cik = row["cik"]
        before = conn.execute(
            "SELECT COUNT(*) FROM filings WHERE cik = ?", (cik,)).fetchone()[0]
        try:
            sec.load_submissions(conn, cik)
            after = conn.execute(
                "SELECT COUNT(*) FROM filings WHERE cik = ?", (cik,)).fetchone()[0]
            if after > before:
                # New filing: pull the facts immediately rather than waiting
                # for the daily fundamentals sweep.
                sec.load_company_facts(conn, cik)
                changed += 1
        except Exception:
            continue
    return changed


def task_market(conn: sqlite3.Connection, tickers: list[str]) -> TaskResult:
    """Estimates, targets, short interest and ownership, one request per name.

    REPORTS ITS OWN COVERAGE. The provider's per-symbol logging is suppressed
    (sources/_provider.py), so this is the only thing that would say a source
    had stopped answering — "0 loaded, 503 no coverage" is unmistakable, and
    a handful of no-coverage names among hundreds is the ordinary case of an
    index constituent the provider has dropped after an acquisition.
    """
    from .sources import market

    updated = missing = failed = 0
    for ticker in tickers:
        try:
            counts = market.load_market_data(conn, ticker)
        except Exception:
            failed += 1
            continue
        if not counts or "error" in counts:
            failed += 1
        elif "empty" in counts:
            missing += 1
        else:
            updated += 1

    note = f"{updated} loaded"
    if missing:
        note += f", {missing} no coverage"
    if failed:
        note += f", {failed} failed"
    return TaskResult(updated, note)


def task_logos(conn: sqlite3.Connection, tickers: list[str]) -> int:
    """Company logos, for names that do not have one yet.

    Weekly, and cheap: it reads each company's website from the profile table
    the market loader already fills, skips anything already tried, and records
    a miss so a company with no icon is not re-requested every pass. A logo is
    decoration — nothing here can fail in a way that matters.
    """
    from .sources import logos

    counts = logos.load_logos(conn, tickers)
    return counts.get("fetched", 0)


def task_macro(conn: sqlite3.Connection, _tickers: list[str]) -> int:
    from .sources import fred

    if not config.FRED_API_KEY:
        return 0
    return sum(fred.load_default_series(conn).values())


TASKS: list[Task] = [
    Task("quotes", 3 * MINUTE, task_quotes,
         "batched price bars — the headline price and every multiple"),
    Task("news", 5 * MINUTE, task_news, "multi-source news"),
    Task("filings", 10 * MINUTE, task_filings,
         "EDGAR filing watch (pulls facts on a new filing)"),
    Task("market", 6 * HOUR, task_market,
         "targets, consensus estimates, 52-week range, short interest"),
    Task("macro", 1 * DAY, task_macro, "FRED series"),
    # Weekly because a company changes its logo roughly never, and every name
    # that already has one is skipped without a request.
    Task("logos", 7 * DAY, task_logos, "company logos for the universe"),
]
BY_NAME = {t.name: t for t in TASKS}


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------
def run_task(conn: sqlite3.Connection, task: Task, tickers: list[str]) -> int:
    started = _now()
    conn.execute(
        "INSERT OR REPLACE INTO refresh_log "
        "(task, started_at, status) VALUES (?,?,?)",
        (task.name, started, "running"))
    conn.commit()

    try:
        result = task.fn(conn, tickers)
        if not isinstance(result, TaskResult):
            result = TaskResult(int(result))
        conn.execute(
            "UPDATE refresh_log SET finished_at=?, status=?, rows=?, note=? "
            "WHERE task=? AND started_at=?",
            (_now(), "ok", result.rows, result.note or task.description,
             task.name, started))
        conn.commit()
        return result.rows
    except Exception as exc:
        # Recorded, not raised. One failing source must never stop the
        # others, and a silent failure is worse than a slow one: without
        # this row the task simply looks like it never ran.
        conn.execute(
            "UPDATE refresh_log SET finished_at=?, status=?, rows=?, note=? "
            "WHERE task=? AND started_at=?",
            (_now(), "failed", 0,
             f"{type(exc).__name__}: {exc}"[:300], task.name, started))
        conn.commit()
        return 0


def due_tasks(conn: sqlite3.Connection) -> list[Task]:
    return [t for t in TASKS if is_due(conn, t)]


def run_once(conn: sqlite3.Connection, *, limit: int | None = None,
             only: list[str] | None = None, verbose: bool = True,
             on_task=None) -> dict[str, int]:
    """One pass over whatever is due. This IS the catch-up path.

    A task that has never run, or whose last success is older than its
    interval, is due — so a machine that was off for a week runs everything
    once on start and then settles into its normal cadence. No separate
    catch-up mode to keep in sync with the normal one.
    """
    ensure_schema(conn)
    tickers = _universe_tickers(conn, limit)
    if not tickers:
        if verbose:
            print("No universe in the cache yet. Run scripts/build.py first.")
        return {}

    tasks = [BY_NAME[n] for n in only if n in BY_NAME] if only else due_tasks(conn)
    results: dict[str, int] = {}
    for task in tasks:
        if verbose:
            age = seconds_since(conn, task.name)
            age_txt = "never run" if age is None else f"{humanize(age)} ago"
            print(f"  {task.name:<10} ({age_txt}) ... ", end="", flush=True)
        t0 = time.time()
        rows = run_task(conn, task, tickers)
        results[task.name] = rows
        elapsed = time.time() - t0
        if verbose:
            print(f"{rows} updated in {elapsed:.0f}s")
        # A caller driving this from a long-running loop wants to report each
        # tier as it lands. Without it the daemon prints nothing for the
        # length of the slowest task and looks hung.
        #
        # The note carries the tier's own coverage summary — see TaskResult.
        # It is read back from the log rather than returned, so a caller sees
        # exactly what was recorded rather than a second, parallel version of
        # it that could drift.
        if on_task is not None:
            try:
                on_task(task.name, rows, elapsed, _last_note(conn, task.name))
            except Exception:
                pass
    return results


def _last_note(conn: sqlite3.Connection, task: str) -> str | None:
    row = conn.execute(
        "SELECT note FROM refresh_log WHERE task = ? "
        "ORDER BY started_at DESC LIMIT 1", (task,)).fetchone()
    return row["note"] if row else None


def daemon(*, poll_seconds: int = 60, limit: int | None = None) -> None:
    """Run until interrupted, doing whatever is due each tick.

    Polls rather than sleeps until the next deadline: the machine suspends,
    the clock jumps, and a computed sleep would overshoot by however long the
    lid was closed. Checking cheaply once a minute is correct across suspend
    and costs nothing.
    """
    print("finlake refresh daemon. Ctrl-C to stop.")
    for task in TASKS:
        print(f"  {task.name:<10} every {humanize(task.interval_seconds):>4}  "
              f"— {task.description}")
    print()

    while True:
        try:
            with store.session() as conn:
                ensure_schema(conn)
                due = due_tasks(conn)
                if due:
                    print(f"[{_now()}] {len(due)} due: "
                          f"{', '.join(t.name for t in due)}")
                    run_once(conn, limit=limit)
        except KeyboardInterrupt:
            print("\nstopped.")
            return
        except Exception:
            # The loop itself must survive anything a task did not catch --
            # a daemon that dies overnight is indistinguishable from one that
            # was never started.
            traceback.print_exc()
        time.sleep(poll_seconds)


def status(conn: sqlite3.Connection) -> list[dict]:
    """What each task last did, and whether it is due. Rendered in the UI so
    freshness is visible rather than assumed."""
    ensure_schema(conn)
    out = []
    for task in TASKS:
        row = conn.execute(
            "SELECT started_at, finished_at, status, rows, note FROM refresh_log "
            "WHERE task = ? ORDER BY started_at DESC LIMIT 1", (task.name,),
        ).fetchone()
        age = seconds_since(conn, task.name)
        out.append({
            "task": task.name,
            "every": humanize(task.interval_seconds),
            "last_success": None if age is None else f"{humanize(age)} ago",
            "last_status": row["status"] if row else "never run",
            "rows": row["rows"] if row else None,
            "due": is_due(conn, task),
            # `note` is NULL while a task is running, and printing that as
            # "None" made the daemon's own header read
            # "news every 5m — None". The description is what the column is
            # for when there is no note yet.
            "note": (row["note"] if row and row["note"] else task.description),
        })
    return out
