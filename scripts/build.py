#!/usr/bin/env python3
"""Bulk build the local store.

    python scripts/build.py --tickers AAPL MSFT NVDA
    python scripts/build.py --sp500-ish --limit 500
    python scripts/build.py --macro

Resumable: everything is INSERT OR IGNORE and the HTTP layer caches raw
responses, so a killed run costs you nothing. Re-running is cheap.

Rough cost for the full ~8,000 XBRL filers: 2 requests each at 8 req/s is
about 35 minutes and ~6 GB of raw JSON. Start with 200 tickers.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Windows consoles still default to cp1252, which cannot encode the arrows and
# box characters in the progress output — the build would die on its first
# status line with a UnicodeEncodeError, after doing no work at all.
# errors="replace" so an odd character in a company name degrades to '?'
# rather than taking down a run that is otherwise fine.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from finlake import config, store  # noqa: E402
from finlake.sources import fred, market, news, prices, sec  # noqa: E402


def build(tickers: list[str] | None, limit: int | None, do_prices: bool,
          do_macro: bool, do_market: bool = False, do_news: bool = False,
          names: dict[str, str] | None = None) -> None:
    if "example.com" in config.SEC_USER_AGENT or "set SEC_USER_AGENT" in config.SEC_USER_AGENT:
        print("!! Set SEC_USER_AGENT to your real email first, or the SEC "
              "will 403 you.\n   export SEC_USER_AGENT='finlake/0.1 (you@example.com)'")
        return

    store.init_db()
    with store.session() as conn:
        print("→ ticker map")
        n = sec.load_ticker_map(conn)
        print(f"  {n} new mappings")

        if tickers:
            targets, unknown, recovered = [], [], []
            for t in tickers:
                cik = sec.resolve_cik(conn, t)
                if cik is None:
                    # Try EDGAR company search before giving up: the SEC's
                    # ticker files are a convenience index and do miss real
                    # companies (AEP is in neither of them).
                    #
                    # Search on the COMPANY NAME, not the ticker. EDGAR's
                    # company search matches names, so handing it "AEP" finds
                    # nothing while "American Electric Power" resolves
                    # immediately — which is why the universe file carries a
                    # name column, and why `names` has to actually reach here.
                    # It did not: this call was always made with the ticker,
                    # always returned None, and AEP was silently absent from
                    # the whole cache.
                    cik = sec.resolve_cik_by_name((names or {}).get(t.upper(), t))
                if cik is None:
                    unknown.append(t)
                    continue
                targets.append((t.upper(), cik))

                # The mapping has to be WRITTEN BACK, or the facts arrive
                # under a CIK that no ticker resolves to and the company stays
                # invisible anyway. This covers two cases that look different
                # and fail identically:
                #
                #   never mapped     -- not in the SEC ticker file at all.
                #   mapped, closed   -- it WAS mapped and a previous run
                #                       closed the window, because a mapping
                #                       absent from the ticker file looks
                #                       exactly like a delisting. `resolve_cik`
                #                       still answers from the closed row, so
                #                       this repair has to test liveness
                #                       rather than resolvability.
                #
                # A name we are building from a constituent list is current by
                # assertion, so a live mapping is the honest record. No-ops
                # when one already exists, which is all 502 other names here.
                if sec.record_ticker_mapping(conn, t, cik):
                    recovered.append(t.upper())
            if recovered:
                print(f"  + {len(recovered)} resolved by name, not in the SEC "
                      f"ticker files: {', '.join(recovered[:20])}")
            if unknown:
                # Listed, never silently dropped. A ticker that vanishes
                # between the universe file and the run is the kind of gap
                # that is invisible until someone asks why a name is missing.
                print(f"  ! {len(unknown)} unresolved: {', '.join(unknown[:20])}")
        else:
            rows = conn.execute(
                "SELECT ticker, cik FROM ticker_map WHERE valid_to IS NULL "
                "ORDER BY ticker" + (f" LIMIT {int(limit)}" if limit else "")
            ).fetchall()
            targets = [(r["ticker"], r["cik"]) for r in rows]

        print(f"→ {len(targets)} companies")
        t0 = time.time()
        for i, (tkr, cik) in enumerate(targets, 1):
            try:
                sec.load_submissions(conn, cik)
                nf = sec.load_company_facts(conn, cik)
                msg = f"{nf:>7} facts"
            except Exception as exc:
                msg = f"FAILED: {exc}"
            if do_prices:
                try:
                    prices.load_prices(conn, tkr)
                except Exception as exc:
                    msg += f" | prices failed: {exc}"
            if do_market:
                # Analyst estimates, short interest, ownership, targets.
                # Never raises -- a failure is recorded per section, so one
                # dead endpoint costs that section and not the ticker.
                counts = market.load_market_data(conn, tkr)
                if "error" in counts or "empty" in counts:
                    msg += " | market: none"
                else:
                    msg += f" | market: {sum(counts.values())} rows"
            if do_news:
                try:
                    name = conn.execute(
                        "SELECT name FROM securities WHERE cik=?", (cik,)
                    ).fetchone()
                    got = news.load_news(conn, tkr, cik=cik,
                                         company=name["name"] if name else None)
                    msg += f" | news: {got['new']} new of {got['total_seen']}"
                except Exception as exc:
                    msg += f" | news failed: {exc}"
            rate = i / max(time.time() - t0, 1e-9)
            eta = (len(targets) - i) / max(rate, 1e-9) / 60
            print(f"  [{i}/{len(targets)}] {tkr:<6} {msg}  (~{eta:.0f}m left)")

        # Extend ticker validity windows back to each company's first filing,
        # now that submissions have set first_filed. Without this every
        # historical as-of query resolves to no ticker and returns an empty
        # universe -- see sec.backfill_ticker_validity.
        print("→ backfilling ticker validity windows")
        print(f"  {sec.backfill_ticker_validity(conn)} windows extended")

        if do_macro:
            print("→ macro (FRED vintages)")
            for sid, n in fred.load_default_series(conn).items():
                print(f"  {sid:<14} {n} obs")

    print("\ndone. try:  python -c \"import finlake; "
          "print(finlake.fundamentals('AAPL', years=10).tail())\"")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--tickers", nargs="*", help="specific tickers")
    p.add_argument("--universe-file", type=Path,
                   help="CSV with a 'ticker' column (e.g. "
                        "universe/sp500_ndx.csv) — builds exactly those names")
    p.add_argument("--limit", type=int, help="cap when building everything")
    p.add_argument("--prices", action="store_true", help="also fetch price bars")
    p.add_argument("--macro", action="store_true", help="also fetch FRED series")
    p.add_argument("--market", action="store_true",
                   help="also fetch estimates, targets, short interest, "
                        "ownership (required for forward P/E)")
    p.add_argument("--news", action="store_true",
                   help="also fetch news from all sources")
    p.add_argument("--all", action="store_true",
                   help="prices + market + news + macro")
    a = p.parse_args()

    tickers = a.tickers
    names: dict[str, str] = {}
    universe_rows: list[tuple[str, int | None, str | None]] = []
    if a.universe_file:
        universe_rows = store.read_universe_file(a.universe_file)
        # The name column is what makes an unresolvable ticker recoverable —
        # see the EDGAR company-search fallback in build(). Collected here and
        # actually passed, which it previously was not.
        names = {t: n for t, _cik, n in universe_rows if n}
        if a.tickers:
            # BOTH: the file supplies the names, `--tickers` narrows the work.
            # Repairing one name out of a 503-name list is the normal way this
            # script gets run after the first build, and without this the only
            # way to get a company name to the EDGAR fallback was to rebuild
            # everything.
            wanted = {t.upper() for t in a.tickers}
            tickers = [t for t, _c, _n in universe_rows if t in wanted]
            missing = sorted(wanted - set(tickers))
            if missing:
                print(f"  ! not in {a.universe_file}: {', '.join(missing)}")
            print(f"universe file: {len(tickers)} of {len(universe_rows)} "
                  f"tickers selected from {a.universe_file}")
        else:
            tickers = [t for t, _cik, _name in universe_rows]
            print(f"universe file: {len(tickers)} tickers from {a.universe_file}")
            # --limit previously applied only when walking the whole
            # ticker_map, so it silently did nothing against an explicit list
            # -- which turns an intended two-ticker smoke test into a full
            # 503-name build.
            if a.limit:
                tickers = tickers[:a.limit]
                universe_rows = universe_rows[:a.limit]
                print(f"  limited to {len(tickers)}")

    build(tickers, a.limit, a.prices or a.all, a.macro or a.all,
          a.market or a.all, a.news or a.all, names=names)

    # Declared only for a full pass over the file. A `--tickers` repair run
    # touches a few names and must not redefine the universe as just those.
    if universe_rows and not a.tickers:
        # Declare what the refresh tiers should keep current. Without this the
        # daemon falls back to ticker_map, which lists every instrument a
        # filer has registered — preferred series, warrants, baby bonds — and
        # sweeps all of them on all five tiers forever.
        with store.session() as conn:
            n = store.declare_universe(conn, universe_rows,
                                       source=str(a.universe_file))
        print(f"→ declared {n} symbols as the refresh universe")
