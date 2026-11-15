# Stat Merchant

Query completed NBA seasons from a local database. Pull by player, team, year,
per-mode (totals / per-game / per-36 / per-100) and stat — plus derived
"random stats" like double-doubles, 50-point games and single-digit scoring
seasons.

Two ways to ask a question: **aggregated**, for season and career lines that
can be ranked and compared, and the **game finder**, for the individual games
matching a set of conditions — every 50-point game, triple-doubles in a loss,
20 rebounds against one opponent.

Command: `nbastats`. The Python package stays `nbastats` whatever the project
is called, so a rename needs no reinstall.

Not affiliated with the NBA. Team colors stand in for logos, which are
trademarks.

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
pip install -e .

# Pull data (run from your own machine — see the warning below)
python scripts/backfill.py --start 1996 --end 2025

# Open the web interface
nbastats serve
```

`nbastats serve` starts a local server and opens a browser. Two ways in:

- **All players** — every player for the chosen season. Click any column
  header to sort by it, which is how you get leaderboards: sort by PTS for
  scoring leaders, by TD for triple-doubles, by USG% for usage. Sorting runs
  in the database, so the top of the table is the real leader, not the best of
  whichever rows happened to load. Results page 100 at a time, labelled by
  rank range (1–100, 101–200, …); the number of pages follows whatever the
  filters match, so raising the minimum games shrinks it.
- **Search** — type a player's name for their career table and a projection
  for their next game.

Nothing leaves your machine — it binds to `127.0.0.1` and only reads the local
database.

There is a command line too:

```bash
nbastats query --season 2025 --stats pts,reb,ast --limit 10
nbastats recipe double-doubles --season 2025
```

> **Run the backfill locally, not in CI or on a cloud host.** stats.nba.com
> does not respond to many AWS / GCP / Azure / Colab IP ranges, and it throttles
> rapid requests. The client sleeps 1 second between calls for this reason.

A full backfill from 1996 is roughly 60 requests (two per season) and takes a
few minutes. The resulting database is a few hundred MB.

## The web interface

```bash
nbastats serve                      # http://127.0.0.1:8000
nbastats serve --port 9000
nbastats serve --no-browser
```

Built on Python's standard library — no FastAPI, no Node, nothing to install
beyond what the backfill already needed. The page is a single HTML file in
`src/nbastats/static/`, and the JSON API lives in `src/nbastats/web.py`:

| Endpoint | Returns |
|---|---|
| `/api/meta` | seasons, stat names, recipes, row counts |
| `/api/search?q=` | player autocomplete |
| `/api/query?...` | the same filters as the `query` command |
| `/api/finder?...` | individual games matching conditions |
| `/api/compare?p=...` | one aggregated row per player-and-scope |
| `/api/recipe/<name>?...` | a recipe's rows |

It binds to localhost. `--host 0.0.0.0` exposes it to your network, which is
fine on a home network and a bad idea on public wifi — there is no
authentication.

### Columns

The picker sits on the **left** of the controls strip, where reading starts.
It is the control people reach for most, and it used to be last on the row.

Every table leads with the same columns in the same order: **team, then
player, then games played and minutes.** The badge goes before the name
because it is the easiest thing on a row to find by eye. Games played and
minutes follow the name because both say how much of a season a line is
built on, which you want before the averages beside them rather than after.

```
Team  Player  Season  GP  MIN  PTS  REB  AST          leaderboard
Team  Season  GP  MIN  PTS  REB  AST                  one player
Date  Team  Player  Opp  Result  Score  PTS  …        find games
Team  Player  Season  GP  DD  TD                      recipes
```

The comparison table is transposed — one column per player, one row per
stat — so the same order applies turned ninety degrees: the team badge sits
above the name in each header, and the rows run GP, MIN, then the stats. It
is also the one table that does **not** fill the page. Stretched to full
width, two players give two 590px columns with a single number adrift in
each; side by side only works if the sides are close enough to read
together. It sizes to its content instead, with a 155px floor per column.

**Clear all** strips the table back to the identity columns — who, which
team, which season, and games played — so you can add back only what you
care about instead of switching off ten things you didn't ask for. Games
played is selected by the query itself rather than by a chip, so clearing
leaves a legible table rather than nothing. With everything off the button
becomes **Restore defaults**.

This means `stats=` and an absent `stats` are different requests: absent is
"use the defaults", empty is "no stat columns". The server parses the query
string with `keep_blank_values=True` so the distinction survives the round
trip — without it `stats=` vanishes and Clear all silently does nothing.


## Projections

`nbastats serve`, then search a player — a **Next game** panel appears under
their career table. Pick a stat, an opponent, a venue, and optionally type a
line.

It reports a projection, the spread around it, and several over/under
estimates side by side. Seeing them disagree is the point — a single confident
number would be hiding the uncertainty rather than reporting it.

The headline probability is a normal approximation around the projection.
That default was settled by backtesting, and getting there took two wrong
turns worth recording.

**First measurement.** Calibration at a line set on the projection:

| Predicted | Actual |
|---|---|
| 45% | 44.8% |
| 55% | 46.8% |

The 55% bucket missed by 8 points. The cause is skew — scoring has a floor at
zero and a long right tail, so the mean sits above the median and a symmetric
spread overstates the over.

**First fix, which was worse.** Reading the probability off the player's own
game log scored 5.5% weighted miss against the normal method's 4.2%. It spread
predictions from 25% to 65% while the true rate stayed flat near 45% —
confident variation that predicted nothing. Shrinking it heavily helped, and
a constant 45% that looked at nothing at all scored best of everything at 0.8%.

**Which was an artifact of the measurement.** The backtest only ever placed the
line *at* the projection — the one spot where refusing to commit is free.
Scored across lines from −1.5 to +1.5 standard deviations out:

| Method | −1.5 | −0.75 | 0 | +0.75 | +1.5 | mean |
|---|---|---|---|---|---|---|
| normal approximation | 3.2% | 1.7% | 4.2% | 1.5% | 5.3% | **3.2%** |
| empirical, light shrinkage | 3.5% | 4.9% | 5.5% | 4.0% | 4.6% | 4.5% |
| adaptive shrinkage | 5.4% | 7.6% | 3.8% | 5.3% | 5.3% | 5.5% |
| empirical, heavy shrinkage | 18.9% | 10.8% | 3.2% | 12.6% | 18.5% | 12.8% |
| empirical, heavy toward 45% | 22.5% | 13.6% | 2.7% | 10.6% | 15.6% | 13.0% |
| constant 45% | 46.8% | 28.3% | 0.8% | 21.5% | 34.7% | 26.4% |

The constant is the best method at a centered line and the worst by a wide
margin everywhere else. The plain normal approximation wins overall.

So the original flaw is real and still present: near the projection this runs
about 8 points hot on the over, and the panel says so when the line sits
there. Every attempt to fix it made the overall answer worse. The empirical
figure is shown alongside, because where the two disagree the distribution
isn't symmetric.

The lesson is about the metric rather than the model. A measurement taken in
one regime will happily crown a method that fails in every other one.

How the projection is built:

```
rate       = recency-weighted per-minute production
minutes    = recency-weighted minutes
opp_adj    = what the opponent allows / league average
venue_adj  = the player's home or road split, shrunk toward 1 and capped at ±10%
projection = rate × minutes × opp_adj × venue_adj
```

Weights decay 0.94 per game back, so a game 11 back counts about half as much
as the most recent one.

**Only the newest loaded season can be projected.** A finished season has
already happened, so presenting it as a forecast would be nonsense — pick an
older season and the panel says so instead of producing a number. The backtest
overrides this deliberately (`allow_past_seasons=True`), because scoring
against known outcomes is the whole point there.

**What it does not know:** injuries, rest, minutes restrictions, foul trouble,
lineup changes, trades, or anything after the last ingested game. That list is
most of what actually moves a line.

### Checking whether it works

```bash
python scripts/backtest.py --season 2024 --stat pts
```

Walk-forward validation: every projection uses only games played before the
game being scored, opponent strength included. It reports MAE and RMSE against
two simpler baselines (a flat average, and rate × minutes with no opponent
adjustment), the signed bias, and a calibration table.

If the model can't beat a flat average, the script says so in those words. A
projection tool that can't be shown to beat doing nothing is not worth
trusting, and the backtest is how you find out.

Calibration being good means the stated uncertainty is honest. It does **not**
mean the model can beat a priced market — nothing in the backtest is measured
against odds.

### On betting

This tool has no odds feed and makes no picks, by design. Two things worth
knowing if that is why you came:

A sportsbook's line already prices in injury reports, minutes projections,
lineup data and money flow. A box-score model sees none of it, so when your
number disagrees with the market, the base rate heavily favors the market.

And parlays compound the house edge. At −110 per leg:

| Legs | Pays | Implied | Fair | House edge |
|---|---|---|---|---|
| 1 | 1.91× | 52.4% | 50.0% | 4.6% |
| 2 | 3.65× | 27.4% | 25.0% | 8.9% |
| 3 | 6.96× | 14.4% | 12.5% | 13.0% |
| 4 | 13.28× | 7.5% | 6.3% | 17.0% |
| 5 | 25.36× | 3.9% | 3.1% | 20.8% |

A model that is genuinely 2% sharper than the market still loses money on
four-leg parlays. `nbastats.project.parlay_math(legs)` computes this for any
leg count.

## Command line

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



### Comparing players

Click **Compare players** beside the search box, then search to add players to
the tray. Each entry carries its own scope:

- **Whole career** — every game, across every team
- **One team only** — just that stint, e.g. Harden's Rockets years
- **One season** — a single year

The `+` on a chip adds the same player again, so one player's stints can sit
next to each other: Jordan on the Bulls against Jordan on the Wizards, in the
same table. Up to six entries.

Switching to the game finder and back keeps the comparison: looking at
something else is not the same as being done with it. Turning compare off
deliberately does clear it.

The table is transposed for this view: one **column per player**, one **row
per stat**. Side by side is how you actually read a comparison — scanning
down a short column beats scanning across a wide one — so adding a stat here
adds a row, where the normal table would add a column.

Each entry is colored by the team it unambiguously belongs to. A team scope
is obvious; a **career** scope counts too when the player only ever played
for one team — Curry, Dirk, Tatum — since "whole career" and "on the
Warriors" are the same set of games for them. A season spent entirely with
one team gets that color as well. Anything spanning two teams stays neutral,
because no single color would be honest.

The best value in each row is marked. Turnovers and fouls count the other
way, games played isn't marked at all (more games isn't better), and ties are
judged on the number as displayed, so two cells both reading 3.4 are both
marked.

A career row shows which teams the player appeared for; a team-scoped row
leaves that column out, since the scope already names it. An entry that
matches no games stays in the table marked "no games" rather than silently
disappearing.

### Finding games

Click **Find games** beside the search box. Everything else in this project
aggregates — season averages, career totals, team ratings. This does the
opposite: it returns the individual games that satisfy a set of conditions.

> Every 50-point game. Triple-doubles in a loss. 20 rebounds against one
> opponent. 30 points on 70% true shooting.

Conditions are **ANDed and all apply to the same game**, which is the whole
point: 40 points *and* 15 rebounds means one player doing both on one night,
not a player who averages both. Each row carries the game's context — date,
opponent with `vs` or `@`, result, final score — alongside the stats.

**Twelve named searches** in three groups are the front door: 50-point games,
triple-doubles, 20-assist games, 5 of everything. Each card says in one plain
line what it finds, on its face rather than behind a hover. Dropdowns for
team, opponent, home/road and win/loss narrow any of them.

**"Build your own search"** is folded away underneath, for the question no
card covers. It is the only part of this view written in stats rather than in
sentences, and most people never need to open it.

The heading is the contract: it names every condition *and* every filter that
ran — "Games with 40+ points and 10+ rebounds against LAL in a loss" — so the
table can never quietly be narrower than it looks.

#### Design notes

- **Nothing in this view is written in operators.** The builder reads
  `Points · at least · 50`, and the heading says "50+ points". `cats >= 3`
  becomes "a triple-double", because that is the word people already have for
  it.
- **Percentages are typed as percentages.** The box takes `60` and shows a `%`
  inside the field; the 0–1 share the database wants is produced on the way
  out. Asking for `0.6` is the single most confusing thing about filtering on
  a rate, and typing `60` by mistake matches no game at all — a filter that
  looks broken rather than wrong. Changing a condition from a count to a rate
  clears the number instead of carrying it across.
- **Pair a rate with a volume condition.** A 1-for-1 game is 100% and means
  nothing. 60% field goal shooting plus 20 attempts is the useful form.
- **A stat you filter on becomes a column**, even if you didn't ask for it, so
  a result filtered on three-pointers shows the three-pointers.
- **Season-level metrics leave the Columns picker here** rather than sitting
  struck through. Usage rate, rebound share and the double-double *count* need
  a whole season of team totals, so they have no honest single-game value and
  are not part of this view's vocabulary. If one was already switched on, a
  line under the picker says where it went.
- **The Columns picker itself folds shut** in this view. You came looking for
  games, and two dozen abbreviations is the wrong thing to meet on the way.
  Clear all works here too: with no stat columns a row is just the game —
  date, teams, result, score — plus whatever you filtered on.

Two fields exist only here:

| Field | Shown as | Meaning |
|---|---|---|
| `cats` | Categories in double figures | how many of points, rebounds, assists, steals and blocks reached 10. Two is a double-double, three a triple-double |
| `margin` | Won or lost by | the points the game was decided by, from the player's side: positive in a win, negative in a loss |

From the command line the same thing is one call:

```python
from nbastats import db, finder

