"""Guards on the CONCEPTS fallback map itself (FINLAKE-FINDINGS.md F1-F5).

Not testing data — testing that the map's shape stays correct. A future
edit that re-adds the mislabeled fallback, or drops one of the new
concepts, should fail here immediately rather than surface as a silently
wrong number three layers up in some project built on this.
"""

import os
import tempfile

os.environ.setdefault("FINLAKE_HOME", tempfile.mkdtemp(prefix="finlake_test_"))

from finlake.api import CONCEPTS, INSTANT_CONCEPTS, NON_NEGATIVE_CONCEPTS  # noqa: E402

PRETAX_INCOME_TAG = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItems"
    "NoncontrollingInterest"
)


def test_operating_income_has_no_pretax_income_fallback():
    """DEC/F3: pretax income is a different economic quantity from operating
    income (it includes net interest). A filer without a discrete
    OperatingIncomeLoss tag must come back with operating_income missing,
    not silently substituted with pretax income."""
    assert PRETAX_INCOME_TAG not in CONCEPTS["operating_income"], (
        "the mislabeled fallback is back — this quietly conflates operating "
        "income and pretax income for every filer without a discrete "
        "OperatingIncomeLoss tag (was live for JPM, BAC, WFC, GS, MS, SCHW, "
        "O, PFE, XOM, JNJ, and KLAC before the fix)"
    )
    print("  ok  operating_income has no pretax-income fallback")


def test_pretax_income_is_its_own_concept():
    assert "pretax_income" in CONCEPTS
    assert PRETAX_INCOME_TAG in CONCEPTS["pretax_income"]
    print("  ok  pretax_income exists as its own concept")


def test_new_concepts_present():
    for c in ("shares_outstanding", "interest_expense", "pretax_income",
              "tax_expense", "goodwill", "intangible_assets",
              "accounts_receivable", "accounts_payable",
              "short_term_investments"):
        assert c in CONCEPTS, f"missing concept: {c}"
        assert CONCEPTS[c], f"concept {c} has no tags"
    print("  ok  all nine Phase A concepts are present with at least one tag")


def test_new_instant_concepts_registered():
    for c in ("shares_outstanding", "goodwill", "intangible_assets",
              "accounts_receivable", "accounts_payable",
              "short_term_investments"):
        assert c in INSTANT_CONCEPTS, f"{c} should be registered as instant"
    print("  ok  balance-sheet concepts are registered in INSTANT_CONCEPTS")


def test_phase_c_concepts_present():
    """F14/F15/F16: D&A, shares-repurchased count, goodwill impairment."""
    for c in ("depreciation_amortization", "shares_repurchased", "goodwill_impairment"):
        assert c in CONCEPTS, f"missing concept: {c}"
        assert CONCEPTS[c], f"concept {c} has no tags"
    print("  ok  the three Phase C concepts are present")


def test_ifrs_fallback_tags_present_for_core_concepts():
    """F13: a 20-F filer using the ifrs-full taxonomy must have at least
    one candidate tag in each core concept's fallback list. Note this
    only makes the TAGS resolvable -- a foreign filer that reports
    annually (no quarterly XBRL) still yields no quarterly history, see
    ISSUE-005 in finlake's vault."""
    expected = {
        "revenue": "Revenue",
        "cost_of_revenue": "CostOfSales",
        "operating_income": "ProfitLossFromOperatingActivities",
        "pretax_income": "ProfitLossBeforeTax",
        "equity": "Equity",
        "cash": "CashAndCashEquivalents",
        "cfo": "CashFlowsFromUsedInOperatingActivities",
    }
    for concept, ifrs_tag in expected.items():
        assert ifrs_tag in CONCEPTS[concept], (
            f"{concept} lost its IFRS fallback tag {ifrs_tag}"
        )
    print("  ok  IFRS fallback tags present on the core concepts")


def test_non_negative_concepts_scoped_narrowly():
    """Only revenue — not net_income, cfo, or operating_income, which can
    all legitimately be negative. See the docstring on
    quarterize.quarterize_multi for why this must stay narrow."""
    assert NON_NEGATIVE_CONCEPTS == {"revenue"}
    print("  ok  NON_NEGATIVE_CONCEPTS is scoped to revenue only")


