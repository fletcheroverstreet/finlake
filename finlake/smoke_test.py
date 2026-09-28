"""
SMOKE TEST — is Tier 0 good enough to build on?
===============================================

    python scripts/build.py --tickers AAPL MSFT NVDA AMD MU INTC TSM AVGO QCOM TXN LRCX AMAT KLAC ADI MRVL ON SNDK WDC STX JPM BAC WFC GS MS SCHW SPG O PLD AMT EQIX WMT COST TGT HD LOW KO PEP PG JNJ PFE XOM CVX
    python smoke_test.py

This does NOT check numbers against filings — you already did that for MU.
It checks whether the SHAPE holds across companies that aren't Micron:
banks, REITs, retailers, foreign filers. Those use different XBRL tags and
different fiscal calendars, and that's where a data layer quietly breaks.

Each check answers a yes/no question. Failures are informative, not fatal —
the point is to know what's broken before a project depends on it.
"""

import warnings

import pandas as pd

import finlake

warnings.filterwarnings("ignore")

GROUPS = {
    "Semis":     ["MU", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "ADI",
                  "MRVL", "ON", "LRCX", "AMAT", "KLAC", "SNDK", "WDC", "STX"],
    "Big tech":  ["AAPL", "MSFT"],
    "Banks":     ["JPM", "BAC", "WFC", "GS", "MS", "SCHW"],
    "REITs":     ["SPG", "O", "PLD", "AMT", "EQIX"],
    "Retail":    ["WMT", "COST", "TGT", "HD", "LOW"],
    "Staples":   ["KO", "PEP", "PG"],
    "Pharma":    ["JNJ", "PFE"],
    "Energy":    ["XOM", "CVX"],
}

CORE = ["revenue", "net_income", "assets", "equity", "cfo"]
results, problems = [], []


def check(ticker):
    row = {"ticker": ticker}
    try:
        df = finlake.fundamentals(ticker, years=5, include_provenance=True)
    except Exception as exc:
        problems.append(f"{ticker}: fundamentals() raised {type(exc).__name__}: {exc}")
        return {**row, "status": "ERROR"}

    if df.empty:
        problems.append(f"{ticker}: no data at all (not built? foreign filer?)")
        return {**row, "status": "EMPTY"}

    row["quarters"] = len(df)

    # 1. Are the core concepts present?
    missing = [c for c in CORE if c not in df.columns]
    row["missing"] = ",".join(missing) if missing else "-"
    if "revenue" in missing:
        problems.append(f"{ticker}: NO REVENUE — tag fallback failed for this filer")

    # 2. How complete is revenue across those quarters?
    if "revenue" in df.columns:
        rev = df["revenue"]
        row["rev_filled"] = f"{rev.notna().sum()}/{len(rev)}"
        row["gaps"] = int(rev.isna().sum())

        # 3. Negative revenue is always a bug (a bad differencing chain).
        neg = rev[rev < 0]
        row["negative"] = len(neg)
        if len(neg):
            problems.append(
                f"{ticker}: {len(neg)} NEGATIVE revenue quarter(s) — "
                f"{list(neg.index[:3])}")

        # 4. A YTD leak shows up as a quarter that is a near-exact MULTIPLE
        #    of its neighbours, not merely a large one. Comparing to the median
        #    flags genuine hypergrowth (MU tripled revenue in a year and that
        #    is real). Compare each quarter to the one before it instead, and
        #    only flag jumps too abrupt to be a business.
        prev = rev.shift(1)
        ratio = (rev / prev).replace([float("inf")], float("nan"))
        spikes = rev[(ratio > 3.0) & prev.notna()]
        row["spikes"] = len(spikes)
        if len(spikes):
            problems.append(
                f"{ticker}: {len(spikes)} quarter(s) >3x the prior quarter — "
                f"possible YTD leak at {list(spikes.index[:2])}")

        # 5. Are Q4s being derived? If zero are derived across 5 years,
        #    the Q4 path isn't running for this filer.
        if "revenue__derived" in df.columns:
            d = df["revenue__derived"].fillna(False)
            row["derived"] = int(d.sum())
            # Zero derived is only a problem if quarters are ALSO missing.
            # Filers that tag all four quarters discretely need no derivation
            # at all — that is correct behaviour, not a failure.
            if d.sum() == 0 and int(rev.isna().sum()) > 0:
                problems.append(
                    f"{ticker}: {int(rev.isna().sum())} gaps and zero derived "
                    f"— Q4 derivation not firing for this filer")

    # 6. Balance sheet should never be negative or zero.
    if "assets" in df.columns:
        a = df["assets"].dropna()
        if len(a) and (a <= 0).any():
            problems.append(f"{ticker}: non-positive assets — instant-fact path broken")

    row["status"] = "ok"
    return row


print("Running smoke test...\n")
for group, tickers in GROUPS.items():
    for t in tickers:
        r = check(t)
        r["group"] = group
        results.append(r)

df = pd.DataFrame(results)
cols = ["group", "ticker", "status", "quarters", "rev_filled", "gaps",
        "negative", "spikes", "derived", "missing"]
df = df.reindex(columns=cols).fillna("-")

print(df.to_string(index=False))

ok = (df["status"] == "ok").sum()
print(f"\n{'='*72}")
print(f"{ok}/{len(df)} tickers returned usable data")
print(f"{'='*72}")

if problems:
    print(f"\n{len(problems)} issue(s) found:\n")
    for p in problems:
        print(f"  - {p}")
else:
    print("\nNo structural problems found.")

print("""
HOW TO READ THIS
----------------
BLOCKING — fix before building on it:
  * negative revenue anywhere      -> differencing chain is wrong
  * spikes (>5x median)            -> a YTD figure leaked through as a quarter
  * non-positive assets            -> instant-fact path broken
  * ERROR status                   -> code crashes on a real filer

NOT BLOCKING — expected, note and move on:
  * a few gaps                     -> filer skipped a YTD step; honest NaN
  * banks/REITs missing 'revenue'  -> they use industry-specific tags
  * EMPTY for a ticker             -> probably just not built yet
  * derived count ~1 per year      -> correct, that's Q4

If the only issues are gaps and missing tags on banks/REITs, Tier 0 is done.
Go build the semiconductor comparison tool.
""")