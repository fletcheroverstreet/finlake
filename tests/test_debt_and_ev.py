"""Total debt, and what it does to enterprise value.

Offline, on hand-built frames — never on the output of the implementation
being tested.

Debt is the weakest line in the tag list, because filers describe borrowings
in more ways than any other balance-sheet item. Every case here was live on
real data and none of it raised, logged, or looked wrong: a company simply
appeared to have less debt than it has, and the EV multiples built on that
ranked it as cheap.
"""

import os
import tempfile

os.environ["FINLAKE_HOME"] = tempfile.mkdtemp(prefix="finlake_test_debt_")

import pandas as pd  # noqa: E402
import pytest  # noqa: E402

from finlake import quality, ratios  # noqa: E402


def frame(**columns) -> pd.DataFrame:
    """One-row frame indexed by period end, unless the caller passes lists."""
    n = max(len(v) if isinstance(v, list) else 1 for v in columns.values())
    index = pd.date_range("2025-09-30", periods=n, freq="QE").strftime("%Y-%m-%d")
    return pd.DataFrame(
        {k: (v if isinstance(v, list) else [v] * n) for k, v in columns.items()},
        index=list(index))


def computed(data, price):
    return ratios.compute(data, price=pd.Series([price] * len(data),
                                                index=data.index))


# ---------------------------------------------------------------------------
# A partial resolution must not defeat the combined-total fallback
# ---------------------------------------------------------------------------
def test_the_combined_total_wins_over_a_lone_short_leg():
    """ORACLE, EXACTLY. It tags current maturities under `NotesPayableCurrent`
    ($7.2bn) and everything else under `DebtLongtermAndShorttermCombinedAmount`
    ($129.5bn). One leg resolving was treated as the whole answer, so Oracle's
    total debt read $7.2bn — an eighteen-fold understatement that reached
    enterprise value, net debt, debt/equity, interest coverage and Altman Z,
    and made Oracle look like a company with almost no borrowings.
    """
    data = frame(shares_outstanding=100.0, net_income=10.0, revenue=100.0,
                 cash=5.0, debt_short=7.2, total_debt_reported=129.5)
    out = computed(data, price=10.0)

    assert out["total_debt"].iloc[-1] == pytest.approx(129.5)
    # market cap 1000 + debt 129.5 − cash 5
    assert out["enterprise_value"].iloc[-1] == pytest.approx(1124.5)


def test_the_two_legs_win_when_they_are_the_larger_figure():
    """The combined element can equal the summed legs but never legitimately
    fall below them, so taking the larger can only correct an understatement.
    A filer whose legs are complete keeps them."""
    data = frame(shares_outstanding=100.0, cash=0.0,
                 debt_short=10.0, debt_long=90.0, total_debt_reported=100.0)
    assert computed(data, price=1.0)["total_debt"].iloc[-1] == pytest.approx(100.0)


def test_debt_is_never_the_sum_of_the_legs_and_the_combined_total():
    """The combined element INCLUDES the current portion. Adding it to
    `debt_short` rather than choosing between them would double-count exactly
    the amount the fallback exists to recover."""
    data = frame(shares_outstanding=100.0, cash=0.0,
                 debt_short=20.0, total_debt_reported=100.0)
    assert computed(data, price=1.0)["total_debt"].iloc[-1] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Unknown debt makes enterprise value unknown
# ---------------------------------------------------------------------------
def test_unresolvable_debt_leaves_enterprise_value_missing():
    """THE HOUSE RULE, BROKEN WHERE IT COST MOST. `total_debt.fillna(0)` inside
    the EV formula gave every filer whose debt no tag resolved an enterprise
    value of `market cap − cash`, stated without qualification:

        Ford  $24.9bn against ~$197bn — Ford Credit's book is tagged under
        company-specific extension elements, outside the us-gaap taxonomy

    and with it EV/EBITDA, EV/EBIT and EV/Sales, Ford's off by a factor of
    eight on a screen where it sorted as the cheapest name in its sector.
    """
    data = frame(shares_outstanding=100.0, cash=5.0, revenue=100.0,
                 operating_income=20.0, depreciation_amortization=5.0)
    out = computed(data, price=10.0)

    assert out["market_cap"].iloc[-1] == pytest.approx(1000.0)
    assert pd.isna(out["enterprise_value"].iloc[-1]), (
        "unknown debt was treated as zero debt")
    for key in ("ev_ebitda", "ev_ebit", "ev_sales"):
        assert pd.isna(out[key].iloc[-1]), f"{key} was computed from a guess"


