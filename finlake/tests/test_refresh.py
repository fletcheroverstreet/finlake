"""The refresh scheduler.

Offline. What is tested here is the scheduling and failure-isolation logic —
when a task is due, what happens on catch-up after the machine was off, and
whether one dead source can take the others down with it. The fetching itself
belongs to the source modules and their own tests.
"""

import datetime as dt
import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_refresh_")

import pytest  # noqa: E402

from finlake import refresh, store  # noqa: E402


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    refresh.ensure_schema(conn)
    # Every test module shares one cache — see conftest.py, where FINLAKE_HOME
    # is resolved once for the whole session — so a declared universe left
    # behind here would change what a later module's sweep covers.
    conn.executescript("DELETE FROM refresh_log; DELETE FROM universe;")
    conn.commit()
    return conn


def record(conn, task, *, minutes_ago, status="ok"):
    when = (dt.datetime.now() - dt.timedelta(minutes=minutes_ago)) \
        .isoformat(timespec="seconds")
    conn.execute(
        "INSERT OR REPLACE INTO refresh_log "
        "(task, started_at, finished_at, status, rows) VALUES (?,?,?,?,?)",
        (task, when, when, status, 1))
    conn.commit()


# ---------------------------------------------------------------------------
# Due-ness
# ---------------------------------------------------------------------------
def test_a_task_that_has_never_run_is_due():
    """This is also the catch-up path: a fresh cache and a machine that was
    off for a week look identical to the scheduler, which is why there is no
    separate catch-up mode to keep in sync."""
    conn = fresh_db()
    for task in refresh.TASKS:
        assert refresh.is_due(conn, task), f"{task.name} should be due"
    conn.close()


def test_a_recently_run_task_is_not_due():
    conn = fresh_db()
    record(conn, "news", minutes_ago=1)
    assert not refresh.is_due(conn, refresh.BY_NAME["news"])
    conn.close()


def test_a_task_becomes_due_once_its_interval_has_passed():
    """Each case starts from a clean log.

    Writing an older row on top of a newer one would not move the answer:
    `last_success` is MAX(finished_at), so the most recent success wins
    regardless of insertion order — which is the correct behaviour and the
    reason these two cases cannot share a log.
    """
    news = refresh.BY_NAME["news"]          # 5-minute cadence

    conn = fresh_db()
    record(conn, "news", minutes_ago=4)
    assert not refresh.is_due(conn, news), "due before its interval elapsed"
    conn.close()

    conn = fresh_db()
    record(conn, "news", minutes_ago=6)
    assert refresh.is_due(conn, news), "not due after its interval elapsed"
    conn.close()


def test_the_most_recent_success_wins_regardless_of_insert_order():
    """A late-arriving log row for an older run must not make a fresh task
    look stale and trigger a redundant fetch."""
    conn = fresh_db()
    record(conn, "news", minutes_ago=1)
    record(conn, "news", minutes_ago=90)     # backfilled older row
    assert not refresh.is_due(conn, refresh.BY_NAME["news"])
    conn.close()


def test_a_failed_run_does_not_count_as_success():
    """Otherwise a source that is failing gets quieter the more it fails —
    the failure resets the clock and the task waits a full interval before
    trying again, which is exactly backwards."""
    conn = fresh_db()
    record(conn, "news", minutes_ago=1, status="failed")
    assert refresh.is_due(conn, refresh.BY_NAME["news"]), (
        "a failed run suppressed the retry")
    conn.close()


def test_after_days_off_every_task_is_due_at_once():
    """The laptop-was-closed case. On start the hub should open on current
    data, not on a stale run plus a queue of pending work."""
    conn = fresh_db()
    for task in refresh.TASKS:
        record(conn, task.name, minutes_ago=60 * 24 * 7)
    due = {t.name for t in refresh.due_tasks(conn)}
    assert due == {t.name for t in refresh.TASKS}
    conn.close()


def test_cadences_are_ordered_by_how_fast_the_source_changes():
    """A guard on the tiering itself. Prices move continuously; estimates and
    macro move on their own slow calendars. Flattening these to one interval
    either wastes requests on data that cannot have moved or starves the data
    that has.

    Quotes lead. They used to sit behind news and filings on the theory that a
    quote sweep was the expensive tier — true while it issued one request per
    name, and false once it was batched. It is also the tier the headline
    price, market cap and every valuation multiple are read from, so its
    staleness is the most visible staleness in the product.
    """
    by = {t.name: t.interval_seconds for t in refresh.TASKS}
    assert by["quotes"] <= by["news"] <= by["filings"]
    assert by["filings"] < by["market"] < by["macro"]


