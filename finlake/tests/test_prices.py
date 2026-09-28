"""Tests for the VWAP helper added in the lodestar Phase A amendment
(FINLAKE-FINDINGS.md F6). Offline, synthetic — writes a tiny parquet file
and corp_actions rows directly, the same way the real cache is laid out.
"""

import os
import tempfile

os.environ.setdefault("FINLAKE_HOME", tempfile.mkdtemp(prefix="finlake_test_"))

import pandas as pd  # noqa: E402

from finlake import config, store  # noqa: E402
from finlake.sources import prices  # noqa: E402


def _write_bars(ticker: str, rows: list[dict]) -> None:
    config.ensure_dirs()
    df = pd.DataFrame(rows)
    df.to_parquet(prices._path(ticker), index=False)


def test_vwap_typical_price_hand_computed():
    """Two days, hand-computed VWAP using the conventional
    (high+low+close)/3 typical price, dollar-weighted by volume.

    Day 1: typical = (10+8+9.5)/3 = 9.166666...7, volume 100
    Day 2: typical = (14+10+11)/3 = 11.666666...7, volume 300

    VWAP = (9.1666667*100 + 11.6666667*300) / (100+300)
         = (916.66667 + 3500.0) / 400
         = 4416.66667 / 400
         = 11.0416667
    """
    _write_bars("TVWP", [
        {"date": "2024-01-02", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 100},
        {"date": "2024-01-03", "open": 11, "high": 14, "low": 10, "close": 11, "volume": 300},
    ])
    conn = store.connect()
    v = prices.vwap(conn, "TVWP", start="2024-01-01", end="2024-01-04",
                     adjust="none", price="typical")
    assert v is not None
    assert abs(v - 11.0416667) < 1e-5, v
    print("  ok  VWAP (typical price) matches hand computation")


def test_vwap_close_price_hand_computed():
    """Same two days, VWAP on close price alone instead of typical.

    VWAP = (9.5*100 + 11*300) / 400 = (950 + 3300) / 400 = 4250/400 = 10.625
    """
    conn = store.connect()
    v = prices.vwap(conn, "TVWP", start="2024-01-01", end="2024-01-04",
                     adjust="none", price="close")
    assert v is not None
    assert abs(v - 10.625) < 1e-9, v
    print("  ok  VWAP (close price) matches hand computation")


def test_vwap_respects_the_window():
    """A third day exists in the cache but sits outside [start, end] and
    must not affect the result — the whole point of a windowed VWAP for
    buyback timing is comparing the SAME window on both sides."""
    _write_bars("TVWP2", [
        {"date": "2024-01-02", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 100},
        {"date": "2024-01-03", "open": 11, "high": 14, "low": 10, "close": 11, "volume": 300},
        {"date": "2024-06-01", "open": 500, "high": 500, "low": 500, "close": 500, "volume": 999},
    ])
    conn = store.connect()
    v = prices.vwap(conn, "TVWP2", start="2024-01-01", end="2024-01-04",
                     adjust="none", price="typical")
    assert abs(v - 11.0416667) < 1e-5, v
    print("  ok  VWAP ignores bars outside the requested window")


def test_vwap_does_not_re_apply_a_split_the_source_already_applied():
    """The cached series is ALREADY split-adjusted by the provider.

    `history(auto_adjust=False)` controls dividend adjustment only; splits are
    always applied retroactively to the whole series. Adjusting again divided
    every pre-split bar a second time -- NVDA's 2024-05-21 close read $95.39
    in the cache and came back as $9.54 -- which corrupted price charts, every
    historical valuation multiple, VWAP, and 12-1 momentum whenever its window
    spanned a split.

    Bars here are written the way the provider actually returns them: already
    continuous across the split. VWAP must therefore equal the plain
    volume-weighted typical price, with no further adjustment.

      day 1 typical = (10+8+9.5)/3  = 9.1666667 at volume 100
      day 2 typical = (14+10+11)/3  = 11.6666667 at volume 300
      VWAP = (9.1666667*100 + 11.6666667*300) / 400 = 11.0416667
    """
    _write_bars("TVWPSPLIT", [
        {"date": "2024-01-02", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 100},
        {"date": "2024-01-03", "open": 11, "high": 14, "low": 10, "close": 11, "volume": 300},
    ])
    conn = store.connect()
    store.init_db(conn)
    store.upsert_many(conn, "corp_actions", ("ticker", "date", "kind", "value"),
                       [("TVWPSPLIT", "2024-01-03", "split", 2.0)])
    conn.commit()

    v = prices.vwap(conn, "TVWPSPLIT", start="2024-01-01", end="2024-01-04",
                     adjust="split", price="typical")
    assert abs(v - 11.0416667) < 1e-5, (
        f"got {v}; the split was applied a second time on top of the "
        f"provider's own adjustment")


