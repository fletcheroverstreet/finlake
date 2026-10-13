"""The public surface. Everything above this line is plumbing.

    import finlake
    df = finlake.fundamentals("AAPL", years=10)
    df = finlake.fundamentals("AAPL", years=10, as_of="2019-03-14")
    px = finlake.prices("AAPL", start="2015-01-01")
    m  = finlake.macro("DGS10", as_of="2019-03-14")
    u  = finlake.universe(as_of="2019-03-14")
"""

from __future__ import annotations

import bisect
import datetime as dt
from dataclasses import replace

import pandas as pd

from . import pit, quarterize, store
from .sources import prices as price_src
from .sources import sec

# ---------------------------------------------------------------------------
# Concept -> candidate XBRL tags, in priority order.
#
# This mapping is the single most underrated part of a fundamentals layer.
# There is no tag called "Revenue". Apple uses one tag, a 2013 filing uses
# another, a REIT uses a third. Coding against a single tag silently returns
# NaN for a third of the market, and NaN gets dropped, and the survivors look
# unusually healthy. Always fall back down a list.
# ---------------------------------------------------------------------------
CONCEPTS: dict[str, list[str]] = {
    "revenue": [
        # ORDER MATTERS AND THIS ORDER WAS WRONG. `fundamentals` lets the
        # first tag that covers a period own it, and the ASC 606 element used
        # to sit at the top — but `RevenueFromContractWithCustomer*` is
        # revenue from CONTRACTS WITH CUSTOMERS, which for a large minority of
        # filers is one component of the top line rather than the top line.
        # Rent is not contract-with-customer revenue; nor is interest, nor
        # premium income, nor most commodity trading. So the tag resolves,
        # returns a real number, and understates revenue by however much of
        # the business sits outside ASC 606:
        #
        #     AvalonBay   $0.01bn against a filed $3.04bn   (-99.8%)
        #     Amer. Tower $0.94bn against $10.64bn          (-91%)
        #     Capital One $8.06bn against $53.43bn          (-85%)
        #     Walmart   $674.5bn against $681.0bn            (-1%)
        #
        # 79 of the 115 names filing both tags disagreed by more than 0.5%,
        # and every one of them was understated. It reached revenue, P/S,
        # EV/Sales, every margin, asset turnover and DSO, and none of it
        # looked wrong on screen.
        #
        # `Revenues` is the us-gaap element for the income statement total, so
        # it leads. `RevenuesNetOfInterestExpense` leads it in turn because
        # that is what "revenue" means for a bank — the gross figure a lender
        # tags as `Revenues` counts interest income before the interest it
        # paid to fund it.
        "RevenuesNetOfInterestExpense",
        "Revenues",
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        # Last of the ASC 606 pair: "including assessed tax" is revenue plus
        # the sales taxes collected on it, so a filer reporting both has the
        # net figure as its top line and this one overstates.
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "SalesRevenueNet",
        "SalesRevenueGoodsNet",
        # IFRS (ifrs-full taxonomy) fallbacks -- added in the lodestar
        # Phase C amendment (FINLAKE-FINDINGS.md F13). A foreign private
        # issuer filing 20-F (e.g. TSM) uses these local element names
        # instead of any us-gaap tag; without them a foreign filer's
        # fundamentals() call returns almost nothing despite pit.py's
        # PERIODIC_FORMS already including 20-F/40-F.
        "Revenue", "RevenueFromContractsWithCustomers", "RevenueFromSaleOfGoods",
    ],
    "cost_of_revenue": [
        "CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold",
        "CostOfSales",  # IFRS
    ],
    "gross_profit": ["GrossProfit"],  # "GrossProfit" is also ifrs-full's own element name; no separate entry needed
    "operating_income": [
        "OperatingIncomeLoss",
        "ProfitLossFromOperatingActivities",  # IFRS
    ],
    "net_income": [
        "NetIncomeLoss",
        "ProfitLoss",  # also ifrs-full's own element name for net income/loss
        "NetIncomeLossAvailableToCommonStockholdersBasic",
    ],
    "eps_diluted": ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"],
    "shares_diluted": [
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "rnd": ["ResearchAndDevelopmentExpense"],
    # SG&A as a single reported line. `GeneralAndAdministrativeExpense` is
    # deliberately NOT a fallback here: G&A alone is a component of SG&A, not
    # a synonym for it, so a filer that breaks out selling and G&A separately
    # would have had its bare G&A figure returned as though it were the whole
    # of SG&A — understating the line by however much selling costs are.
    # Those filers are served by composing selling_marketing + general_admin
    # in statements.py, where the composition is visible and flagged.
    "sganda": ["SellingGeneralAndAdministrativeExpense"],
    "assets": ["Assets"],
    "liabilities": ["Liabilities"],
    "equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "Equity",  # IFRS
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "CashAndCashEquivalents",  # IFRS
    ],
    "debt_long": [
        "LongTermDebtNoncurrent", "LongTermDebt",
        # Finance leases are debt under ASC 842, and a large minority of
        # filers tag only the combined element — Phillips 66 among them,
        # which had no debt at all in the frame despite $19bn of it on the
        # balance sheet. Missing debt does not blank a column, it silently
        # understates enterprise value, net debt and every leverage ratio
        # while leaving them looking perfectly ordinary. Adding this takes
        # `debt_long` from 72% of the universe to 86%.
        #
        # The current-maturities variant is deliberately NOT here: it
        # includes the portion `debt_short` already captures, so a filer
        # tagging both would have its near-term debt counted twice. It sits
        # below the existing tags so a filer using both keeps the
        # noncurrent-only figure, which is the one that pairs with
        # `debt_short` without overlap.
        "LongTermDebtAndCapitalLeaseObligations",
        # Filers whose entire long-term debt is one instrument type tag it by
        # that type and never use a generic element. Both were confirmed
        # against the market provider's own enterprise value before being
        # added — Akamai 5.9bn against 5.6bn implied, Cadence 2.5bn against
        # 2.5bn — which is the only evidence that separates "this IS the total"
        # from "this is one component of it".
        #
        # Deliberately LAST, and deliberately narrow. `UnsecuredDebt` and
        # `SecuredDebt` are NOT here despite also matching two names: Digital
        # Realty files both as components of a larger total, so promoting
        # either would understate its debt while resolving cleanly — the same
        # shape as the ASC 606 revenue bug at the top of this table.
        "ConvertibleLongTermNotesPayable",
        "UnsecuredLongTermDebt",
    ],
    "debt_short": [
        "LongTermDebtCurrent", "ShortTermBorrowings",
        "LongTermDebtAndCapitalLeaseObligationsCurrent",
        "NotesPayableCurrent", "DebtCurrent", "CommercialPaper",
    ],
    # Total debt AS THE FILER REPORTED IT, in one line. A fallback, never a
    # preference: it overlaps `debt_short` by construction, so `ratios.py`
    # uses it only when neither leg resolved on its own. Without it, General
    # Motors and Oracle — which tag only the combined element — came through
    # with no debt at all, and an enterprise value equal to market cap.
    #
    # It does not rescue everyone. A captive-finance issuer like Ford splits
    # automotive from finance-arm debt under COMPANY-SPECIFIC extension
    # elements, which are outside the us-gaap taxonomy this layer ingests.
    # That is a real limitation, and reporting no debt for such a filer is at
    # least honest; inventing one would not be.
    "total_debt_reported": [
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities",
        "DebtAndCapitalLeaseObligations",
    ],
    "inventory": ["InventoryNet"],
    "cfo": [
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
        "CashFlowsFromUsedInOperatingActivities",  # IFRS
    ],
    "capex": [
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "PaymentsToAcquireProductiveAssets",
    ],
    "buybacks": ["PaymentsForRepurchaseOfCommonStock"],
    "dividends_paid": ["PaymentsOfDividendsCommonStock", "PaymentsOfDividends"],
    # --- Added in the lodestar Phase A amendment (see FINLAKE-FINDINGS.md) ---
    # Point-in-time shares outstanding, distinct from shares_diluted (which is
    # a *weighted-average* used only as an EPS denominator). Needed for market
    # cap and EV. EntityCommonStockSharesOutstanding is a dei cover-page fact,
    # dated at or near the filing date — that timing is exactly what you want
    # for "shares outstanding as of roughly this price date".
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "EntityCommonStockSharesOutstanding",
    ],
    "interest_expense": ["InterestExpense", "InterestExpenseNonoperating"],
    # Pretax income. Split out from operating_income (see the comment above
    # OperatingIncomeLoss) so a caller who wants an EBIT proxy for a filer
    # with no discrete operating-income line can combine pretax_income +
    # interest_expense explicitly, instead of finlake silently returning the
    # wrong economic quantity under the operating_income name.
    "pretax_income": [
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
        "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
        "ProfitLossBeforeTax",  # IFRS
    ],
    "tax_expense": ["IncomeTaxExpenseBenefit"],
    "goodwill": ["Goodwill"],
    "intangible_assets": [
        "FiniteLivedIntangibleAssetsNet",
        "IntangibleAssetsNetExcludingGoodwill",
    ],
    "accounts_receivable": ["AccountsReceivableNetCurrent", "AccountsReceivableNet"],
    "accounts_payable": ["AccountsPayableCurrent", "AccountsPayable"],
    "short_term_investments": ["ShortTermInvestments", "MarketableSecuritiesCurrent"],
    # --- Added in the lodestar Phase C amendment (see FINLAKE-FINDINGS.md) ---
    "depreciation_amortization": [
        "DepreciationDepletionAndAmortization",  # F14
        "DepreciationAmortizationAndAccretionNet",
        "DepreciationAndAmortization",
        "Depreciation",
    ],
    # Shares repurchased in the period -- distinct from `buybacks` (the
    # dollar amount, already mapped). Needed to compute a per-share
    # average repurchase price, not just total dollars spent. F15.
    "shares_repurchased": ["StockRepurchasedDuringPeriodShares"],
    "goodwill_impairment": ["GoodwillImpairmentLoss"],  # F16

    # ======================================================================
    # 0.4.0 hub rebuild — the rest of the three statements.
    #
    # Everything below exists so `statements.py` can lay out a real income
    # statement, balance sheet, and cash flow statement rather than a
    # scattering of ratio inputs, and so `ratios.py` has the denominators it
    # needs. Every tag here was confirmed present in cached filings by
    # `scripts/audit_concepts.py` before being added — the same
    # check-before-adding process 0.3.0 used, and the reason there are
    # several fallbacks per line rather than one guess.
    # ======================================================================

    # ---- Income statement ------------------------------------------------
    "selling_marketing": [
        "SellingAndMarketingExpense",
        "MarketingAndAdvertisingExpense",
        "AdvertisingExpense",
    ],
    "general_admin": ["GeneralAndAdministrativeExpense"],
    # Total operating cost. `CostsAndExpenses` is the whole cost base
    # including cost of revenue; `OperatingExpenses` usually excludes it.
    # They are different quantities, so `statements.py` labels which one it
    # got rather than presenting either as "operating expenses" unqualified.
    "operating_expenses": ["OperatingExpenses"],
    "costs_and_expenses": ["CostsAndExpenses"],
    "interest_income": [
        "InvestmentIncomeInterest",
        "InterestIncomeOther",
        "InterestAndDividendIncomeOperating",
    ],
    "other_income": [
        "OtherNonoperatingIncomeExpense",
        "NonoperatingIncomeExpense",
    ],
    "net_income_continuing": [
        "IncomeLossFromContinuingOperations",
        "IncomeLossFromContinuingOperationsIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "minority_interest_income": [
        "NetIncomeLossAttributableToNoncontrollingInterest",
        "ProfitLossAttributableToNoncontrollingInterests",  # IFRS
    ],
    "preferred_dividends": [
        "PreferredStockDividendsAndOtherAdjustments",
        "PreferredStockDividendsIncomeStatementImpact",
    ],
    "eps_basic": ["EarningsPerShareBasic", "EarningsPerShareBasicAndDiluted"],
    "shares_basic": ["WeightedAverageNumberOfSharesOutstandingBasic"],
    "comprehensive_income": ["ComprehensiveIncomeNetOfTax"],
    "dividends_per_share": [
        "CommonStockDividendsPerShareDeclared",
        "CommonStockDividendsPerShareCashPaid",
    ],
    "effective_tax_rate_reported": ["EffectiveIncomeTaxRateContinuingOperations"],
    "amortization_intangibles": ["AmortizationOfIntangibleAssets"],
    "restructuring": ["RestructuringCharges"],

    # ---- Balance sheet ---------------------------------------------------
    "assets_current": ["AssetsCurrent"],
    "assets_noncurrent": ["AssetsNoncurrent"],
    "other_assets_current": ["OtherAssetsCurrent"],
    "ppe_gross": ["PropertyPlantAndEquipmentGross"],
    "ppe_net": ["PropertyPlantAndEquipmentNet"],
    "accumulated_depreciation": [
        "AccumulatedDepreciationDepletionAndAmortizationPropertyPlantAndEquipment",
    ],
    "long_term_investments": ["LongTermInvestments", "MarketableSecuritiesNoncurrent"],
    "operating_lease_asset": ["OperatingLeaseRightOfUseAsset"],
    "liabilities_current": ["LiabilitiesCurrent"],
    "accrued_liabilities": [
        "AccruedLiabilitiesCurrent",
        "EmployeeRelatedLiabilitiesCurrent",
    ],
    "deferred_revenue": [
        "ContractWithCustomerLiabilityCurrent",
        "DeferredRevenueCurrent",
        "ContractWithCustomerLiability",
    ],
    "operating_lease_liability": ["OperatingLeaseLiability"],
    "deferred_tax_liabilities": [
        "DeferredIncomeTaxLiabilitiesNet",
        "DeferredTaxLiabilitiesNoncurrent",
        "DeferredTaxLiabilities",
    ],
    "retained_earnings": ["RetainedEarningsAccumulatedDeficit"],
    "paid_in_capital": [
        "AdditionalPaidInCapital",
        "AdditionalPaidInCapitalCommonStock",
    ],
    "treasury_stock": ["TreasuryStockValue", "TreasuryStockCommonValue"],
    "aoci": ["AccumulatedOtherComprehensiveIncomeLossNetOfTax"],
    "common_stock_value": ["CommonStockValue"],
    "preferred_stock_value": ["PreferredStockValue"],
    "minority_interest": ["MinorityInterest"],
    "shares_issued": ["CommonStockSharesIssued"],
    "liabilities_and_equity": ["LiabilitiesAndStockholdersEquity"],

    # ---- Cash flow -------------------------------------------------------
    "cfi": [
        "NetCashProvidedByUsedInInvestingActivities",
        "NetCashProvidedByUsedInInvestingActivitiesContinuingOperations",
        "CashFlowsFromUsedInInvestingActivities",  # IFRS
    ],
    "cff": [
        "NetCashProvidedByUsedInFinancingActivities",
        "NetCashProvidedByUsedInFinancingActivitiesContinuingOperations",
        "CashFlowsFromUsedInFinancingActivities",  # IFRS
    ],
    "stock_comp": ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "deferred_income_tax_cf": ["DeferredIncomeTaxExpenseBenefit"],
    "change_receivables": ["IncreaseDecreaseInAccountsReceivable"],
    "change_inventory": ["IncreaseDecreaseInInventories"],
    "change_payables": ["IncreaseDecreaseInAccountsPayable"],
    "acquisitions": [
        "PaymentsToAcquireBusinessesNetOfCashAcquired",
        "PaymentsToAcquireBusinessesAndInterestInAffiliatesNetOfCashAcquired",
    ],
    "divestitures": [
        "ProceedsFromDivestitureOfBusinesses",
        "ProceedsFromDivestitureOfBusinessesNetOfCashDivested",
    ],
    "investments_purchased": [
        "PaymentsToAcquireInvestments",
        "PaymentsToAcquireMarketableSecurities",
    ],
    "investments_sold": [
        "ProceedsFromSaleMaturityAndCollectionsOfInvestments",
        "ProceedsFromSaleAndMaturityOfMarketableSecurities",
    ],
    "stock_issued": [
        "ProceedsFromIssuanceOfCommonStock",
        "ProceedsFromStockOptionsExercised",
    ],
    "debt_issued": [
        "ProceedsFromIssuanceOfLongTermDebt",
        "ProceedsFromIssuanceOfDebt",
    ],
    "debt_repaid": ["RepaymentsOfLongTermDebt", "RepaymentsOfDebt"],
    "fx_effect": [
        "EffectOfExchangeRateOnCashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
        "EffectOfExchangeRateOnCashAndCashEquivalents",
    ],
    "net_change_in_cash": [
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalentsPeriodIncreaseDecreaseIncludingExchangeRateEffect",
        "CashAndCashEquivalentsPeriodIncreaseDecrease",
    ],
    "interest_paid": ["InterestPaidNet", "InterestPaid"],
    "taxes_paid": ["IncomeTaxesPaidNet", "IncomeTaxesPaid"],
}

