"""The full-history backfill queue, and the provider's own logging.

Offline — the provider is stubbed. What is tested is the bookkeeping around
it, which is where the cost was: a queue that could never drain, spending
about 5,760 full-history requests a day on symbols already known to return
nothing, while a genuine new listing behind them was never fetched at all.
"""

import datetime as dt
import logging
import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_backfill_")

from finlake import refresh, store  # noqa: E402
from finlake.sources import _provider, prices as price_src  # noqa: E402


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    refresh.ensure_schema(conn)
    conn.executescript("DELETE FROM fetch_log;")
    conn.commit()
    return conn


class FakeLoader:
    """Stands in for `load_prices`. Records what it was asked for."""

    def __init__(self, has_history):
        self.has_history = set(has_history)
        self.asked = []

    def __call__(self, _conn, ticker, **_kw):
        self.asked.append(ticker)
        return 250 if ticker in self.has_history else 0


def with_loader(monkeypatch, loader):
    monkeypatch.setattr(price_src, "load_prices", loader)


# ---------------------------------------------------------------------------
# The jam
# ---------------------------------------------------------------------------
def test_a_symbol_with_no_history_is_not_asked_again(monkeypatch):
    """THE JAM. A warrant or preferred series writes no parquet, so it was
    still 'missing' on the next pass, three minutes later, forever."""
    conn = fresh_db()
    loader = FakeLoader(has_history=[])
    with_loader(monkeypatch, loader)

    first = price_src.backfill_missing(conn, ["OXY-WT"], limit=12,
                                       provider_healthy=True)
    second = price_src.backfill_missing(conn, ["OXY-WT"], limit=12,
                                        provider_healthy=True)

    assert first == {"loaded": 0, "empty": 1, "skipped": 0}
    assert second == {"loaded": 0, "empty": 0, "skipped": 1}
    assert loader.asked == ["OXY-WT"], "the dead symbol was re-requested"
    conn.close()


def test_dead_symbols_do_not_starve_a_real_one(monkeypatch):
    """The damaging half. The queue is in ticker order and the budget is
    twelve, so twelve permanently-dead names at the front meant a genuine new
    listing sorting after them would NEVER have been fetched — no price
    series, and nothing anywhere reporting why."""
    conn = fresh_db()
    dead = [f"AAA-P{i}" for i in range(12)]
    loader = FakeLoader(has_history=["ZZZZ"])
    with_loader(monkeypatch, loader)

    price_src.backfill_missing(conn, dead + ["ZZZZ"], limit=12,
                               provider_healthy=True)
    assert "ZZZZ" not in loader.asked, "precondition: the budget is spent"

    # Next pass: the dead twelve are written off, so the real name is reached.
    counts = price_src.backfill_missing(conn, dead + ["ZZZZ"], limit=12,
                                        provider_healthy=True)
    assert counts["loaded"] == 1
    assert loader.asked[-1] == "ZZZZ"
    conn.close()


def test_nothing_is_written_off_while_the_provider_is_down(monkeypatch):
    """The failure mode that would be far worse than the one being fixed: one
    outage marking the whole universe as historyless and stopping the refresh
    of all of it. A symbol is only ever written off while the provider is
    demonstrably answering for something else."""
    conn = fresh_db()
    loader = FakeLoader(has_history=[])
    with_loader(monkeypatch, loader)

    counts = price_src.backfill_missing(conn, ["AAPL", "MSFT"], limit=12,
                                        provider_healthy=False)
    assert counts["empty"] == 0
    assert price_src.backfill_missing(conn, ["AAPL"], limit=12,
                                      provider_healthy=True)["skipped"] == 0
    conn.close()


def test_a_raised_error_is_not_evidence_about_the_symbol(monkeypatch):
    """A timeout says something about the request, not about whether the
    symbol has history. Writing it off on that basis would drop a real name
    for a day on a single flaky call."""
    conn = fresh_db()

    def explode(_conn, _ticker, **_kw):
        raise TimeoutError("connection reset")

    with_loader(monkeypatch, explode)
    counts = price_src.backfill_missing(conn, ["AAPL"], limit=12,
                                        provider_healthy=True)
    assert counts["empty"] == 0
    assert price_src.backfill_missing(conn, ["AAPL"], limit=12,
                                      provider_healthy=True)["skipped"] == 0
    conn.close()