conn = db.connect("data/nba.db")
finder.find(conn, conditions=finder.parse_conditions("pts>=50"), season=2023)
finder.find(conn, conditions=[("cats", ">=", 3)], result="L")
```

Condition text never reaches SQL. The field is looked up in an allowlist, the
operator is mapped through a second one, and the value is bound as a
parameter — so a malformed condition is rejected before a query is built
rather than escaped on the way in.


## Advanced stats

The Columns picker splits into **Regular** (box-score counting stats and
shooting percentages) and **Advanced**:

| Stat | What it is |
|---|---|
| USG% | share of team plays used while on the floor |
| TS% | points per shooting possession, counting threes and free throws |
| eFG% | field goal percentage with threes weighted 1.5 |
| AST% | share of teammates' made field goals assisted |
| AST/poss | assists per 100 possessions the player used |
| REB% / OREB% / DREB% | share of available rebounds grabbed while on the floor |
| STL% | steals per 100 opponent possessions |
| BLK% | share of opponent two-point attempts blocked |
| TOV% | turnovers per 100 possessions used |

Two things about how these are computed here:

**Team totals come from the games the player actually played**, not the team's
full season. Basketball Reference uses full-season team totals, so a player who
missed 30 games will show a slightly different number here. This version
describes the games he was in, which is usually what you want from a rate stat.

**The on-court shares use minutes as a proxy.** Box scores don't record who
else was on the floor, so `(team minutes / 5) / player minutes` stands in for
it. That's the standard public approximation, and it's still an approximation —
a player sharing the floor with a dominant rebounder gets no credit for that
context.

### What is deliberately missing

**BPM, VORP, Win Shares, and individual ORtg/DRtg are not here**, and not
because they were forgotten:

- **BPM and VORP** need regression coefficients fit on play-by-play data. The
  published formula is a fitted model, not an identity; you cannot derive it
  from box scores.
- **Win Shares** needs league-wide marginal points per win and team possession
  estimates for every team-season.
- **Individual ORtg/DRtg** need Dean Oliver's full possession-attribution
  model — roughly 20 interlocking formulas assigning credit for scoring
  possessions across teammates.

Each could be approximated from this data, but a wrong number that looks
authoritative is worse than an absent one, and none of them could be validated
against a reference here. **Team ORtg and DRtg are real** and available through
the `team-ratings` recipe, since those are straightforward points per 100
possessions.

If you want the individual versions, Oliver's formulas are public and this
schema has the team box scores they need. Implementing them is a weekend's work
plus careful spot-checking against Basketball Reference.

## Per-mode math

Stated explicitly because this is where homemade stat sites usually go wrong.

| Mode | Formula |
|---|---|
| `totals` | raw sums |
| `per_game` | total ÷ games played |
| `per36` | total ÷ minutes × 36 |
| `per100` | total ÷ team possessions while on the floor × 100 |

Per-100 means per 100 **team** possessions while the player was on the floor,
not per 100 of the possessions he personally used. Team possessions are
estimated as `tm.fga - tm.oreb + tm.tov + 0.44 × tm.fta` and scaled by the
player's share of floor time (`player minutes ÷ (team minutes ÷ 5)`).

So a starter on a 100-possession team playing 36 of 48 minutes is credited
with about 75 possessions, and 25 points becomes roughly 33 per 100 — the
range real per-100 figures live in.

The possession count is an **estimate**, not an official NBA figure, so these
will differ slightly from Basketball Reference, which averages a team-level
estimate with the opponent's.

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
  query.py                StatQuery -> SQL (the filter engine, aggregated)
  finder.py               GameQuery -> SQL (game finder, one row per game)
  recipes.py              derived stats (double-doubles, thresholds, ratings)
  cli.py                  command line interface
  project.py              prop projections, odds math, de-vigging
  web.py                  local JSON API (standard library only)
  static/index.html       the web interface, one self-contained file
scripts/backfill.py       resumable multi-season ingest
scripts/backtest.py       walk-forward validation of the projection model
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
