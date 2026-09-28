#!/usr/bin/env python3
"""Universe-wide valuation invariants.

    python scripts/audit_valuation.py
    python scripts/audit_valuation.py --tolerance 0.25 --limit 50

RUN THIS AFTER ANY CHANGE THAT TOUCHES VALUATION. Two checks, and between
them they catch a whole class of bug that produces no exception, no log line,
and a perfectly plausible-looking number:

  1. IDENTITY.  enterprise value == market cap + total debt − cash.
     Internal consistency. It fails when one leg of the calculation resolves
     and another does not — a filer whose debt is tagged only under a combined
     element, a cash figure that came from a different period, a share count
     snapped onto the wrong quarter. Every one of those leaves EV looking
     ordinary.

  2. RECONCILIATION.  our market cap vs. the market data provider's.
     An independent second opinion, which is the only thing that catches an
     error shared by every leg of our own arithmetic. This is the check that
     surfaced the multi-class share-count bug: Visa's filed
     `CommonStockSharesOutstanding` covers one class, and the resulting market
     cap was $41bn against $673bn — internally consistent, and wrong by a
     factor of sixteen.

Neither is a pass/fail on data quality in general. They are tripwires on the
specific failure this codebase keeps paying for, and the run is only clean
when both are.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

import finlake  # noqa: E402
from finlake import quality, store  # noqa: E402
from finlake.sources import market  # noqa: E402

# How far our market cap may sit from the provider's before it is a finding.
# Wide on purpose. The two are not measuring quite the same thing — ours pairs
# a filed share count with the last cached close, theirs is consolidated and
# intraday — so a gap of a few percent is the normal state of the world and
# flagging it would bury the cases that matter. Sixteen-fold is what this is
# looking for.
DEFAULT_TOLERANCE = 0.25

# The identity is arithmetic, so it should hold exactly. The allowance is for
# float noise and for the provider rounding its own EV, not for disagreement.
IDENTITY_TOLERANCE = 0.01


def universe(limit: int | None) -> list[str]:
    with store.session(read_only=True) as conn:
        members = store.universe_members(conn)
        if not members:
            members = [r["ticker"] for r in conn.execute(
                "SELECT DISTINCT ticker FROM ticker_map WHERE valid_to IS NULL "
                "AND cik IN (SELECT DISTINCT cik FROM facts) ORDER BY ticker")]
    return members[:limit] if limit else members


def check_identity(row: dict) -> tuple[bool, str] | None:
    """EV == market cap + net debt, or None when a leg is not computable.

    READS `net_debt` AS REPORTED rather than rebuilding it from debt and cash.
    Recomputing it here would test this script's arithmetic instead of the
    thing that actually goes wrong, which is COHERENCE: `latest()` composes
    its dict column by column, taking each one's newest non-missing value, and
    that is right for filling gaps and wrong for figures that have to add up.
    Ford printed net debt of −$49.7bn beside an enterprise value built from a
    different quarter's cash balance — every number individually defensible,
    and the three of them contradicting each other on one screen.
    """
    ev = row.get("enterprise_value")
    cap = row.get("market_cap")
    net_debt = row.get("net_debt")
    if ev is None or cap is None or net_debt is None:
        return None
    expected = cap + net_debt
    if abs(expected) < 1.0:
        return None
    gap = abs(ev - expected) / abs(expected)
    return (gap <= IDENTITY_TOLERANCE,
            f"EV {ev / 1e9:,.1f}bn vs cap+net debt {expected / 1e9:,.1f}bn "
            f"({gap:.1%})")


def check_against_provider(conn, ticker: str, row: dict,
                           tolerance: float) -> tuple[bool, str] | None:
    """Our market cap vs. the provider's, or None when it has no figure."""
    ours = row.get("market_cap")
    if not ours:
        return None
    snap = market.latest_snapshot(conn, ticker)
    theirs = (snap or {}).get("market_cap")
    if not theirs:
        return None
    gap = abs(ours - theirs) / abs(theirs)
    return (gap <= tolerance,
            f"ours {ours / 1e9:,.1f}bn vs provider {theirs / 1e9:,.1f}bn "
            f"({gap:.1%})")


# SIC ranges where "market cap plus net debt" is a category error rather than
# a number worth reconciling: depositories and credit institutions, brokers,
# insurers, and the investment vehicles at the top of the 67xx block.
#
# Deposits and policy reserves are the raw material of the business, not
# borrowings used to fund an asset base, so EV is not a takeover price. Every
# provider resolves it differently and none agree — Citigroup came back at
# $34.7bn against a $180bn market cap, Berkshire at −$234bn. Reported
# separately rather than as findings; a dozen permanent non-findings at the top
# of the list is how a tripwire stops being read.
#
# REITs (6798) and real-estate operators (65xx) are deliberately NOT excluded.
# `quality._is_financial` sweeps the whole 6000–6799 division, which is right
# for the coverage checks it guards and wrong here: a REIT's mortgages and
# unsecured notes are ordinary corporate debt, its EV means exactly what it
# does for a manufacturer, and waving REITs through hid a real understatement
# — Realty Income's debt resolving to $1.4bn of commercial paper against $26bn
# actually outstanding.
EV_MEANINGLESS_SIC = ((6000, 6499), (6770, 6797), (6799, 6799))


