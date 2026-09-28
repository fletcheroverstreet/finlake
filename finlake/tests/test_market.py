"""Market data ingestion and the point-in-time rules around estimates.

Offline. The provider is unofficial and changes shape between releases, so
what is tested here is the layer that DEFENDS against that: coercion of junk
values, and the as-of semantics that stop a revised estimate leaking
backwards into a historical query.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_market_")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from finlake import ratios, store  # noqa: E402
from finlake.sources import market  # noqa: E402


def fresh_db():
    conn = store.connect()
    store.init_db(conn)
    conn.executescript(
        "DELETE FROM market_snapshot; DELETE FROM estimates; "
        "DELETE FROM estimate_trend; DELETE FROM analyst_targets;")
    conn.commit()
    return conn


def snapshot(conn, ticker, as_of, forward_eps=None, price=None):
    store.upsert_many(
        conn, "market_snapshot",
        ("ticker", "as_of", "price", "forward_eps"),
        [(ticker, as_of, price, forward_eps)])
    conn.commit()


# ---------------------------------------------------------------------------
# Coercion: the provider returns junk, and none of it may reach the store
# ---------------------------------------------------------------------------
def test_junk_values_become_none_not_zero():
    """The provider returns strings, empty strings, NaN and 'Infinity'
    depending on the field and the day. Every one of them means "no usable
    number" — and must not land as 0.0, which reads downstream as a real
    measurement of nothing."""
    for junk in (None, "", "N/A", float("nan"), float("inf"), float("-inf"), {}):
        assert market._num(junk) is None, junk
        assert market._int(junk) is None, junk

    # Real values, including numeric strings, still come through.
    assert market._num("12.5") == 12.5
    assert market._num(0) == 0.0          # a genuine zero is NOT junk
    assert market._int("42") == 42


def test_str_coercion_treats_blank_as_missing():
    assert market._str("  ") is None
    assert market._str(None) is None
    assert market._str(" Technology ") == "Technology"


def test_rows_of_survives_every_shape_the_provider_returns():
    """None, an empty frame, and a raising object all mean "no data here" and
    must not take down the whole ticker."""
    assert list(market._rows_of(None)) == []
    assert list(market._rows_of(pd.DataFrame())) == []

    class Explodes:
        empty = False

        def iterrows(self):
            raise RuntimeError("provider changed shape again")

    assert list(market._rows_of(Explodes())) == []

    real = pd.DataFrame({"avg": [1.0]}, index=["0q"])
    assert len(list(market._rows_of(real))) == 1


# ---------------------------------------------------------------------------
# Point-in-time: an estimate is a fact about a DATE, not about a period
# ---------------------------------------------------------------------------
def test_a_later_estimate_is_invisible_to_an_earlier_query():
    """The same guarantee the `filed` column gives filings, applied to
    estimates. Consensus is revised constantly; a backtest that reads today's
    consensus into last year's decision is using information that did not
    exist then."""
    conn = fresh_db()
    snapshot(conn, "TEST", "2026-01-15", forward_eps=5.0)
    snapshot(conn, "TEST", "2026-06-15", forward_eps=9.0)

    assert market.forward_eps(conn, "TEST", as_of="2026-03-01") == 5.0
    assert market.forward_eps(conn, "TEST", as_of="2026-08-01") == 9.0
    assert market.forward_eps(conn, "TEST", as_of="2026-01-01") is None
    conn.close()


def test_forward_eps_falls_back_to_the_next_year_consensus():
    """Thinly covered names often have no `forwardEps` statistic but do have a
    next-fiscal-year estimate. Using it beats leaving forward P/E blank."""
    conn = fresh_db()
    snapshot(conn, "THIN", "2026-06-01", forward_eps=None, price=10.0)
    store.upsert_many(
        conn, "estimates",
        ("ticker", "as_of", "metric", "period", "avg"),
        [("THIN", "2026-06-01", "eps", "+1y", 2.5)])
    conn.commit()

    assert market.forward_eps(conn, "THIN", as_of="2026-07-01") == 2.5
    conn.close()


def test_estimate_alignment_uses_the_period_window_not_the_period_end():
    """The rule that makes forward P/E work at all.

    A June quarter is only reported in late July, so every estimate for it is
    published AFTER the period end. Aligning strictly to the period end finds
    no snapshot at or before 30 June and returns nothing — for every ticker,
    forever. A period's window instead runs until the NEXT period end.
    """
    conn = fresh_db()
    snapshot(conn, "WINDOW", "2026-07-20", forward_eps=8.0)   # after Q2 end
    snapshot(conn, "WINDOW", "2026-10-20", forward_eps=9.0)   # after Q3 end
    conn.close()

    index = pd.Index(["2026-03-31", "2026-06-30", "2026-09-30"])
    out = ratios.forward_eps_at_period_ends("WINDOW", index, as_of="2026-12-31")

    assert pd.isna(out.loc["2026-03-31"]), (
        "a snapshot from July leaked back into the March quarter")
    assert out.loc["2026-06-30"] == 8.0, (
        "the June quarter found no estimate — alignment is still using the "
        "period end rather than the period's window")
    assert out.loc["2026-09-30"] == 9.0


def test_estimate_alignment_respects_the_query_horizon():
    """Every snapshot is still bounded by the query's as_of, so a run dated
    before an estimate was published cannot see it."""
    conn = fresh_db()
    snapshot(conn, "HORIZON", "2026-07-20", forward_eps=8.0)
    conn.close()

    index = pd.Index(["2026-06-30"])
    early = ratios.forward_eps_at_period_ends("HORIZON", index, as_of="2026-07-01")
    later = ratios.forward_eps_at_period_ends("HORIZON", index, as_of="2026-08-01")

    assert pd.isna(early.iloc[0]), "an estimate leaked before it was published"
    assert later.iloc[0] == 8.0


def test_no_market_data_leaves_forward_pe_missing_not_trailing():
    """Forward P/E must never silently degrade into trailing P/E. They are
    different numbers and a reader comparing them would be comparing one
    number to itself."""
    conn = fresh_db()
    conn.close()

    index = pd.Index(["2026-06-30"])
    out = ratios.forward_eps_at_period_ends("NOCOVER", index, as_of="2026-12-31")
    assert pd.isna(out.iloc[0])

    data = pd.DataFrame({"net_income": [100.0], "shares_outstanding": [10.0]},
                        index=["2026-06-30"])
    computed = ratios.compute(data, price=pd.Series([50.0], index=["2026-06-30"]))
    assert pd.isna(computed["pe_forward"].iloc[0])
    assert computed["pe_ttm"].iloc[0] == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
def test_market_tables_are_created_and_keyed_on_as_of():
    """Snapshots must accumulate rather than overwrite — without as_of in the
    primary key there is no history, and no point-in-time at all."""
    conn = fresh_db()
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    for expected in ("market_snapshot", "estimates", "estimate_trend",
                     "analyst_targets", "recommendations", "earnings_history",
                     "short_interest", "ownership", "profile",
                     "earnings_calendar"):
        assert expected in tables, f"{expected} was not created"

    for table in ("market_snapshot", "estimates", "short_interest", "ownership"):
        pk = [r["name"] for r in conn.execute(f"PRAGMA table_info({table})")
              if r["pk"]]
        assert "as_of" in pk, f"{table} is not keyed on as_of, so it cannot "\
                              f"hold history"

    snapshot(conn, "ACC", "2026-01-01", forward_eps=1.0)
    snapshot(conn, "ACC", "2026-02-01", forward_eps=2.0)
    n = conn.execute("SELECT COUNT(*) FROM market_snapshot WHERE ticker='ACC'"
                     ).fetchone()[0]
    assert n == 2, "a second snapshot overwrote the first instead of accumulating"
    conn.close()


# ---------------------------------------------------------------------------
# What the tier reports about itself
# ---------------------------------------------------------------------------
def test_the_market_tier_reports_coverage_rather_than_printing_404s(monkeypatch):
    """The provider narrates every endpoint it has no data for, at ERROR, on
    a logger with no handler — four or five raw 404 bodies per symbol, printed
    straight to the daemon's console. Suppressing that without replacing it
    would trade noise for silence, and a source that has stopped answering
    entirely would look exactly like a healthy one.

    So the tier counts its own outcomes. "0 loaded, 503 no coverage" is
    unmistakable; a handful among hundreds is the ordinary case of a
    constituent the provider dropped after an acquisition.
    """
    from finlake import refresh

    outcomes = {"AAPL": {"snapshot": 1}, "MSFT": {"snapshot": 1},
                "GONE": {"empty": 0}, "BAD": {"error": 0}}
    monkeypatch.setattr(market, "load_market_data",
                        lambda _c, t, **_k: outcomes[t])

    conn = fresh_db()
    result = refresh.task_market(conn, list(outcomes))
    conn.close()

    assert result.rows == 2
    assert result.note == "2 loaded, 1 no coverage, 1 failed"


def test_a_raising_symbol_is_counted_not_swallowed(monkeypatch):
    """`load_market_data` promises never to raise, so if it does, that is a
    real defect — and counting it as a plain miss would hide it."""
    from finlake import refresh

    def explode(_conn, ticker, **_kw):
        if ticker == "BOOM":
            raise RuntimeError("provider changed shape")
        return {"snapshot": 1}

    monkeypatch.setattr(market, "load_market_data", explode)
    conn = fresh_db()
    result = refresh.task_market(conn, ["AAPL", "BOOM"])
    conn.close()

    assert result.rows == 1
    assert "1 failed" in result.note