if __name__ == "__main__":
    for fn in [
        test_operating_income_has_no_pretax_income_fallback,
        test_pretax_income_is_its_own_concept,
        test_new_concepts_present,
        test_new_instant_concepts_registered,
        test_phase_c_concepts_present,
        test_ifrs_fallback_tags_present_for_core_concepts,
        test_non_negative_concepts_scoped_narrowly,
    ]:
        fn()
    print("\nall api/CONCEPTS tests passed")


# ---------------------------------------------------------------------------
# 0.4.0 hub rebuild: structural guards on the expanded concept map.
# ---------------------------------------------------------------------------
def test_every_concept_has_at_least_one_tag():
    empty = [c for c, tags in CONCEPTS.items() if not tags]
    assert not empty, f"concepts with no candidate tags: {empty}"


def test_no_tag_is_claimed_by_two_concepts():
    """One XBRL tag must mean exactly one thing here.

    If two concepts list the same tag, they silently return identical
    columns under different names — which reads as corroboration ("both
    operating income and pretax income say 4.2B") when it is one number
    printed twice. The `operating_income` / `pretax_income` bug fixed in
    0.2.0 was this failure exactly, so it gets a standing guard.
    """
    # The exceptions, each deliberate: XBRL has combined elements a filer uses
    # when two quantities are equal, and those genuinely do belong to both
    # concepts. Anything not listed here sharing a tag is a mistake.
    shared_on_purpose = {
        # Filed when basic and diluted EPS are identical (no dilutive
        # securities outstanding). It is the correct value for both.
        "EarningsPerShareBasicAndDiluted": {"eps_basic", "eps_diluted"},
        # Last-resort fallback for diluted share count: when a filer reports
        # no diluted figure, basic is the closest available and is a floor
        # (diluted >= basic), never an overstatement.
        "WeightedAverageNumberOfSharesOutstandingBasic": {
            "shares_basic", "shares_diluted"},
    }

    owners: dict[str, set[str]] = {}
    for concept, tags in CONCEPTS.items():
        for tag in tags:
            owners.setdefault(tag, set()).add(concept)

    clashes = [
        f"{tag}: {sorted(cs)}"
        for tag, cs in owners.items()
        if len(cs) > 1 and shared_on_purpose.get(tag) != cs
    ]
    assert not clashes, "tags claimed by more than one concept: " + "; ".join(clashes)


def test_weighted_average_share_counts_are_not_registered_as_instant():
    """shares_basic/shares_diluted are weighted AVERAGES over a period.

    They look like balance-sheet quantities and are not: they are duration
    facts, and treating them as instants would mean never differencing a
    year-to-date average into a discrete quarter. shares_outstanding IS a
    genuine point-in-time level and does belong in the instant set.
    """
    assert "shares_diluted" not in INSTANT_CONCEPTS
    assert "shares_basic" not in INSTANT_CONCEPTS
    assert "shares_outstanding" in INSTANT_CONCEPTS


def test_instant_concepts_all_exist():
    unknown = INSTANT_CONCEPTS - set(CONCEPTS)
    assert not unknown, f"INSTANT_CONCEPTS names concepts that don't exist: {unknown}"


def test_unit_registries_reference_real_concepts():
    from finlake.api import (
        PER_SHARE_CONCEPTS, RATIO_CONCEPTS, SHARE_COUNT_CONCEPTS,
        COVER_PAGE_CONCEPTS, expected_unit,
    )
    for name, group in [("SHARE_COUNT", SHARE_COUNT_CONCEPTS),
                        ("PER_SHARE", PER_SHARE_CONCEPTS),
                        ("RATIO", RATIO_CONCEPTS),
                        ("COVER_PAGE", COVER_PAGE_CONCEPTS)]:
        unknown = group - set(CONCEPTS)
        assert not unknown, f"{name}_CONCEPTS names unknown concepts: {unknown}"

    assert expected_unit("revenue") == "USD"
    assert expected_unit("shares_outstanding") == "shares"
    assert expected_unit("eps_diluted") == "USD/shares"
    assert expected_unit("effective_tax_rate_reported") == "pure"


def test_non_negative_is_scoped_to_concepts_that_really_cannot_be_negative():
    """Guard on the 0.2.0 fix. Revenue can't be negative; net income, CFO,
    and operating income legitimately can. Adding one of those here would
    drop real losses and is a worse bug than the one it fixes."""
    for c in ("net_income", "cfo", "operating_income", "pretax_income"):
        assert c not in NON_NEGATIVE_CONCEPTS, (
            f"{c} was marked non-negative — this silently deletes real losses")
