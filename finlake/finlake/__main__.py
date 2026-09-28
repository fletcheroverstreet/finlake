"""finlake's command line.

    python -m finlake refresh --daemon     keep the cache current, continuously
    python -m finlake refresh --once       one pass of whatever is due
    python -m finlake refresh --status     what is stale right now
    python -m finlake universe --file F    which symbols to keep current
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

from . import refresh, store  # noqa: E402


def cmd_refresh(args: argparse.Namespace) -> int:
    if args.status:
        with store.session(read_only=False) as conn:
            rows = refresh.status(conn)
        width = max(len(r["task"]) for r in rows)
        print(f"{'task'.ljust(width)}  {'every':>6}  {'last ok':>12}  "
              f"{'status':>8}  due")
        for r in rows:
            print(f"{r['task'].ljust(width)}  {r['every']:>6}  "
                  f"{str(r['last_success'] or '—'):>12}  "
                  f"{str(r['last_status']):>8}  {'YES' if r['due'] else ''}")
        return 0

    if args.daemon:
        refresh.daemon(poll_seconds=args.poll, limit=args.limit)
        return 0

    with store.session() as conn:
        results = refresh.run_once(conn, limit=args.limit, only=args.only)
    if not results:
        print("nothing was due.")
    return 0


def cmd_universe(args: argparse.Namespace) -> int:
    """Declare which symbols the refresh tiers keep current, or show them.

    Separate from the builder because the two are separate decisions. The
    builder decides what to DOWNLOAD, once; this decides what to keep CURRENT,
    every three minutes, forever — and until it existed the second question was
    being answered by `ticker_map`, which lists instruments rather than
    companies and put 209 preferred series, warrants and baby bonds into every
    sweep.
    """
    with store.session() as conn:
        store.init_db(conn)
        if args.file:
            rows = store.read_universe_file(args.file)
            n = store.declare_universe(conn, rows, source=str(args.file))
            print(f"declared {n} symbols from {args.file}")
        members = store.universe_members(conn)
        source = store.universe_source(conn)

    if not members:
        print("No universe declared. Every ticker with cached facts is swept "
              "— including each filer's preferred series, warrants and notes.\n"
              "  python -m finlake universe --file universe/sp500_ndx.csv")
        return 0
    print(f"{len(members)} symbols declared" + (f" from {source}" if source else ""))
    if args.list:
        print("  " + ", ".join(members))
    return 0


def cmd_rescore_news(args: argparse.Namespace) -> int:
    """Re-apply the current sentiment scoring to every stored article.

    News is stored with its tone alongside it, so a change to the scoring
    reaches new articles immediately and old ones never — the cache keeps
    serving verdicts from whatever lexicon was current when each row landed.
    """
    from .sources import news as news_src

    with store.session() as conn:
        counts = news_src.rescore(conn)
    print(f"rescored {counts['total']:,} articles "
          f"({counts['articles']:,} news, {counts['filings']:,} filings)")
    print(f"  {counts['now_unscored']:,} no longer carry a tone "
          f"(procedural filings, mostly)")
    print(f"  {counts['sign_flipped']:,} changed sign")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    from . import quality

    tickers = args.ticker
    if not tickers:
        with store.session(read_only=True) as conn:
            rows = conn.execute(
                "SELECT DISTINCT tm.ticker FROM ticker_map tm "
                "JOIN facts f ON f.cik = tm.cik "
                "WHERE tm.valid_to IS NULL ORDER BY tm.ticker"
                + (f" LIMIT {int(args.limit)}" if args.limit else "")
            ).fetchall()
        tickers = [r["ticker"] for r in rows]
    if not tickers:
        print("Nothing cached yet. Run scripts/build.py first.")
        return 1

    if args.verbose or len(tickers) <= 5:
        floor = quality.SEVERITY_ORDER[args.min_severity]
        worst_seen = 0
        for ticker in tickers:
            findings = [f for f in quality.check_ticker(ticker)
                        if quality.SEVERITY_ORDER.get(f.severity, 9) <= floor]
            score = quality.score_ticker(quality.check_ticker(ticker))
            print(f"\n=== {ticker}  score {score:.0f}/100 "
                  f"({len(findings)} findings at {args.min_severity}+) ===")
            for f in findings:
                print(f"  [{f.severity:<8}] {f}")
            worst_seen = max(worst_seen, len(findings))
        return 0

    print(f"Checking {len(tickers)} companies...")
    df = quality.check_universe(tickers)
    print(f"\n{'ticker':<8} {'score':>6}  {'crit':>4} {'ser':>4} {'warn':>5}  worst")
    print("-" * 46)
    for _, r in df.head(30).iterrows():
        print(f"{r['ticker']:<8} {r['score']:>6.0f}  {r['critical']:>4} "
              f"{r['serious']:>4} {r['warning']:>5}  {r['worst']}")
    print(f"\nmedian score {df['score'].median():.0f}/100 · "
          f"{(df['critical'] > 0).sum()} with critical findings · "
          f"{(df['score'] == 100).sum()} clean")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finlake")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("refresh", help="keep the local cache current")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--daemon", action="store_true",
                      help="run continuously (Ctrl-C to stop)")
    mode.add_argument("--once", action="store_true",
                      help="single pass over whatever is due (the default)")
    mode.add_argument("--status", action="store_true",
                      help="show what is stale without fetching anything")
    p.add_argument("--only", nargs="*",
                   help="run only these tasks, due or not "
                        "(quotes news filings market macro)")
    p.add_argument("--limit", type=int,
                   help="cap the number of tickers touched — useful for a "
                        "quick check without a full universe sweep")
    p.add_argument("--poll", type=int, default=60,
                   help="daemon tick in seconds (default 60)")
    p.set_defaults(func=cmd_refresh)

    u = sub.add_parser(
        "universe",
        help="declare which symbols the refresh tiers keep current")
    u.add_argument("--file", type=Path,
                   help="constituents CSV with a 'ticker' column "
                        "(e.g. universe/sp500_ndx.csv)")
    u.add_argument("--list", action="store_true",
                   help="print every declared symbol, not just the count")
    u.set_defaults(func=cmd_universe)

    r = sub.add_parser(
        "rescore-news",
        help="re-apply the current sentiment scoring to every stored article")
    r.set_defaults(func=cmd_rescore_news)

    h = sub.add_parser("health", help="check the data is complete and consistent")
    h.add_argument("--ticker", nargs="*", help="specific tickers (default: all cached)")
    h.add_argument("--limit", type=int, help="cap companies checked")
    h.add_argument("--verbose", action="store_true",
                   help="list every finding, not just the summary")
    h.add_argument("--min-severity", default="warning",
                   choices=["critical", "serious", "warning", "info"])
    h.set_defaults(func=cmd_health)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
