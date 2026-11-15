"""Game finder: one row per game, where the rest of this project aggregates.

Everything else here collapses games into averages — season leaderboards,
career totals, team ratings. This module does the opposite. It hands back the
individual games that satisfy a set of conditions:

    every 50-point game ever
    triple-doubles in a loss
    LeBron's 20-rebound games against Boston
    games shooting 60%+ on at least 20 attempts

This is the one question shape the aggregate query layer genuinely cannot
answer, which is why it is a separate module rather than another flag on
StatQuery: there is no GROUP BY, the filters apply to single rows instead of
to HAVING, and the output columns are game context (date, opponent, result)
rather than season context.

Safety: conditions arrive from the front end as text like "pts>=50". Field
names are looked up in GAME_FIELDS and operators in OPERATORS, so anything
unrecognised raises before touching SQL, and the compared value is always a
bound parameter. There is no string path from user input into the query.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .query import _CAT_COUNT

# Points / rebounds / assists / steals / blocks at 10 or better, counted. Two
# of them is a double-double, three a triple-double. Reusing query.py's
# expression rather than restating it keeps one definition of the concept.
CATEGORY_COUNT = f"({_CAT_COUNT})"

# The margin a game was decided by, from the player's team's point of view:
# positive in a win, negative in a loss.
MARGIN = "(tm.pts - og.pts)"

# Everything a condition can test and a column can show. All row-level — no
# aggregates — so each one is valid in SELECT, WHERE and ORDER BY alike.
GAME_FIELDS: dict[str, str] = {
    "pts": "pg.pts",
    "reb": "pg.reb",
    "oreb": "pg.oreb",
    "dreb": "pg.dreb",
    "ast": "pg.ast",
    "stl": "pg.stl",
    "blk": "pg.blk",
    "tov": "pg.tov",
    "pf": "pg.pf",
    "fgm": "pg.fgm",
    "fga": "pg.fga",
    "fg3m": "pg.fg3m",
    "fg3a": "pg.fg3a",
    "ftm": "pg.ftm",
    "fta": "pg.fta",
    "min": "pg.min",
    "plus_minus": "pg.plus_minus",
    # Shooting rates for a single game. Legitimate to filter on, but pair them
    # with a volume condition: 1-for-1 is 100% and means nothing.
    "fg_pct": "(CAST(pg.fgm AS REAL) / NULLIF(pg.fga, 0))",
    "fg3_pct": "(CAST(pg.fg3m AS REAL) / NULLIF(pg.fg3a, 0))",
    "ft_pct": "(CAST(pg.ftm AS REAL) / NULLIF(pg.fta, 0))",
    "ts_pct": ("(CAST(pg.pts AS REAL) / "
               "NULLIF(2 * (pg.fga + 0.44 * pg.fta), 0))"),
    "efg_pct": ("(CAST(pg.fgm + 0.5 * pg.fg3m AS REAL) / "
                "NULLIF(pg.fga, 0))"),
    # Derived context.
    "cats": CATEGORY_COUNT,
    "margin": MARGIN,
}

# Fields whose value is a share, not a count. The front end formats these
# differently and a condition on them takes 0.6, not 60.
RATE_FIELDS = {"fg_pct", "fg3_pct", "ft_pct", "ts_pct", "efg_pct"}

# Columns always present in a result, so always valid to sort by. Mapped to
# qualified expressions because ORDER BY does not reliably resolve an output
# alias ahead of a source column: `season` exists on player_games and on both
# joined team_games rows, and SQLite rejects the whole query as ambiguous
# rather than preferring the alias. Ordering by the qualified expression has
# no such problem.
CONTEXT_COLUMNS = {
    "game_date": "pg.game_date",
    "player": "p.full_name",
    "team": "t.abbreviation",
    "opp": "ot.abbreviation",
    "wl": "pg.wl",
    "season": "pg.season",
}

# Mapped rather than passed through, so only these six strings can ever reach
# the SQL, and "==" / "!=" from a URL land on valid SQLite operators.
OPERATORS = {
    ">=": ">=", ">": ">", "=": "=", "==": "=",
    "<=": "<=", "<": "<", "!=": "<>", "<>": "<>",
}

# Longest first, so ">=" is recognised before ">".
_OP_TOKENS = sorted(OPERATORS, key=len, reverse=True)

DEFAULT_STATS = ["pts", "reb", "ast"]

# A game log reads newest first, the way a box-score archive does. Sorting by
# a stat is one header click away.
DEFAULT_ORDER = "game_date"

MAX_CONDITIONS = 6


def parse_conditions(raw: str | None) -> list[tuple[str, str, float]]:
    """Parse "pts>=50,fga<=20" into [(field, op, value), …].

    Rejects unknown fields and operators here, before anything is built, so a
    typo produces a clear message instead of a SQL error.
    """
    if not raw or not raw.strip():
        return []

    out: list[tuple[str, str, float]] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue

        left = sep = right = ""
        for token in _OP_TOKENS:
            left, sep, right = chunk.partition(token)
            if sep:
                break
        if not sep:
            raise ValueError(
                f"{chunk!r} has no comparison in it. Write something like "
                f"pts>=50."
            )

        field_name = left.strip().lower()
        if field_name not in GAME_FIELDS:
            raise ValueError(
                f"can't filter on {field_name!r}. available: "
                f"{', '.join(sorted(GAME_FIELDS))}"
            )
        try:
            value = float(right.strip())
        except ValueError:
            raise ValueError(
                f"{right.strip()!r} isn't a number, so {chunk!r} can't be "
                f"compared."
            ) from None

        out.append((field_name, OPERATORS[sep], value))

    if len(out) > MAX_CONDITIONS:
        raise ValueError(
            f"that's {len(out)} conditions; {MAX_CONDITIONS} is the limit"
        )
    return out


def describe_conditions(conditions: list[tuple[str, str, float]]) -> str:
    """"pts >= 50 and reb >= 10" — for a heading, and to echo back what ran."""
    pretty = {"<>": "≠", ">=": "≥", "<=": "≤"}
    parts = []
    for name, op, value in conditions:
        shown = f"{value:g}"
        parts.append(f"{name} {pretty.get(op, op)} {shown}")
    return " and ".join(parts)


@dataclass
class GameQuery:
    """A declarative game-level filter. Same discipline as StatQuery: callers
    describe what they want and never assemble SQL themselves."""

    stats: list[str] = field(default_factory=lambda: list(DEFAULT_STATS))
    conditions: list[tuple[str, str, float]] = field(default_factory=list)
    season: int | None = None
    season_range: tuple[int, int] | None = None
    season_type: str = "Regular Season"
    player: str | None = None           # name substring, case-insensitive
    player_ids: list[int] | None = None
    team: str | None = None             # the player's team, e.g. 'LAL'
    opponent: str | None = None         # who they played
    venue: str | None = None            # 'home' | 'away'
    result: str | None = None           # 'W' | 'L'
    date_from: str | None = None        # ISO YYYY-MM-DD, inclusive
    date_to: str | None = None
    order_by: str = DEFAULT_ORDER
    descending: bool = True
    limit: int = 100
    offset: int = 0


def shown_columns(q: GameQuery) -> list[str]:
    """The stat columns this query will actually return, in order.

    Not the same as q.stats: a field used in a condition is added, so a query
    filtered on three-pointers shows the three-pointers. The front end needs
    this to build its header before the rows arrive.
    """
    return _validate(q)


def _validate(q: GameQuery) -> list[str]:
    unknown = [s for s in q.stats if s not in GAME_FIELDS]
    if unknown:
        raise ValueError(
            f"unknown column(s): {unknown}. available: "
            f"{', '.join(sorted(GAME_FIELDS))}"
        )
    # The fields a condition tests are worth seeing. Filtering on three-pointers
    # and then not showing them makes the result look arbitrary.
    shown = list(q.stats)
    for name, _, _ in q.conditions:
        if name not in shown and name in GAME_FIELDS:
            shown.append(name)
    # An empty list is allowed: every row still carries the game context
    # (date, teams, result, score), which is a legible result on its own and
    # something to add columns back onto.
    return shown


def _where(q: GameQuery) -> tuple[list[str], list]:
    where: list[str] = ["pg.season_type = ?"]
    params: list = [q.season_type]

    if q.season is not None:
        where.append("pg.season = ?")
        params.append(q.season)
    elif q.season_range is not None:
        where.append("pg.season BETWEEN ? AND ?")
        params += [q.season_range[0], q.season_range[1]]

    if q.player:
        where.append("LOWER(p.full_name) LIKE ?")
        params.append(f"%{q.player.lower()}%")

    if q.player_ids:
        where.append(f"pg.player_id IN ({','.join('?' * len(q.player_ids))})")
        params += list(q.player_ids)

    if q.team:
        where.append("t.abbreviation = ?")
        params.append(q.team.upper())

    if q.opponent:
        where.append("ot.abbreviation = ?")
        params.append(q.opponent.upper())

    if q.venue:
        if q.venue not in ("home", "away"):
            raise ValueError("venue must be 'home' or 'away'")
        where.append("tm.is_home = ?")
        params.append(1 if q.venue == "home" else 0)

    if q.result:
        if q.result.upper() not in ("W", "L"):
            raise ValueError("result must be 'W' or 'L'")
        where.append("pg.wl = ?")
        params.append(q.result.upper())

    if q.date_from:
        where.append("pg.game_date >= ?")
        params.append(q.date_from)
    if q.date_to:
        where.append("pg.game_date <= ?")
        params.append(q.date_to)

    for name, op, value in q.conditions:
        # name and op are both allowlisted; only the value is interpolated,
        # and it goes in as a bound parameter.
        where.append(f"{GAME_FIELDS[name]} {op} ?")
        params.append(value)

    return where, params


# The player's own team box score and the opponent's, for venue, result margin
# and the final score. LEFT so that a player-game with a missing team row still
# appears — an absent score is better than a silently dropped game.
_JOINS = """
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        JOIN teams   t ON t.team_id   = pg.team_id
        LEFT JOIN team_games tm
               ON tm.game_id = pg.game_id AND tm.team_id = pg.team_id
        LEFT JOIN teams ot ON ot.team_id = tm.opp_team_id
        LEFT JOIN team_games og
               ON og.game_id = pg.game_id AND og.team_id = tm.opp_team_id
