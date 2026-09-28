"""Turning XBRL into clean quarters.

This is where the "missing quarters" problem in your spec actually lives, and
it is nastier than it sounds. Three separate traps:

TRAP 1 — Q4 is never filed.
    There is no Q4 10-Q. The 10-K reports the full year. So Q4 must be derived:
        Q4 = FY - 9M   (for flow items)
    For balance-sheet items no derivation is needed: the fiscal-year-end
    instant IS the Q4 balance.

TRAP 2 — many filers tag YTD, not discrete quarters.
    A Q3 10-Q may contain Revenues for a 273-day period (9 months) and nothing
    for the discrete 92-day quarter. You have to difference:
        Q2 = H1 - Q1,  Q3 = 9M - H1,  Q4 = FY - 9M
    Filers are inconsistent about this both across companies and across years
    within one company, so you cannot pick a strategy per company. You have to
    look at what's actually there each year.

TRAP 3 — flow vs instant.
    Never difference a balance-sheet item. Assets at Q3-end minus Assets at
    Q2-end is not "Q3 assets", it's a change in assets. XBRL distinguishes
    these: instant facts have no `start`. Detect it from the data; do not
    hardcode a tag list, because filers use tags you didn't anticipate.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field

# Day-count windows for classifying a duration fact. Fiscal calendars drift
# (52/53-week retailers especially), so these are deliberately loose.
# Windows must cover BOTH calendar quarters and 52/53-week fiscal calendars.
#
# Most retailers run 13/13/13/13 weeks -> quarters of ~91 days.
# PepsiCo and Costco (among others) run 12/12/12/16 -> Q1-Q3 are 84 days and
# Q4 is 112 (or 119 in a 53-week year). Their nine-month YTD is 252 days, not
# 273. Windows tuned only to calendar quarters silently drop both the 9M link
# and Q4 for every one of these filers.
DURATION_BUCKETS = {
    "Q": (75, 120),      # 12wk (84) .. 17wk (119) .. calendar (92)
    "H": (160, 200),     # 24wk (168) .. calendar (183)
    "9M": (245, 290),    # 36wk (252) .. calendar (273)
    "FY": (345, 385),    # 52wk (364) .. 53wk (371) .. calendar (365)
}
# A single quarter, by implied span. Anything longer is a multi-quarter lump.
QUARTER_SPAN = (75, 120)



@dataclass
class Quarter:
    """One quarterly observation, with its provenance attached."""

    period_end: str
    value: float
    fy: int | None = None
    fq: int | None = None            # 1..4, fiscal not calendar
    derived: bool = False            # True => computed, not reported directly
    span_days: int | None = None     # implied length of the quarter emitted
    source_accns: list[str] = field(default_factory=list)
    filed: str | None = None         # latest filing date among sources -> PIT key


def _days(start: str, end: str) -> int:
    return (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days


def classify_duration(start: str | None, end: str) -> str | None:
    """'Q' | 'H' | '9M' | 'FY' | 'INSTANT' | None (odd stub period)."""
    if start is None:
        return "INSTANT"
    n = _days(start, end)
    for label, (lo, hi) in DURATION_BUCKETS.items():
        if lo <= n <= hi:
            return label
    return None


def quarterize_multi(
    rows: list[dict], *, non_negative: bool = False
) -> list[Quarter]:
    """Convert resolved facts for ONE tag into discrete quarters.

    `non_negative`, when True, drops any DERIVED (differenced) quarter whose
    value is negative before returning. Default False preserves prior
    behaviour exactly for every existing caller.

    This exists because of a real bug (see
    ISSUE-001-wdc-divestiture-restatement in finlake's vault): a divestiture
    or discontinued-operation restatement can pair a post-restatement
    full-year figure against a pre-restatement nine-month figure within one
    YTD chain, so `FY - 9M` goes negative even though the underlying quantity
    (e.g. revenue) can never actually be negative. That is always a scope
    mismatch, not real data — the honest fix is to drop the quarter and leave
    a gap, the same way a genuinely missing quarter is already handled.

    Only pass `non_negative=True` for concepts that are economically
    guaranteed non-negative (revenue, not net income — see
    `api.NON_NEGATIVE_CONCEPTS`). A derived net-income or CFO quarter *can*
    legitimately be negative; dropping a real loss would be a worse bug than
    the one this parameter fixes.

    Use `implausible_quarters()` to see exactly what this would drop, for
    reporting rather than for use.

    GROUPING KEY: `period_start`, NOT the XBRL `fy` field.

    This is the correction that matters. In SEC XBRL, `fy` is the fiscal year
    of the FILING a fact appeared in, not of the period it describes. Because
    every 10-Q republishes the prior year's comparatives, a nine-month figure
    re-shown in next year's filing carries the LATER fy. Group on `fy` and the
    nine-month figure and the full-year figure for one fiscal year land in
    different buckets, so `FY - 9M` never happens and Q4 comes out blank — or
    worse, gets differenced against a neighbouring year and goes negative.

    `period_start` has no such problem. Every year-to-date figure within one
    fiscal year shares the same start date by construction, so each group of
    facts sharing a start IS exactly one fiscal year's cumulative chain. It
    also handles non-calendar fiscal years for free.

    `rows` must already be point-in-time resolved (one row per period) — see
    pit.as_of_facts. Feeding raw multi-vintage facts here produces garbage.
    """
    qs = _quarterize_chains(rows)
    if not non_negative:
        return qs
    return [q for q in qs if not (q.derived and q.value < 0)]


def implausible_quarters(rows: list[dict]) -> list[Quarter]:
    """The derived, negative quarters that `quarterize_multi(non_negative=True)`
    would drop — for reporting what was excluded and why, not for use in a
    calculation. Empty for any tag where no scope mismatch occurred.
    """
    qs = _quarterize_chains(rows)
    return [q for q in qs if q.derived and q.value < 0]


def _quarterize_chains(rows: list[dict]) -> list[Quarter]:
    """All quarters implied by `rows`, with no plausibility filtering."""
    if not rows:
        return []

    instants = [r for r in rows if r.get("period_start") is None]
    durations = [r for r in rows if r.get("period_start") is not None]

    # Instant (balance sheet) facts are already quarter-end snapshots.
    # Never difference them: that would turn a level into a change.
    if instants and not durations:
        return _dedupe_by_period([
            Quarter(period_end=r["period_end"], value=float(r["val"]),
                    fy=r.get("fy"), fq=_fp_to_fq(r.get("fp")), derived=False,
                    source_accns=[r["accn"]], filed=r.get("filed"))
            for r in instants
        ])

    # Build one cumulative chain per fiscal year, keyed by start date.
    chains: dict[str, dict[str, dict]] = {}
    for r in durations:
        if classify_duration(r["period_start"], r["period_end"]) is None:
            continue  # stub period from a fiscal-year change; not usable
        group = chains.setdefault(r["period_start"], {})
        prev = group.get(r["period_end"])
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            group[r["period_end"]] = r

    out: list[Quarter] = []
    for start, by_end in chains.items():
        facts = sorted(by_end.values(), key=lambda x: x["period_end"])
        prev_cum, prev_end = 0.0, start
        accns: list[str] = []
        prev_filed = ""

        for i, f in enumerate(facts):
            cum = float(f["val"])
            q_val = cum - prev_cum
            span = _days(prev_end, f["period_end"])
            filed = max(prev_filed, f.get("filed") or "")

            # Only emit when the implied span really is one quarter. If a
            # filer skipped a YTD step, the difference covers two quarters —
            # emitting that as "a quarter" would silently corrupt every margin
            # and growth rate downstream. A gap is the honest answer.
            if QUARTER_SPAN[0] <= span <= QUARTER_SPAN[1]:
                out.append(Quarter(
                    period_end=f["period_end"], value=q_val, fy=f.get("fy"),
                    fq=None, derived=i > 0, span_days=span,
                    source_accns=accns + [f["accn"]], filed=filed or None,
                ))

            prev_cum, prev_end = cum, f["period_end"]
            accns = accns + [f["accn"]]
            prev_filed = filed

    return _dedupe_by_period(out)


# Kept as an alias: quarterize() and quarterize_multi() are now the same path.
quarterize = quarterize_multi


def quarterize_average(rows: list[dict]) -> list[Quarter]:
    """Quarters for a concept that is an AVERAGE over its period, not a sum.

    TRAP 5 — never difference an average.

    The README names three traps; this is a fourth, and it is the same shape
    as "never difference a balance sheet" wearing a disguise. Weighted-average
    diluted shares IS a duration fact — it has a period_start, so every
    flow-vs-instant check passes it through — but it is an average over that
    period, not a quantity accumulated across it. Differencing the year-to-date
    chain computes `FY - 9M`, which for a company whose share count barely
    moves is the difference between two nearly identical numbers.

    Live example: Apple's fiscal Q4 2024 diluted share count came out as
    **negative 55 million shares**. It then flowed into the annual rollup and
    dragged the reported share count 25% below the truth, which in turn made
    every per-share figure derived from it about a third too high — all of them
    printing as ordinary, plausible numbers.

    What this does instead:

      * A fact whose span is already one quarter is taken as reported.
      * A quarter covered only by a year-to-date figure is recovered with the
        averaging identity rather than by subtraction. If YTD_k is the average
        over k quarters then the total across them is k * YTD_k, so

            Q_k = k * YTD_k - (k-1) * YTD_(k-1)

        which is exact when quarters carry equal weight and a close
        approximation otherwise. This is what recovers the fiscal Q4 that no
        10-Q ever reports.

    Anything that cannot be recovered is left as a gap, as everywhere else.
    """
    if not rows:
        return []

    instants = [r for r in rows if r.get("period_start") is None]
    durations = [r for r in rows if r.get("period_start") is not None]
    if instants and not durations:
        return _quarterize_chains(rows)  # not really an average; pass through

    chains: dict[str, dict[str, dict]] = {}
    for r in durations:
        if classify_duration(r["period_start"], r["period_end"]) is None:
            continue
        group = chains.setdefault(r["period_start"], {})
        prev = group.get(r["period_end"])
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            group[r["period_end"]] = r

    out: list[Quarter] = []
    for start, by_end in chains.items():
        facts = sorted(by_end.values(), key=lambda x: x["period_end"])
        prev_total, prev_end, prev_k = 0.0, start, 0

        for f in facts:
            span_from_start = _days(start, f["period_end"])
            # How many quarters this year-to-date figure spans. Rounded from
            # the day count so 52/53-week fiscal calendars land correctly.
            k = max(1, round(span_from_start / 91.3))
            total = float(f["val"]) * k
            q_span = _days(prev_end, f["period_end"])

            if QUARTER_SPAN[0] <= q_span <= QUARTER_SPAN[1] and k > prev_k:
                q_val = total - prev_total
                out.append(Quarter(
                    period_end=f["period_end"], value=q_val, fy=f.get("fy"),
                    fq=None, derived=prev_k > 0, span_days=q_span,
                    source_accns=[f["accn"]], filed=f.get("filed"),
                ))

            prev_total, prev_end, prev_k = total, f["period_end"], k

    return _dedupe_by_period(out)


def quarterize_passthrough(rows: list[dict]) -> list[Quarter]:
    """Facts taken exactly as filed, with no differencing at all.

    For quantities where no arithmetic across periods is valid — a reported
    effective tax rate is a ratio, so neither summing nor averaging its
    year-to-date chain reconstructs the quarter. Where a discrete quarter was
    filed it is used; where only a year-to-date figure exists, that figure is
    reported against its own period end and never turned into something it
    isn't. Gaps stay gaps.
    """
    out = [
        Quarter(period_end=r["period_end"], value=float(r["val"]),
                fy=r.get("fy"), fq=_fp_to_fq(r.get("fp")), derived=False,
                span_days=(None if r.get("period_start") is None
                           else _days(r["period_start"], r["period_end"])),
                source_accns=[r["accn"]], filed=r.get("filed"))
        for r in rows
    ]
    # Prefer a genuine quarter-length fact over a year-to-date one at the same
    # period end; a YTD figure is kept only when it is all there is.
    best: dict[str, Quarter] = {}
    for q in out:
        cur = best.get(q.period_end)
        if cur is None:
            best[q.period_end] = q
            continue
        cur_is_q = cur.span_days is not None and QUARTER_SPAN[0] <= cur.span_days <= QUARTER_SPAN[1]
        new_is_q = q.span_days is not None and QUARTER_SPAN[0] <= q.span_days <= QUARTER_SPAN[1]
        if (new_is_q and not cur_is_q) or (
                new_is_q == cur_is_q and (q.filed or "") > (cur.filed or "")):
            best[q.period_end] = q
    return sorted(best.values(), key=lambda q: q.period_end)


def _fp_to_fq(fp: str | None) -> int | None:
    return {"Q1": 1, "Q2": 2, "Q3": 3, "FY": 4, "Q4": 4}.get(fp or "")


def _dedupe_by_period(qs: list[Quarter]) -> list[Quarter]:
    """One row per period_end. Prefer reported over derived; then latest filed."""
    best: dict[str, Quarter] = {}
    for q in qs:
        cur = best.get(q.period_end)
        if cur is None:
            best[q.period_end] = q
        elif cur.derived and not q.derived:
            best[q.period_end] = q
        elif cur.derived == q.derived and (q.filed or "") > (cur.filed or ""):
            best[q.period_end] = q
    return sorted(best.values(), key=lambda q: q.period_end)