def test_a_filer_reporting_zero_debt_still_gets_an_enterprise_value():
    """The distinction the rule turns on: a tagged 0.0 is a measurement, and
    an absent tag is not. Only the second is unknown."""
    data = frame(shares_outstanding=100.0, cash=5.0, revenue=100.0,
                 debt_short=0.0, debt_long=0.0)
    out = computed(data, price=10.0)
    assert out["enterprise_value"].iloc[-1] == pytest.approx(995.0)


# ---------------------------------------------------------------------------
# Surfacing debt that resolved only partly
# ---------------------------------------------------------------------------
def test_a_lone_short_leg_is_reported():
    """REALTY INCOME. `CommercialPaper` resolved and nothing else did, so its
    total debt came through as $1.4bn against roughly $26bn outstanding — not
    a gap, a small confident number. Reported rather than corrected: the tag
    that would fix it, `NotesPayable`, is a COMPONENT for other filers, and
    promoting it would trade this understatement for an overstatement
    elsewhere."""
    data = frame(debt_short=[1.4] * 4)
    findings = quality.check_debt_completeness("O", data)
    assert any(f.check == "debt_short_leg_only" for f in findings)


def test_a_complete_debt_picture_is_not_reported():
    data = frame(debt_short=[1.0] * 4, debt_long=[9.0] * 4,
                 interest_expense=[0.1] * 4)
    assert quality.check_debt_completeness("KO", data) == []


def test_an_impossible_borrowing_rate_is_reported():
    """BOSTON PROPERTIES paid $0.63bn of interest on $0.75bn of resolved debt.
    Nobody borrows at 84%; the denominator is a fraction of the balance. This
    is the stronger of the two signals because it needs no opinion about which
    tags a filer 'should' have used — the filing contradicts itself."""
    data = frame(debt_short=[0.75] * 4, debt_long=[0.0] * 4,
                 interest_expense=[0.16] * 4)
    findings = quality.check_debt_completeness("BXP", data)
    assert any(f.check == "implied_rate_implausible" for f in findings)


def test_a_normal_borrowing_rate_is_not_reported():
    data = frame(debt_short=[5.0] * 4, debt_long=[40.0] * 4,
                 interest_expense=[0.45] * 4)     # ~4% annualised
    assert not any(f.check == "implied_rate_implausible"
                   for f in quality.check_debt_completeness("KO", data))


def test_the_implied_rate_is_annualised_before_it_is_judged():
    """A quarterly frame's interest line is ONE QUARTER. Dividing that by a
    balance understates the implied rate fourfold, which would put every real
    finding under the threshold and silently disable the check."""
    # 0.15/quarter on 1.0 of debt = 60% a year. Quarterly it reads 15%, under
    # the 25% threshold.
    data = frame(debt_short=[1.0] * 4, interest_expense=[0.15] * 4)
    assert any(f.check == "implied_rate_implausible"
               for f in quality.check_debt_completeness("X", data))


def test_an_old_debt_tag_does_not_make_a_current_gap_look_complete():
    """Bounded to the recent periods, for the same reason `ratios.latest` had
    to be. Taking each column's newest non-missing value over a twelve-year
    frame answers "was this ever tagged", not "did it resolve for the balance
    sheet on screen"."""
    data = frame(debt_short=[2.0] * 8,
                 debt_long=[50.0, 50.0, None, None, None, None, None, None])
    assert any(f.check == "debt_short_leg_only"
               for f in quality.check_debt_completeness("X", data))