"""


def build_sql(q: GameQuery) -> tuple[str, list]:
    """Turn a GameQuery into (sql, params). Exposed for inspection, the same
    way build_sql is in query.py — when a row looks wrong, read the SQL."""
    shown = _validate(q)
    where, params = _where(q)

    select = [
        "pg.game_id",
        "pg.game_date AS game_date",
        "pg.season AS season",
        "p.full_name AS player",
        "t.abbreviation AS team",
        # Reads the way a schedule does: "vs BOS" at home, "@ BOS" away.
        "CASE WHEN tm.is_home = 1 THEN 'vs ' ELSE '@ ' END "
        "|| COALESCE(ot.abbreviation, '?') AS opp",
        "pg.wl AS wl",
        # The game's final score from this player's side, e.g. "120-118".
        "CASE WHEN tm.pts IS NULL OR og.pts IS NULL THEN NULL "
        "ELSE tm.pts || '-' || og.pts END AS score",
    ]
    for name in shown:
        expr = GAME_FIELDS[name]
        if name in RATE_FIELDS:
            select.append(f"ROUND({expr}, 4) AS {name}")
        elif name == "min":
            select.append(f"ROUND({expr}, 1) AS {name}")
        else:
            select.append(f"{expr} AS {name}")

    # Sorting by a column that isn't shown is a SQL error, so fall back to the
    # default rather than failing the query — the same forgiveness the
    # aggregate layer gives when the UI drops the column it was ordering by.
    sortable = set(shown) | set(CONTEXT_COLUMNS)
    order_key = q.order_by if q.order_by in sortable else DEFAULT_ORDER
    order_sql = CONTEXT_COLUMNS.get(order_key) or GAME_FIELDS[order_key]
    direction = "DESC" if q.descending else "ASC"

    # Without a unique tiebreaker, rows that tie on the sort key can come back
    # in a different order on each page, so paging would repeat and skip games.
    # game_id makes the ordering total and therefore the paging correct.
    sql = f"""
        SELECT {", ".join(select)}
        {_JOINS.strip()}
        WHERE {" AND ".join(where)}
        ORDER BY {order_sql} {direction} NULLS LAST, pg.game_date DESC,
                 pg.game_id DESC
        LIMIT ? OFFSET ?
    """
    return sql, params + [q.limit, max(0, q.offset)]


def build_count_sql(q: GameQuery) -> tuple[str, list]:
    """How many games match, ignoring the limit — so the table can say "100 of
    1,284" and page through the rest."""
    _validate(q)
    where, params = _where(q)
    sql = f"""
        SELECT COUNT(*) AS total
        {_JOINS.strip()}
        WHERE {" AND ".join(where)}
    """
    return sql, params