# Instant (balance-sheet) concepts. Used only as a sanity check — the real
# determination comes from whether the fact has a period_start.
INSTANT_CONCEPTS = {
    # Assets
    "assets", "assets_current", "assets_noncurrent", "cash",
    "short_term_investments", "accounts_receivable", "inventory",
    "other_assets_current", "ppe_gross", "ppe_net",
    "accumulated_depreciation", "goodwill", "intangible_assets",
    "long_term_investments", "operating_lease_asset",
    # Liabilities
    "liabilities", "liabilities_current", "accounts_payable",
    "accrued_liabilities", "deferred_revenue", "debt_short", "debt_long",
    "total_debt_reported",
    "operating_lease_liability", "deferred_tax_liabilities",
    # Equity
    "equity", "retained_earnings", "paid_in_capital", "treasury_stock",
    "aoci", "common_stock_value", "preferred_stock_value",
    "minority_interest", "liabilities_and_equity",
    # Share counts that are point-in-time levels, not period averages.
    # `shares_diluted` and `shares_basic` are deliberately NOT here: those are
    # WEIGHTED AVERAGES over a period, which makes them duration facts even
    # though they look like balance-sheet quantities.
    "shares_outstanding", "shares_issued",
}

# Concepts counted in shares, and concepts denominated per share. Everything
# not listed here is monetary. Used by `expected_unit` to pick between the
# several units one XBRL tag can arrive in — see the (tag, unit) keying in
# `fundamentals`.
SHARE_COUNT_CONCEPTS = {
    "shares_diluted", "shares_basic", "shares_outstanding", "shares_issued",
    "shares_repurchased",
}
PER_SHARE_CONCEPTS = {
    "eps_diluted", "eps_basic", "dividends_per_share",
}
# Dimensionless ratios, tagged with the XBRL unit "pure".
RATIO_CONCEPTS = {"effective_tax_rate_reported"}

