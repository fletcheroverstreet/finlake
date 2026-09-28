"""Financial statements, laid out the way an investor reads them.

`fundamentals()` returns a wide frame of whatever concepts resolved. That is
the right shape for computation and the wrong shape for reading: it has no
order, no subtotals, and no distinction between a line the company filed and a
line we worked out. This module turns it into an income statement, a balance
sheet, and a cash flow statement.

Three rules hold everywhere here.

**A derived line is labelled derived.** Filers tag inconsistently — plenty
report revenue and cost of revenue but never tag GrossProfit. Computing the
difference is correct and useful; presenting it as though the company reported
it is not. Every line carries `is_derived`, and every derived line records the
formula that produced it.

**A missing line stays missing.** No zero-filling. If cost of revenue is
absent, gross profit is absent too, rather than silently equal to revenue —
which would show a 100% gross margin for every bank in the universe.

**Annual means fiscal year, not calendar year.** Apple's FY2024 ends in
September. Summing calendar quarters would blend two fiscal years and produce
a revenue figure the company never reported and no analyst would recognise.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Literal

import pandas as pd

from . import api

Frequency = Literal["quarterly", "annual", "ttm"]

# Bounds on the span between the FIRST and LAST period end in a 4-quarter
# window — which is three quarter-gaps, roughly 275 days, NOT 365. (365 is the
# span of the window itself, from the start of the first quarter; the ends are
# one quarter closer together than that.)
#
# Getting this backwards nulls every trailing-twelve-month flow in the
# database while leaving balance-sheet levels intact, so market cap and P/B
# still populate and only the income-statement ratios silently vanish.
#
# The range covers calendar quarters (275), 12/12/12/16-week retail calendars
# (280), and 52/53-week drift, while staying below the ~365 that four
# NON-consecutive quarters would span — which is the gap this guard exists to
# catch in the first place.
TTM_END_SPAN_MIN_DAYS = 240
TTM_END_SPAN_MAX_DAYS = 330


@dataclass(frozen=True)
class Line:
    """One row of a statement.

    `key` is the concept name (or a derived name); `label` is what a reader
    sees. `formula` is populated only for derived lines and is the whole point
    of the distinction — it lets any number on screen be traced to its inputs.
    """

    key: str
    label: str
    indent: int = 0                  # 0 = top level, 1 = component, 2 = detail
    is_subtotal: bool = False
    is_derived: bool = False
    formula: str | None = None
    # Computes the line from a frame of already-resolved concepts. None means
    # the line is taken straight from the filing.
    derive: Callable[[pd.DataFrame], pd.Series] | None = field(
        default=None, repr=False, compare=False)


def _col(df: pd.DataFrame, name: str) -> pd.Series:
    """A concept column, or an all-missing column of the right shape.

    Returning NaN rather than 0 for an absent concept is what keeps a missing
    input from turning into a confident wrong answer downstream: NaN
    propagates through arithmetic, 0 does not.
    """
    if name in df.columns:
        return pd.to_numeric(df[name], errors="coerce")
    return pd.Series(float("nan"), index=df.index, dtype="float64")


def _effective_sganda(df: pd.DataFrame) -> pd.Series:
    """SG&A as filed, or composed from its parts when it is not filed as one.

    A NAMED FUNCTION RATHER THAN AN INLINE LAMBDA BECAUSE TWO LINES NEED IT,
    and when only one of them had it the other was quietly wrong. Total
    operating expenses summed the RAW `sganda` column — which is empty for
    every filer that breaks selling and G&A out separately, Microsoft among
    them. The SG&A row displayed 23,098 (composed) while the total operating
    expenses row directly above it displayed 16,876, which is R&D alone. Both
    were labelled "derived"; only one of them had actually done the
    composition.
    """
    return _col(df, "sganda").fillna(
        _sum_present(df, "selling_marketing", "general_admin"))


def _sum_present(df: pd.DataFrame, *names: str) -> pd.Series:
    """Sum of the named concepts, missing where NONE of them are present.

    Deliberately not `.sum()` with skipna, which returns 0 for an all-missing
    row and would report a company with no disclosed operating costs as having
    zero of them. A partial sum is still returned when at least one component
    exists — flagged as derived, so the reader knows it is a floor rather than
    a complete figure.
    """
    return _sum_present_series(*(_col(df, n) for n in names))


def _sum_present_series(*series: pd.Series) -> pd.Series:
    """`_sum_present` over already-resolved series rather than column names.

    Needed so a derived line can be summed into another derived line — see
    `_effective_sganda`, which has no column of its own to name.
    """
    stacked = pd.concat(list(series), axis=1)
    out = stacked.sum(axis=1, skipna=True)
    return out.where(stacked.notna().any(axis=1))


# ---------------------------------------------------------------------------
# Statement definitions
# ---------------------------------------------------------------------------
INCOME_STATEMENT: list[Line] = [
    Line("revenue", "Revenue"),
    Line("cost_of_revenue", "Cost of revenue", indent=1),
    Line("gross_profit", "Gross profit", is_subtotal=True, is_derived=True,
         formula="revenue - cost_of_revenue",
         derive=lambda d: _col(d, "gross_profit").fillna(
             _col(d, "revenue") - _col(d, "cost_of_revenue"))),
    Line("rnd", "Research & development", indent=1),
    Line("sganda", "Selling, general & administrative", indent=1,
         is_derived=True, formula="selling_marketing + general_admin (when not filed as one line)",
         derive=_effective_sganda),
    Line("restructuring", "Restructuring", indent=1),
    Line("operating_expenses_total", "Total operating expenses", indent=1,
         is_derived=True,
         formula="OperatingExpenses as filed, else R&D + SG&A + restructuring "
                 "(SG&A itself composed from selling + G&A when not filed as "
                 "one line)",
         derive=lambda d: _col(d, "operating_expenses").fillna(
             _sum_present_series(
                 _col(d, "rnd"), _effective_sganda(d), _col(d, "restructuring")))),
    Line("operating_income", "Operating income", is_subtotal=True),
    Line("interest_expense", "Interest expense", indent=1),
    Line("interest_income", "Interest income", indent=1),
    Line("other_income", "Other income (expense)", indent=1),
    Line("pretax_income", "Pretax income", is_subtotal=True),
    Line("tax_expense", "Income tax", indent=1),
    Line("net_income", "Net income", is_subtotal=True),
    Line("eps_basic", "EPS, basic"),
    Line("eps_diluted", "EPS, diluted"),
    Line("shares_basic", "Shares, basic (wtd avg)"),
    Line("shares_diluted", "Shares, diluted (wtd avg)"),
    Line("ebitda", "EBITDA", is_subtotal=True, is_derived=True,
         formula="operating_income + depreciation_amortization",
         derive=lambda d: _col(d, "operating_income")
         + _col(d, "depreciation_amortization")),
]

BALANCE_SHEET: list[Line] = [
    Line("cash", "Cash & equivalents", indent=1),
    Line("short_term_investments", "Short-term investments", indent=1),
    Line("accounts_receivable", "Accounts receivable", indent=1),
    Line("inventory", "Inventory", indent=1),
    Line("other_assets_current", "Other current assets", indent=1),
    Line("assets_current", "Total current assets", is_subtotal=True),
    Line("ppe_net", "Property, plant & equipment, net", indent=1),
    Line("goodwill", "Goodwill", indent=1),
    Line("intangible_assets", "Intangible assets", indent=1),
    Line("long_term_investments", "Long-term investments", indent=1),
    Line("operating_lease_asset", "Operating lease right-of-use asset", indent=1),
    Line("assets", "Total assets", is_subtotal=True),

    Line("accounts_payable", "Accounts payable", indent=1),
    Line("accrued_liabilities", "Accrued liabilities", indent=1),
    Line("deferred_revenue", "Deferred revenue", indent=1),
    Line("debt_short", "Short-term debt", indent=1),
    Line("liabilities_current", "Total current liabilities", is_subtotal=True),
    Line("debt_long", "Long-term debt", indent=1),
    Line("operating_lease_liability", "Operating lease liability", indent=1),
    Line("deferred_tax_liabilities", "Deferred tax liabilities", indent=1),
    Line("liabilities", "Total liabilities", is_subtotal=True),

    Line("common_stock_value", "Common stock", indent=1),
    Line("paid_in_capital", "Additional paid-in capital", indent=1),
    Line("retained_earnings", "Retained earnings", indent=1),
    Line("treasury_stock", "Treasury stock", indent=1),
    Line("aoci", "Accumulated other comprehensive income", indent=1),
    Line("minority_interest", "Non-controlling interests", indent=1),
    Line("equity", "Total shareholders' equity", is_subtotal=True),
    Line("shares_outstanding", "Shares outstanding"),

    Line("total_debt", "Total debt", is_derived=True,
         formula="debt_short + debt_long",
         derive=lambda d: _sum_present(d, "debt_short", "debt_long")),
    Line("net_debt", "Net debt", is_derived=True,
         formula="total debt - cash - short-term investments",
         derive=lambda d: _sum_present(d, "debt_short", "debt_long")
         - _col(d, "cash").fillna(0) - _col(d, "short_term_investments").fillna(0)),
    Line("working_capital", "Working capital", is_derived=True,
         formula="assets_current - liabilities_current",
         derive=lambda d: _col(d, "assets_current") - _col(d, "liabilities_current")),
    Line("invested_capital", "Invested capital", is_derived=True,
         formula="total debt + equity - cash - short-term investments",
         derive=lambda d: (_sum_present(d, "debt_short", "debt_long").fillna(0)
                           + _col(d, "equity")
                           - _col(d, "cash").fillna(0)
                           - _col(d, "short_term_investments").fillna(0))),
    Line("book_value", "Book value (common equity)", is_derived=True,
         formula="equity - preferred stock",
         derive=lambda d: _col(d, "equity") - _col(d, "preferred_stock_value").fillna(0)),
]

CASH_FLOW: list[Line] = [
    Line("net_income", "Net income"),
    Line("depreciation_amortization", "Depreciation & amortization", indent=1),
    Line("stock_comp", "Stock-based compensation", indent=1),
    Line("deferred_income_tax_cf", "Deferred income tax", indent=1),
    Line("change_receivables", "Change in receivables", indent=2),
    Line("change_inventory", "Change in inventory", indent=2),
    Line("change_payables", "Change in payables", indent=2),
    Line("cfo", "Cash from operations", is_subtotal=True),

    Line("capex", "Capital expenditure", indent=1),
    Line("acquisitions", "Acquisitions", indent=1),
    Line("divestitures", "Divestitures", indent=1),
    Line("investments_purchased", "Investments purchased", indent=1),
    Line("investments_sold", "Investments sold", indent=1),
    Line("cfi", "Cash from investing", is_subtotal=True),

    Line("buybacks", "Share repurchases", indent=1),
    Line("dividends_paid", "Dividends paid", indent=1),
    Line("debt_issued", "Debt issued", indent=1),
    Line("debt_repaid", "Debt repaid", indent=1),
    Line("stock_issued", "Stock issued", indent=1),
    Line("cff", "Cash from financing", is_subtotal=True),

    Line("fx_effect", "Effect of exchange rates", indent=1),
    Line("net_change_in_cash", "Net change in cash", is_subtotal=True),

    Line("free_cash_flow", "Free cash flow", is_subtotal=True, is_derived=True,
         formula="cfo - capex",
         derive=lambda d: _col(d, "cfo") - _col(d, "capex").abs()),
    Line("shareholder_return", "Total returned to shareholders", is_derived=True,
         formula="buybacks + dividends_paid",
         derive=lambda d: _sum_present(d, "buybacks", "dividends_paid")),
]

STATEMENTS: dict[str, list[Line]] = {
    "income": INCOME_STATEMENT,
    "balance": BALANCE_SHEET,
    "cash_flow": CASH_FLOW,
}

# Balance-sheet lines are levels, not flows. Summing four quarters of "total
# assets" produces a number four times too large; a trailing-twelve-month
# balance sheet is just the latest one. This is trap 3 from the README, one
# layer up.
_LEVEL_KEYS = api.INSTANT_CONCEPTS | {
    "total_debt", "net_debt", "working_capital", "invested_capital",
    "book_value", "shares_outstanding",
}
# Per-share amounts don't sum meaningfully across quarters either in the same
# way — but EPS specifically DOES: four quarters of EPS is annual EPS. Only
# genuine levels are excluded.
_LEVEL_KEYS -= {"eps_basic", "eps_diluted"}


# Concepts that are period AVERAGES or RATES rather than flows. These are the
# third category, and missing it is subtle: they are not balance-sheet levels
# (they do describe a period), but summing them is still wrong.
#
# Weighted-average diluted shares is the case that bites. Four quarters of
# "average shares outstanding" summed gives roughly 4x the real share count,
# which then divides into every per-share figure built on it — book value per
# share, revenue per share, and any P/E computed from a share count rather
# than from reported EPS all come out about 75% too low, and all of them look
# like ordinary numbers.
_AVERAGE_KEYS = {
    "shares_basic", "shares_diluted", "effective_tax_rate_reported",
}


def is_level(key: str) -> bool:
    """True for balance-sheet levels, which must never be summed over time."""
    return key in _LEVEL_KEYS


def is_average(key: str) -> bool:
    """True for period averages and rates, which are meaned, never summed."""
    return key in _AVERAGE_KEYS


# ---------------------------------------------------------------------------
# Period assembly
# ---------------------------------------------------------------------------
def fiscal_year_end_month(ticker: str | None, index: pd.Index) -> int | None:
    """The month a company's fiscal year ends in, as 1-12.

    READ FROM THE FILER'S OWN DECLARATION, not inferred from the data. The
    previous rule — the most common month among period ends — cannot work, and
    its own docstring said why without following the thought through: quarters
    land in four different months, each appears exactly as often as the others,
    and `value_counts()` then returns whichever the hash table happened to
    order first. The result was not a bad estimate, it was a coin flip.

    It landed wrong nearly everywhere. Microsoft resolved to September against
    a real June year-end, Walmart and Nvidia to October against January,
    Costco to November against August. Every "Annual" column those produced was
    a rolling four-quarter window that the company never reported and that
    matches no figure in any filing, press release, or data provider —
    Microsoft's FY2025 revenue came out at $293.8bn against a filed $281.7bn.

    The SEC publishes the answer directly, as `fiscal_year_end` on the
    submissions record (MMDD), and the builder already stores it. The modal
    month survives only as a fallback for a company with no securities row,
    where a coin flip still beats refusing to produce an annual statement.
    """
    if ticker:
        try:
            from . import store
            from .sources import sec

            with store.session(read_only=True) as conn:
                cik = sec.resolve_cik(conn, ticker)
                if cik is not None:
                    row = conn.execute(
                        "SELECT fiscal_year_end FROM securities WHERE cik = ?",
                        (cik,),
                    ).fetchone()
                    raw = (row["fiscal_year_end"] or "") if row else ""
                    # Stored as MMDD ('0630'). Anything else is unusable.
                    if len(raw) == 4 and raw.isdigit():
                        month = int(raw[:2])
                        if 1 <= month <= 12:
                            return month
        except Exception:
            pass  # fall through to the inference below

    if len(index) == 0:
        return None
    months = pd.Series([str(d)[5:7] for d in index])
    counts = months.value_counts()
    if not len(counts):
        return None
    try:
        return int(counts.index[0])
    except (TypeError, ValueError):
        return None


# How far a period end may fall on the wrong side of the fiscal anchor and
# still belong to that fiscal year. 52/53-week filers anchor on a weekday
# ("the Sunday nearest 31 August"), so a year end drifts a few days either
# way and can cross a month boundary — Costco's FY2019 ended 1 September.
# The nearest OTHER quarter end is ~91 days away, so a window this wide
# cannot capture the wrong quarter.
_FISCAL_ANCHOR_TOLERANCE_DAYS = 20


def _fiscal_year_of(ends: pd.Series, fy_month: int) -> pd.Series:
    """Which fiscal year each period end belongs to.

    Shifts every date so the fiscal year end lands at the calendar year end,
    then reads the year off. Done as a shift rather than as a month comparison
    because the comparison has no way to express the 52/53-week tolerance: a
    year end of 1 September against a fiscal anchor of 31 August is one day
    late, and `month > fy_month` puts it in the following fiscal year.
    """
    shifted = (ends
               + pd.DateOffset(months=(12 - fy_month) % 12)
               - pd.Timedelta(days=_FISCAL_ANCHOR_TOLERANCE_DAYS))
    return shifted.dt.year


def _annualize(df: pd.DataFrame, fy_end_month: int | None) -> pd.DataFrame:
    """Roll discrete quarters up into fiscal years.

    Flows sum across the four quarters of the fiscal year; levels take the
    value at the fiscal year end. A year missing any of its four quarters is
    returned with the flows it has AND a `quarters_in_year` count, so a
    partial year is visibly partial rather than silently understated.
    """
    if df.empty or not fy_end_month:
        return pd.DataFrame()

    ends = pd.to_datetime(pd.Series(list(df.index), index=df.index))
    fiscal_year = _fiscal_year_of(ends, int(fy_end_month))

    out_rows = {}
    for fy, group in df.groupby(fiscal_year.values):
        row = {}
        for col in df.columns:
            if col.endswith("__derived") or col.endswith("__filed"):
                continue
            series = pd.to_numeric(group[col], errors="coerce")
            if not series.notna().any():
                row[col] = float("nan")
            elif is_level(col):
                row[col] = series.dropna().iloc[-1]   # the year-end snapshot
            elif is_average(col):
                row[col] = series.mean()              # a period average, not a flow
            else:
                row[col] = series.sum()
        row["quarters_in_year"] = int(len(group))
        row["period_end"] = group.index[-1]
        out_rows[int(fy)] = row

    out = pd.DataFrame.from_dict(out_rows, orient="index").sort_index()
    out.index.name = "fiscal_year"
    return out


def _ttm(df: pd.DataFrame) -> pd.DataFrame:
    """Trailing twelve months at every quarter end.

    Flows are a rolling 4-quarter sum; levels are the value as of that
    quarter. The rolling sum requires four consecutive quarters actually
    present — `min_periods=4` — because a 3-quarter sum presented as TTM
    understates by roughly 25% and looks entirely plausible.
    """
    if df.empty:
        return pd.DataFrame()

    numeric = df[[c for c in df.columns
                  if not c.endswith("__derived") and not c.endswith("__filed")]]
    numeric = numeric.apply(pd.to_numeric, errors="coerce")

    levels = [c for c in numeric.columns if is_level(c)]
    averages = [c for c in numeric.columns if is_average(c)]
    flows = [c for c in numeric.columns
             if c not in levels and c not in averages]

    out = pd.DataFrame(index=numeric.index)
    if flows:
        out[flows] = numeric[flows].rolling(4, min_periods=4).sum()
    if averages:
        # A trailing-twelve-month share count is the average over those four
        # quarters, not their sum — same reasoning as the annual rollup.
        out[averages] = numeric[averages].rolling(4, min_periods=4).mean()
    if levels:
        out[levels] = numeric[levels]

    # Guard against a rolling window that silently spans a gap. Four rows are
    # not four quarters if one of them is two years earlier.
    ends = pd.to_datetime(pd.Series(list(numeric.index), index=numeric.index))
    span = ends - ends.shift(3)
    valid = span.dt.days.between(TTM_END_SPAN_MIN_DAYS, TTM_END_SPAN_MAX_DAYS)
    if flows or averages:
        out.loc[~valid.values, flows + averages] = float("nan")
    return out


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------
def periods(
    ticker: str, *, freq: Frequency = "quarterly", years: int = 20,
    as_of: str | None = None,
) -> pd.DataFrame:
    """Resolved concepts for one ticker at the requested frequency.

    The shared substrate under all three statements: `income_statement`,
    `balance_sheet`, and `cash_flow` all call this and then select and label
    the lines they care about.
    """
    raw = api.fundamentals(ticker, years=years, as_of=as_of)
    if raw.empty:
        return raw

    if freq == "quarterly":
        out = raw
    elif freq == "ttm":
        out = _ttm(raw)
    elif freq == "annual":
        fy_month = fiscal_year_end_month(ticker, raw.index)
        out = _annualize(raw, fy_month)
        out.attrs["fiscal_year_end_month"] = fy_month
    else:
        raise ValueError(f"unknown freq {freq!r}; use quarterly, annual, or ttm")

    fy_month = out.attrs.get("fiscal_year_end_month")
    out.attrs.update(raw.attrs)
    out.attrs["freq"] = freq
    if fy_month is not None:
        out.attrs["fiscal_year_end_month"] = fy_month
    return out


def statement(
    ticker: str, kind: str = "income", *, freq: Frequency = "quarterly",
    years: int = 20, as_of: str | None = None, periods_back: int | None = None,
) -> pd.DataFrame:
    """One financial statement, ready to render.

    Returns a frame indexed by line key with one column per period, plus
    `label`, `indent`, `is_subtotal`, `is_derived`, and `formula` metadata
    columns. Lines with no data in any period are dropped — an income
    statement shouldn't carry twelve empty rows for a company that reports
    six of them.
    """
    if kind not in STATEMENTS:
        raise ValueError(f"unknown statement {kind!r}; use one of {list(STATEMENTS)}")

    data = periods(ticker, freq=freq, years=years, as_of=as_of)
    if data.empty:
        return pd.DataFrame()

    if periods_back:
        data = data.tail(periods_back)

    rows, meta = {}, {}
    for line in STATEMENTS[kind]:
        if line.derive is not None:
            values = line.derive(data)
        elif line.key in data.columns:
            values = pd.to_numeric(data[line.key], errors="coerce")
        else:
            continue
        if not values.notna().any():
            continue  # nothing filed and nothing derivable: omit the row
        rows[line.key] = values
        meta[line.key] = {
            "label": line.label, "indent": line.indent,
            "is_subtotal": line.is_subtotal, "is_derived": line.is_derived,
            "formula": line.formula,
        }

    if not rows:
        return pd.DataFrame()

    out = pd.DataFrame(rows).T
    meta_df = pd.DataFrame.from_dict(meta, orient="index")
    out = meta_df.join(out)
    out.index.name = "line"
    out.attrs.update(data.attrs)
    out.attrs["statement"] = kind

    # How complete each annual column is, carried out to the renderer.
    #
    # `_annualize` has always counted the quarters behind a fiscal year, and
    # nothing has ever looked at the count. The first and last year in any
    # window are routinely partial — the newest because the year is still
    # running, the oldest because the `years=` cutoff sliced it — and a
    # three-quarter revenue figure printed in the same row as four-quarter
    # ones is understated by about a quarter while looking entirely ordinary.
    if "quarters_in_year" in data.columns:
        out.attrs["quarters_in_period"] = {
            str(period): int(count)
            for period, count in data["quarters_in_year"].items()
            if pd.notna(count)
        }
    if "period_end" in data.columns:
        out.attrs["period_ends"] = {
            str(period): str(end)
            for period, end in data["period_end"].items()
            if pd.notna(end)
        }
    return out


def income_statement(ticker: str, **kw) -> pd.DataFrame:
    return statement(ticker, "income", **kw)


def balance_sheet(ticker: str, **kw) -> pd.DataFrame:
    return statement(ticker, "balance", **kw)


def cash_flow(ticker: str, **kw) -> pd.DataFrame:
    return statement(ticker, "cash_flow", **kw)


def latest_period_end(ticker: str, *, as_of: str | None = None) -> str | None:
    """The most recent quarter this company has reported as of a date."""
    raw = api.fundamentals(ticker, years=3, as_of=as_of, concepts=["revenue", "assets"])
    return str(raw.index[-1]) if not raw.empty else None
