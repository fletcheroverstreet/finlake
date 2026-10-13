"""The ratios an investor actually asks for, computed from the statements.

Everything here is derived — nothing is fetched. That matters for trust: any
number this module returns can be reproduced by hand from two filed figures,
and `explain()` will tell you which two.

Three conventions, consistent with the rest of finlake:

**A missing input gives a missing ratio.** Never a zero, never a substituted
default. A company that doesn't report inventory has no inventory turnover,
which is different from an inventory turnover of zero.

**Denominators are guarded.** Division by zero or by a negative quantity that
makes the ratio meaningless returns missing rather than infinity. A P/E on
negative earnings is the standard case: the arithmetic works, the number is
nonsense, and "-8.4x" printed in a table reads as cheap.

**Point-in-time is preserved.** Ratios that mix a market price with a filed
figure use the price as of the period end, not today's price against a
five-year-old balance sheet.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from . import api, statements

# Below this, a denominator is treated as zero rather than as a small number.
# Guards against a ratio exploding on a rounding artifact in a filed figure.
EPS = 1e-9

# Invested capital must be at least this fraction of total assets for ROIC to
# mean anything.
#
# ROIC's denominator is a RESIDUAL — debt plus equity minus cash — so for a
# company that has bought back its equity down to nothing it is the small
# difference between several large numbers. The division is well defined and
# the answer is noise: United Airlines' invested capital came to 0.07% of its
# assets and produced a ROIC of 6,416%; McKesson, with negative book equity,
# 1,579%.
#
# Neither is a data error and neither is a business fact. Printing them in a
# "return on capital" column is the same failure as printing a P/E on negative
# earnings, which this module already refuses to do — a number that is
# arithmetically right and means nothing a reader will take it to mean.
#
# Calibrated against the universe: 2% excludes exactly the four names whose
# ROIC exceeds 100% on a denominator under 2% of assets, and leaves every name
# with a real capital base — including the genuinely high-return ones like
# Home Depot at 11% — untouched.
MIN_INVESTED_CAPITAL_TO_ASSETS = 0.02


@dataclass(frozen=True)
class RatioDef:
    """One ratio: what it is, how it's computed, and how to read it."""

    key: str
    label: str
    group: str
    formula: str
    # Presentation only — the value itself is always the raw ratio.
    unit: str = "x"          # 'x' multiple | '%' percent | '$' currency | 'd' days
    higher_is_better: bool | None = None   # None => depends / neutral


def _safe_div(num: pd.Series, den: pd.Series, *,
              require_positive_den: bool = False) -> pd.Series:
    """Element-wise division that returns missing instead of nonsense.

    `require_positive_den` is what keeps a P/E on negative earnings out of the
    output. The division is perfectly well-defined; the resulting number just
    doesn't mean what a reader will assume it means, and a negative multiple
    sorted into a "cheapest names" table reads as the cheapest thing there.
    """
    num = pd.to_numeric(num, errors="coerce")
    den = pd.to_numeric(den, errors="coerce")
    bad = den.abs() < EPS
    if require_positive_den:
        bad = bad | (den <= 0)
    return (num / den.where(~bad))


def _get(df: pd.DataFrame, key: str) -> pd.Series:
    if key in df.columns:
        return pd.to_numeric(df[key], errors="coerce")
    return pd.Series(float("nan"), index=df.index, dtype="float64")