def _ev_is_meaningless(conn, ticker: str) -> bool:
    row = conn.execute(
        "SELECT s.sic FROM securities s JOIN ticker_map tm ON tm.cik = s.cik "
        "WHERE tm.ticker = ? AND tm.valid_to IS NULL LIMIT 1", (ticker.upper(),)
    ).fetchone()
    try:
        sic = int(row["sic"]) if row and row["sic"] else None
    except (TypeError, ValueError):
        return False
    if sic is None:
        return False
    return any(lo <= sic <= hi for lo, hi in EV_MEANINGLESS_SIC)


def check_enterprise_value(conn, ticker: str, row: dict,
                           tolerance: float) -> tuple[bool, str] | None:
    """Our enterprise value vs. the provider's.

    THE CHECK THAT WAS MISSING, and the reason a real bug sat behind two clean
    invariants. The identity holds vacuously when a leg is absent — EV was
    computed as `market cap − cash` for every filer whose debt no CONCEPTS tag
    resolved, so EV, market cap and net debt were mutually consistent and EV
    was wrong by a factor of eight for Ford. Only an independent measure of the
    same quantity catches an error the whole of our own arithmetic agrees on.

    Skipped where either side has no figure, which now includes every name
    whose debt is genuinely unresolvable — that is the honest gap, not a
    finding. Also skipped where the provider's own EV is negative: for an
    insurer holding a large float they subtract investments as though they were
    corporate cash, and Berkshire comes back at −$234bn. A second opinion is
    not a source of truth.
    """
    ours = row.get("enterprise_value")
    if not ours:
        return None
    snap = market.latest_snapshot(conn, ticker)
    theirs = (snap or {}).get("enterprise_value")
    if not theirs or theirs <= 0:
        return None
    if _ev_is_meaningless(conn, ticker):
        return None
    gap = abs(ours - theirs) / abs(theirs)
    return (gap <= tolerance,
            f"ours {ours / 1e9:,.1f}bn vs provider {theirs / 1e9:,.1f}bn "
            f"({gap:.1%})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE,
                   help="market-cap reconciliation tolerance (default 0.25)")
    p.add_argument("--limit", type=int, help="cap the number of names checked")
    p.add_argument("--quiet", action="store_true",
                   help="print only the summary and the failures")
    a = p.parse_args(argv)

    tickers = universe(a.limit)
    if not tickers:
        print("Nothing cached. Run scripts/build.py first.")
        return 1
    print(f"Auditing {len(tickers)} names ...\n")

    identity = {"ok": 0, "bad": 0, "n/a": 0}
    provider = {"ok": 0, "bad": 0, "n/a": 0}
    ev_check = {"ok": 0, "bad": 0, "n/a": 0}
    failures: list[str] = []

    with store.session(read_only=True) as conn:
        for i, ticker in enumerate(tickers, 1):
            try:
                row = finlake.ratios_latest(ticker, as_of=None, live=True)
            except Exception as exc:                       # noqa: BLE001
                failures.append(f"{ticker:<8} raised: {str(exc)[:120]}")
                identity["n/a"] += 1
                provider["n/a"] += 1
                continue
            if not row:
                identity["n/a"] += 1
                provider["n/a"] += 1
                continue

            got = check_identity(row)
            if got is None:
                identity["n/a"] += 1
            elif got[0]:
                identity["ok"] += 1
            else:
                identity["bad"] += 1
                failures.append(f"{ticker:<8} IDENTITY   {got[1]}")

            got = check_against_provider(conn, ticker, row, a.tolerance)
            if got is None:
                provider["n/a"] += 1
            elif got[0]:
                provider["ok"] += 1
            else:
                provider["bad"] += 1
                failures.append(f"{ticker:<8} PROVIDER   {got[1]}")

            got = check_enterprise_value(conn, ticker, row, a.tolerance)
            if got is None:
                ev_check["n/a"] += 1
            elif got[0]:
                ev_check["ok"] += 1
            else:
                ev_check["bad"] += 1
                failures.append(f"{ticker:<8} EV         {got[1]}")

            if not a.quiet and i % 50 == 0:
                print(f"  {i}/{len(tickers)} ...")

    print(f"\nenterprise value == market cap + net debt")
    print(f"  {identity['ok']}/{identity['ok'] + identity['bad']} clean"
          f"   ({identity['n/a']} not computable)")
    print(f"market cap within {a.tolerance:.0%} of the provider's")
    print(f"  {provider['ok']}/{provider['ok'] + provider['bad']} clean"
          f"   ({provider['n/a']} no provider figure)")
    print(f"enterprise value within {a.tolerance:.0%} of the provider's")
    print(f"  {ev_check['ok']}/{ev_check['ok'] + ev_check['bad']} clean"
          f"   ({ev_check['n/a']} not comparable — a financial, debt "
          f"unresolved, or the provider's own EV is negative)")
    print("\nnote: our EV excludes operating lease liabilities and most "
          "providers include them,\nso a lease-heavy retailer or casino sits "
          "legitimately below the provider's figure.")

    if failures:
        print(f"\n{len(failures)} finding(s):")
        for line in failures:
            print(f"  {line}")
        return 1
    print("\nboth invariants clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