# Concepts that are an AVERAGE over their period rather than a sum across it.
# These have a period_start like any flow, so nothing structural distinguishes
# them — the difference is semantic and has to be declared. Differencing their
# year-to-date chain subtracts two nearly identical averages and produced a
# NEGATIVE share count for Apple's fiscal Q4. See quarterize.quarterize_average.
PERIOD_AVERAGE_CONCEPTS = {"shares_basic", "shares_diluted"}

# Concepts that can resolve to a dei COVER-PAGE fact, which is dated at the
# filing rather than at the period it describes. These are snapped back onto
# the nearest real reporting period in `fundamentals` — see the comment there.
# `shares_outstanding` is the case that matters: its second candidate tag,
# EntityCommonStockSharesOutstanding, is a cover-page fact, while its first
# (CommonStockSharesOutstanding) is a normal balance-sheet instant.
COVER_PAGE_CONCEPTS = {"shares_outstanding"}


def expected_unit(concept: str) -> str:
    """The unit a concept is denominated in.

    Monetary by default: USD is the reporting currency for every domestic
    filer, and for the foreign filers that report in something else the
    fallback in `fundamentals` picks up whatever unit they actually used.
    """
    if concept in SHARE_COUNT_CONCEPTS:
        return "shares"
    if concept in PER_SHARE_CONCEPTS:
        return "USD/shares"
    if concept in RATIO_CONCEPTS:
        return "pure"
    return "USD"


