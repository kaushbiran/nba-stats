#!/usr/bin/env python3
"""Command line interface.

    nbastats query --player "stephen curry" --stats pts,ast --per-mode per36
    nbastats query --season 2015 --stats pts --min-games 58 --limit 10
    nbastats recipe single-digit-scoring --limit 20
    nbastats recipe threshold-games --stat pts --threshold 50
    nbastats recipes
"""

from __future__ import annotations

import argparse
import json
import sys

from . import db, recipes
from .query import COUNTING_STATS, RATE_STATS, StatQuery, run


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0].keys())
    widths = {
        c: max(len(c), max(len(_fmt(r.get(c))) for r in rows)) for c in cols
    }
    print("  ".join(c.ljust(widths[c]) for c in cols))
    print("  ".join("-" * widths[c] for c in cols))
    for r in rows:
        print("  ".join(_fmt(r.get(c)).ljust(widths[c]) for c in cols))


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="nbastats", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="path to the SQLite file")
    ap.add_argument("--json", action="store_true", help="output JSON instead of a table")
    sub = ap.add_subparsers(dest="command", required=True)

    q = sub.add_parser("query", help="filter season stats")
    q.add_argument("--entity", choices=["player", "team"], default="player")
    q.add_argument("--stats", default="pts,reb,ast", help="comma-separated")
    q.add_argument("--per-mode", default="per_game",
                   choices=["totals", "per_game", "per36", "per100"])
    q.add_argument("--season", type=int)
    q.add_argument("--season-range", help="e.g. 2015-2020")
    q.add_argument("--season-type", default="Regular Season")
    q.add_argument("--player")
    q.add_argument("--team")
    q.add_argument("--min-games", type=int, default=0)
    q.add_argument("--min-minutes", type=float, default=0.0)
    q.add_argument("--career", action="store_true",
                   help="aggregate across seasons instead of per season")
    q.add_argument("--order-by")
    q.add_argument("--ascending", action="store_true")
    q.add_argument("--limit", type=int, default=25)
    q.add_argument("--show-sql", action="store_true")

    r = sub.add_parser("recipe", help="run a derived-stat recipe")
    r.add_argument("name", choices=sorted(recipes.RECIPES))
    r.add_argument("--player")
    r.add_argument("--season", type=int)
    r.add_argument("--season-type", default="Regular Season")
    r.add_argument("--stat", default="pts")
    r.add_argument("--threshold", type=int, default=40)
    r.add_argument("--min-games", type=int)
    r.add_argument("--limit", type=int, default=25)

    s = sub.add_parser("serve", help="open the web interface")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--host", default="127.0.0.1",
                   help="bind address; 0.0.0.0 exposes it to your network")
    s.add_argument("--no-browser", action="store_true",
                   help="don't open a browser window automatically")

    sub.add_parser("recipes", help="list available recipes")
    sub.add_parser("stats", help="list available stat names")
    sub.add_parser("info", help="show database contents")

    args = ap.parse_args(argv)

    if args.command == "recipes":
        for name, fn in sorted(recipes.RECIPES.items()):
            doc = (fn.__doc__ or "").strip().split("\n")[0]
            print(f"{name:<22} {doc}")
        return 0

    if args.command == "stats":
        print("counting (rescaled by --per-mode):")
        print("  " + ", ".join(sorted(COUNTING_STATS)))
        print("rate (never rescaled):")
        print("  " + ", ".join(sorted(RATE_STATS)))
        return 0

    if args.command == "serve":
        from .web import serve
        # Create the file and schema if they don't exist, so the UI can show
        # its own "no data yet" state instead of a stack trace.
        db.init_schema(db.connect(args.db))
        serve(port=args.port, db_path=args.db, host=args.host,
              open_browser=not args.no_browser)
        return 0

    conn = db.connect(args.db)
    db.init_schema(conn)

    if args.command == "info":
        counts = db.table_counts(conn)
        for table, n in counts.items():
            print(f"{table:<15} {n:>10,}")
        seasons = conn.execute(
            "SELECT MIN(season), MAX(season), COUNT(DISTINCT season) FROM player_games"
        ).fetchone()
        if seasons[0] is not None:
            print(f"\nseasons: {seasons[0]}-{seasons[1]} ({seasons[2]} ingested)")
        else:
            print("\nno seasons ingested yet — run scripts/backfill.py")
        return 0

    if args.command == "query":
        season_range = None
        if args.season_range:
            lo, hi = args.season_range.split("-")
            season_range = (int(lo), int(hi))

        sq = StatQuery(
            entity=args.entity,
            stats=[s.strip() for s in args.stats.split(",")],
            per_mode=args.per_mode,
            season=args.season,
            season_range=season_range,
            season_type=args.season_type,
            player=args.player,
            team=args.team,
            min_games=args.min_games,
            min_minutes=args.min_minutes,
            group_by_season=not args.career,
            order_by=args.order_by,
            descending=not args.ascending,
            limit=args.limit,
        )
        if args.show_sql:
            from .query import build_sql
            sql, params = build_sql(sq)
            print(sql)
            print("params:", params)
            return 0
        rows = run(conn, sq)

    else:  # recipe
        fn = recipes.RECIPES[args.name]
        kwargs = {"season_type": args.season_type, "limit": args.limit}
        import inspect
        accepted = inspect.signature(fn).parameters
        for key, value in [("player", args.player), ("season", args.season),
                           ("stat", args.stat), ("threshold", args.threshold),
                           ("min_games", args.min_games)]:
            if key in accepted and value is not None:
                kwargs[key] = value
        kwargs = {k: v for k, v in kwargs.items() if k in accepted}
        rows = fn(conn, **kwargs)

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
