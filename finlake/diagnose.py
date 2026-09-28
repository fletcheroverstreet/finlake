"""Dump the raw revenue facts finlake resolved for a ticker, so we can see
exactly what quarterize.py was handed. Run:  python diagnose.py MU"""

import datetime as dt
import sys

from finlake import api, pit, store
from finlake.quarterize import classify_duration
from finlake.sources import sec

ticker = sys.argv[1] if len(sys.argv) > 1 else "MU"

with store.session(read_only=True) as conn:
    cik = sec.resolve_cik(conn, ticker)
    rows = pit.as_of_facts(
        conn, cik, api.CONCEPTS["revenue"],
        as_of=dt.date.today().isoformat(), min_period="2022-01-01",
    )

print(f"{ticker} (CIK {cik}) — {len(rows)} resolved revenue facts\n")
print(f"{'period_start':<13} {'period_end':<12} {'days':>5} {'kind':>6} "
      f"{'val ($M)':>10} {'fy':>5} {'fp':>4} {'filed':<12} form")
print("-" * 92)

for r in sorted(rows, key=lambda x: (x["period_end"], x["period_start"] or "")):
    ps, pe = r["period_start"], r["period_end"]
    days = ""
    if ps:
        days = (dt.date.fromisoformat(pe) - dt.date.fromisoformat(ps)).days
    kind = classify_duration(ps, pe) or "??"
    print(f"{ps or '(instant)':<13} {pe:<12} {days:>5} {kind:>6} "
          f"{r['val']/1e6:>10,.0f} {str(r['fy']):>5} {str(r['fp']):>4} "
          f"{r['filed']:<12} {r['form']}")

print("""
WHAT TO LOOK AT
---------------
The `fy` column. In SEC XBRL, `fy` is the fiscal year of the FILING the fact
appeared in — NOT the fiscal year of the period the fact describes. A 9-month
figure that got re-published as a comparative in next year's 10-Q carries the
LATER year's fy.

quarterize.py grouped facts into fiscal years using that field. So the 9-month
figure and the full-year figure for the same fiscal year can land in different
buckets, and `FY minus 9M` never happens.

Look instead at `period_start`. Every YTD figure in one fiscal year shares the
same start date. That's the reliable key.
""")