# Concepts where a negative *derived* (differenced) quarter is never
# economically valid — revenue cannot be negative, so a negative derived
# figure always means a scope mismatch in the YTD chain (see
# ISSUE-001-wdc-divestiture-restatement in finlake's vault). Deliberately
# narrow: only concepts verified against real data during the Phase A
# amendment. Net income, CFO, and operating income are excluded on purpose —
# those are legitimately negative for loss-making or cash-burning companies,
# and dropping a real loss would be a worse bug than the one being fixed.
NON_NEGATIVE_CONCEPTS = {"revenue"}


def _quarterize_for(concept: str, facts: list[dict]) -> list[quarterize.Quarter]:
    """Pick the right period-reconstruction strategy for a concept.

    Three strategies, because there are three kinds of quantity and applying
    the wrong one produces numbers rather than errors:

      flow     -- accumulates across the period. Difference the year-to-date
                  chain. (Revenue, net income, cash flow.)
      average  -- an average OVER the period. Differencing two nearly equal
                  averages is meaningless; use the averaging identity instead.
                  (Weighted-average share counts.)
      ratio    -- take as filed; no arithmetic across periods reconstructs a
                  quarter. (Reported effective tax rate.)

    Balance-sheet instants need no strategy at all — quarterize detects them
    from the absence of a period_start and passes them through.
    """
    if concept in PERIOD_AVERAGE_CONCEPTS:
        return quarterize.quarterize_average(facts)
    if concept in RATIO_CONCEPTS:
        return quarterize.quarterize_passthrough(facts)
    return quarterize.quarterize_multi(
        facts, non_negative=concept in NON_NEGATIVE_CONCEPTS)


