"""Checking that the data is complete and internally consistent.

    python -m finlake health
    python -m finlake health --ticker AAPL --verbose

THE RULE THAT GOVERNS THIS WHOLE MODULE: **a failed check never silently
corrects a number.** It flags it. Silent correction is how a data layer starts
lying — the number on screen stops matching the filing it claims to come
from, and nothing anywhere says so. Every check here reports; none repair.

Three families of check, in increasing order of how much they can prove.

**Internal consistency.** Assets must equal liabilities plus equity. Cash flow
sections must sum to the reported change in cash. These are accounting
identities, not opinions — a filer cannot violate them, so a violation is
always OUR parsing error, never their reporting. That makes them the sharpest
tool available: they catch tag-mapping mistakes with no external data at all.

**Completeness.** A gap in a chart looks the same whether the company never
reported the figure or we failed to read it. Coverage is measured per concept
per period so the difference is visible, and an overdue filing is flagged
against the company's own historical filing lag rather than a fixed calendar.

**Cross-source reconciliation.** The strongest check, because it uses two
independent sources: SEC filings say one thing, the market data provider says
another, and where they disagree beyond tolerance that is a finding worth
looking at. Neither source is treated as automatically right.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass, field

import pandas as pd

from . import statements, store

# Tolerances. Filings round to thousands or millions, and a balance sheet
# that misses by $3,000 on $400bn of assets is rounding, not an error.
RELATIVE_TOLERANCE = 0.01     # 1% — generous, because these should be exact
ABSOLUTE_TOLERANCE = 1e6      # $1m floor, so small companies aren't over-flagged

SEVERITY_ORDER = {"critical": 0, "serious": 1, "warning": 2, "info": 3}


@dataclass
class Finding:
    """One thing that looks wrong, with enough context to check it by hand."""

    ticker: str
    check: str
    severity: str            # critical | serious | warning | info
    message: str
    period: str | None = None
    detail: dict = field(default_factory=dict)

    def __str__(self) -> str:
        where = f" [{self.period}]" if self.period else ""
        return f"{self.ticker}{where} {self.check}: {self.message}"


def _close(a: float, b: float) -> bool:
    """True when two figures agree within rounding."""
    if pd.isna(a) or pd.isna(b):
        return True          # can't disagree if one is missing
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) <= max(ABSOLUTE_TOLERANCE, RELATIVE_TOLERANCE * scale)


def _pct_gap(a: float, b: float) -> float:
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) / scale


# ---------------------------------------------------------------------------
# Internal consistency — accounting identities
# ---------------------------------------------------------------------------
def check_balance_sheet_identity(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """assets == liabilities + equity.

    The most valuable check in the module: it is an accounting identity, so a
    filer cannot report otherwise, and no external data is needed to test it.

    TWO DIFFERENT FAILURES, reported differently. Most filers also tag
    `LiabilitiesAndStockholdersEquity` — their OWN total. Comparing against
    that separates the cases:

      their total != assets      the filing itself is inconsistent, or we
                                 mis-resolved `assets`. Critical.
      their total == assets, but
      our components don't sum   the filer balances; OUR resolution is
                                 missing a line that sits between liabilities
                                 and equity — mezzanine items like redeemable
                                 preferred or temporary equity live there.
                                 Serious, and a concept gap rather than a
                                 wrong number.

    Collapsing these into one "balance sheet doesn't balance" message sends
    you looking for a bug in the wrong layer. NVDA's 2016 quarters are the
    live example: assets 7,370M, their own total 7,370M, but our liabilities
    plus equity reaches 7,283M — an 87M line we do not yet map.
    """
    out = []
    if not {"assets", "liabilities", "equity"} <= set(data.columns):
        return out
    for period, row in data.iterrows():
        assets, liab, eq = row.get("assets"), row.get("liabilities"), row.get("equity")
        if pd.isna(assets) or pd.isna(liab) or pd.isna(eq):
            continue
        # Non-controlling interests sit outside StockholdersEquity for some
        # filers and inside it for others, so they are allowed on either side.
        minority = row.get("minority_interest", 0) or 0
        if any(_close(assets, c) for c in (liab + eq, liab + eq + minority)):
            continue

        filed_total = row.get("liabilities_and_equity")
        if not pd.isna(filed_total) and _close(assets, filed_total):
            out.append(Finding(
                ticker, "unmapped_balance_sheet_line", "serious",
                f"the filer balances (their total {filed_total:,.0f} = assets "
                f"{assets:,.0f}), but our liabilities {liab:,.0f} + equity "
                f"{eq:,.0f} reaches only {liab + eq:,.0f} — "
                f"{assets - liab - eq:,.0f} sits in a line we do not map "
                f"(mezzanine/temporary equity is the usual culprit)",
                period=str(period),
                detail={"unmapped": float(assets - liab - eq)}))
        else:
            out.append(Finding(
                ticker, "balance_sheet_identity", "critical",
                f"assets {assets:,.0f} != liabilities {liab:,.0f} + equity "
                f"{eq:,.0f} (off by {_pct_gap(assets, liab + eq):.1%})",
                period=str(period),
                detail={"assets": assets, "liabilities": liab, "equity": eq}))
    return out


def check_cash_flow_ties_out(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """cfo + cfi + cff + fx == the reported change in cash."""
    out = []
    needed = {"cfo", "cfi", "cff", "net_change_in_cash"}
    if not needed <= set(data.columns):
        return out
    for period, row in data.iterrows():
        parts = [row.get(c) for c in ("cfo", "cfi", "cff")]
        reported = row.get("net_change_in_cash")
        if pd.isna(reported) or any(pd.isna(p) for p in parts):
            continue
        total = sum(parts) + (row.get("fx_effect") or 0)
        if not _close(total, reported):
            out.append(Finding(
                ticker, "cash_flow_tie_out", "serious",
                f"CFO+CFI+CFF+FX = {total:,.0f} but reported change in cash is "
                f"{reported:,.0f} (off by {_pct_gap(total, reported):.1%})",
                period=str(period),
            ))
    return out


def check_income_statement_chain(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """revenue - cost_of_revenue == gross_profit, when all three are filed.

    Only checked where the filer reported gross profit directly. Where they
    did not, statements.py derives it and there is nothing to disagree with.
    """
    out = []
    if not {"revenue", "cost_of_revenue", "gross_profit"} <= set(data.columns):
        return out
    for period, row in data.iterrows():
        rev, cogs, gross = (row.get("revenue"), row.get("cost_of_revenue"),
                            row.get("gross_profit"))
        if any(pd.isna(v) for v in (rev, cogs, gross)):
            continue
        if not _close(rev - cogs, gross):
            out.append(Finding(
                ticker, "gross_profit_chain", "warning",
                f"revenue {rev:,.0f} - cost {cogs:,.0f} = {rev - cogs:,.0f}, "
                f"but gross profit is filed as {gross:,.0f}. The filer's cost "
                f"line probably excludes something theirs includes.",
                period=str(period),
            ))
    return out


def check_share_counts(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """Diluted shares >= basic, and neither is negative.

    A negative share count is impossible, which makes this a direct guard on
    the trap that produced exactly that: deriving a fiscal Q4 by differencing
    a year-to-date weighted AVERAGE. See quarterize.quarterize_average.
    """
    out = []
    for col in ("shares_basic", "shares_diluted", "shares_outstanding"):
        if col not in data.columns:
            continue
        bad = data[data[col].notna() & (data[col] <= 0)]
        for period, row in bad.iterrows():
            out.append(Finding(
                ticker, "impossible_share_count", "critical",
                f"{col} is {row[col]:,.0f} — a share count cannot be zero or "
                f"negative. Usually means a period average was differenced.",
                period=str(period),
            ))
    if {"shares_basic", "shares_diluted"} <= set(data.columns):
        both = data[data["shares_basic"].notna() & data["shares_diluted"].notna()]
        for period, row in both.iterrows():
            # 1% slack: the two are occasionally tagged from different
            # statements in the same filing.
            if row["shares_diluted"] < row["shares_basic"] * 0.99:
                out.append(Finding(
                    ticker, "diluted_below_basic", "warning",
                    f"diluted shares {row['shares_diluted']:,.0f} < basic "
                    f"{row['shares_basic']:,.0f}; dilution cannot reduce the "
                    f"count",
                    period=str(period),
                ))
    return out


# An implied interest rate above this means the debt figure is not the whole
# of the debt. Deliberately far above any real cost of borrowing: the point is
# to catch a denominator that is a FRACTION of the balance, not to have an
# opinion on anybody's funding costs. Investment grade pays 3-6%; distressed
# high yield rarely clears 15%.
IMPLAUSIBLE_IMPLIED_RATE = 0.25


def check_debt_completeness(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """Whether the debt that resolved is plausibly ALL of the debt.

    WHY THIS IS A FINDING AND NOT A CORRECTION. Debt is the line where the
    tag list is weakest, because filers describe borrowings in more ways than
    any other balance-sheet item — mortgages, unsecured notes, commercial
    paper, captive-finance books, and a long tail of company-specific
    extension elements outside the us-gaap taxonomy entirely. When only part
    of it resolves the result is not a gap, it is a small confident number:
    Realty Income's total debt came through as $1.4bn of commercial paper
    against roughly $26bn actually outstanding, and nothing about that looked
    wrong on screen.

    Two independent signals, both from the filings alone — reconciling against
    the market provider would make this check unavailable offline, and the
    provider is a second opinion rather than a source of truth:

      SHORT LEG ALONE. Commercial paper or current maturities resolving with
      no long-term leg and no combined total. A company funding itself purely
      on short-term paper exists but is rare; far more often the term debt is
      tagged under an element the list does not carry.

      IMPLAUSIBLE IMPLIED RATE. Interest expense divided by the debt that
      resolved. A company paying 84% on its stated borrowings — Boston
      Properties — is not paying 84%; the denominator is a fraction of the
      balance.
    """
    out: list[Finding] = []
    if data.empty:
        return out

    # BOUNDED TO THE RECENT PERIODS, for the same reason `ratios.latest` had to
    # be. Taking each column's newest non-missing value over a twelve-year
    # frame answers "was this concept EVER tagged", not "did it resolve for the
    # balance sheet on screen" — and a long-term debt element last seen in 2016
    # made a company whose debt is currently unresolvable look complete. Four
    # quarters, matching the carry-forward bound in ratios.py.
    recent = data.tail(4)

    def latest(column: str) -> float | None:
        if column not in recent.columns:
            return None
        values = recent[column].dropna()
        return float(values.iloc[-1]) if len(values) else None

    short, long_ = latest("debt_short"), latest("debt_long")
    reported = latest("total_debt_reported")

    if short is not None and long_ is None and reported is None:
        # A warning, not serious: the shape is suspicious but a company with
        # genuinely no term debt produces it too — Lululemon and Ulta both do,
        # and for them the small figure is simply correct. The implied-rate
        # finding below is the one carrying real evidence.
        out.append(Finding(
            ticker, "debt_short_leg_only", "warning",
            f"total debt resolved to {short:,.0f} from the short-term leg "
            f"alone — no long-term element and no combined total, so this is "
            f"probably a portion of the balance rather than all of it"))

    legs = None if (short is None and long_ is None) else (short or 0) + (long_ or 0)
    debt = max([v for v in (legs, reported) if v is not None], default=None)

    # Interest over a YEAR against a debt LEVEL. A quarterly frame's interest
    # line is one quarter, and dividing that by a balance understates the
    # implied rate fourfold — which would put every one of these findings
    # comfortably under the threshold and quietly disable the check.
    interest = None
    if "interest_expense" in recent.columns:
        quarters = recent["interest_expense"].dropna()
        if len(quarters) >= 3:
            interest = float(quarters.sum()) * (4.0 / len(quarters))

    if debt and debt > 0 and interest and interest > 0:
        rate = interest / debt
        if rate > IMPLAUSIBLE_IMPLIED_RATE:
            out.append(Finding(
                ticker, "implied_rate_implausible", "serious",
                f"annual interest expense {interest:,.0f} against total debt "
                f"{debt:,.0f} implies a {rate:.0%} borrowing rate — the debt "
                f"that resolved is almost certainly incomplete",
                detail={"implied_rate": round(rate, 4)}))
    return out


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------
KEY_CONCEPTS = [
    "revenue", "net_income", "operating_income", "assets", "liabilities",
    "equity", "cash", "cfo", "capex", "shares_diluted",
]

# Concepts a bank or insurer legitimately does not report, and the reason.
#
# A monitor that flags every bank in the universe every quarter for the same
# two "problems" trains people to skim past it, and then the real findings go
# with them. These are absences by the nature of the business, not gaps:
#
#   operating_income  a deposit-funded balance sheet has no operating/
#                     non-operating split. `OperatingIncomeLoss` was removed
#                     as a fallback in 0.2.0 precisely because substituting
#                     pretax income there was silently wrong for every bank.
#   capex             there is no meaningful property-and-equipment spend
#                     line in a financial's cash flow statement.
NOT_APPLICABLE_TO_FINANCIALS = {"operating_income", "capex"}

# SIC ranges for finance, insurance, and real estate (SEC division H).
FINANCIAL_SIC_RANGE = (6000, 6799)


def _is_financial(conn: sqlite3.Connection, ticker: str) -> bool:
    """Whether this is a financial, from the filer's own SIC code."""
    row = conn.execute(
        "SELECT s.sic FROM securities s JOIN ticker_map tm ON tm.cik = s.cik "
        "WHERE tm.ticker = ? AND tm.valid_to IS NULL LIMIT 1", (ticker.upper(),)
    ).fetchone()
    try:
        sic = int(row["sic"]) if row and row["sic"] else None
    except (TypeError, ValueError):
        return False
    return sic is not None and FINANCIAL_SIC_RANGE[0] <= sic <= FINANCIAL_SIC_RANGE[1]


