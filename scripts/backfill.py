#!/usr/bin/env python3
"""Backfill completed NBA seasons into the local database.

Run this from your own machine, not a cloud host — stats.nba.com does not
respond to many AWS/GCP/Azure IP ranges.

    python scripts/backfill.py --start 1996 --end 2024
    python scripts/backfill.py --start 2023 --end 2024 --season-type Playoffs
    python scripts/backfill.py --status

Safe to interrupt and re-run: completed seasons are skipped.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nbastats import db, ingest  # noqa: E402

# Box-score data on NBA.com is reliable from 1996-97 onward; earlier seasons
# have sparse or missing fields.
EARLIEST_RELIABLE_SEASON = 1996


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", type=int, default=EARLIEST_RELIABLE_SEASON,
                    help="first season (2015 = the 2015-16 season)")
    ap.add_argument("--end", type=int, default=2024, help="last season, inclusive")
    ap.add_argument("--season-type", default="Regular Season",
                    choices=["Regular Season", "Playoffs", "Pre Season"])
    ap.add_argument("--db", default=None, help="path to the SQLite file")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest seasons already marked complete")
    ap.add_argument("--status", action="store_true",
                    help="print ingest status and exit")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s  %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")

    conn = db.connect(args.db)
    db.init_schema(conn)

    if args.status:
        rows = ingest.ingest_status(conn)
        if not rows:
            print("nothing ingested yet")
        for r in rows:
            print(f"{r['season']}  {r['season_type']:<15} {r['status']:<9} "
                  f"player_rows={r['rows_player'] or '-'}  {r['error'] or ''}")
        print("\ntable counts:", db.table_counts(conn))
        return 0

    if args.start < EARLIEST_RELIABLE_SEASON:
        logging.warning("seasons before %d have incomplete box scores",
                        EARLIEST_RELIABLE_SEASON)

    ingest.load_reference(conn)

    results = []
    for season in range(args.start, args.end + 1):
        results.append(
            ingest.ingest_season(conn, season, args.season_type, force=args.force)
        )

    ok = [r for r in results if r["status"] == "complete"]
    skipped = [r for r in results if r["status"] == "skipped"]
    failed = [r for r in results if r["status"] == "failed"]

    print(f"\ningested {len(ok)}, skipped {len(skipped)}, failed {len(failed)}")
    for r in failed:
        print(f"  FAILED {r['season']}: {r['error']}")
    print("table counts:", db.table_counts(conn))

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