def fundamentals(
    ticker: str,
    *,
    years: int = 10,
    as_of: str | None = None,
    concepts: list[str] | None = None,
    include_provenance: bool = False,
) -> pd.DataFrame:
    """Ten years of clean quarterly fundamentals. One call. Offline.

    Returns a DataFrame indexed by period_end with one column per concept.
    With include_provenance=True you also get `<concept>__derived` and
    `<concept>__filed` columns, so you can always answer "where did this
    number come from and when did it become public".
    """
    concepts = concepts or list(CONCEPTS)
    as_of_date = as_of or dt.date.today().isoformat()
    min_period = (
        dt.date.fromisoformat(as_of_date) - dt.timedelta(days=365 * years + 120)
    ).isoformat()

    with store.session(read_only=True) as conn:
        cik = sec.resolve_cik(conn, ticker, as_of=as_of)
        if cik is None:
            raise KeyError(f"No CIK for {ticker!r} as of {as_of_date}. "
                           f"Run the builder, or check the ticker.")

        wanted_tags = [t for c in concepts for t in CONCEPTS[c]]
        rows = pit.as_of_facts(
            conn, cik, wanted_tags, as_of=as_of_date, min_period=min_period
        )

    # Keyed by (tag, unit), NOT tag alone.
    #
    # One tag routinely arrives in several units. A filer with foreign
    # operations tags Revenues in USD *and* EUR/CAD/GBP; a share-count tag can
    # carry both `shares` and a monetary unit. Pooling them means quarterize
    # differences a USD year-to-date figure against a EUR one and emits the
    # result as a quarter — a number that is not wrong by a little, it is not
    # a quantity at all. Keeping unit in the key makes each chain internally
    # consistent, and `expected_unit` then picks the one the caller meant.
    by_tag_unit: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        by_tag_unit.setdefault((r["tag"], r["unit"]), []).append(r)

    by_concept: dict[str, list[quarterize.Quarter]] = {}
    for concept in concepts:
        want_unit = expected_unit(concept)

        # MERGE the candidate tags into one history; never pick just one.
        #
        # Filers switch tags mid-history, and the switch is permanent. Apple
        # reports revenue under SalesRevenueNet through 2018 and under
        # RevenueFromContractWithCustomerExcludingAssessedTax from 2017 on.
        # Any rule that selects a single tag has to drop one era: choosing
        # "the tag with the most quarters" picks the legacy tag and blanks
        # every quarter after the switch (Apple's revenue was missing from
        # 2018 onward), while choosing "the first tag that resolves" blanks
        # everything before it.
        #
        # Neither era is wrong, so both are kept: walk the candidates in
        # CONCEPTS order — which is priority order, most specific first — and
        # let the first tag that covers a period own it, with later tags
        # filling only the periods still empty. Where two tags overlap, the
        # higher-priority one wins, so the seam is a preference and not an
        # average of two definitions.
        merged: dict[str, quarterize.Quarter] = {}
        for tag in CONCEPTS[concept]:
            units = [u for (t, u) in by_tag_unit if t == tag]
            if not units:
                continue
            # Prefer the unit this concept is denominated in. Fall back to
            # whichever unit has the most facts, so an all-IFRS filer
            # reporting only in EUR still resolves rather than returning
            # nothing — the alternative is a blank column for every
            # non-USD-reporting company.
            unit = max(units, key=lambda u: (u == want_unit, len(by_tag_unit[(tag, u)])))
            qs = _quarterize_for(concept, by_tag_unit[(tag, unit)])
            for q in qs:
                merged.setdefault(q.period_end, q)

        if not merged:
            continue
        chosen = sorted(merged.values(), key=lambda q: q.period_end)

        by_concept[concept] = chosen

    if not by_concept:
        return pd.DataFrame()

    # ---- Snap cover-page facts onto real reporting periods ----------------
    #
    # A dei cover-page fact is dated at the FILING, not at the period it
    # belongs to: Apple's EntityCommonStockSharesOutstanding carries dates
    # like 2026-07-17, roughly three weeks after the 2026-06-27 quarter it
    # was filed with. Left alone, each of those dates becomes its own row in
    # the frame — a phantom period where every column is empty except one,
    # tripling the row count and putting quarters in the index that no
    # company ever reported.
    #
    # The value itself is worth keeping (it is the freshest share count
    # available, which is what market cap wants), so it is snapped back to
    # the most recent real reporting period instead of being dropped. The
    # latest such fact wins when several land in one period.
    period_index = sorted({
        q.period_end
        for concept, qs in by_concept.items()
        if concept not in COVER_PAGE_CONCEPTS
        for q in qs
    })
    if period_index:
        for concept in COVER_PAGE_CONCEPTS & by_concept.keys():
            snapped: dict[str, quarterize.Quarter] = {}
            for q in by_concept[concept]:
                pos = bisect.bisect_right(period_index, q.period_end) - 1
                if pos < 0:
                    continue  # predates every reporting period we have
                target = period_index[pos]
                prev = snapped.get(target)
                if prev is None or q.period_end >= prev.period_end:
                    snapped[target] = q
            by_concept[concept] = [
                # Re-key to the reporting period, keeping the value and its
                # filed date so provenance still points at the real filing.
                replace(q, period_end=target)
                for target, q in sorted(snapped.items())
            ]

    series: dict[str, pd.Series] = {}
    for concept, chosen in by_concept.items():
        idx = [q.period_end for q in chosen]
        series[concept] = pd.Series([q.value for q in chosen], index=idx)
        if include_provenance:
            series[f"{concept}__derived"] = pd.Series(
                [q.derived for q in chosen], index=idx)
            series[f"{concept}__filed"] = pd.Series(
                [q.filed for q in chosen], index=idx)

    df = pd.DataFrame(series).sort_index()
    df.index.name = "period_end"
    cutoff = (
        dt.date.fromisoformat(as_of_date) - dt.timedelta(days=365 * years)
    ).isoformat()
    df = df[df.index >= cutoff]
    df.attrs["ticker"] = ticker.upper()
    df.attrs["cik"] = cik
    df.attrs["as_of"] = as_of_date
    return df


