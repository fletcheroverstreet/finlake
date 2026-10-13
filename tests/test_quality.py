"""The data-quality monitor.

Offline, against hand-built frames. The governing rule is asserted directly:
a failed check REPORTS, it never repairs. Silent correction is how a data
layer starts lying — the number on screen stops matching the filing it claims
to come from, and nothing says so.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_quality_")

import pandas as pd  # noqa: E402

from finlake import quality  # noqa: E402

PERIODS = ["2023-03-31", "2023-06-30"]


def frame(**cols) -> pd.DataFrame:
    return pd.DataFrame({k: [v] * len(PERIODS) for k, v in cols.items()},
                        index=PERIODS)


def checks(findings, name):
    return [f for f in findings if f.check == name]


# ---------------------------------------------------------------------------
# Balance sheet identity — the sharpest check available
# ---------------------------------------------------------------------------
def test_a_balanced_sheet_produces_no_finding():
    out = quality.check_balance_sheet_identity(
        "T", frame(assets=1_000_000_000.0, liabilities=600_000_000.0,
                   equity=400_000_000.0))
    assert out == []


def test_an_unbalanced_sheet_is_critical():
    """It is an accounting identity — a filer cannot report otherwise, so a
    violation we cannot attribute to the filer is our parsing error.

    Figures are in real dollars throughout this file. The checks carry a $1m
    absolute tolerance so that rounding in a multi-billion-dollar filing does
    not trip them, which means toy values below that floor all read as clean
    and would make these tests pass for the wrong reason.
    """
    out = quality.check_balance_sheet_identity(
        "T", frame(assets=1_000_000_000.0, liabilities=500_000_000.0,
                   equity=200_000_000.0))
    assert len(out) == 2                       # one per period
    assert all(f.severity == "critical" for f in out)


def test_the_filers_own_total_separates_two_different_failures():
    """The NVDA case, minimised.

    When the filer also tags LiabilitiesAndStockholdersEquity and it equals
    assets, the FILER balances and our components are what fall short — a
    concept we do not map (mezzanine/temporary equity). That is a different
    problem in a different layer from "the balance sheet doesn't balance",
    and collapsing them sends you hunting in the wrong place.
    """
    out = quality.check_balance_sheet_identity("T", frame(
        assets=1_000_000_000.0, liabilities=500_000_000.0,
        equity=413_000_000.0, liabilities_and_equity=1_000_000_000.0))
    assert len(out) == 2
    assert all(f.check == "unmapped_balance_sheet_line" for f in out)
    assert all(f.severity == "serious" for f in out), (
        "an unmapped line was reported as a wrong number")
    assert out[0].detail["unmapped"] == 87_000_000.0


def test_rounding_does_not_trip_the_identity():
    """Filings round to thousands. A sheet off by $3k on $400bn is rounding,
    and a monitor that cries about it gets ignored."""
    out = quality.check_balance_sheet_identity(
        "T", frame(assets=400_000_000_000.0, liabilities=250_000_000_000.0,
                   equity=149_999_997_000.0))
    assert out == []


def test_non_controlling_interests_may_sit_on_either_side():
    """Some filers put NCI inside StockholdersEquity and some outside. Both
    are correct, so both must balance."""
    out = quality.check_balance_sheet_identity(
        "T", frame(assets=1_000_000_000.0, liabilities=600_000_000.0,
                   equity=350_000_000.0, minority_interest=50_000_000.0))
    assert out == []


# ---------------------------------------------------------------------------
# Cash flow and income statement
# ---------------------------------------------------------------------------
def test_cash_flow_that_ties_out_is_clean():
    out = quality.check_cash_flow_ties_out("T", frame(
        cfo=500_000_000.0, cfi=-200_000_000.0, cff=-100_000_000.0,
        fx_effect=10_000_000.0, net_change_in_cash=210_000_000.0))
    assert out == []


def test_cash_flow_that_does_not_tie_out_is_flagged():
    out = quality.check_cash_flow_ties_out("T", frame(
        cfo=500_000_000.0, cfi=-200_000_000.0, cff=-100_000_000.0,
        net_change_in_cash=900_000_000.0))
    assert len(out) == 2
    assert all(f.severity == "serious" for f in out)


def test_a_negative_share_count_is_critical():
    """The direct guard on the trap that produced exactly this: deriving a
    fiscal Q4 by differencing a year-to-date weighted AVERAGE, which gave
    Apple a share count of minus 55 million."""
    out = quality.check_share_counts("T", frame(shares_diluted=-55_000_000.0))
    assert len(out) == 2
    assert all(f.severity == "critical" for f in out)
    assert "cannot be zero or negative" in out[0].message


def test_diluted_below_basic_is_flagged():
    out = quality.check_share_counts(
        "T", frame(shares_basic=1_000_000_000.0, shares_diluted=900_000_000.0))
    assert any(f.check == "diluted_below_basic" for f in out)


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
def test_missing_core_concepts_are_reported():
    out = quality.check_coverage("T", frame(revenue=100.0))
    missing = {f.detail.get("concept") or f.message.split()[0]
               for f in checks(out, "missing_concept")}
    assert "net_income" in missing
    assert "assets" in missing


def test_financials_are_not_flagged_for_concepts_they_never_report():
    """A bank has no operating/non-operating split and no capex line. A
    monitor that flags every bank every quarter for the same two non-problems
    teaches people to skim past it — and the real findings go with them."""
    data = frame(revenue=100.0, net_income=10.0, assets=1000.0,
                 liabilities=600.0, equity=400.0, cash=50.0, cfo=20.0,
                 shares_diluted=5.0)

    industrial = quality.check_coverage("T", data, is_financial=False)
    bank = quality.check_coverage("T", data, is_financial=True)

    industrial_missing = {f.message.split()[0]
                          for f in checks(industrial, "missing_concept")}
    bank_missing = {f.message.split()[0]
                    for f in checks(bank, "missing_concept")}

    assert {"operating_income", "capex"} <= industrial_missing
    assert not ({"operating_income", "capex"} & bank_missing), (
        "a bank was flagged for line items banks do not report")


def test_a_period_gap_is_surfaced_not_smoothed():
    """The quarterizer emits a gap deliberately when it cannot reconstruct a
    period honestly. Those gaps should be visible, not filled in."""
    data = pd.DataFrame({"revenue": [1.0, 2.0]},
                        index=["2022-03-31", "2023-06-30"])
    out = quality.check_period_continuity("T", data)
    assert len(out) == 1 and out[0].check == "period_gap"


def test_continuous_quarters_produce_no_gap_finding():
    data = pd.DataFrame({"revenue": [1.0, 2.0, 3.0]},
                        index=["2023-03-31", "2023-06-30", "2023-09-30"])
    assert quality.check_period_continuity("T", data) == []


# ---------------------------------------------------------------------------
# The governing rule
# ---------------------------------------------------------------------------
def test_no_check_ever_modifies_the_data():
    """A failed check reports; it never repairs. Silent correction is how a
    data layer starts lying."""
    data = frame(assets=1_000_000_000.0, liabilities=500_000_000.0,
                 equity=200_000_000.0, cfo=1e6, cfi=1e6, cff=1e6,
                 net_change_in_cash=99e6, shares_diluted=-5e6,
                 revenue=10e6, cost_of_revenue=4e6, gross_profit=99e6)
    before = data.copy(deep=True)

    quality.check_balance_sheet_identity("T", data)
    quality.check_cash_flow_ties_out("T", data)
    quality.check_income_statement_chain("T", data)
    quality.check_share_counts("T", data)
    quality.check_coverage("T", data)

    pd.testing.assert_frame_equal(data, before)


def test_score_weights_wrong_above_missing():
    """A broken identity means a number on screen is WRONG; thin coverage
    means a number is absent. Wrong is worse, and the score has to say so or
    it is just a finding count."""
    critical = [quality.Finding("T", "x", "critical", "m")]
    warnings = [quality.Finding("T", "x", "warning", "m") for _ in range(3)]
    assert quality.score_ticker(critical) < quality.score_ticker(warnings)


def test_a_clean_company_scores_100():
    assert quality.score_ticker([]) == 100.0


def test_info_findings_do_not_reduce_the_score():
    """A restatement is a real signal about a business, not a defect in the
    cache. It is surfaced without being penalised."""
    info = [quality.Finding("T", "restatement", "info", "revised")]
    assert quality.score_ticker(info) == 100.0