# ---------------------------------------------------------------------------
# The catalogue. Every ratio the hub can show, with its formula, in one place
# so the "How it works" page and the tooltips read from the same source as the
# computation rather than from a second, drifting copy.
# ---------------------------------------------------------------------------
CATALOGUE: list[RatioDef] = [
    # ---- Profitability ----
    RatioDef("gross_margin", "Gross margin", "Profitability",
             "gross profit / revenue", "%", True),
    RatioDef("operating_margin", "Operating margin", "Profitability",
             "operating income / revenue", "%", True),
    RatioDef("net_margin", "Net margin", "Profitability",
             "net income / revenue", "%", True),
    RatioDef("ebitda_margin", "EBITDA margin", "Profitability",
             "(operating income + D&A) / revenue", "%", True),
    RatioDef("fcf_margin", "Free cash flow margin", "Profitability",
             "(cash from operations - capex) / revenue", "%", True),
    RatioDef("roe", "Return on equity", "Profitability",
             "net income / shareholders' equity", "%", True),
    RatioDef("roa", "Return on assets", "Profitability",
             "net income / total assets", "%", True),
    RatioDef("roic", "Return on invested capital", "Profitability",
             "NOPAT / (total debt + equity - cash - short-term investments)",
             "%", True),
    RatioDef("roce", "Return on capital employed", "Profitability",
             "operating income / (total assets - current liabilities)", "%", True),

    # ---- Valuation ----
    RatioDef("pe_ttm", "P/E (trailing)", "Valuation",
             "market cap / trailing-twelve-month net income", "x", False),
    RatioDef("pe_forward", "P/E (forward)", "Valuation",
             "price / consensus next-twelve-month EPS estimate", "x", False),
    RatioDef("ps", "P/S", "Valuation", "market cap / TTM revenue", "x", False),
    RatioDef("pb", "P/B", "Valuation", "market cap / book value", "x", False),
    RatioDef("p_fcf", "P/FCF", "Valuation",
             "market cap / TTM free cash flow", "x", False),
    RatioDef("ev_ebitda", "EV/EBITDA", "Valuation",
             "enterprise value / TTM EBITDA", "x", False),
    RatioDef("ev_ebit", "EV/EBIT", "Valuation",
             "enterprise value / TTM operating income", "x", False),
    RatioDef("ev_sales", "EV/Sales", "Valuation",
             "enterprise value / TTM revenue", "x", False),
    RatioDef("peg", "PEG", "Valuation",
             "P/E trailing / 3-year earnings growth rate (%)", "x", False),
    RatioDef("earnings_yield", "Earnings yield", "Valuation",
             "TTM net income / market cap", "%", True),
    RatioDef("fcf_yield", "FCF yield", "Valuation",
             "TTM free cash flow / market cap", "%", True),
    RatioDef("dividend_yield", "Dividend yield", "Valuation",
             "TTM dividends per share / price", "%", True),
    RatioDef("buyback_yield", "Buyback yield", "Valuation",
             "TTM share repurchases / market cap", "%", True),
    RatioDef("shareholder_yield", "Shareholder yield", "Valuation",
             "dividend yield + buyback yield", "%", True),
    RatioDef("payout_ratio", "Payout ratio", "Valuation",
             "dividends paid / net income", "%", None),

    # ---- Financial health ----
    RatioDef("current_ratio", "Current ratio", "Health",
             "current assets / current liabilities", "x", True),
    RatioDef("quick_ratio", "Quick ratio", "Health",
             "(current assets - inventory) / current liabilities", "x", True),
    RatioDef("debt_to_equity", "Debt / equity", "Health",
             "total debt / shareholders' equity", "x", False),
    RatioDef("debt_to_assets", "Debt / assets", "Health",
             "total debt / total assets", "x", False),
    RatioDef("net_debt_to_ebitda", "Net debt / EBITDA", "Health",
             "(total debt - cash - short-term investments) / TTM EBITDA",
             "x", False),
    RatioDef("interest_coverage", "Interest coverage", "Health",
             "operating income / interest expense", "x", True),
    RatioDef("altman_z", "Altman Z-score", "Health",
             "1.2*WC/TA + 1.4*RE/TA + 3.3*EBIT/TA + 0.6*MktCap/TL + 1.0*Rev/TA",
             "x", True),

    # ---- Efficiency ----
    RatioDef("asset_turnover", "Asset turnover", "Efficiency",
             "TTM revenue / total assets", "x", True),
    RatioDef("inventory_turnover", "Inventory turnover", "Efficiency",
             "TTM cost of revenue / inventory", "x", True),
    RatioDef("dso", "Days sales outstanding", "Efficiency",
             "receivables / TTM revenue * 365", "d", False),
    RatioDef("dio", "Days inventory outstanding", "Efficiency",
             "inventory / TTM cost of revenue * 365", "d", False),
    RatioDef("dpo", "Days payables outstanding", "Efficiency",
             "payables / TTM cost of revenue * 365", "d", None),
    RatioDef("cash_conversion_cycle", "Cash conversion cycle", "Efficiency",
             "DSO + DIO - DPO", "d", False),
    RatioDef("fcf_conversion", "FCF conversion", "Efficiency",
             "free cash flow / net income", "%", True),

    # ---- Per share ----
    RatioDef("revenue_per_share", "Revenue per share", "Per share",
             "TTM revenue / diluted shares", "$", True),
    RatioDef("book_value_per_share", "Book value per share", "Per share",
             "shareholders' equity / shares outstanding", "$", True),
    RatioDef("fcf_per_share", "FCF per share", "Per share",
             "TTM free cash flow / diluted shares", "$", True),
    RatioDef("cash_per_share", "Cash per share", "Per share",
             "(cash + short-term investments) / shares outstanding", "$", True),
]

BY_KEY: dict[str, RatioDef] = {r.key: r for r in CATALOGUE}
GROUPS: list[str] = list(dict.fromkeys(r.group for r in CATALOGUE))


def explain(key: str) -> RatioDef | None:
    """The definition behind a ratio, for tooltips and the methodology page."""
    return BY_KEY.get(key)