def test_only_a_batched_task_may_poll_faster_than_five_minutes():
    """The real constraint is REQUESTS PER SWEEP, not minutes per sweep.

    The market source is unofficial and rate-limited invisibly, and a block
    takes prices down ENTIRELY rather than merely making them stale — strictly
    worse than the staleness it was avoiding. But what provokes a block is
    hundreds of round trips, not the calendar. `quotes` asks for forty symbols
    per request, so a full universe sweep is ~15 requests and three minutes is
    gentler than the fifteen-minute serial sweep it replaced. `market` costs
    one `info` request per name and cannot be batched, so it stays slow.

    This test therefore guards the pairing: a sub-five-minute tier must be one
    of the batched ones.
    """
    BATCHED = {"quotes"}
    for task in refresh.TASKS:
        if task.interval_seconds >= 5 * 60:
            continue
        assert task.name in BATCHED, (
            f"{task.name} polls every {task.interval_seconds}s but is not a "
            f"batched task — that is one request per name inside the range "
            f"that gets an unofficial source to block us")
        assert task.interval_seconds >= 60, (
            f"{task.name} polls every {task.interval_seconds}s; even batched, "
            f"under a minute is pointless for daily bars and rude to the host")


def test_a_slow_sweep_takes_a_slice_and_resumes(  ):
    """NEWS CANNOT SWEEP THE UNIVERSE IN FIVE MINUTES AND NEVER COULD. Four
    feeds per name, rate-limited to one request per second per host, is about
    four seconds a ticker — half an hour for 500 names against a five-minute
    interval. And because `run_once` walks the tiers in order, that half hour
    was spent with `filings` and `logos` marked "due" behind it: the status
    table showed quotes fresh and everything else 24 minutes late.

    A slice per pass returns inside the interval and still reaches every name
    in turn.
    """
    conn = fresh_db()
    universe = [f"T{i:03d}" for i in range(100)]

    first = refresh.next_slice(conn, "news", universe, size=40)
    second = refresh.next_slice(conn, "news", universe, size=40)
    third = refresh.next_slice(conn, "news", universe, size=40)

    assert first == universe[:40]
    assert second == universe[40:80]
    assert not set(first) & set(second), "a name was swept twice in a cycle"
    # The third wraps, and the wrap covers exactly what is left plus the start.
    assert third[:20] == universe[80:]
    assert third[20:] == universe[:20]
    conn.close()


def test_each_task_has_its_own_cursor():
    """News and filings advance independently — sharing a position would make
    one of them skip whatever the other just covered."""
    conn = fresh_db()
    universe = [f"T{i:03d}" for i in range(100)]

    refresh.next_slice(conn, "news", universe, size=40)
    assert refresh.next_slice(conn, "filings", universe, size=40) == universe[:40]
    conn.close()


def test_a_shrinking_universe_cannot_produce_an_out_of_range_slice():
    """The cursor is a position in a list that changes between passes — a
    name delisted overnight must not turn the next slice into an IndexError."""
    conn = fresh_db()
    refresh.next_slice(conn, "news", [f"T{i}" for i in range(100)], size=90)
    small = ["AAA", "BBB", "CCC"]
    assert set(refresh.next_slice(conn, "news", small, size=40)) <= set(small)
    conn.close()


def test_a_running_task_reports_its_description_not_none():
    """`note` is NULL while a task is in flight, and printing that made the
    daemon's own header read "news every 5m — None"."""
    conn = fresh_db()
    conn.execute(
        "INSERT INTO refresh_log (task, started_at, status) VALUES (?,?,?)",
        ("news", "2026-08-11T12:00:00", "running"))
    conn.commit()

    row = next(r for r in refresh.status(conn) if r["task"] == "news")
    assert row["note"] and row["note"] != "None"
    assert row["note"] == refresh.BY_NAME["news"].description
    conn.close()


