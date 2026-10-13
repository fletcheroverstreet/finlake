#!/usr/bin/env python3
"""Build the investable universe: S&P 500 + Nasdaq-100.

    python scripts/build_universe.py
    python scripts/build_universe.py --out universe/sp500_ndx.csv

Writes a CSV of ticker, name, sector, and source index. That file is what
`lodestar/config.yaml: universe.constituents_file` points at, and pointing it
there **replaces** finlake's filing-activity-proxy `universe()` rather than
filtering it (lodestar DEC-011).

Why that matters: the proxy universe answers "was this company filing with the
SEC on that date", which includes every shell, trust, and OTC name that ever
filed a 10-K. A constituent list answers "was this a real, liquid, investable
company" — which is the question a screen is actually asking, and closes the
ISSUE-004 limitation both READMEs carry.

**This is a CURRENT snapshot, not a historical membership file.** Wikipedia's
list is today's index; it has no add/drop dates. Applying today's constituents
to a 2015 screen reintroduces survivorship bias — the companies that were
dropped for doing badly are exactly the ones missing. A real historical
backtest needs a point-in-time constituent file with add/drop dates, which is
a separate (usually paid) dataset. The file this writes carries that warning
in a header comment so it cannot be mistaken for one.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from finlake import config, store  # noqa: E402
from finlake.sources import sec  # noqa: E402

SOURCES = [
    ("S&P 500", "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"),
    ("Nasdaq-100", "https://en.wikipedia.org/wiki/Nasdaq-100"),
]

# Wikipedia writes share classes with a dot (BRK.B); the SEC uses a dash
# (BRK-B). Without this, every multi-class name silently fails to resolve.
_TICKER_OK = re.compile(r"^[A-Z][A-Z0-9\-]{0,6}$")


def normalize(ticker: str) -> str:
    return ticker.strip().upper().replace(".", "-").replace("​", "")


def scrape(url: str) -> list[dict]:
    """Constituent rows from a Wikipedia list page.

    Finds the table that has both a symbol-ish and a name-ish header rather
    than assuming a table id or position — those change whenever someone
    edits the page, and an assumption that breaks silently returns zero rows.
    """
    import requests
    from bs4 import BeautifulSoup

    resp = requests.get(url, timeout=30, headers={
        "User-Agent": config.SEC_USER_AGENT or "finlake/1.0"})
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for table in soup.find_all("table", class_="wikitable"):
        header = [th.get_text(" ", strip=True).lower()
                  for th in table.find_all("th")]
        if not header:
            continue
        sym_idx = next((i for i, h in enumerate(header)
                        if "symbol" in h or "ticker" in h), None)
        name_idx = next((i for i, h in enumerate(header)
                         if "security" in h or "company" in h or "name" in h), None)
        if sym_idx is None or name_idx is None:
            continue
        sector_idx = next((i for i, h in enumerate(header)
                           if "sector" in h), None)

        rows = []
        for tr in table.find_all("tr")[1:]:
            cells = tr.find_all(["td", "th"])
            if len(cells) <= max(sym_idx, name_idx):
                continue
            ticker = normalize(cells[sym_idx].get_text(" ", strip=True))
            if not _TICKER_OK.match(ticker):
                continue
            rows.append({
                "ticker": ticker,
                "name": cells[name_idx].get_text(" ", strip=True),
                "sector": (cells[sector_idx].get_text(" ", strip=True)
                           if sector_idx is not None and len(cells) > sector_idx
                           else ""),
            })
        if len(rows) >= 50:      # a constituent table, not a footnote table
            return rows
    return []


def build(out_path: Path) -> int:
    members: dict[str, dict] = {}
    for label, url in SOURCES:
        try:
            rows = scrape(url)
        except Exception as exc:
            print(f"  ! {label}: {exc}")
            continue
        print(f"  {label:<12} {len(rows):>4} constituents")
        for row in rows:
            existing = members.get(row["ticker"])
            if existing:
                # A name in both indices records both rather than the last
                # one scraped.
                existing["indices"] = f"{existing['indices']}+{label}"
                existing["sector"] = existing["sector"] or row["sector"]
            else:
                members[row["ticker"]] = {**row, "indices": label}

    if not members:
        print("No constituents scraped. Nothing written — refusing to "
              "overwrite an existing universe file with an empty one.")
        return 1

    # Resolve each to a CIK so a name that finlake cannot identify is visible
    # NOW, rather than as a mysteriously empty row after a three-hour build.
    store.init_db()
    with store.session() as conn:
        if not conn.execute("SELECT 1 FROM ticker_map LIMIT 1").fetchone():
            print("  ! ticker_map is empty — run scripts/build.py first so "
                  "tickers can be resolved to CIKs")
        for ticker, row in members.items():
            row["cik"] = sec.resolve_cik(conn, ticker)

        # The SEC's ticker files are a convenience index, not the company
        # registry, and they do miss real companies -- neither
        # company_tickers.json nor company_tickers_exchange.json contains AEP,
        # an S&P 500 utility. Fall back to EDGAR's company search by name.
        stragglers = [t for t, r in members.items() if not r["cik"]]
        if stragglers:
            print(f"  resolving {len(stragglers)} by company name...")
            for ticker in stragglers:
                found = sec.resolve_cik_by_name(members[ticker]["name"])
                if found:
                    members[ticker]["cik"] = found
                    print(f"    {ticker:<6} -> CIK {found} (by name)")

    unresolved = sorted(t for t, r in members.items() if not r["cik"])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        fh.write("# Investable universe: S&P 500 + Nasdaq-100, CURRENT membership.\n")
        fh.write("# NOT a historical constituent file -- it has no add/drop dates,\n")
        fh.write("# so applying it to a past date reintroduces survivorship bias.\n")
        writer = csv.DictWriter(
            fh, fieldnames=["ticker", "name", "sector", "indices", "cik"])
        writer.writeheader()
        for ticker in sorted(members):
            writer.writerow(members[ticker])

    print(f"\n  {len(members)} unique tickers -> {out_path}")
    print(f"  {len(members) - len(unresolved)} resolved to a CIK, "
          f"{len(unresolved)} unresolved")
    if unresolved:
        print(f"  unresolved: {', '.join(unresolved[:25])}"
              + (" ..." if len(unresolved) > 25 else ""))
    return 0


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", type=Path,
                   default=Path(__file__).resolve().parents[1] / "universe"
                   / "sp500_ndx.csv")
    a = p.parse_args()
    sys.exit(build(a.out))