def prices(ticker: str, *, start: str | None = None, end: str | None = None,
           adjust: str = "split") -> pd.DataFrame:
    with store.session(read_only=True) as conn:
        return price_src.get_prices(
            conn, ticker, start=start, end=end, adjust=adjust)


def refresh_once(*, limit: int | None = None,
                 only: list[str] | None = None,
                 on_task=None) -> dict[str, int]:
    """Run whatever refresh tier is due, once. Returns rows touched per task.

    Public because keeping the cache current is a first-class operation, not
    an internal detail — a downstream tool that wants one background loop for
    the whole system should not have to reach into `finlake.refresh` and take
    a dependency on its internals.
    """
    from . import refresh as refresh_mod

    with store.session() as conn:
        return refresh_mod.run_once(conn, limit=limit, only=only,
                                    verbose=False, on_task=on_task)


def refresh_status() -> list[dict]:
    """What each refresh tier last did, and whether it is due now."""
    from . import refresh as refresh_mod

    with store.session() as conn:
        return refresh_mod.status(conn)


def universe_status() -> dict:
    """Which symbols the refresh tiers are keeping current, and from where.

    Public because it is the one number that says whether the loop is
    maintaining a universe or a symbol dump. Undeclared, it falls back to
    every ticker with cached facts — which includes each filer's preferred
    series, warrants and baby bonds, and was 711 symbols against a 503-name
    index. Printing the count and its source at start-up is what makes that
    difference visible instead of something you find by reading a log.
    """
    from . import refresh as refresh_mod

    with store.session(read_only=True) as conn:
        members = store.universe_members(conn)
        if members:
            return {"count": len(members),
                    "source": store.universe_source(conn) or "declared",
                    "declared": True}
        return {"count": len(refresh_mod._universe_tickers(conn)),
                "source": "every ticker with cached facts — no universe "
                          "declared (python -m finlake universe --file ...)",
                "declared": False}