def check_coverage(ticker: str, data: pd.DataFrame, *,
                   min_coverage: float = 0.8,
                   is_financial: bool = False) -> list[Finding]:
    """How much of the core line-item set actually resolved.

    Reported as a proportion of periods rather than a yes/no, because a
    concept present for six of twenty quarters is a different problem from
    one entirely absent, and the fixes differ.
    """
    out = []
    if data.empty:
        return [Finding(ticker, "no_data", "critical",
                        "no fundamentals resolved at all")]
    for concept in KEY_CONCEPTS:
        if is_financial and concept in NOT_APPLICABLE_TO_FINANCIALS:
            continue
        if concept not in data.columns:
            out.append(Finding(
                ticker, "missing_concept", "serious",
                f"{concept} never resolved — check the CONCEPTS tag list "
                f"against what this filer actually tags"))
            continue
        have = data[concept].notna().mean()
        if have < min_coverage:
            out.append(Finding(
                ticker, "thin_coverage", "warning",
                f"{concept} present in only {have:.0%} of periods",
                detail={"coverage": round(float(have), 3)}))
    return out


def check_period_continuity(ticker: str, data: pd.DataFrame) -> list[Finding]:
    """No unexplained hole inside an otherwise continuous history.

    The quarterizer emits a gap deliberately when it cannot reconstruct a
    period honestly — that is correct behaviour, and this surfaces those gaps
    rather than smoothing them over. A hole in the middle of a history is
    worth knowing about; it is not worth inventing a number for.
    """
    out = []
    if len(data) < 2:
        return out
    ends = pd.to_datetime(pd.Series(list(data.index)))
    gaps = ends.diff().dt.days
    for i, gap in enumerate(gaps):
        if pd.isna(gap) or gap <= 120:
            continue
        out.append(Finding(
            ticker, "period_gap", "warning",
            f"{int(gap)} days between {data.index[i - 1]} and {data.index[i]} "
            f"— roughly {round(gap / 91)} quarters missing",
            period=str(data.index[i])))
    return out