def count(conn: sqlite3.Connection, q: GameQuery) -> int:
    sql, params = build_count_sql(q)
    return conn.execute(sql, params).fetchone()[0]


def run(conn: sqlite3.Connection, q: GameQuery) -> list[dict]:
    sql, params = build_sql(q)
    return [dict(r) for r in conn.execute(sql, params)]


def find(conn: sqlite3.Connection, **kwargs) -> list[dict]:
    """find(conn, conditions=[('pts', '>=', 50)], season=2023)."""
    return run(conn, GameQuery(**kwargs))


# ---------------------------------------------------------------------------
# Presets
#
# These carry the view. Someone who follows basketball knows what a 50-point
# game is and does not know what `pts>=50` is, so the named searches are the
# front door and the condition builder is the thing you open only if none of
# them is the question you had.
#
# Each entry: label (what the button says), help (one plain line under it, no
# jargon and no abbreviations), group (which heading it sits under),
# conditions, and optionally a pinned filter and a sort.
#
# GROUPS is ordered, and the UI renders in this order.
# ---------------------------------------------------------------------------

GROUPS = ["Big nights", "Doing everything", "One specialty"]

PRESETS: dict[str, dict] = {
    # --- Big nights ---------------------------------------------------------
    "50-point-games": {
        "label": "50-point games",
        "group": "Big nights",
        "conditions": "pts>=50",
        "order_by": "pts",
        "help": "The 50-burgers. About 30 happen in a typical decade.",
    },
    "40-point-games": {
        "label": "40-point games",
        "group": "Big nights",
        "conditions": "pts>=40",
        "order_by": "pts",
        "help": "A big scoring night, without being once-a-season rare.",
    },
    "ten-threes": {
        "label": "10 threes in a game",
        "group": "Big nights",
        "conditions": "fg3m>=10",
        "order_by": "fg3m",
        "help": "Almost nobody did this before 2015. Now it happens most "
                "weeks.",
    },
    "40-in-a-loss": {
        "label": "40 points in a loss",
        "group": "Big nights",
        "conditions": "pts>=40",
        "result": "L",
        "order_by": "pts",
        "help": "Huge games that still lost — the case against judging a "
                "player by one box score.",
    },

    # --- Doing everything ---------------------------------------------------
    "triple-doubles": {
        "label": "Triple-doubles",
        "group": "Doing everything",
        "conditions": "cats>=3",
        "order_by": "game_date",
        "help": "Double figures in three categories in the same game.",
    },
    "double-doubles": {
        "label": "Double-doubles",
        "group": "Doing everything",
        "conditions": "cats>=2",
        "order_by": "game_date",
        "help": "Double figures in two categories — usually points and "
                "rebounds.",
    },
    "30-10-10": {
        "label": "30 / 10 / 10",
        "group": "Doing everything",
        "conditions": "pts>=30,reb>=10,ast>=10",
        "order_by": "pts",
        "help": "A triple-double with 30 points on top of it.",
    },
    "5x5": {
        "label": "5 of everything",
        "group": "Doing everything",
        "conditions": "pts>=5,reb>=5,ast>=5,stl>=5,blk>=5",
        "order_by": "game_date",
        "help": "Five or more points, rebounds, assists, steals and blocks. "
                "Rarer than a triple-double.",
    },

    # --- One specialty ------------------------------------------------------
    "20-rebound-games": {
        "label": "20-rebound games",
        "group": "One specialty",
        "conditions": "reb>=20",
        "order_by": "reb",
        "help": "A dominant night on the glass.",
    },
    "20-assist-games": {
        "label": "20-assist games",
        "group": "One specialty",
        "conditions": "ast>=20",
        "order_by": "ast",
        "help": "Setting up twenty baskets in one game.",
    },
    "efficient-30": {
        "label": "30 points, barely missing",
        "group": "One specialty",
        "conditions": "pts>=30,ts_pct>=0.7",
        "order_by": "ts_pct",
        "help": "Scoring 30 at 70% true shooting: high volume without the "
                "bricks.",
    },
    "defensive-nights": {
        "label": "5 steals and blocks",
        "group": "One specialty",
        "conditions": "stl>=5,blk>=5",
        "order_by": "game_date",
        "help": "Five steals and five blocks in the same game. Very few "
                "players ever do it.",
    },
}
