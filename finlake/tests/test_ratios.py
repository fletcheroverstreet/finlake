"""Ratio formulas, against hand-computed fixtures.

Every expected value below is worked out in the test or its comment. None of
them were produced by running the implementation and pasting the output —
that only proves the code does what it does.

`compute()` takes a plain frame, so these need no cache, no network, and no
price file.
"""

import os
import tempfile

os.environ.setdefault("FINLAKE_HOME", tempfile.mkdtemp(prefix="finlake_test_ratios_"))

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from finlake import ratios  # noqa: E402

PERIODS = ["2023-03-31", "2023-06-30", "2023-09-30", "2023-12-31"]


def frame(**cols) -> pd.DataFrame:
    """A resolved-concepts frame with the same value in every period, so a
    ratio can be checked at a single row without rolling-window effects."""
    return pd.DataFrame(
        {k: [v] * len(PERIODS) for k, v in cols.items()}, index=PERIODS)


def only(df: pd.DataFrame, key: str) -> float:
    return float(df[key].iloc[-1])


# ---------------------------------------------------------------------------
# Margins and returns
# ---------------------------------------------------------------------------
def test_margins_hand_computed():
    """revenue 1000, COGS 600, operating income 250, net income 200.
        gross  = (1000-600)/1000 = 0.40
        op     = 250/1000        = 0.25
        net    = 200/1000        = 0.20
    """
    out = ratios.compute(frame(revenue=1000.0, cost_of_revenue=600.0,
                               operating_income=250.0, net_income=200.0))
    assert only(out, "gross_margin") == pytest.approx(0.40)
    assert only(out, "operating_margin") == pytest.approx(0.25)
    assert only(out, "net_margin") == pytest.approx(0.20)


def test_gross_profit_prefers_the_filed_line_over_the_derivation():
    """A filer that tags GrossProfit directly must have that number used, not
    a recomputation from revenue minus cost of revenue — the two differ when
    the filer's cost line excludes something their gross profit line does."""
    out = ratios.compute(frame(revenue=1000.0, cost_of_revenue=600.0,
                               gross_profit=350.0))
    assert only(out, "gross_margin") == pytest.approx(0.35)


def test_roe_and_roa_hand_computed():
    """net income 200, equity 800, assets 2000 -> ROE 0.25, ROA 0.10"""
    out = ratios.compute(frame(net_income=200.0, equity=800.0, assets=2000.0))
    assert only(out, "roe") == pytest.approx(0.25)
    assert only(out, "roa") == pytest.approx(0.10)


def test_roic_uses_nopat_not_net_income():
    """operating income 250, tax 30 on pretax 200 -> effective rate 0.15,
    NOPAT = 250 * 0.85 = 212.5.
    Invested capital = debt 400 + equity 800 - cash 200 = 1000.
    ROIC = 212.5 / 1000 = 0.2125.

    Using net income instead would give a number that moves when the company
    changes its financing mix, which is the opposite of what the ratio is for.
    """
    out = ratios.compute(frame(
        operating_income=250.0, pretax_income=200.0, tax_expense=30.0,
        net_income=170.0, debt_long=400.0, equity=800.0, cash=200.0))
    assert only(out, "roic") == pytest.approx(0.2125)


# ---------------------------------------------------------------------------
# Valuation
# ---------------------------------------------------------------------------
def test_roic_is_missing_when_invested_capital_is_a_rounding_residual():
    """Invested capital is debt + equity - cash, so for a company that has
    bought its equity down it is the small difference between large numbers.
    The division is well defined and the answer is noise: United Airlines
    returned 6,416%, McKesson 1,579%.

    assets 10,000, debt 100, equity 100, cash 150 -> invested capital 50,
    which is 0.5% of assets, under the 2% floor.
    """
    out = ratios.compute(frame(
        operating_income=250.0, pretax_income=200.0, tax_expense=30.0,
        assets=10_000.0, debt_long=100.0, equity=100.0, cash=150.0))
    assert only(out, "roic") != only(out, "roic"), (
        "a 425% ROIC on a denominator worth 0.5% of assets was reported as a "
        "return on capital")


def test_roic_survives_a_thin_but_real_capital_base():
    """The floor must not swallow genuinely high-return companies. Invested
    capital of 500 against assets of 10,000 is 5% — thin, and real."""
    out = ratios.compute(frame(
        operating_income=250.0, pretax_income=200.0, tax_expense=30.0,
        assets=10_000.0, debt_long=400.0, equity=200.0, cash=100.0))
    assert only(out, "roic") == pytest.approx(212.5 / 500.0)