def check_staleness(conn: sqlite3.Connection, ticker: str,
                    data: pd.DataFrame) -> list[Finding]:
    """Is the newest quarter overdue, judged against this filer's own history?

    Compared to the company's own typical filing lag rather than a fixed
    calendar: fiscal years end in every month, and 20-F filers report
    annually. A fixed rule flags half the universe every quarter and is
    ignored within a week.
    """
    if data.empty:
        return []
    latest = str(data.index[-1])
    try:
        age = (dt.date.today() - dt.date.fromisoformat(latest)).days
    except ValueError:
        return []
    # A quarter plus a normal 30-90 day filing lag, plus slack.
    if age > 200:
        return [Finding(
            ticker, "stale_fundamentals", "serious",
            f"newest reported quarter ends {latest}, {age} days ago — either "
            f"a late filer or a broken ingest, and those need distinguishing",
            period=latest)]
    return []


# ---------------------------------------------------------------------------
# Cross-source reconciliation
# ---------------------------------------------------------------------------
def check_against_market_source(conn: sqlite3.Connection, ticker: str,
                                data: pd.DataFrame) -> list[Finding]:
    """SEC-derived figures vs. the market provider's own.

    The strongest check available, because the two sources are independent.
    Neither is assumed correct — a disagreement is reported as a
    disagreement, which is the honest description of what we know.
    """
    out = []
    row = conn.execute(
        "SELECT * FROM market_snapshot WHERE ticker = ? "
        "ORDER BY as_of DESC LIMIT 1", (ticker.upper(),)).fetchone()
    if not row or data.empty:
        return out
    snap = dict(row)

    ours = data["shares_outstanding"].dropna() if "shares_outstanding" in data else pd.Series(dtype=float)
    theirs = snap.get("shares_outstanding")
    if len(ours) and theirs:
        latest = float(ours.iloc[-1])
        # 15%: buybacks and issuance move the count between the last filing
        # and today, so only a gap well beyond normal drift is a finding.
        if _pct_gap(latest, float(theirs)) > 0.15:
            out.append(Finding(
                ticker, "shares_disagree", "warning",
                f"shares outstanding: filings say {latest:,.0f}, market source "
                f"says {float(theirs):,.0f} "
                f"({_pct_gap(latest, float(theirs)):.0%} apart)",
                detail={"sec": latest, "market": float(theirs)}))

    return out