# ---------------------------------------------------------------------------
# Market data alignment
# ---------------------------------------------------------------------------
def price_at_period_ends(ticker: str, index: pd.Index) -> pd.Series:
    """Closing price on (or just before) each period end.

    Uses the last close at or before the period end rather than today's price,
    so a five-year-old balance sheet is valued against the price that actually
    prevailed then. Without this, every historical valuation multiple silently
    becomes "today's price over an old fundamental", which makes a chart of
    P/E history a chart of earnings history with the axis mislabelled.
    """
    empty = pd.Series(float("nan"), index=index, dtype="float64")
    if len(index) == 0:
        return empty
    try:
        # AS TRADED, not split-adjusted. This series is multiplied by the
        # share count a company REPORTED at the time, and those two have to
        # agree about which side of a split they are on. Pairing a
        # split-adjusted price with a pre-split share count understates
        # market cap by the split ratio — NVDA's April 2024 market cap came
        # out at $21.6bn against a real ~$2.2tn, which then produced a "5-year
        # median P/E" of 1.13x on a stock that never traded below 30x.
        px = api.prices(ticker, start=str(min(index)), end=str(max(index)),
                        adjust="as_traded")
    except (FileNotFoundError, KeyError, ValueError):
        return empty
    if px is None or px.empty:
        return empty

    col = "adj_close" if "adj_close" in px.columns else "close"
    series = pd.Series(px[col].values,
                       index=pd.to_datetime(px["date"])).sort_index()
    wanted = pd.to_datetime(pd.Series(list(index)))
    # reindex+ffill = "last known close at or before this date", which handles
    # period ends landing on weekends and market holidays.
    aligned = series.reindex(
        series.index.union(wanted)).ffill().reindex(wanted)
    return pd.Series(aligned.values, index=index, dtype="float64")


def forward_eps_at_period_ends(
    ticker: str, index: pd.Index, *, as_of: str | None = None,
) -> pd.Series:
    """Consensus next-twelve-month EPS, matched to each reported period.

    The matching rule is the subtle part. A quarter is the *current* quarter
    from the day it is reported until the next one lands, so the consensus
    that belongs to it is the newest one published during that span — not the
    consensus as of the quarter end itself.

    That distinction is the difference between forward P/E working and being
    permanently blank: a company reports its June quarter in late July, and
    every estimate for it is published afterwards. Aligning strictly to the
    period end would find no snapshot at or before 30 June and return nothing,
    for every ticker, forever.

    It is still point-in-time. Every snapshot is bounded by the query's
    `as_of`, so a run dated last month sees only what existed last month, and
    a period's window closes when the next period arrives. What it will not do
    is back-apply today's consensus across a decade of history — that would
    plot what analysts believe *now* against what a company earned *then*, a
    pairing that was never true on any date.

    Snapshots only start accumulating the first time the market source runs,
    so forward P/E is legitimately missing for older periods. That is the
    correct answer there, not a gap to fill.
    """
    empty = pd.Series(float("nan"), index=index, dtype="float64")
    if len(index) == 0:
        return empty

    horizon = as_of or dt.date.today().isoformat()
    try:
        from . import store
        with store.session(read_only=True) as conn:
            rows = conn.execute(
                "SELECT as_of, forward_eps FROM market_snapshot "
                "WHERE ticker = ? AND forward_eps IS NOT NULL AND as_of <= ? "
                "ORDER BY as_of",
                (str(ticker).upper(), horizon),
            ).fetchall()
    except Exception:
        return empty
    if not rows:
        return empty

    snaps = pd.Series([float(r["forward_eps"]) for r in rows],
                      index=pd.to_datetime([r["as_of"] for r in rows])).sort_index()

    # Each period's window runs up to the NEXT period end (exclusive), or to
    # the query horizon for the most recent period. Take the newest snapshot
    # inside that window.
    ends = pd.to_datetime(pd.Series(list(index)))
    windows = list(ends[1:]) + [pd.Timestamp(horizon) + pd.Timedelta(days=1)]

    out = []
    for window_end in windows:
        eligible = snaps[snaps.index < window_end]
        out.append(float(eligible.iloc[-1]) if len(eligible) else float("nan"))
    return pd.Series(out, index=index, dtype="float64")


def _market_cap(data: pd.DataFrame, price: pd.Series) -> pd.Series:
    """Shares outstanding at the period end times the price then.

    Falls back to the diluted weighted average when a point-in-time share
    count is unavailable — an approximation, and flagged as one wherever it
    surfaces, but far better than dropping market cap entirely for a filer
    that never tags CommonStockSharesOutstanding.

    A NON-POSITIVE SHARE COUNT IS NOT A MEASUREMENT. Filers do tag zero:
    Carvana carries `shares_outstanding = 0` for eighteen quarters in this
    cache, from a cover-page fact filed before its class structure settled.
    Multiplied by a price that is a market cap of zero, and a market cap of
    zero is not a small company — it makes P/E, P/S and P/B all exactly 0.0,
    which is the cheapest possible value on every one of them and sorts
    straight to the top of a value screen.

    `_safe_div(require_positive_den=True)` already refuses to divide BY a
    non-positive market cap, which is the same judgement applied to the
    denominator only; earnings yield came back NaN on the very same row that
    reported a P/E of zero. Applying it at the source makes the two agree, and
    the count falls through to the diluted average exactly as a missing one
    does.
    """
    shares = _get(data, "shares_outstanding")
    shares = shares.where(shares > 0)
    diluted = _get(data, "shares_diluted")
    shares = shares.where(shares.notna(), diluted.where(diluted > 0))
    return shares * price


