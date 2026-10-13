#!/usr/bin/env python3
"""Check every CONCEPTS entry against what companies actually filed.

    python scripts/audit_concepts.py
    python scripts/audit_concepts.py --min-coverage 0.5
    python scripts/audit_concepts.py --concept revenue --verbose

Why this exists: a concept is a *guess* about which XBRL tags filers use for
one economic quantity, and a wrong guess does not raise — it returns an empty
column, which reads downstream as "this company doesn't report that" rather
than "we asked for the wrong tag". Coverage is the only way to tell those
apart, so it gets measured rather than assumed.

Two numbers per concept:

  tag coverage    -- % of cached companies with at least one fact under ANY
                     of the concept's candidate tags. Low here means the tag
                     list is wrong or incomplete.
  resolved        -- % that survive quarterize and reach the frame as usable
                     quarters. Much lower than tag coverage means the facts
                     exist but the periods can't be reconstructed.

A concept can legitimately be sparse (`preferred_dividends` on companies with
no preferred stock, `goodwill_impairment` on companies that never took one).
The output flags low coverage; it does not assume it is a bug.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from finlake import api, store  # noqa: E402


def cached_tickers(conn, limit: int | None = None) -> list[tuple[str, int]]:
    """Companies with facts on disk, most-facts first.

    Ordering by fact count puts the well-covered filers first, so a truncated
    run still audits against companies that have something to say.
    """
    rows = conn.execute(
        """
        SELECT tm.ticker, f.cik, COUNT(*) n
        FROM facts f
        JOIN ticker_map tm ON tm.cik = f.cik AND tm.valid_to IS NULL
        GROUP BY f.cik
        HAVING n > 500
        ORDER BY n DESC
        """ + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()
    return [(r["ticker"], r["cik"]) for r in rows]


def audit(concepts: list[str], limit: int | None, min_coverage: float,
          verbose: bool) -> int:
    with store.session(read_only=True) as conn:
        targets = cached_tickers(conn, limit)
        if not targets:
            print("No cached companies with facts. Run scripts/build.py first.")
            return 1
        print(f"Auditing {len(concepts)} concepts against {len(targets)} "
              f"companies with cached facts.\n")

        # Which candidate tags exist, and for how many of THESE companies.
        # Counting across the whole facts table instead would divide a count
        # over every cached company by a sample of 25 and report coverage
        # above 100%.
        target_ciks = [cik for _t, cik in targets]
        placeholders = ",".join("?" * len(target_ciks))
        tag_hits: dict[str, int] = {}
        all_tags = {t for c in concepts for t in api.CONCEPTS[c]}
        for tag in all_tags:
            tag_hits[tag] = conn.execute(
                f"SELECT COUNT(DISTINCT cik) FROM facts "
                f"WHERE tag = ? AND cik IN ({placeholders})",
                (tag, *target_ciks),
            ).fetchone()[0]

    # Resolution is measured through the real public entry point, not a
    # reimplementation of it — otherwise the audit can pass while the thing
    # callers actually use is broken.
    resolved: dict[str, int] = {c: 0 for c in concepts}
    for ticker, _cik in targets:
        try:
            df = api.fundamentals(ticker, years=25, concepts=concepts)
        except Exception:
            continue
        for c in concepts:
            if c in df.columns and df[c].notna().any():
                resolved[c] += 1

    n = len(targets)
    rows = []
    for c in concepts:
        best_tag_cov = max((tag_hits[t] for t in api.CONCEPTS[c]), default=0)
        rows.append((c, best_tag_cov / n, resolved[c] / n))
    rows.sort(key=lambda r: r[2])

    print(f"{'concept':<30} {'tags':>7} {'resolved':>9}   status")
    print("-" * 68)
    problems = 0
    for concept, tag_cov, res_cov in rows:
        if res_cov >= min_coverage:
            status = "ok"
        elif tag_cov >= min_coverage:
            # The facts are there and we still didn't get a column. That is a
            # resolution bug (unit mismatch, unreconstructable periods), not a
            # missing-tag problem — and it is the interesting failure.
            status = "TAGS FOUND BUT UNRESOLVED"
            problems += 1
        elif tag_cov == 0:
            status = "NO TAG FOUND — check tag names"
            problems += 1
        else:
            status = "sparse"
        print(f"{concept:<30} {tag_cov:>6.0%} {res_cov:>8.0%}   {status}")

    if verbose:
        print("\nPer-tag company counts:")
        for tag, hits in sorted(tag_hits.items(), key=lambda kv: -kv[1]):
            print(f"  {hits:>4}/{n}  {tag}")

    print(f"\n{len(concepts) - problems}/{len(concepts)} concepts resolve on "
          f"at least {min_coverage:.0%} of companies.")
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--concept", nargs="*", help="audit only these concepts")
    p.add_argument("--limit", type=int, help="cap companies audited")
    p.add_argument("--min-coverage", type=float, default=0.30)
    p.add_argument("--verbose", action="store_true", help="per-tag counts")
    a = p.parse_args()
    sys.exit(audit(a.concept or list(api.CONCEPTS), a.limit, a.min_coverage,
                   a.verbose))