def test_the_quote_sweep_is_actually_batched():
    """The bug this whole tier was rebuilt around: `task_quotes` DESCRIBED
    itself as taking many symbols per request and then looped, one request per
    ticker. Six hundred serial round trips do not fit in the fifteen-minute
    window it claimed, so the sweep never finished cleanly — while generating
    exactly the traffic pattern most likely to get the IP throttled.

    A docstring cannot be asserted on, so this asserts on the call: the task
    must hand the whole ticker list to the batch loader in one go.
    """
    from finlake.sources import prices as price_src

    seen = {}

    def fake_many(_conn, tickers, **_kw):
        seen["batch"] = list(tickers)
        return {t: 1 for t in tickers}, []

    original = price_src.load_prices_many
    price_src.load_prices_many = fake_many
    try:
        conn = fresh_db()
        universe = ["AAPL", "MSFT", "NVDA", "KO"]
        assert refresh.task_quotes(conn, universe).rows == len(universe)
        conn.close()
    finally:
        price_src.load_prices_many = original

    assert seen["batch"] == ["AAPL", "MSFT", "NVDA", "KO"], (
        "task_quotes did not pass the universe to the batch loader in one call")


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------
def test_a_failing_task_is_recorded_and_does_not_raise():
    """One dead feed must cost that feed and nothing else. It must also not
    fail SILENTLY — without the log row the task simply looks like it never
    ran, which is indistinguishable from a scheduler that is broken."""
    conn = fresh_db()

    def explode(_conn, _tickers):
        raise RuntimeError("feed is down")

    task = refresh.Task("boom", 60, explode, "test")
    rows = refresh.run_task(conn, task, ["AAPL"])
    assert rows == 0

    row = conn.execute(
        "SELECT status, note FROM refresh_log WHERE task = 'boom'").fetchone()
    assert row["status"] == "failed"
    assert "feed is down" in row["note"]
    conn.close()


def test_a_failed_task_leaves_earlier_successes_intact():
    conn = fresh_db()
    record(conn, "news", minutes_ago=30)

    def explode(_conn, _tickers):
        raise RuntimeError("nope")

    refresh.run_task(conn, refresh.Task("news", 300, explode, "test"), ["A"])
    # The old success is still the last SUCCESS, so due-ness is computed from
    # it rather than from the failure.
    age = refresh.seconds_since(conn, "news")
    assert age is not None and age > 25 * 60
    conn.close()


def test_run_task_records_row_counts_on_success():
    conn = fresh_db()
    task = refresh.Task("counted", 60, lambda c, t: 42, "test")
    assert refresh.run_task(conn, task, ["AAPL"]) == 42
    row = conn.execute(
        "SELECT status, rows FROM refresh_log WHERE task='counted'").fetchone()
    assert row["status"] == "ok" and row["rows"] == 42
    conn.close()


def test_status_reports_every_task_even_when_none_have_run():
    conn = fresh_db()
    rows = refresh.status(conn)
    assert {r["task"] for r in rows} == {t.name for t in refresh.TASKS}
    assert all(r["due"] for r in rows)
    assert all(r["last_status"] == "never run" for r in rows)
    conn.close()


def test_the_per_tier_callback_receives_the_coverage_note():
    """A CONTRACT THAT FAILS SILENTLY IF BROKEN. `run_once` wraps the callback
    in a bare except, because a reporting failure must never stop the refresh
    — which also means an arity mismatch would swallow a TypeError and the
    daemon would simply print nothing per tier, looking hung. And the note is
    now the only thing that reports a source which has stopped answering, the
    provider's own narration being suppressed.
    """
    conn = fresh_db()
    store.declare_universe(
        conn, [(t, None, None) for t in ("AAPL", "MSFT", "NVDA", "KO")],
        source="test.csv")
    seen = []

    def fake_quotes(_conn, tickers):
        return refresh.TaskResult(len(tickers), "4 priced of 4")

    original = refresh.BY_NAME["quotes"].fn
    object.__setattr__(refresh.BY_NAME["quotes"], "fn", fake_quotes)
    try:
        refresh.run_once(conn, only=["quotes"], verbose=False,
                         on_task=lambda *args: seen.append(args))
    finally:
        object.__setattr__(refresh.BY_NAME["quotes"], "fn", original)
    conn.close()

    assert seen, "the callback was never invoked, or its call raised"
    name, rows, seconds, note = seen[0]
    assert name == "quotes"
    assert note == "4 priced of 4"