# ---------------------------------------------------------------------------
# The computation
# ---------------------------------------------------------------------------
def compute(data: pd.DataFrame, *, price: pd.Series | None = None,
            forward_eps: pd.Series | None = None) -> pd.DataFrame:
    """Every ratio in the catalogue, from a frame of resolved concepts.

    `data` is what `statements.periods()` returns — normally the TTM frame,
    since trailing-twelve-month is the right basis for a valuation multiple.
    Split out from `for_ticker()` so it is testable against a hand-built frame
    with no cache, no network, and no price file.
    """
    if data.empty:
        return pd.DataFrame()

    out = pd.DataFrame(index=data.index)
    g = lambda k: _get(data, k)  # noqa: E731 — reads better than the alternative

    revenue, net_income = g("revenue"), g("net_income")
    op_income, assets, equity = g("operating_income"), g("assets"), g("equity")
    cash_st = g("cash").fillna(0) + g("short_term_investments").fillna(0)
    # Total debt from its two legs where either resolves, and from the filer's
    # own combined line only where neither does.
    #
    # That order matters both ways. The combined element includes the current
    # portion, so preferring it would double-count against `debt_short` for
    # the filers that tag both. But falling back to it is what stops a company
    # that tags ONLY the combined line — General Motors, Oracle — coming
    # through with no debt at all, which does not blank a column: it silently
    # sets enterprise value equal to market cap and every leverage ratio to
    # missing, all of which look entirely ordinary on screen.
    #
    # AND A PARTIAL RESOLUTION MUST NOT DEFEAT THE FALLBACK, which is what
    # `where(has_debt, ...)` did. One leg resolving was treated as the whole
    # answer, so a filer that tags its current maturities under a us-gaap
    # element and everything else under the combined line reported only the
    # current portion:
    #
    #     Oracle    debt_short  $7.2bn   (NotesPayableCurrent)
    #               debt_long   —
    #               combined    $129.5bn (DebtLongtermAndShorttermCombined)
    #               resolved to $7.2bn
    #
    # an eighteen-fold understatement that reached enterprise value, net debt,
    # debt/equity, interest coverage and Altman Z — none of which looked
    # unusual, because Oracle simply appeared to be a company with very little
    # debt. The 0.12.0 fix added the combined element as a fallback and stopped
    # one step short: it only ever applied when BOTH legs were missing.
    #
    # The larger figure wins. The combined element is by construction long plus
    # short, so it can equal the summed legs but never legitimately fall below
    # them — and taking the maximum can therefore only ever correct an
    # understatement, never manufacture debt. It is a choice between two
    # figures, never a sum of them, so nothing is double-counted.
    legs = g("debt_short").fillna(0) + g("debt_long").fillna(0)
    legs = legs.where(g("debt_short").notna() | g("debt_long").notna())
    total_debt = pd.concat([legs, g("total_debt_reported")], axis=1).max(axis=1)
    ebitda = op_income + g("depreciation_amortization")
    fcf = g("cfo") - g("capex").abs()
    gross_profit = g("gross_profit").where(
        g("gross_profit").notna(), revenue - g("cost_of_revenue"))

    # ---- Profitability ----
    out["gross_margin"] = _safe_div(gross_profit, revenue, require_positive_den=True)
    out["operating_margin"] = _safe_div(op_income, revenue, require_positive_den=True)
    out["net_margin"] = _safe_div(net_income, revenue, require_positive_den=True)
    out["ebitda_margin"] = _safe_div(ebitda, revenue, require_positive_den=True)
    out["fcf_margin"] = _safe_div(fcf, revenue, require_positive_den=True)
    out["roe"] = _safe_div(net_income, equity, require_positive_den=True)
    out["roa"] = _safe_div(net_income, assets, require_positive_den=True)
    out["roce"] = _safe_div(op_income, assets - g("liabilities_current"),
                            require_positive_den=True)

    # ROIC uses NOPAT, not net income: the return on capital shouldn't move
    # when a company changes its financing mix. Effective tax rate is clamped
    # to a sane band — a small tax charge against near-zero pretax income
    # produces rates like 300%, which is a data artifact, not a tax policy.
    tax_rate = _safe_div(g("tax_expense"), g("pretax_income"),
                         require_positive_den=True).clip(0.0, 0.50).fillna(0.21)
    nopat = op_income * (1 - tax_rate)
    invested_capital = (total_debt.fillna(0) + equity - cash_st)
    # A denominator that is a residual of much larger numbers has to be
    # material before the ratio built on it means anything — see
    # MIN_INVESTED_CAPITAL_TO_ASSETS.
    #
    # The test is stated as "known to be immaterial", not "not known to be
    # material": with total assets missing, `invested_capital >= nan` is False
    # and a plain `.where` would blank ROIC for every caller that has the
    # capital figures but not the balance sheet total.
    immaterial = (assets.notna()
                  & (invested_capital
                     < assets.abs() * MIN_INVESTED_CAPITAL_TO_ASSETS))
    invested_capital = invested_capital.where(~immaterial)
    out["roic"] = _safe_div(nopat, invested_capital, require_positive_den=True)

    # ---- Valuation (needs a price) ----
    if price is None:
        price = pd.Series(float("nan"), index=data.index, dtype="float64")
    price = pd.to_numeric(price, errors="coerce").reindex(data.index)
    mcap = _market_cap(data, price)

    # UNKNOWN DEBT MAKES ENTERPRISE VALUE UNKNOWN. It used to make it zero.
    #
    # `.fillna(0)` here was the house rule — a missing number is missing and
    # never zero — broken in the one place it costs the most. For 46 of 503
    # names no debt element in CONCEPTS resolves at all, and every one of them
    # got an enterprise value of `market cap − cash`, presented with no
    # qualification whatsoever:
    #
    #     Ford        $24.9bn   against ~$197bn      (Ford Credit's book)
    #     KKR         $48.7bn   against ~$101bn
    #     AES          $8.7bn   against ~$49.5bn
    #     Extra Space $32.3bn   against ~$45.6bn
    #
    # and with it EV/EBITDA, EV/EBIT and EV/Sales — Ford's off by a factor of
    # eight, on a screen where it sorted as the cheapest name in its sector.
    #
    # The tempting alternative is to infer zero when the filer discloses no
    # borrowing at all. It cannot be done from this data: a captive-finance
    # issuer like Ford reports its debt under COMPANY-SPECIFIC extension
    # elements, outside the us-gaap taxonomy this layer ingests, so "no debt
    # element resolved" and "no debt" are genuinely indistinguishable here.
    # Roughly a dozen of the 46 have material debt and the rest have none, and
    # nothing in the filings separates them.
    #
    # So the answer is "unknown", which is the honest one and the one every
    # consumer already handles: a missing ratio drops out of a scoring bucket
    # and the remaining weights renormalise, where a wrong one is ranked
    # against its peers as though it were measured.
    ev = mcap + total_debt - cash_st

    out["pe_ttm"] = _safe_div(mcap, net_income, require_positive_den=True)
    out["ps"] = _safe_div(mcap, revenue, require_positive_den=True)
    out["pb"] = _safe_div(mcap, equity, require_positive_den=True)
    out["p_fcf"] = _safe_div(mcap, fcf, require_positive_den=True)
    out["ev_ebitda"] = _safe_div(ev, ebitda, require_positive_den=True)
    out["ev_ebit"] = _safe_div(ev, op_income, require_positive_den=True)
    out["ev_sales"] = _safe_div(ev, revenue, require_positive_den=True)
    out["earnings_yield"] = _safe_div(net_income, mcap, require_positive_den=True)
    out["fcf_yield"] = _safe_div(fcf, mcap, require_positive_den=True)
    out["buyback_yield"] = _safe_div(g("buybacks").abs(), mcap,
                                     require_positive_den=True)
    out["dividend_yield"] = _safe_div(g("dividends_per_share"), price,
                                      require_positive_den=True)
    out["shareholder_yield"] = out["dividend_yield"].fillna(0) + out["buyback_yield"].fillna(0)
    out["shareholder_yield"] = out["shareholder_yield"].where(
        out["dividend_yield"].notna() | out["buyback_yield"].notna())
    out["payout_ratio"] = _safe_div(g("dividends_paid").abs(), net_income,
                                    require_positive_den=True)

    # Forward P/E is the one ratio here that cannot be derived from filings:
    # the SEC publishes no estimates. It comes from the market data source, and
    # stays missing rather than being faked when that source has nothing.
    if forward_eps is not None:
        fe = pd.to_numeric(forward_eps, errors="coerce").reindex(data.index)
        out["pe_forward"] = _safe_div(price, fe, require_positive_den=True)
    else:
        out["pe_forward"] = float("nan")

    # PEG: trailing P/E over the earnings growth rate, in percent.
    earnings_growth = net_income.pct_change(4) * 100
    out["peg"] = _safe_div(out["pe_ttm"], earnings_growth, require_positive_den=True)

    # ---- Health ----
    out["current_ratio"] = _safe_div(g("assets_current"), g("liabilities_current"),
                                     require_positive_den=True)
    out["quick_ratio"] = _safe_div(g("assets_current") - g("inventory").fillna(0),
                                   g("liabilities_current"), require_positive_den=True)
    out["debt_to_equity"] = _safe_div(total_debt, equity, require_positive_den=True)
    out["debt_to_assets"] = _safe_div(total_debt, assets, require_positive_den=True)
    out["net_debt_to_ebitda"] = _safe_div(total_debt - cash_st, ebitda,
                                          require_positive_den=True)
    out["interest_coverage"] = _safe_div(op_income, g("interest_expense").abs(),
                                         require_positive_den=True)
    # TOTAL LIABILITIES, FROM THE IDENTITY WHERE THE FILER DOES NOT TAG IT.
    #
    # `Liabilities` is a subtotal a great many filers simply omit — they present
    # current and non-current sections and let the reader add them — so it never
    # resolves for 140 of 503 names here, among them Amazon, AbbVie and AMD.
    # That silently removed the fourth term of Altman Z, and a Z-score missing
    # its solvency term is not a slightly different Z-score.
    #
    # Recovered from assets − equity, which is the balance sheet identity
    # rearranged rather than an estimate. Used ONLY as a fallback, and only
    # here: `statements.periods` deliberately keeps returning the concept as
    # filed, because `quality.check_balance_sheet_identity` tests exactly this
    # identity and deriving one side of it there would make the check pass
    # trivially on every one of those 140 names.
    liabilities = g("liabilities")
    liabilities = liabilities.where(liabilities.notna(), assets - equity)
    out["altman_z"] = (
        1.2 * _safe_div(g("assets_current") - g("liabilities_current"), assets)
        + 1.4 * _safe_div(g("retained_earnings"), assets)
        + 3.3 * _safe_div(op_income, assets)
        + 0.6 * _safe_div(mcap, liabilities, require_positive_den=True)
        + 1.0 * _safe_div(revenue, assets)
    )

    # ---- Efficiency ----
    cogs = g("cost_of_revenue")
    out["asset_turnover"] = _safe_div(revenue, assets, require_positive_den=True)
    out["inventory_turnover"] = _safe_div(cogs, g("inventory"),
                                          require_positive_den=True)
    out["dso"] = _safe_div(g("accounts_receivable"), revenue,
                           require_positive_den=True) * 365
    out["dio"] = _safe_div(g("inventory"), cogs, require_positive_den=True) * 365
    out["dpo"] = _safe_div(g("accounts_payable"), cogs, require_positive_den=True) * 365
    out["cash_conversion_cycle"] = out["dso"] + out["dio"] - out["dpo"]
    out["fcf_conversion"] = _safe_div(fcf, net_income, require_positive_den=True)

    # ---- Per share ----
    shares_out = g("shares_outstanding")
    shares_out = shares_out.where(shares_out.notna(), g("shares_diluted"))
    out["revenue_per_share"] = _safe_div(revenue, g("shares_diluted"),
                                         require_positive_den=True)
    out["book_value_per_share"] = _safe_div(equity, shares_out,
                                            require_positive_den=True)
    out["fcf_per_share"] = _safe_div(fcf, g("shares_diluted"),
                                     require_positive_den=True)
    out["cash_per_share"] = _safe_div(cash_st, shares_out, require_positive_den=True)

    # Useful raw aggregates alongside the ratios — these are what the hub
    # shows as headline numbers, and recomputing them elsewhere would risk
    # two different market caps on two different pages.
    out["market_cap"] = mcap
    out["enterprise_value"] = ev
    out["price"] = price
    out["free_cash_flow"] = fcf
    out["ebitda"] = ebitda
    out["total_debt"] = total_debt
    out["net_debt"] = total_debt - cash_st

    return out[[c for c in
                [r.key for r in CATALOGUE] +
                ["market_cap", "enterprise_value", "price", "free_cash_flow",
                 "ebitda", "total_debt", "net_debt"]
                if c in out.columns]]


