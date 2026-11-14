# nba-stats

Query completed NBA seasons from a local database. Pull by player, team, year,
per-mode (totals / per-game / per-36 / per-100) and stat — plus derived
"random stats" like double-doubles, 50-point games and single-digit scoring
seasons.

## The core idea

Every number in this project is computed from **one table**: `player_games`,
one row per player per game.

That matters because questions like *"how many double-doubles did he have?"* or
*"which players averaged under 10 points over a full season?"* cannot be
answered from season averages. A double-double is a property of an individual
game; season-average tables have already thrown that information away. So we
store the game-level grain and aggregate upward at query time. Season totals,
per-36 conversions and exotic filters all fall out of the same source.

Completed seasons never change, which means the database is write-once per
season and read-many forever. There is no reason to hit the API at query time.

## Quick start

```bash
git clone <your-repo-url> && cd nba-stats
pip install -r requirements.txt

# Pull data (run from your own machine — see the warning below)
python scripts/backfill.py --start 2015 --end 2024

# Query it
python -m nbastats.cli query --season 2023 --stats pts,reb,ast --limit 10
python -m nbastats.cli recipe double-doubles --season 2023
```

> **Run the backfill locally, not in CI or on a cloud host.** stats.nba.com
> does not respond to many AWS / GCP / Azure / Colab IP ranges, and it throttles
> rapid requests. The client sleeps 1 second between calls for this reason.

A full backfill from 1996 is roughly 60 requests (two per season) and takes a
few minutes. The resulting database is a few hundred MB.

## Usage

### Query

```bash
# Scoring leaders, 2023-24
nbastats query --season 2023 --stats pts --min-games 58 --limit 10

# One player across a range of seasons, per 36 minutes
nbastats query --player "stephen curry" --season-range 2015-2020 \
               --stats pts,fg3m,fg3_pct --per-mode per36

# Career totals instead of a row per season
nbastats query --player "lebron james" --stats pts,reb,ast \
               --per-mode totals --career

# Efficiency, per 100 possessions
nbastats query --season 2023 --stats pts,ts_pct --per-mode per100 --min-minutes 1000

# See the generated SQL instead of running it
nbastats query --player curry --show-sql
```

From Python:

```python
import nbastats

conn = nbastats.connect()
rows = nbastats.query(conn, player="jokic", season=2022,
                      stats=["pts", "reb", "ast"], per_mode="per_game")
```

### Recipes (derived stats)

```bash
nbastats recipes                                          # list them
nbastats recipe double-doubles --season 2023
nbastats recipe triple-doubles --player "russell westbrook"
nbastats recipe threshold-games --stat pts --threshold 50
nbastats recipe threshold-games --stat reb --threshold 20
nbastats recipe single-digit-scoring --min-games 58
nbastats recipe averages-club                             # 20/10/5 seasons
nbastats recipe team-ratings --season 2023
nbastats recipe team-records --season 2023
```

## Per-mode math

Stated explicitly because this is where homemade stat sites usually go wrong.

| Mode | Formula |
|---|---|
| `totals` | raw sums |
| `per_game` | total ÷ games played |
| `per36` | total ÷ minutes × 36 |
| `per100` | total ÷ possessions × 100 |

Possessions are **estimated** as `fga - oreb + tov + 0.44 × fta`. This is the
standard approximation, not an official NBA figure, so per-100 numbers will
differ slightly from Basketball Reference, which averages a team-level estimate
with the opponent's.

**Rate stats are never rescaled.** `fg_pct` is the same number whether you ask
for totals or per-36, and it is computed from summed components
(`SUM(fgm) / SUM(fga)`) rather than by averaging per-game percentages. Those
two give different answers, and the averaged version is wrong.

## Adding a stat

Counting or rate stat — one line in `src/nbastats/query.py`:

```python
COUNTING_STATS["pts_in_paint"] = "SUM(pg.pts_in_paint)"
```

New derived question — one function in `src/nbastats/recipes.py` plus an entry
in the `RECIPES` registry, and a test. The query engine does not need to change.

## Project layout

```
sql/schema.sql            tables, indexes, views — the data model, readable on its own
src/nbastats/
  db.py                   connections, schema init
  client.py               nba_api wrapper: rate limiting, retries, raw JSON cache
  ingest.py               parsers: game logs -> normalized tables
  query.py                StatQuery -> SQL (the filter engine)
  recipes.py              derived stats (double-doubles, thresholds, ratings)
  cli.py                  command line interface
scripts/backfill.py       resumable multi-season ingest
tests/                    runs against a synthetic fixture DB, never the live API
```

## Design decisions worth knowing

**Raw JSON is cached to `data/raw/` before parsing.** When a parser bug turns
up, re-parsing is a local operation instead of a multi-season re-pull.

**The backfill is idempotent and resumable.** An `ingest_log` table records
which seasons completed. Re-running skips them, so a crash at season 18 costs
you one season, not seventeen.

**Percentages are not stored, only computed.** Storing `fg_pct` per game invites
the bug of averaging percentages across games.

**The database file is gitignored.** Ship the backfill script, not a few hundred
MB of data. The repo stays clonable.

**Tests never touch the network.** `tests/conftest.py` builds a three-player,
two-season fixture where every expected value is hand-checkable. The live API is
slow, rate-limited and changes without notice; tests that depend on it fail for
reasons unrelated to your code.

**Defunct franchises and unknown players are backfilled as stubs.** The Sonics
appear in 2007 game logs but not in nba_api's static team list. Rather than
letting one missing row fail an entire season's insert, the ingest inserts a
placeholder and keeps going.

## Known limitations

- **Advanced stats (BPM, VORP, Win Shares) are not included.** Those are
  Basketball Reference formulas, not NBA.com stats. They need either
  reimplementation from components or a separate scraper — and BBRef's terms
  and rate limits apply if you go that route.
- **Box-score data is reliable from 1996-97 onward.** Earlier seasons have
  sparse or missing fields on NBA.com.
- **Lockout seasons break games-played assumptions.** 1998-99 was 50 games,
  2011-12 was 66, 2019-20 and 2020-21 were shortened. Recipes that default to
  `min_games=58` will behave oddly in those years. Pass an explicit value.
- **Traded players produce one row per team-season** unless you aggregate.
- **NBA.com's endpoints are undocumented** and change without announcement.
  When something breaks, check the nba_api issue tracker first.

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```

## Data source and terms

Data comes from stats.nba.com via [nba_api](https://github.com/swar/nba_api)
(MIT licensed). NBA.com has its own
[Terms of Use](https://www.nba.com/termsofuse) governing its digital platforms;
review them before deploying this publicly or redistributing the data.