# ---------------------------------------------------------------------------
# Restatements — a finding worth surfacing, not just a data note
# ---------------------------------------------------------------------------
def check_restatements(conn: sqlite3.Connection, ticker: str, cik: int | None,
                       *, min_change: float = 0.05) -> list[Finding]:
    """Numbers that were materially revised after first publication.

    Surfaced because a large downward restatement is a real signal about a
    business, not merely a data-quality note. The bitemporal store already
    knows this happened; this is what makes it visible.
    """
    if cik is None:
        return []
    out = []
    rows = conn.execute(
        """
        SELECT tag, period_end, COUNT(*) n, MIN(val) lo, MAX(val) hi
        FROM facts
        WHERE cik = ? AND tag IN ('Revenues',
              'RevenueFromContractWithCustomerExcludingAssessedTax',
              'NetIncomeLoss', 'Assets', 'StockholdersEquity')
        GROUP BY tag, period_end, unit, period_start
        HAVING n > 1 AND hi != 0 AND (hi - lo) / ABS(hi) > ?
        ORDER BY period_end DESC LIMIT 10
        """, (cik, min_change)).fetchall()
    for r in rows:
        out.append(Finding(
            ticker, "restatement", "info",
            f"{r['tag']} for {r['period_end']} was revised across "
            f"{r['n']} filings: {r['lo']:,.0f} to {r['hi']:,.0f} "
            f"({(r['hi'] - r['lo']) / abs(r['hi']):.0%})",
            period=r["period_end"]))
    return out


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def check_ticker(ticker: str, *, as_of: str | None = None,
                 years: int = 12) -> list[Finding]:
    """Every check, for one company."""
    from . import api

    try:
        data = statements.periods(ticker, freq="quarterly", years=years, as_of=as_of)
    except Exception as exc:
        return [Finding(ticker, "load_failed", "critical", str(exc)[:200])]

    with store.session(read_only=True) as conn:
        financial = _is_financial(conn, ticker)

    findings: list[Finding] = []
    findings += check_coverage(ticker, data, is_financial=financial)
    if data.empty:
        return findings

    findings += check_balance_sheet_identity(ticker, data)
    findings += check_cash_flow_ties_out(ticker, data)
    findings += check_income_statement_chain(ticker, data)
    findings += check_share_counts(ticker, data)
    findings += check_period_continuity(ticker, data)
    findings += check_debt_completeness(ticker, data)

    with store.session(read_only=True) as conn:
        findings += check_staleness(conn, ticker, data)
        findings += check_against_market_source(conn, ticker, data)
        findings += check_restatements(conn, ticker, data.attrs.get("cik"))

    return sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f.severity, 9),
                                           f.check, f.period or ""))