def quote(ticker: str) -> dict | None:
    """The latest cached bar: price, its date, and the move from the prior close.

    Reads the same cached series `prices()` returns, deliberately. The hub's
    header and its price chart must never be able to disagree, and the only
    way to guarantee that is for both to read one accessor.

    Returns None when nothing is cached for the symbol.
    """
    return price_src.last_quote(ticker)


def quotes(tickers) -> dict[str, dict]:
    """`quote()` for many tickers at once, for a universe-wide table."""
    return price_src.last_quotes(tickers)


def vwap(ticker: str, *, start: str, end: str, adjust: str = "split",
         price: str = "typical") -> float | None:
    """Dollar-weighted average price over [start, end]. See sources.prices.vwap
    for the formula and parameter meanings. Returns None if there are no
    cached bars in the window."""
    with store.session(read_only=True) as conn:
        return price_src.vwap(
            conn, ticker, start=start, end=end, adjust=adjust, price=price)


def macro(series_id: str, *, as_of: str | None = None) -> pd.DataFrame:
    with store.session(read_only=True) as conn:
        rows = pit.macro_as_of(conn, series_id, as_of=as_of)
    if not rows:
        return pd.DataFrame(columns=["obs_date", "value", "realtime_start"])
    return pd.DataFrame(rows).set_index("obs_date")