def test_as_traded_undoes_the_providers_split_adjustment():
    """`as_traded` recovers the price actually printed on the tape.

    This is the mode that pairs with an as-reported share count: multiplying a
    split-adjusted price by a pre-split share count understates market cap by
    the split ratio, which is how a "5-year median P/E" of 1.13x appeared for
    a stock that never traded below 30x.

    With a 2-for-1 on day 2, day 1's cached close of 9.5 was really 19.0.
    """
    _write_bars("TASTRADED", [
        {"date": "2024-01-02", "open": 9, "high": 10, "low": 8, "close": 9.5, "volume": 100},
        {"date": "2024-01-03", "open": 11, "high": 14, "low": 10, "close": 11, "volume": 300},
    ])
    conn = store.connect()
    store.init_db(conn)
    conn.execute("DELETE FROM corp_actions WHERE ticker='TASTRADED'")
    store.upsert_many(conn, "corp_actions", ("ticker", "date", "kind", "value"),
                       [("TASTRADED", "2024-01-03", "split", 2.0)])
    conn.commit()

    out = prices.get_prices(conn, "TASTRADED", adjust="as_traded")
    assert abs(out["adj_close"].iloc[0] - 19.0) < 1e-9, (
        "the pre-split bar was not restored to its as-traded price")
    assert abs(out["adj_close"].iloc[1] - 11.0) < 1e-9, (
        "a post-split bar must be untouched")
    # Volume moves the other way: fewer, larger shares before the split.
    assert abs(out["adj_volume"].iloc[0] - 50.0) < 1e-9


def test_vwap_returns_none_when_no_bars_in_window():
    conn = store.connect()
    v = prices.vwap(conn, "TVWP", start="2030-01-01", end="2030-06-01", adjust="none")
    assert v is None
    print("  ok  VWAP returns None rather than dividing by zero")


if __name__ == "__main__":
    store.init_db()
    for fn in [
        test_vwap_typical_price_hand_computed,
        test_vwap_close_price_hand_computed,
        test_vwap_respects_the_window,
        test_vwap_applies_split_adjustment,
        test_vwap_returns_none_when_no_bars_in_window,
    ]:
        fn()
    print("\nall VWAP tests passed")


def test_dividends_never_adjust_volume():
    """Regression: adjust='total' divided historical VOLUME by the
    dividend-inclusive price factor.

    A split changes the share count, so historical volume divides by the split
    ratio. A dividend changes no share count whatsoever. Folding the dividend
    factor into volume inflates every historical volume figure by the
    cumulative dividend drag, and that error flows straight into VWAP and
    every dollar-volume number built on it.

    Two bars, one $1.00 dividend on day 2 against a $100 prior close. Prices
    before the dividend get multiplied by (1 - 1/100) = 0.99; volume must not
    move at all.
    """
    ticker = "DIVTEST"
    _write_bars(ticker, [
        {"date": "2020-01-01", "open": 100.0, "high": 100.0, "low": 100.0,
         "close": 100.0, "volume": 1_000},
        {"date": "2020-01-02", "open": 99.0, "high": 99.0, "low": 99.0,
         "close": 99.0, "volume": 2_000},
    ])
    conn = store.connect()
    store.init_db(conn)
    conn.execute("DELETE FROM corp_actions WHERE ticker=?", (ticker,))
    store.upsert_many(conn, "corp_actions", ("ticker", "date", "kind", "value"),
                      [(ticker, "2020-01-02", "dividend", 1.0)])
    conn.commit()

    out = prices.get_prices(conn, ticker, adjust="total")
    assert abs(out["adj_close"].iloc[0] - 99.0) < 1e-9, (
        "the dividend was not applied to prices")
    assert out["adj_volume"].iloc[0] == 1_000, (
        f"a dividend changed historical volume ({out['adj_volume'].iloc[0]} "
        f"instead of 1000) — dividends do not change the share count")
    assert out["adj_volume"].iloc[1] == 2_000
    conn.close()


def test_split_mode_returns_the_cached_series_untouched():
    """`split` is a no-op because the provider already applied splits. The
    adj_* columns are still populated so callers can read one set of column
    names regardless of mode."""
    ticker = "SPLITVOL"
    _write_bars(ticker, [
        {"date": "2020-01-01", "open": 50.0, "high": 50.0, "low": 50.0,
         "close": 50.0, "volume": 2_000},
        {"date": "2020-01-02", "open": 50.0, "high": 50.0, "low": 50.0,
         "close": 50.0, "volume": 2_000},
    ])
    conn = store.connect()
    store.init_db(conn)
    conn.execute("DELETE FROM corp_actions WHERE ticker=?", (ticker,))
    store.upsert_many(conn, "corp_actions", ("ticker", "date", "kind", "value"),
                      [(ticker, "2020-01-02", "split", 2.0)])
    conn.commit()

    out = prices.get_prices(conn, ticker, adjust="split")
    assert abs(out["adj_close"].iloc[0] - 50.0) < 1e-9, (
        "a split was applied on top of the provider's own adjustment")
    assert abs(out["adj_volume"].iloc[0] - 2_000) < 1e-9
    conn.close()


def test_an_unknown_adjust_mode_is_rejected():
    """Silently treating a typo as a default would hand back a series the
    caller did not ask for, and price mode changes what every downstream
    number means."""
    import pytest

    _write_bars("BADMODE", [
        {"date": "2020-01-01", "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "volume": 1},
    ])
    conn = store.connect()
    store.init_db(conn)
    with pytest.raises(ValueError, match="unknown adjust"):
        prices.get_prices(conn, "BADMODE", adjust="raw")
    conn.close()
