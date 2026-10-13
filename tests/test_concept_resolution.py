"""Regression tests for how `fundamentals()` turns raw facts into columns.

Every test here encodes a bug that was live in the cache on this machine and
produced a number that looked completely plausible on screen. They are the
expensive kind of bug: nothing crashes, nothing logs a warning, a column is
just quietly blank or quietly wrong.

All synthetic, all offline. FINLAKE_HOME points at a temp dir so these never
touch a real cache.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_concepts_")

from finlake import api, store  # noqa: E402

FACT_COLS = ("cik", "taxonomy", "tag", "unit", "period_start", "period_end",
             "val", "fy", "fp", "form", "accn", "filed", "frame")

CIK = 4242
TICKER = "TEST"

# Four quarter-ends and the year-to-date boundaries that imply them, so a
# filer can be made to tag either discrete quarters or a cumulative chain.
Q = [("2020-01-01", "2020-03-31"), ("2020-04-01", "2020-06-30"),
     ("2020-07-01", "2020-09-30"), ("2020-10-01", "2020-12-31")]


def fact(tag, start, end, val, *, filed, unit="USD", taxonomy="us-gaap",
         form="10-Q", accn=None):
    return (CIK, taxonomy, tag, unit, start, end, float(val), None, None, form,
            accn or f"{tag}-{end}", filed, None)


def fresh_db(rows):
    conn = store.connect()
    store.init_db(conn)
    conn.executescript(
        "DELETE FROM facts; DELETE FROM securities; DELETE FROM ticker_map;")
    store.upsert_many(conn, "facts", FACT_COLS, rows)
    conn.execute(
        "INSERT INTO ticker_map (ticker, cik, exchange, valid_from, valid_to) "
        "VALUES (?,?,?,?,?)", (TICKER, CIK, "NYSE", "2000-01-01", None))
    conn.commit()
    conn.close()


def test_history_is_stitched_across_a_tag_switch():
    """The Apple bug.

    A filer reports revenue under a legacy tag, then switches permanently to a
    modern one. Both eras are real; the frame must contain both.

    This was live: Apple tags revenue as SalesRevenueNet through 2018 and as
    RevenueFromContractWithCustomerExcludingAssessedTax from 2017 on. The
    resolver picked whichever single tag yielded more quarters, chose the
    legacy one, and returned a revenue column that was BLANK for every quarter
    after 2018 — on the most-looked-at ticker in the universe, with no error
    anywhere.
    """
    rows = []
    for i, (s, e) in enumerate(Q):                      # 2020: legacy tag
        rows.append(fact("SalesRevenueNet", s, e, 100 + i, filed="2020-12-31"))
    for i, (s, e) in enumerate(Q):                      # 2021: modern tag
        s, e = s.replace("2020", "2021"), e.replace("2020", "2021")
        rows.append(fact("RevenueFromContractWithCustomerExcludingAssessedTax",
                         s, e, 200 + i, filed="2021-12-31"))
    fresh_db(rows)

    rev = api.fundamentals(TICKER, years=10, as_of="2022-06-30")["revenue"]
    assert rev.notna().sum() == 8, (
        f"expected 8 quarters spanning the tag switch, got "
        f"{rev.notna().sum()} — one era of history was dropped")
    assert rev.loc["2020-03-31"] == 100, "legacy-tag era lost"
    assert rev.loc["2021-12-31"] == 203, "modern-tag era lost"


def test_higher_priority_tag_wins_an_overlapping_period():
    """Where two tags cover the SAME period, CONCEPTS order decides.

    Order in the fallback list is a deliberate preference (most specific
    first), so an overlap must resolve to the higher-priority tag rather than
    to whichever happened to be read last, and must never average the two.
    """
    s, e = Q[0]
    fresh_db([
        fact("SalesRevenueNet", s, e, 999, filed="2020-05-01"),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", s, e, 111,
             filed="2020-05-01"),
    ])
    rev = api.fundamentals(TICKER, years=10, as_of="2021-01-01")["revenue"]
    assert rev.loc[e] == 111, (
        "the lower-priority tag won an overlapping period, or the two were "
        "blended into a value that was never reported")


def test_foreign_currency_facts_never_mix_into_a_usd_series():
    """One tag, several units.

    A filer with foreign operations tags Revenues in USD *and* in EUR. Pooling
    them lets the quarterizer difference a USD year-to-date figure against a
    EUR one and emit the result as a quarter — a number that is not merely
    wrong, it is not a quantity at all. Unit has to be part of the key.
    """
    rows = []
    for i, (s, e) in enumerate(Q):
        rows.append(fact("Revenues", s, e, 100 + i, filed="2020-12-31"))
        rows.append(fact("Revenues", s, e, 9_000 + i, filed="2020-12-31",
                         unit="EUR", accn=f"eur-{e}"))
    fresh_db(rows)

    rev = api.fundamentals(TICKER, years=10, as_of="2021-06-30")["revenue"]
    assert set(rev.dropna()) <= {100, 101, 102, 103}, (
        f"EUR facts leaked into the USD revenue series: {sorted(set(rev.dropna()))}")


def test_cover_page_share_count_snaps_to_a_real_reporting_period():
    """The phantom-row bug.

    dei's EntityCommonStockSharesOutstanding is dated at the FILING, weeks
    after the quarter it ships with. Left on its own date it becomes a row in
    the frame where every other column is empty — Apple's frame went from 75
    real quarters to 143 rows, half of them phantom periods no company ever
    reported. The value is worth keeping (it is the freshest share count
    available, which is what market cap wants), so it is snapped back onto the
    most recent real period instead of being dropped.
    """
    rows = [fact("Revenues", s, e, 100 + i, filed="2021-01-31")
            for i, (s, e) in enumerate(Q)]
    # Filed 2021-01-20: three weeks after the 2020-12-31 quarter it belongs to.
    rows.append(fact("EntityCommonStockSharesOutstanding", None, "2021-01-20",
                     5_000, filed="2021-01-31", unit="shares", taxonomy="dei"))
    fresh_db(rows)

    df = api.fundamentals(TICKER, years=10, as_of="2021-06-30")
    assert "2021-01-20" not in df.index, (
        "a cover-page filing date became its own row in the frame")
    assert len(df) == 4, f"expected 4 real quarters, got {len(df)} rows"
    assert df["shares_outstanding"].loc["2020-12-31"] == 5_000, (
        "the share count was dropped instead of snapped onto its quarter")


def test_balance_sheet_share_count_keeps_its_own_date():
    """The snap must not fire on a normal balance-sheet instant.

    CommonStockSharesOutstanding is tagged at period end like any other
    balance-sheet line. It is already on a real reporting date and must pass
    through untouched.
    """
    rows = [fact("Revenues", s, e, 100 + i, filed="2021-01-31")
            for i, (s, e) in enumerate(Q)]
    rows.append(fact("CommonStockSharesOutstanding", None, "2020-12-31", 7_000,
                     filed="2021-01-31", unit="shares"))
    fresh_db(rows)

    df = api.fundamentals(TICKER, years=10, as_of="2021-06-30")
    assert df["shares_outstanding"].loc["2020-12-31"] == 7_000
    assert len(df) == 4


def test_a_restatement_still_cannot_leak_backwards_through_the_merge():
    """The tag-merge must not become a hole in the point-in-time guarantee.

    Merging across tags reads more rows than before, so it gets its own
    as-of test: a value filed later must stay invisible to an earlier query
    no matter which tag carried it.
    """
    s, e = Q[0]
    fresh_db([
        fact("SalesRevenueNet", s, e, 100, filed="2020-05-01"),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", s, e, 555,
             filed="2023-01-01"),
    ])
    early = api.fundamentals(TICKER, years=10, as_of="2020-06-30")["revenue"]
    assert early.loc[e] == 100, "a 2023 filing leaked into a 2020 query"

    later = api.fundamentals(TICKER, years=10, as_of="2024-01-01")["revenue"]
    assert later.loc[e] == 555, "the higher-priority tag never took over"
