"""Fiscal-year assembly, and the revenue tag that quietly resolved to a part.

Both bugs here were live and neither raised anything. An annual column that
belongs to the wrong fiscal year is still four real quarters added up, and a
revenue line resolved to the ASC 606 component is still a filed number — they
just answer a question nobody asked, under a label that says otherwise.

All synthetic, all offline. FINLAKE_HOME points at a temp dir.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_fiscal_")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from finlake import api, statements, store  # noqa: E402

FACT_COLS = ("cik", "taxonomy", "tag", "unit", "period_start", "period_end",
             "val", "fy", "fp", "form", "accn", "filed", "frame")

CIK = 7777
TICKER = "FISCAL"


def fact(tag, start, end, val, *, filed, unit="USD", form="10-Q"):
    return (CIK, "us-gaap", tag, unit, start, end, float(val), None, None,
            form, f"{tag}-{start}-{end}", filed, None)


def fresh_db(rows, *, fiscal_year_end: str | None):
    conn = store.connect()
    store.init_db(conn)
    conn.executescript(
        "DELETE FROM facts; DELETE FROM securities; DELETE FROM ticker_map;")
    store.upsert_many(conn, "facts", FACT_COLS, rows)
    conn.execute(
        "INSERT INTO ticker_map (ticker, cik, exchange, valid_from, valid_to) "
        "VALUES (?,?,?,?,?)", (TICKER, CIK, "NASDAQ", "2000-01-01", None))
    conn.execute(
        "INSERT INTO securities (cik, name, fiscal_year_end) VALUES (?,?,?)",
        (CIK, "Fiscal Test Co", fiscal_year_end))
    conn.commit()
    conn.close()


# A June year-end filer: the Microsoft shape. Eight discrete quarters spanning
# two complete fiscal years, each quarter tagged on its own so no year-to-date
# differencing is involved and the only thing under test is the grouping.
JUNE_QUARTERS = [
    ("2023-07-01", "2023-09-30", 100.0),
    ("2023-10-01", "2023-12-31", 110.0),
    ("2024-01-01", "2024-03-31", 120.0),
    ("2024-04-01", "2024-06-30", 130.0),   # FY2024 ends here: 100+110+120+130
    ("2024-07-01", "2024-09-30", 140.0),
    ("2024-10-01", "2024-12-31", 150.0),
    ("2025-01-01", "2025-03-31", 160.0),
    ("2025-04-01", "2025-06-30", 170.0),   # FY2025: 140+150+160+170
]
FY2024_REVENUE = 100.0 + 110.0 + 120.0 + 130.0   # 460
FY2025_REVENUE = 140.0 + 150.0 + 160.0 + 170.0   # 620


def _june_filer(fiscal_year_end="0630"):
    fresh_db(
        [fact("Revenues", s, e, v, filed=e) for s, e, v in JUNE_QUARTERS],
        fiscal_year_end=fiscal_year_end)


def test_fiscal_year_comes_from_the_filers_own_declaration():
    """The bug: the year-end month was inferred as the most common month
    among period ends. Quarters land in four different months, each appears
    exactly as often as the others, and the tie fell to whatever the hash
    table ordered first.

    It was a coin flip that landed wrong nearly everywhere — Microsoft
    resolved to September against a real June, Walmart and Nvidia to October
    against January, Costco to November against August.
    """
    _june_filer()
    assert statements.fiscal_year_end_month(TICKER, pd.Index([])) == 6


def test_annual_columns_are_the_fiscal_year_the_company_reported():
    """Every annual figure must be a year the company actually published.

    With a September year-end wrongly inferred, "FY2025" became the four
    quarters ending 30 September 2025 — a rolling window that reconciles to no
    filing, no press release, and no data provider. Microsoft's FY2025 revenue
    came out at $293.8bn against a filed $281.7bn.
    """
    _june_filer()
    annual = statements.periods(TICKER, freq="annual", years=10)

    assert annual.attrs["fiscal_year_end_month"] == 6
    assert annual.loc[2024, "revenue"] == pytest.approx(FY2024_REVENUE)
    assert annual.loc[2025, "revenue"] == pytest.approx(FY2025_REVENUE)
    # The fiscal year ENDS on its year-end date, so FY2024's last period is
    # June 2024 -- not December 2024, which is where a calendar-year
    # assumption would put it.
    assert annual.loc[2024, "period_end"] == "2024-06-30"
    assert annual.loc[2025, "period_end"] == "2025-06-30"


def test_a_52_53_week_year_end_a_few_days_late_stays_in_its_own_year():
    """Retail and warehouse filers anchor on a weekday, not a date: "the
    Sunday nearest 31 August". So a year end drifts either way and sometimes
    crosses the month boundary — Costco's FY2019 ended 1 September.

    A plain `month > fiscal_month` comparison pushes that year end into the
    NEXT fiscal year, which both truncates one year to three quarters and
    gives the next one five.
    """
    quarters = [
        ("2023-09-05", "2023-11-26", 10.0),
        ("2023-11-27", "2024-02-18", 20.0),
        ("2024-02-19", "2024-05-12", 30.0),
        ("2024-05-13", "2024-09-01", 40.0),   # FY2024 ends 1 SEPTEMBER
    ]
    fresh_db([fact("Revenues", s, e, v, filed=e) for s, e, v in quarters],
             fiscal_year_end="0830")
    annual = statements.periods(TICKER, freq="annual", years=10)

    assert list(annual.index) == [2024]
    assert annual.loc[2024, "quarters_in_year"] == 4
    assert annual.loc[2024, "revenue"] == pytest.approx(100.0)


def test_a_january_year_end_rolls_the_calendar_year_forward():
    """Walmart and Nvidia: FY2026 runs February 2025 to January 2026, so
    three of its four quarters end in the PREVIOUS calendar year."""
    quarters = [
        ("2025-02-01", "2025-04-30", 1.0),
        ("2025-05-01", "2025-07-31", 2.0),
        ("2025-08-01", "2025-10-31", 3.0),
        ("2025-11-01", "2026-01-31", 4.0),
    ]
    fresh_db([fact("Revenues", s, e, v, filed=e) for s, e, v in quarters],
             fiscal_year_end="0131")
    annual = statements.periods(TICKER, freq="annual", years=10)

    assert list(annual.index) == [2026]
    assert annual.loc[2026, "revenue"] == pytest.approx(10.0)


def test_a_partial_year_is_counted_so_it_can_be_labelled_partial():
    """The newest fiscal year is always still running. Its revenue is a
    three-quarter figure printed in the same column as four-quarter ones,
    understated by roughly a quarter and looking entirely ordinary.

    `_annualize` has always counted the quarters; nothing ever read the count.
    `statement()` now carries it out to the renderer.
    """
    partial = JUNE_QUARTERS[:6]        # FY2024 complete, FY2025 only half
    fresh_db([fact("Revenues", s, e, v, filed=e) for s, e, v in partial],
             fiscal_year_end="0630")

    annual = statements.periods(TICKER, freq="annual", years=10)
    assert annual.loc[2024, "quarters_in_year"] == 4
    assert annual.loc[2025, "quarters_in_year"] == 2

    stmt = statements.statement(TICKER, "income", freq="annual", years=10)
    assert stmt.attrs["quarters_in_period"] == {"2024": 4, "2025": 2}


def test_missing_fiscal_year_end_still_produces_an_annual_statement():
    """A company with no securities row falls back to inference. A coin flip
    beats refusing to produce an annual statement at all."""
    _june_filer(fiscal_year_end=None)
    annual = statements.periods(TICKER, freq="annual", years=10)
    assert not annual.empty
    assert annual["revenue"].notna().any()


# ---------------------------------------------------------------------------
# Revenue tag priority
# ---------------------------------------------------------------------------
def test_revenue_prefers_the_income_statement_total_over_the_asc606_part():
    """`RevenueFromContractWithCustomer*` is revenue from contracts with
    customers, which for a REIT, a lender or an insurer is one component of
    the top line rather than the top line. Sitting first in the priority list,
    it resolved, returned a real number, and understated revenue by whatever
    part of the business sits outside ASC 606:

        AvalonBay    $0.01bn against a filed $3.04bn
        Amer. Tower  $0.94bn against $10.64bn
        Humana       $5.83bn against $129.66bn

    79 of the 115 universe names filing both tags disagreed by more than
    0.5%, and every one of them was understated.
    """
    fresh_db([
        fact("RevenueFromContractWithCustomerExcludingAssessedTax",
             "2024-01-01", "2024-03-31", 10.0, filed="2024-04-30"),
        fact("Revenues", "2024-01-01", "2024-03-31", 3040.0, filed="2024-04-30"),
    ], fiscal_year_end="1231")

    frame = api.fundamentals(TICKER, years=10, as_of="2025-01-01")
    assert frame.loc["2024-03-31", "revenue"] == pytest.approx(3040.0)


def test_a_bank_reports_revenue_net_of_the_interest_it_paid():
    """For a lender, `Revenues` is interest income before the interest paid to
    fund it. `RevenuesNetOfInterestExpense` is what "revenue" means for a bank
    and what its income statement leads with, so it outranks the gross tag."""
    fresh_db([
        fact("Revenues", "2024-01-01", "2024-03-31", 900.0, filed="2024-04-30"),
        fact("RevenuesNetOfInterestExpense",
             "2024-01-01", "2024-03-31", 400.0, filed="2024-04-30"),
    ], fiscal_year_end="1231")

    frame = api.fundamentals(TICKER, years=10, as_of="2025-01-01")
    assert frame.loc["2024-03-31", "revenue"] == pytest.approx(400.0)


def test_assessed_tax_is_not_treated_as_revenue():
    """"Including assessed tax" is revenue plus the sales taxes collected on
    it. A filer reporting both has the net figure as its top line, so the
    gross one must never outrank it."""
    fresh_db([
        fact("RevenueFromContractWithCustomerExcludingAssessedTax",
             "2024-01-01", "2024-03-31", 1000.0, filed="2024-04-30"),
        fact("RevenueFromContractWithCustomerIncludingAssessedTax",
             "2024-01-01", "2024-03-31", 1080.0, filed="2024-04-30"),
    ], fiscal_year_end="1231")

    frame = api.fundamentals(TICKER, years=10, as_of="2025-01-01")
    assert frame.loc["2024-03-31", "revenue"] == pytest.approx(1000.0)


def test_a_filer_with_only_the_asc606_tag_is_unaffected():
    """The reorder must not cost anything for the ordinary case — most of the
    universe tags revenue exactly one way."""
    fresh_db([
        fact("RevenueFromContractWithCustomerExcludingAssessedTax",
             "2024-01-01", "2024-03-31", 1234.0, filed="2024-04-30"),
    ], fiscal_year_end="1231")

    frame = api.fundamentals(TICKER, years=10, as_of="2025-01-01")
    assert frame.loc["2024-03-31", "revenue"] == pytest.approx(1234.0)