def test_roic_is_not_blanked_when_total_assets_are_unknown():
    """The test is "known to be immaterial", not "not known to be material".
    With assets missing, `invested_capital >= nan` is False, so a plain
    comparison would blank ROIC for every caller holding the capital figures
    but not the balance-sheet total."""
    out = ratios.compute(frame(
        operating_income=250.0, pretax_income=200.0, tax_expense=30.0,
        debt_long=400.0, equity=800.0, cash=200.0))
    assert only(out, "roic") == pytest.approx(0.2125)


def test_pe_and_market_cap_hand_computed():
    """100 shares at $50 = $5,000 market cap; net income 250 -> P/E 20."""
    data = frame(net_income=250.0, shares_outstanding=100.0, revenue=1000.0)
    out = ratios.compute(data, price=pd.Series([50.0] * 4, index=PERIODS))
    assert only(out, "market_cap") == pytest.approx(5000.0)
    assert only(out, "pe_ttm") == pytest.approx(20.0)
    assert only(out, "ps") == pytest.approx(5.0)


def test_pe_is_missing_on_negative_earnings_not_negative():
    """A loss-making company has no meaningful P/E.

    The arithmetic is fine — 5000 / -250 = -20 — but "-20x" in a table sorted
    cheapest-first puts the biggest loser at the top of the buy list. Missing
    is the honest answer.
    """
    data = frame(net_income=-250.0, shares_outstanding=100.0)
    out = ratios.compute(data, price=pd.Series([50.0] * 4, index=PERIODS))
    assert pd.isna(only(out, "pe_ttm"))


def test_enterprise_value_hand_computed():
    """100 shares at $50 = 5,000 market cap.
        total debt = 100 short + 400 long   =   500
        cash+equiv =  200 cash + 300 ST inv =   500
        EV = 5000 + 500 - 500               = 5,000
    Net cash exactly offsets debt here, so EV lands back on market cap.
    EV/EBIT = 5000 / 500 = 10.
    """
    data = frame(shares_outstanding=100.0, debt_short=100.0, debt_long=400.0,
                 cash=200.0, short_term_investments=300.0, operating_income=500.0)
    out = ratios.compute(data, price=pd.Series([50.0] * 4, index=PERIODS))
    assert only(out, "enterprise_value") == pytest.approx(5000.0)
    assert only(out, "ev_ebit") == pytest.approx(10.0)


def test_enterprise_value_reflects_net_debt():
    """The same company with more debt than cash: EV must exceed market cap.
        total debt 900, cash 100 -> EV = 5000 + 900 - 100 = 5,800
    """
    data = frame(shares_outstanding=100.0, debt_long=900.0, cash=100.0,
                 operating_income=500.0)
    out = ratios.compute(data, price=pd.Series([50.0] * 4, index=PERIODS))
    assert only(out, "enterprise_value") == pytest.approx(5800.0)
    assert only(out, "net_debt") == pytest.approx(800.0)


def test_forward_pe_uses_the_estimate_and_is_missing_without_one():
    """Forward P/E is the one ratio here that can't come from a filing — the
    SEC publishes no estimates. With an estimate of $5 and a $50 price it is
    10x; with no estimate it must be missing, never silently the trailing P/E.
    """
    data = frame(net_income=250.0, shares_outstanding=100.0)
    price = pd.Series([50.0] * 4, index=PERIODS)

    with_est = ratios.compute(data, price=price,
                              forward_eps=pd.Series([5.0] * 4, index=PERIODS))
    assert only(with_est, "pe_forward") == pytest.approx(10.0)

    without = ratios.compute(data, price=price)
    assert pd.isna(only(without, "pe_forward"))


def test_free_cash_flow_and_yield_hand_computed():
    """CFO 400, capex 150 -> FCF 250. Market cap 5000 -> FCF yield 5%.

    capex is taken as an absolute value because filers are inconsistent about
    its sign: some tag the outflow positive, some negative. Subtracting a
    negative would ADD capex to cash flow and roughly double reported FCF.
    """
    for capex_sign in (150.0, -150.0):
        data = frame(cfo=400.0, capex=capex_sign, shares_outstanding=100.0)
        out = ratios.compute(data, price=pd.Series([50.0] * 4, index=PERIODS))
        assert only(out, "free_cash_flow") == pytest.approx(250.0), capex_sign
        assert only(out, "fcf_yield") == pytest.approx(0.05), capex_sign


# ---------------------------------------------------------------------------
# Health and efficiency
# ---------------------------------------------------------------------------
def test_liquidity_ratios_hand_computed():
    """current assets 600, inventory 200, current liabilities 300.
        current = 600/300           = 2.0
        quick   = (600-200)/300     = 1.333...
    """
    out = ratios.compute(frame(assets_current=600.0, inventory=200.0,
                               liabilities_current=300.0))
    assert only(out, "current_ratio") == pytest.approx(2.0)
    assert only(out, "quick_ratio") == pytest.approx(4 / 3)