# The default history window. Named rather than repeated as a literal, because
# `latest()` and `live_valuation()` MUST use the same one — see the note in
# `latest`.
DEFAULT_YEARS = 20


def for_ticker(ticker: str, *, freq: statements.Frequency = "ttm",
               years: int = DEFAULT_YEARS, as_of: str | None = None,
               forward_eps: pd.Series | None = None) -> pd.DataFrame:
    """Ratio history for one ticker.

    Defaults to TTM because that is the correct basis for a valuation
    multiple: a quarterly P/E is four times too high and an annual one is
    stale for up to a year.
    """
    data = statements.periods(ticker, freq=freq, years=years, as_of=as_of)
    if data.empty:
        return pd.DataFrame()
    price = price_at_period_ends(ticker, data.index)
    if forward_eps is None:
        forward_eps = forward_eps_at_period_ends(ticker, data.index, as_of=as_of)
    out = compute(data, price=price, forward_eps=forward_eps)
    out.attrs.update(data.attrs)
    return out


def latest(ticker: str, *, as_of: str | None = None,
           live: bool = False) -> dict:
    """The most recent value of every ratio, as a flat dict.

    What the top of a company page shows. Each value is the newest one that
    exists, per ratio — so one late-arriving line item doesn't blank the whole
    panel.

    **`live=True` re-prices the newest period at the latest cached close.**
    Read the warning on `live_valuation` before using either mode: which one
    is correct depends entirely on the question being asked, and the default
    stays point-in-time so no existing caller changes meaning.
    """
    hist = for_ticker(ticker, as_of=as_of, years=DEFAULT_YEARS)
    if hist.empty:
        return {}

    # "LATEST" HAS TO MEAN RECENT, NOT MERELY NEWEST.
    #
    # This dict is composed column by column, each taking its own last
    # non-missing value — which is what stops one late-arriving line item
    # blanking the whole panel, and is worth keeping. What it did NOT have was
    # any bound on how old that value could be. Assurant last tagged long-term
    # debt in March 2020; six years later `latest()` still reported it as
    # total debt, paired with 2026 cash, and produced a net debt figure that
    # belonged to no date at all.
    #
    # A value more than a year behind the newest reported period is dropped.
    # Missing is missing — that is this module's first rule, and a six-year-old
    # number presented as current breaks it more thoroughly than a gap would.
    positions = {period: i for i, period in enumerate(hist.index)}
    newest = len(hist) - 1
    out: dict = {}
    for col, series in hist.items():
        last = series.last_valid_index()
        if last is None:
            continue
        if newest - positions[last] > LEVEL_CARRY_FORWARD_QUARTERS:
            continue
        out[col] = float(series.loc[last])
    if not live:
        return out

    # SAME WINDOW AS THE HISTORY ABOVE. When the two differed, the dicts they
    # produced were mixed: a column that only exists in older filings appeared
    # in the point-in-time half and was missing from the live half, so
    # Assurant's enterprise value was computed with no debt while the net debt
    # printed beside it came from an older period — market cap plus net debt
    # did not equal enterprise value on the same screen.
    fresh = live_valuation(ticker, as_of=as_of, years=DEFAULT_YEARS)
    if not fresh:
        return out

    # The live figures replace their point-in-time counterparts, and an
    # aggregate the live row could NOT produce is removed rather than left
    # behind. Enterprise value is computed on the live row; leaving a net debt
    # figure from an earlier row beside it means market cap plus net debt does
    # not equal enterprise value on the same screen, which is the kind of
    # contradiction a reader is right to distrust the whole page over.
    for key in LIVE_AGGREGATES:
        if key not in fresh:
            out.pop(key, None)
    out.update(fresh)
    return out