def test_the_write_off_expires(monkeypatch):
    """A symbol that has just started trading, or whose history was briefly
    unavailable, must come back on its own."""
    conn = fresh_db()
    loader = FakeLoader(has_history=["NEW"])
    with_loader(monkeypatch, loader)

    stale = (dt.datetime.now()
             - dt.timedelta(hours=price_src.BACKFILL_RETRY_HOURS + 1))
    conn.execute("INSERT OR REPLACE INTO fetch_log VALUES (?,?,?,?)",
                 ("prices:NEW", stale.isoformat(), "empty", "no history"))
    conn.commit()

    assert price_src.backfill_missing(conn, ["NEW"], limit=12,
                                      provider_healthy=True)["loaded"] == 1
    conn.close()


def test_a_successful_backfill_clears_the_write_off(monkeypatch):
    conn = fresh_db()
    with_loader(monkeypatch, FakeLoader(has_history=["OK"]))
    price_src.backfill_missing(conn, ["OK"], limit=12, provider_healthy=True)

    row = conn.execute(
        "SELECT status FROM fetch_log WHERE resource = 'prices:OK'").fetchone()
    assert row["status"] == "ok"
    conn.close()


# ---------------------------------------------------------------------------
# What the tier says about itself
# ---------------------------------------------------------------------------
def test_the_quote_tier_reports_its_own_coverage(monkeypatch):
    """The provider's per-symbol narration is suppressed, so the tier has to
    say what it covered. Otherwise a source that had stopped answering would
    be indistinguishable from a quiet, healthy one — which is the exact
    failure this codebase keeps paying for."""
    conn = fresh_db()
    monkeypatch.setattr(price_src, "load_prices_many",
                        lambda _c, tickers, **_k: ({t: 1 for t in tickers}, []))
    result = refresh.task_quotes(conn, ["AAPL", "MSFT"])

    assert isinstance(result, refresh.TaskResult)
    assert result.rows == 2
    assert "2 priced of 2" in result.note
    conn.close()


def test_the_note_reaches_the_refresh_log(monkeypatch):
    conn = fresh_db()
    task = refresh.Task("noted", 60,
                        lambda _c, _t: refresh.TaskResult(7, "7 loaded, 1 no coverage"),
                        "description")
    assert refresh.run_task(conn, task, ["AAPL"]) == 7

    row = conn.execute(
        "SELECT rows, note FROM refresh_log WHERE task='noted'").fetchone()
    assert row["rows"] == 7
    assert row["note"] == "7 loaded, 1 no coverage"
    conn.close()


def test_a_plain_int_return_still_works():
    """Most tiers have nothing extra to say and return a count. Breaking that
    shape would break them silently — `run_task` catches everything."""
    conn = fresh_db()
    task = refresh.Task("counted", 60, lambda _c, _t: 42, "description")
    assert refresh.run_task(conn, task, ["AAPL"]) == 42

    row = conn.execute(
        "SELECT status, note FROM refresh_log WHERE task='counted'").fetchone()
    assert row["status"] == "ok" and row["note"] == "description"
    conn.close()


# ---------------------------------------------------------------------------
# Silencing the provider
# ---------------------------------------------------------------------------
def test_provider_logging_is_silenced_and_restored():
    """The 404 bodies the daemon was printing came from the provider's own
    logger, at ERROR, with no handler — so Python's last-resort handler wrote
    them straight to stderr, four or five times per symbol.

    Restoration is the point of the test: a blanket silence would also hide a
    provider that has genuinely started refusing everything."""
    logger = logging.getLogger("yfinance")
    assert not logger.disabled, "precondition: not already silenced"

    with _provider.quiet_provider():
        assert logger.disabled
    assert not logger.disabled


def test_silencing_survives_an_exception():
    logger = logging.getLogger("yfinance")
    try:
        with _provider.quiet_provider():
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert not logger.disabled, "a raising call left the provider muted"