def test_cash_conversion_cycle_hand_computed():
    """revenue 1000, COGS 500, AR 100, inventory 250, AP 50.
        DSO = 100/1000 * 365 = 36.5
        DIO = 250/500  * 365 = 182.5
        DPO =  50/500  * 365 = 36.5
        CCC = 36.5 + 182.5 - 36.5 = 182.5
    """
    out = ratios.compute(frame(revenue=1000.0, cost_of_revenue=500.0,
                               accounts_receivable=100.0, inventory=250.0,
                               accounts_payable=50.0))
    assert only(out, "dso") == pytest.approx(36.5)
    assert only(out, "dio") == pytest.approx(182.5)
    assert only(out, "dpo") == pytest.approx(36.5)
    assert only(out, "cash_conversion_cycle") == pytest.approx(182.5)


def test_interest_coverage_handles_either_sign_convention():
    """operating income 250, interest expense 25 -> 10x, whichever sign the
    filer used for the expense."""
    for sign in (25.0, -25.0):
        out = ratios.compute(frame(operating_income=250.0, interest_expense=sign))
        assert only(out, "interest_coverage") == pytest.approx(10.0), sign


# ---------------------------------------------------------------------------
# The house rule: missing stays missing
# ---------------------------------------------------------------------------
def test_a_missing_input_gives_a_missing_ratio_never_zero():
    """A company that reports no inventory has no inventory turnover. Zero
    would sort it to the bottom of an efficiency screen as though it were the
    worst in the universe rather than unmeasured."""
    out = ratios.compute(frame(revenue=1000.0, cost_of_revenue=600.0))
    assert pd.isna(only(out, "inventory_turnover"))
    assert pd.isna(only(out, "dio"))
    assert pd.isna(only(out, "current_ratio"))


def test_zero_denominator_gives_missing_not_infinity():
    out = ratios.compute(frame(revenue=0.0, net_income=100.0, equity=0.0))
    assert pd.isna(only(out, "net_margin"))
    assert pd.isna(only(out, "roe"))


def test_debt_absent_is_missing_but_debt_of_zero_is_zero():
    """A genuine zero and an unreported figure are different facts.

    A company that reports no debt line at all has unknown leverage; one that
    reports zero debt has none. Collapsing them hides real balance-sheet risk
    behind the same blank as a data gap.
    """
    unknown = ratios.compute(frame(equity=800.0, assets=2000.0))
    assert pd.isna(only(unknown, "debt_to_equity"))

    zero = ratios.compute(frame(equity=800.0, assets=2000.0, debt_long=0.0))
    assert only(zero, "debt_to_equity") == pytest.approx(0.0)


def test_every_catalogue_entry_is_actually_computed():
    """The catalogue drives the UI's tooltips and the methodology page. An
    entry with no implementation would document a ratio that never appears."""
    rich = frame(
        revenue=1000.0, cost_of_revenue=600.0, gross_profit=400.0,
        operating_income=250.0, pretax_income=230.0, tax_expense=30.0,
        net_income=200.0, depreciation_amortization=50.0, cfo=400.0,
        capex=150.0, assets=2000.0, assets_current=600.0, equity=800.0,
        liabilities=1200.0, liabilities_current=300.0, inventory=200.0,
        accounts_receivable=100.0, accounts_payable=50.0, cash=200.0,
        short_term_investments=100.0, debt_short=100.0, debt_long=400.0,
        retained_earnings=500.0, interest_expense=25.0, dividends_paid=60.0,
        dividends_per_share=0.6, buybacks=40.0, shares_outstanding=100.0,
        shares_diluted=105.0,
    )
    out = ratios.compute(rich, price=pd.Series([50.0] * 4, index=PERIODS),
                         forward_eps=pd.Series([5.0] * 4, index=PERIODS))
    missing = [r.key for r in ratios.CATALOGUE if r.key not in out.columns]
    assert not missing, f"catalogue entries with no implementation: {missing}"

    # PEG needs growth, which a flat fixture can't produce; everything else
    # should have resolved on inputs this complete.
    blank = [r.key for r in ratios.CATALOGUE
             if r.key in out.columns and pd.isna(out[r.key].iloc[-1])
             and r.key != "peg"]
    assert not blank, f"ratios that stayed missing on complete inputs: {blank}"


def test_explain_covers_every_ratio():
    for r in ratios.CATALOGUE:
        assert ratios.explain(r.key) is not None
        assert ratios.explain(r.key).formula