# Ratios whose value depends on the price.
PRICE_DEPENDENT = [
    "pe_ttm", "pe_forward", "peg", "ps", "pb", "p_fcf", "ev_ebitda", "ev_ebit",
    "ev_sales", "earnings_yield", "fcf_yield", "dividend_yield",
    "buyback_yield", "shareholder_yield", "altman_z",
    "market_cap", "enterprise_value", "price",
]

# Aggregates that do NOT depend on the price but that the price-dependent
# figures are built from. They come from the live row too, so the headline
# block is one internally consistent statement rather than a set of numbers
# from different balance sheet dates.
#
# `latest()` composes its dict column by column, taking each one's newest
# non-missing value — which is right for filling gaps and wrong for a set of
# figures that must add up. Ford printed net debt of -$49.7bn beside an
# enterprise value computed from a different quarter's cash balance, so market
# cap plus net debt did not equal enterprise value on the same screen.
LIVE_AGGREGATES = ["total_debt", "net_debt", "free_cash_flow", "ebitda"]


# How many quarters a balance-sheet level may be carried forward before it is
# treated as unknown. Four is one full year: a company that has not restated a
# level in longer than that has usually stopped reporting it, and carrying it
# further would be presenting an old number as a current one.
LEVEL_CARRY_FORWARD_QUARTERS = 4


