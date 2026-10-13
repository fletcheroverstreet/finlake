#!/usr/bin/env python3
"""Regression snapshot for finlake amendment windows.

Amendments are additive by design (see the lodestar project's amendment
protocol), but "additive" is a claim, not a guarantee. This script is the
check: dump a fixed set of query outputs to JSON before an amendment,
dump them again after, and diff. The only differences allowed are the
ones the approved fixes were supposed to produce.

    python scripts/regression_snapshot.py --out snapshots/before.json
    ... make the approved changes ...
    python scripts/regression_snapshot.py --out snapshots/after.json
    python scripts/regression_snapshot.py --diff snapshots/before.json snapshots/after.json

The ticker list and AS_OF date are pinned constants, not CLI args, on
purpose: a snapshot taken with a different universe or a different "as of
today" is not comparable to the last one.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd  # noqa: E402

import finlake  # noqa: E402

# Pinned so every snapshot in this amendment window is directly comparable.
# Do not change this without starting a new snapshot pair from scratch.
AS_OF = "2026-08-08"

TICKERS = [
    "MU", "NVDA", "AMD", "INTC", "AVGO", "QCOM", "TXN", "ADI", "MRVL", "ON",
    "LRCX", "AMAT", "KLAC", "SNDK", "WDC", "STX",
    "AAPL", "MSFT",
    "JPM", "BAC", "WFC", "GS", "MS", "SCHW",
    "SPG", "O", "PLD", "AMT", "EQIX",
    "WMT", "COST", "TGT", "HD", "LOW",
    "KO", "PEP", "PG",
    "JNJ", "PFE",
    "XOM", "CVX",
]


def _df_to_records(df: pd.DataFrame) -> list[dict]:
    """DataFrame -> JSON-safe records, NaN -> null, deterministic column order."""
    out = df.reset_index()
    out = out.reindex(sorted(out.columns), axis=1)
    return json.loads(out.to_json(orient="records", date_format="iso"))


def snapshot() -> dict:
    result: dict = {"as_of": AS_OF, "tickers": {}}
    for t in TICKERS:
        try:
            df = finlake.fundamentals(t, years=10, as_of=AS_OF, include_provenance=True)
            result["tickers"][t] = {
                "status": "ok",
                "shape": list(df.shape),
                "columns": sorted(df.columns.tolist()),
                "rows": _df_to_records(df),
            }
        except Exception as exc:  # a query that starts crashing IS a regression
            result["tickers"][t] = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}

    u = finlake.universe(as_of=AS_OF)
    result["universe"] = {
        "count": len(u),
        "tickers": sorted(u["ticker"].tolist()) if not u.empty and "ticker" in u.columns else [],
    }
    return result


def diff(before_path: str, after_path: str) -> None:
    before = json.loads(Path(before_path).read_text())
    after = json.loads(Path(after_path).read_text())

    if before["as_of"] != after["as_of"]:
        print(f"!! as_of changed ({before['as_of']} -> {after['as_of']}); "
              f"snapshots are not comparable")
        return

    all_tickers = sorted(set(before["tickers"]) | set(after["tickers"]))
    any_diff = False
    for t in all_tickers:
        b, a = before["tickers"].get(t), after["tickers"].get(t)
        if b is None or a is None:
            print(f"{t}: present in only one snapshot")
            any_diff = True
            continue
        if b.get("status") != a.get("status"):
            print(f"{t}: status {b.get('status')} -> {a.get('status')}")
            any_diff = True
            continue
        if b.get("status") == "ERROR":
            continue

        b_cols, a_cols = set(b["columns"]), set(a["columns"])
        added, removed = a_cols - b_cols, b_cols - a_cols
        if added:
            print(f"{t}: + columns {sorted(added)}")
            any_diff = True
        if removed:
            print(f"{t}: - columns {sorted(removed)}")
            any_diff = True

        shared = sorted(b_cols & a_cols)
        b_rows = {(r.get("period_end")): r for r in b["rows"]}
        a_rows = {(r.get("period_end")): r for r in a["rows"]}
        for period in sorted(set(b_rows) | set(a_rows)):
            br, ar = b_rows.get(period), a_rows.get(period)
            if br is None or ar is None:
                print(f"{t} {period}: row present in only one snapshot")
                any_diff = True
                continue
            for col in shared:
                bv, av = br.get(col), ar.get(col)
                if bv != av:
                    print(f"{t} {period} {col}: {bv!r} -> {av!r}")
                    any_diff = True

    b_u, a_u = set(before["universe"]["tickers"]), set(after["universe"]["tickers"])
    if b_u != a_u:
        print(f"universe: + {sorted(a_u - b_u)}  - {sorted(b_u - a_u)}")
        any_diff = True

    if not any_diff:
        print("no differences")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--out", help="write a snapshot to this path")
    p.add_argument("--diff", nargs=2, metavar=("BEFORE", "AFTER"),
                    help="diff two existing snapshots")
    a = p.parse_args()

    if a.diff:
        diff(*a.diff)
    elif a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(json.dumps(snapshot(), indent=1))
        print(f"wrote {a.out}")
    else:
        p.error("pass --out to snapshot or --diff BEFORE AFTER to compare")