# ---------------------------------------------------------------------------
# A share count of zero is not a company worth nothing
# ---------------------------------------------------------------------------
def test_a_zero_share_count_does_not_become_a_zero_market_cap():
    """CARVANA carries `shares_outstanding = 0` for eighteen quarters here,
    from a cover-page fact filed before its class structure settled. Times a
    price that is a market cap of zero — and a market cap of zero is not a
    small company, it makes P/E, P/S and P/B all exactly 0.0, the cheapest
    possible value on every one of them, sorting straight to the top of a
    value screen.

    `_safe_div(require_positive_den=True)` already refused to divide BY that
    market cap, so earnings yield came back NaN on the very same row that
    reported a P/E of zero. The judgement existed; it was applied to the
    denominator only.
    """
    data = frame(shares_outstanding=0.0, shares_diluted=80.0, net_income=5.0,
                 revenue=100.0, equity=50.0)
    out = computed(data, price=25.0)

    assert out["market_cap"].iloc[-1] == pytest.approx(2000.0), (
        "the zero count was used instead of falling through to diluted")
    assert out["pe_ttm"].iloc[-1] == pytest.approx(400.0)


def test_no_usable_share_count_leaves_market_cap_missing():
    """With no fallback either, the honest answer is missing — never zero."""
    data = frame(shares_outstanding=0.0, net_income=5.0, revenue=100.0)
    out = computed(data, price=25.0)

    assert pd.isna(out["market_cap"].iloc[-1])
    for key in ("pe_ttm", "ps", "pb"):
        assert pd.isna(out[key].iloc[-1]), f"{key} was computed from a zero cap"


def test_a_negative_share_count_is_refused_too():
    """Differencing a year-to-date weighted AVERAGE produces these. See
    quarterize.quarterize_average."""
    data = frame(shares_outstanding=-10.0, shares_diluted=80.0,
                 net_income=5.0, revenue=100.0)
    assert computed(data, price=25.0)["market_cap"].iloc[-1] == pytest.approx(2000.0)


# ---------------------------------------------------------------------------
# Total liabilities from the identity
# ---------------------------------------------------------------------------
def test_altman_z_survives_a_filer_that_omits_total_liabilities():
    """`Liabilities` is a subtotal a great many filers omit — they present the
    current and non-current sections and let the reader add them — so it never
    resolves for 140 of 503 names, Amazon and AMD among them. That removed the
    fourth term of Altman Z entirely, and a Z-score missing its solvency term
    is not a slightly different Z-score."""
    common = dict(assets=1000.0, equity=400.0, assets_current=500.0,
                  liabilities_current=200.0, retained_earnings=300.0,
                  operating_income=100.0, revenue=800.0,
                  shares_outstanding=100.0)
    filed = computed(frame(liabilities=600.0, **common), price=10.0)
    omitted = computed(frame(**common), price=10.0)

    assert pd.notna(omitted["altman_z"].iloc[-1]), (
        "Altman Z dropped because the filer did not tag a subtotal")
    assert omitted["altman_z"].iloc[-1] == pytest.approx(
        filed["altman_z"].iloc[-1]), "assets - equity did not reproduce it"


def test_a_filed_total_liabilities_is_never_overridden():
    """The identity is a fallback, not a preference. Where the filer states
    the subtotal, that is the number."""
    data = frame(assets=1000.0, equity=400.0, liabilities=550.0,
                 assets_current=500.0, liabilities_current=200.0,
                 retained_earnings=300.0, operating_income=100.0,
                 revenue=800.0, shares_outstanding=100.0)
    out = computed(data, price=10.0)
    # 0.6 * mcap/liabilities differs between 550 and 600, so the term is
    # observable through the total.
    expected = (1.2 * 0.3 + 1.4 * 0.3 + 3.3 * 0.1
                + 0.6 * (1000.0 / 550.0) + 1.0 * 0.8)
    assert out["altman_z"].iloc[-1] == pytest.approx(expected)