def _provider_shares_outstanding(ticker: str, *,
                                 as_of: str | None = None) -> float | None:
    """A consolidated share count from the market source, or None if uncached.

    PREFERS THE COUNT IMPLIED BY THE PROVIDER'S OWN MARKET CAP — that is,
    `marketCap / price` — over its `sharesOutstanding` field. For a
    single-class company the two are the same number. For a multi-class one
    they are not, and the implied count is the one that reconciles:
    `sharesOutstanding` is still per-class for several filers (Berkshire,
    Interactive Brokers, Carvana, Blackstone), while `marketCap` is the whole
    company. Dividing gives a share count expressed in units of the traded
    class, which is exactly what multiplying by the traded price needs.

    Bounded by `as_of` like every other snapshot read, so a run dated last
    month cannot pick up a share count captured today.
    """
    horizon = as_of or dt.date.today().isoformat()
    try:
        from . import store

        with store.session(read_only=True) as conn:
            row = conn.execute(
                "SELECT market_cap, price, shares_outstanding "
                "FROM market_snapshot WHERE ticker = ? AND as_of <= ? "
                "ORDER BY as_of DESC LIMIT 1",
                (str(ticker).upper(), horizon),
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None

    cap, price = row["market_cap"], row["price"]
    if cap and price and price > 0:
        implied = float(cap) / float(price)
        if implied > 0:
            return implied
    shares = row["shares_outstanding"]
    return float(shares) if shares and float(shares) > 0 else None


def _carry_forward_levels(data: pd.DataFrame) -> pd.DataFrame:
    """Fill balance-sheet levels forward to the newest period.

    ONLY FOR THE LIVE ROW, and only for levels. A balance-sheet figure is a
    level that persists until the company reports the next one, so "as of
    today" for total debt means "the last total debt they reported". A flow —
    revenue, cash from operations — is nothing of the kind and is never
    carried.

    The bug this fixes was a contradiction on screen rather than a merely
    stale number. General Motors tags its combined debt line annually, so the
    newest quarterly row carried no debt at all; enterprise value computed
    from that row came out BELOW market cap, printed directly beside a net
    debt figure of +$110bn that `latest()` had taken from the last row where
    it existed. Two panels, two different balance sheet dates, one impossible
    pair of numbers.
    """
    levels = [c for c in data.columns if statements.is_level(c)]
    if not levels:
        return data
    out = data.copy()
    out[levels] = out[levels].ffill(limit=LEVEL_CARRY_FORWARD_QUARTERS)
    return out


def live_valuation(ticker: str, *, as_of: str | None = None,
                   years: int = DEFAULT_YEARS) -> dict:
    """The newest reported period, valued at the latest cached close.

    WHY THIS EXISTS, AND WHY IT IS NOT THE DEFAULT.

    `for_ticker` prices every period at that period's own close, which is the
    only honest basis for a HISTORY: pairing today's price with a 2019 balance
    sheet plots a chart of earnings with the axis mislabelled as P/E.

    But it makes the *newest* row answer a question nobody asked. "Microsoft's
    P/E" does not mean "Microsoft's market cap on 30 June divided by trailing
    earnings" — it means the multiple you would pay now. The hub was showing
    the former under the latter's label, and by mid-August that header price
    was $373 against a $509 close: a 27% error propagated into market cap,
    enterprise value, and every multiple derived from them.

    So: take the latest fundamentals row exactly as filed, substitute the
    latest close for the period-end close, and recompute. Same `compute()`,
    same formulas — a second implementation of P/E is how two pages end up
    disagreeing about one company.

    Returns `{}` when there is no cached price or no fundamentals, so a caller
    can always fall back to the point-in-time figure rather than showing a gap.
    Includes `price_as_of` (the bar's date) and `fundamentals_as_of` (the
    period end), because a live price against stale fundamentals is a real
    condition that the reader has to be able to see.
    """
    data = statements.periods(ticker, freq="ttm", years=years, as_of=as_of)
    if data.empty:
        return {}
    data = _carry_forward_levels(data)

    from .sources import prices as price_src

    quote = price_src.last_quote(ticker)
    if not quote or not quote.get("price"):
        return {}

    # A MULTI-CLASS FILER'S FILED SHARE COUNT IS ONE CLASS, NOT THE COMPANY.
    #
    # `CommonStockSharesOutstanding` arrives undimensioned from the SEC's
    # companyfacts feed, and for a company with several share classes that
    # figure covers whichever class the filer tagged without a member — often
    # the smaller one. Multiplied by the traded price it produces a market cap
    # that is not wrong at the margin, it is wrong by multiples:
    #
    #     Visa         $41bn   against $673bn
    #     Berkshire    $0.2bn  against $1,150bn
    #     Carvana      $2.7bn  against $108bn
    #     CrowdStrike  $57bn   against $230bn
    #
    # and with it P/E, P/S, P/B, EV and every screen that sorts or filters on
    # size. The market data provider's share count is consolidated across
    # classes and current, which is exactly what a LIVE market cap wants, so
    # it is preferred here — and only here. The point-in-time history keeps
    # using the filed count, because that is the number that was knowable on
    # each historical date.
    #
    # This is a stated preference, not a silent one: `shares_source` records
    # which count produced the figure, and the filed count is still returned
    # beside it.
    shares_now = _provider_shares_outstanding(ticker, as_of=as_of)
    shares_filed = _get(data, "shares_outstanding").dropna()
    shares_filed = float(shares_filed.iloc[-1]) if len(shares_filed) else None
    if shares_now:
        data = data.copy()
        data.loc[data.index[-1], "shares_outstanding"] = shares_now

    # Only the FINAL period's price is replaced. Several ratios need history
    # to compute at all — PEG divides by four-quarter earnings growth — so the
    # earlier rows keep their own period-end close and stay point-in-time. The
    # alternative, re-pricing a one-row frame, silently returns NaN for every
    # ratio with a lookback and leaves the stale value in place beside the
    # fresh ones.
    price = price_at_period_ends(ticker, data.index)
    # `data` carries the share count the company REPORTED, which is a
    # post-split figure in a current filing, and `last_quote` reads the
    # provider's split-adjusted series. Both are on the same side of any split
    # that has already happened, so no as-traded conversion belongs here —
    # unlike `price_at_period_ends`, which reaches back across splits and
    # therefore must undo them.
    price.iloc[-1] = float(quote["price"])

    forward = forward_eps_at_period_ends(ticker, data.index, as_of=as_of)
    computed = compute(data, price=price, forward_eps=forward)
    if computed.empty:
        return {}

    row = computed.iloc[-1]
    out = {
        key: float(row[key])
        for key in PRICE_DEPENDENT + LIVE_AGGREGATES
        if key in computed.columns and pd.notna(row[key])
    }
    if not any(key in out for key in PRICE_DEPENDENT):
        return {}
    out["price_as_of"] = quote["as_of"]
    out["fundamentals_as_of"] = str(data.index[-1])
    out["shares_source"] = "market data provider" if shares_now else "as filed"
    if shares_now:
        out["shares_outstanding"] = shares_now
        if shares_filed:
            out["shares_outstanding_filed"] = shares_filed
    elif shares_filed:
        out["shares_outstanding"] = shares_filed
    for key in ("previous_close", "change", "change_pct"):
        if quote.get(key) is not None:
            out[key] = quote[key]
    return out