def universe(as_of: str, **kwargs) -> pd.DataFrame:
    with store.session(read_only=True) as conn:
        return pd.DataFrame(pit.universe(conn, as_of, **kwargs))


def news(ticker: str, *, days: int = 60, as_of: str | None = None,
         limit: int = 200) -> pd.DataFrame:
    """Articles for one ticker in the window ending at `as_of`.

    Point-in-time like everything else: an article published after `as_of` is
    invisible to the query.
    """
    from .sources import news as news_src

    with store.session(read_only=True) as conn:
        rows = news_src.recent(conn, ticker, days=days, as_of=as_of, limit=limit)
    return pd.DataFrame(rows)


def news_signal(tickers: list[str], *, as_of: str | None = None,
                lookback_days: int = 60,
                half_life_days: float = 20.0) -> dict[str, dict]:
    """Aggregate news signal per ticker — event-weighted, time-decayed tone.

    Batched by design: a caller scoring a universe wants one pass over the
    news table, not one query per name.

    A ticker with no articles comes back with `score=None, coverage=0.0`,
    never a fabricated neutral zero. See sources.news.signal for why that
    distinction is load-bearing.
    """
    from .sources import news as news_src

    as_of = as_of or dt.date.today().isoformat()
    with store.session(read_only=True) as conn:
        return news_src.signal(
            conn, list(tickers), as_of,
            lookback_days=lookback_days, half_life_days=half_life_days)


def restatements(ticker: str, concept: str, period_end: str) -> pd.DataFrame:
    """Every published version of one number. Sanity check and signal."""
    with store.session(read_only=True) as conn:
        cik = sec.resolve_cik(conn, ticker)
        if cik is None:
            raise KeyError(ticker)
        out = []
        for tag in CONCEPTS[concept]:
            out.extend(pit.restatement_history(conn, cik, tag, period_end))
    return pd.DataFrame(out)