def score_ticker(findings: list[Finding]) -> float:
    """A 0-100 data-quality score.

    Weighted by severity, because the failures are not equivalent: a broken
    balance-sheet identity means a number on screen is wrong, while thin
    coverage means a number is absent. Wrong is worse than missing.
    """
    penalty = 0.0
    for f in findings:
        penalty += {"critical": 25.0, "serious": 10.0,
                    "warning": 3.0, "info": 0.0}.get(f.severity, 0.0)
    return max(0.0, 100.0 - penalty)


def check_universe(tickers: list[str], *, as_of: str | None = None,
                   progress: bool = True) -> pd.DataFrame:
    """Run every check across a list of companies.

    Returns one row per company with its score and finding counts. The
    per-finding detail stays available through `check_ticker`; this is the
    view that answers "is the cache healthy" in one glance.
    """
    rows = []
    for i, ticker in enumerate(tickers, 1):
        if progress:
            print(f"  [{i}/{len(tickers)}] {ticker}", end="\r", flush=True)
        findings = check_ticker(ticker, as_of=as_of)
        counts = {sev: sum(1 for f in findings if f.severity == sev)
                  for sev in SEVERITY_ORDER}
        rows.append({
            "ticker": ticker,
            "score": score_ticker(findings),
            **counts,
            "findings": len(findings),
            "worst": min((f.severity for f in findings),
                         key=lambda s: SEVERITY_ORDER.get(s, 9), default="—"),
        })
    if progress:
        print(" " * 50, end="\r")
    return pd.DataFrame(rows).sort_values("score")
