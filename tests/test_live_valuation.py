"""The header price, and the header market cap, and everything downstream.

The bug these pin: `ratios.latest()` prices the newest fundamentals row at the
close on that row's own PERIOD END, because a point-in-time ratio history has
to. The company page then printed that as "Price", beside a chart drawn from
the live series. On 10 August 2026 the header read $373 for Microsoft — the
30 June close — while the chart's own last point was $509. Market cap was a
trillion dollars light and every multiple built on it was 27% too cheap.

Synthetic prices and synthetic fundamentals; no network, no real cache.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_live_")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from finlake import config, ratios, store  # noqa: E402
from finlake.sources import prices as price_src  # noqa: E402

FACT_COLS = ("cik", "taxonomy", "tag", "unit", "period_start", "period_end",
             "val", "fy", "fp", "form", "accn", "filed", "frame")

CIK = 9001
TICKER = "LIVE"

# Eight quarters of $100 revenue and $25 net income, on a December year-end,
# and a constant 1,000-share count. TTM net income is therefore 100 and TTM
# revenue 400, which makes every expected multiple below a one-line division.
#
# Eight rather than four so the frame has enough history for a rolling TTM
# window AND for a figure to be measurably stale — a one-row frame cannot
# express "this number is five quarters behind the rest of the page".
QUARTERS = [
    ("2024-01-01", "2024-03-31"), ("2024-04-01", "2024-06-30"),
    ("2024-07-01", "2024-09-30"), ("2024-10-01", "2024-12-31"),
    ("2025-01-01", "2025-03-31"), ("2025-04-01", "2025-06-30"),
    ("2025-07-01", "2025-09-30"), ("2025-10-01", "2025-12-31"),
]
SHARES = 1_000.0
TTM_NET_INCOME = 100.0
TTM_REVENUE = 400.0
DEBT = 400.0                # long-term debt, every quarter
CASH = 150.0                # so net debt is a clean 250
NET_DEBT = DEBT - CASH

PERIOD_END_PRICE = 50.0     # the close on 2025-12-31
LATEST_PRICE = 80.0         # the close on 2026-02-20, +60%


def _fact(tag, start, end, val, unit="USD"):
    return (CIK, "us-gaap", tag, unit, start, end, float(val), None, None,
            "10-Q", f"{tag}-{end}", end, None)


@pytest.fixture(autouse=True)
def cache():
    conn = store.connect()
    store.init_db(conn)
    conn.executescript(
        "DELETE FROM facts; DELETE FROM securities; DELETE FROM ticker_map; "
        "DELETE FROM corp_actions;")

    rows = []
    for start, end in QUARTERS:
        rows.append(_fact("Revenues", start, end, TTM_REVENUE / 4))
        rows.append(_fact("NetIncomeLoss", start, end, TTM_NET_INCOME / 4))
        rows.append(_fact("CommonStockSharesOutstanding", None, end, SHARES,
                          unit="shares"))
        rows.append(_fact("Assets", None, end, 5_000.0))
        rows.append(_fact("StockholdersEquity", None, end, 2_000.0))
        rows.append(_fact("LongTermDebtNoncurrent", None, end, DEBT))
        rows.append(_fact("CashAndCashEquivalentsAtCarryingValue", None, end,
                          CASH))
    store.upsert_many(conn, "facts", FACT_COLS, rows)
    conn.execute(
        "INSERT INTO ticker_map (ticker, cik, exchange, valid_from, valid_to) "
        "VALUES (?,?,?,?,?)", (TICKER, CIK, "NASDAQ", "2000-01-01", None))
    conn.execute(
        "INSERT INTO securities (cik, name, fiscal_year_end) VALUES (?,?,?)",
        (CIK, "Live Test Co", "1231"))
    conn.commit()
    conn.close()

    # Daily bars: flat at the period-end price through year end, then a rally.
    config.ensure_dirs()
    dates = pd.date_range("2025-12-01", "2026-02-20", freq="B")
    closes = [PERIOD_END_PRICE if d <= pd.Timestamp("2025-12-31")
              else LATEST_PRICE for d in dates]
    frame = pd.DataFrame({
        "date": [d.date().isoformat() for d in dates],
        "open": closes, "high": closes, "low": closes,
        "close": closes, "volume": [1_000] * len(dates),
    })
    frame.to_parquet(config.PRICE_DIR / f"{TICKER}.parquet", index=False)
    price_src._QUOTE_MEMO.clear()
    yield


def test_the_quote_is_the_last_bar_of_the_series_the_chart_draws():
    quote = price_src.last_quote(TICKER)
    assert quote["price"] == pytest.approx(LATEST_PRICE)
    assert quote["as_of"] == "2026-02-20"


def test_the_quote_reports_the_bar_date_so_staleness_is_visible():
    """A bar from three days ago is a perfectly good last close and a very
    bad "current price". The only way a reader can tell is if the date is
    carried alongside the number."""
    assert "as_of" in price_src.last_quote(TICKER)


def test_point_in_time_still_prices_at_the_period_end():
    """The default must not change. Re-pricing a HISTORY at today's close
    plots what the market believes now against what a company earned then —
    a pairing that was never true on any date."""
    pit = ratios.latest(TICKER, as_of="2026-02-20")
    assert pit["price"] == pytest.approx(PERIOD_END_PRICE)
    assert pit["market_cap"] == pytest.approx(SHARES * PERIOD_END_PRICE)


def test_live_prices_the_newest_period_at_the_latest_close():
    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    assert live["price"] == pytest.approx(LATEST_PRICE)
    assert live["market_cap"] == pytest.approx(SHARES * LATEST_PRICE)     # 80k
    # P/E = market cap / TTM net income = 80,000 / 100
    assert live["pe_ttm"] == pytest.approx(800.0)
    # P/S = market cap / TTM revenue = 80,000 / 400
    assert live["ps"] == pytest.approx(200.0)


def test_live_and_point_in_time_differ_by_exactly_the_price_move():
    """The whole error, stated as an identity: every price-based figure is
    linear in price, so the live and stored versions must differ by the price
    ratio and nothing else. If they differ by anything more, something other
    than the price was changed too."""
    pit = ratios.latest(TICKER, as_of="2026-02-20")
    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    factor = LATEST_PRICE / PERIOD_END_PRICE

    for key in ("market_cap", "pe_ttm", "ps", "pb"):
        assert live[key] == pytest.approx(pit[key] * factor), key
    for key in ("earnings_yield",):
        assert live[key] == pytest.approx(pit[key] / factor), key


def test_live_reports_both_dates_it_depends_on():
    """A live price against stale fundamentals is a real condition, and the
    page can only warn about it if the numbers say which is which."""
    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    assert live["price_as_of"] == "2026-02-20"
    assert live["fundamentals_as_of"] == "2025-12-31"


def test_a_ratio_needing_history_is_not_blanked_by_going_live():
    """Re-pricing a ONE-ROW frame silently returns NaN for every ratio with a
    lookback — PEG divides by four-quarter earnings growth — and the stale
    value is then left in place beside the fresh ones. Only the final row's
    price is replaced, so the history behind it survives."""
    live = ratios.live_valuation(TICKER, as_of="2026-02-20")
    assert "peg" not in live or live["peg"] == live["peg"]   # not NaN


def test_enterprise_value_equals_market_cap_plus_net_debt():
    """An identity, not an approximation — and it was failing on real data.

    `latest()` composes its dict column by column, each taking its own newest
    non-missing value. Enterprise value came from the live row while net debt
    came from whatever earlier row last had one, so the two did not reconcile
    on the same screen. General Motors printed an enterprise value BELOW its
    market cap beside net debt of +$110bn.
    """
    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    assert live["enterprise_value"] == pytest.approx(
        live["market_cap"] + live["net_debt"])


def test_a_stale_figure_is_dropped_rather_than_reported_as_current():
    """"Latest" has to mean recent, not merely newest.

    Assurant last tagged long-term debt in March 2020; six years later
    `latest()` still reported it as total debt, paired it with 2026 cash, and
    produced a net debt figure belonging to no date at all. Nothing was
    missing on screen — it was worse than missing, it was confident.
    """
    from finlake import store

    # A figure reported every quarter is current, and is reported.
    assert ratios.latest(TICKER, as_of="2026-02-20")["total_debt"] == \
        pytest.approx(DEBT)

    # Now stop reporting it after the third quarter of the eight, leaving the
    # newest value five periods behind the rest of the frame.
    conn = store.connect()
    conn.execute("DELETE FROM facts WHERE tag = 'LongTermDebtNoncurrent' "
                 "AND period_end > ?", (QUARTERS[2][1],))
    conn.commit()
    conn.close()

    pit = ratios.latest(TICKER, as_of="2026-02-20")
    assert "total_debt" not in pit, (
        "a debt figure five quarters stale was reported as current total debt")
    # And nothing current was lost along with it.
    assert pit["net_margin"] == pytest.approx(TTM_NET_INCOME / TTM_REVENUE)


def test_a_multi_class_filer_uses_the_providers_consolidated_share_count():
    """`CommonStockSharesOutstanding` arrives undimensioned and, for a filer
    with several share classes, covers only one of them. Multiplied by the
    traded price that is not wrong at the margin — Visa's market cap came out
    at $41bn against $673bn, Berkshire's at $0.2bn against $1,150bn — and it
    carried into P/E, P/S, P/B, EV and every screen that sorts on size.
    """
    from finlake import store

    # The provider says the company is worth 10x what the filed share count
    # implies, which is the multi-class signature.
    consolidated = SHARES * 10
    conn = store.connect()
    store.upsert_many(
        conn, "market_snapshot",
        ("ticker", "as_of", "price", "market_cap", "shares_outstanding"),
        [(TICKER, "2026-02-19", 40.0, consolidated * 40.0, SHARES)],
        ignore_conflicts=False)
    conn.commit()
    conn.close()

    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    assert live["shares_source"] == "market data provider"
    assert live["market_cap"] == pytest.approx(consolidated * LATEST_PRICE)
    # The filed count is kept alongside rather than discarded.
    assert live["shares_outstanding_filed"] == pytest.approx(SHARES)

    # The point-in-time history is untouched: the filed count is what was
    # knowable on each historical date.
    assert ratios.latest(TICKER, as_of="2026-02-20")["market_cap"] == \
        pytest.approx(SHARES * PERIOD_END_PRICE)


def test_no_cached_price_degrades_instead_of_raising():
    """A name the price loader has never reached must still render. Live mode
    returns exactly what point-in-time mode does — which for a name with no
    bars at all is every fundamentals-only ratio and no price. That is the
    honest answer; the company page falls back to the market snapshot's own
    quote for the header, and margins and returns are unaffected either way.
    """
    (config.PRICE_DIR / f"{TICKER}.parquet").unlink()
    price_src._QUOTE_MEMO.clear()

    assert price_src.last_quote(TICKER) is None
    assert ratios.live_valuation(TICKER, as_of="2026-02-20") == {}

    live = ratios.latest(TICKER, as_of="2026-02-20", live=True)
    assert live == ratios.latest(TICKER, as_of="2026-02-20")
    assert "price" not in live and "market_cap" not in live
    # Nothing that does not depend on a price is lost.
    assert live["net_margin"] == pytest.approx(TTM_NET_INCOME / TTM_REVENUE)


def test_the_quote_index_cannot_serve_a_price_from_a_rewritten_file():
    """The index is a CACHE OF THE PARQUETS, not a second source of price.

    Two sources of price that can disagree is the bug this whole area was
    fixed for, so every index row records the mtime and size of the file it
    was read from and is discarded the moment those change. That check is the
    design, not an optimisation on top of it.
    """
    import time

    from finlake import store

    assert price_src.last_quotes([TICKER])[TICKER]["price"] == pytest.approx(
        LATEST_PRICE)
    with store.session(read_only=True) as conn:
        row = conn.execute("SELECT * FROM quote_cache WHERE ticker = ?",
                           (TICKER,)).fetchone()
    assert row is not None, "the index was never populated"

    # Rewrite the parquet behind the index's back.
    path = config.PRICE_DIR / f"{TICKER}.parquet"
    frame = pd.read_parquet(path)
    frame.loc[frame.index[-1], "close"] = 999.0
    time.sleep(0.01)
    frame.to_parquet(path, index=False)
    price_src._QUOTE_MEMO.clear()

    assert price_src.last_quotes([TICKER])[TICKER]["price"] == pytest.approx(
        999.0), "a stale index row was served instead of the file"


def test_writing_bars_refreshes_the_index_in_the_same_call():
    """Every quote sweep rewrites all ~500 parquets, which invalidates all
    ~500 index rows at once. If the index were only rebuilt lazily on read,
    a FASTER refresh tier would have made the screener slower — it would pay
    the full parquet walk after every sweep."""
    import sqlite3

    from finlake import store

    path = config.PRICE_DIR / f"{TICKER}.parquet"
    frame = pd.read_parquet(path)
    frame.loc[frame.index[-1], "close"] = 77.5

    with store.session() as conn:
        price_src._write_history(conn, TICKER, frame.rename(
            columns={"date": "Date"}).set_index("Date"), None)

    with store.session(read_only=True) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM quote_cache WHERE ticker = ?",
                           (TICKER,)).fetchone()
    assert row["close"] == pytest.approx(77.5)
    assert row["source_mtime"] == path.stat().st_mtime, (
        "the index recorded a different mtime than the file it just wrote, "
        "so every read will discard it")


def test_the_quote_memo_does_not_serve_a_stale_price():
    """The memo is keyed on the file's own mtime, so a rewritten parquet
    cannot be served from cache. Without that, a sweep that just wrote new
    bars would keep returning the old ones for the life of the process."""
    import time

    first = price_src.last_quote(TICKER)["price"]
    frame = pd.read_parquet(config.PRICE_DIR / f"{TICKER}.parquet")
    frame.loc[frame.index[-1], "close"] = 123.45
    time.sleep(0.01)
    frame.to_parquet(config.PRICE_DIR / f"{TICKER}.parquet", index=False)

    assert first == pytest.approx(LATEST_PRICE)
    assert price_src.last_quote(TICKER)["price"] == pytest.approx(123.45)
