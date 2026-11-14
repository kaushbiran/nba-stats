"""The query layer: filters in, rows out.

One builder handles the whole "player / team / year / per-mode / stat" matrix
from your spec, instead of a function per question. Everything is computed from
player_games at query time, so adding a stat means adding one line to a dict.

Per-mode math, stated once so it is auditable:
    totals   : raw sums
    per_game : total / games_played
    per36    : total / minutes * 36
    per100   : total / possessions * 100, where possessions are ESTIMATED as
               fga - oreb + tov + 0.44 * fta
               This is the standard approximation; it is not an official NBA
               number, and per-100 figures will differ slightly from
               basketball-reference, which uses a team-level possession
               estimate averaged with the opponent's.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Literal

PerMode = Literal["totals", "per_game", "per36", "per100"]

# Counting stats that can be summed across games and then rescaled.
COUNTING_STATS = {
    "pts": "SUM(pg.pts)",
    "reb": "SUM(pg.reb)",
    "oreb": "SUM(pg.oreb)",
    "dreb": "SUM(pg.dreb)",
    "ast": "SUM(pg.ast)",
    "stl": "SUM(pg.stl)",
    "blk": "SUM(pg.blk)",
    "tov": "SUM(pg.tov)",
    "pf": "SUM(pg.pf)",
    "fgm": "SUM(pg.fgm)",
    "fga": "SUM(pg.fga)",
    "fg3m": "SUM(pg.fg3m)",
    "fg3a": "SUM(pg.fg3a)",
    "ftm": "SUM(pg.ftm)",
    "fta": "SUM(pg.fta)",
    "min": "SUM(pg.min)",
    "plus_minus": "SUM(pg.plus_minus)",
}

# Rate stats. These are NEVER rescaled by per-mode — a field goal percentage
# is the same number whether you asked for totals or per-36. Computed from
# summed components, not averaged per-game, which is the usual off-by-a-little
# bug in homemade stat sites.
RATE_STATS = {
    "fg_pct": "CAST(SUM(pg.fgm) AS REAL) / NULLIF(SUM(pg.fga), 0)",
    "fg3_pct": "CAST(SUM(pg.fg3m) AS REAL) / NULLIF(SUM(pg.fg3a), 0)",
    "ft_pct": "CAST(SUM(pg.ftm) AS REAL) / NULLIF(SUM(pg.fta), 0)",
    "ts_pct": ("CAST(SUM(pg.pts) AS REAL) / "
               "NULLIF(2 * (SUM(pg.fga) + 0.44 * SUM(pg.fta)), 0)"),
    "efg_pct": ("CAST(SUM(pg.fgm) + 0.5 * SUM(pg.fg3m) AS REAL) / "
                "NULLIF(SUM(pg.fga), 0)"),
}

# ---------------------------------------------------------------------------
# Advanced stats
#
# These need team and opponent context, not just the player's own line, so the
# query joins the player's team box score (tm) and the opponent's (op) for each
# game. Like rate stats they are never rescaled by per-mode.
#
# Two things to know about how these are computed here:
#
# 1. Team totals come from THE GAMES THE PLAYER ACTUALLY PLAYED, not the team's
#    whole season. Basketball Reference uses full-season team totals. For a
#    player who missed 30 games, this version describes the games he was in,
#    which is usually what you want from a rate stat; it will differ slightly
#    from BBRef's published figure.
#
# 2. The on-court approximations use minutes share — (team minutes / 5) / player
#    minutes — because box scores don't record who else was on the floor. This
#    is the standard public approximation and it is an approximation: a player
#    who shares the floor with a dominant rebounder gets no credit for that
#    context.
#
# Deliberately NOT included: BPM, VORP, Win Shares, and individual ORtg/DRtg.
# BPM and VORP need regression coefficients fit on play-by-play data that this
# database doesn't have. Win Shares needs league-wide marginal points per win,
# and individual ORtg/DRtg need Dean Oliver's full possession-attribution
# model. Each could be approximated badly from box scores; a wrong number that
# looks authoritative is worse than an absent one. Team ORtg/DRtg are real and
# available through the team-ratings recipe.
# ---------------------------------------------------------------------------

# Team minutes / 5 = the "team possessions on floor" denominator basis.
_TM_MP5 = "(SUM(tm.min) / 5.0)"
_PLAYER_MP = "NULLIF(SUM(pg.min), 0)"
_OPP_POSS = "(SUM(op.fga) - SUM(op.oreb) + SUM(op.tov) + 0.44 * SUM(op.fta))"

ADVANCED_STATS = {
    # Share of team plays a player used while on the floor.
    "usg_pct": (
        f"100.0 * ((SUM(pg.fga) + 0.44 * SUM(pg.fta) + SUM(pg.tov)) * {_TM_MP5}) "
        f"/ NULLIF({_PLAYER_MP} * (SUM(tm.fga) + 0.44 * SUM(tm.fta) + SUM(tm.tov)), 0)"
    ),
    # Share of teammates' made field goals a player assisted while on the floor.
    "ast_pct": (
        f"100.0 * SUM(pg.ast) / NULLIF((({_PLAYER_MP} / {_TM_MP5}) * SUM(tm.fgm)) "
        f"- SUM(pg.fgm), 0)"
    ),
    # Assists as a share of a player's own possessions used.
    "ast_ratio": (
        "100.0 * SUM(pg.ast) / NULLIF(SUM(pg.fga) + 0.44 * SUM(pg.fta) "
        "+ SUM(pg.ast) + SUM(pg.tov), 0)"
    ),
    # Share of available rebounds grabbed while on the floor.
    "dreb_pct": (
        f"100.0 * (SUM(pg.dreb) * {_TM_MP5}) "
        f"/ NULLIF({_PLAYER_MP} * (SUM(tm.dreb) + SUM(op.oreb)), 0)"
    ),
    "oreb_pct": (
        f"100.0 * (SUM(pg.oreb) * {_TM_MP5}) "
        f"/ NULLIF({_PLAYER_MP} * (SUM(tm.oreb) + SUM(op.dreb)), 0)"
    ),
    "reb_pct": (
        f"100.0 * (SUM(pg.reb) * {_TM_MP5}) "
        f"/ NULLIF({_PLAYER_MP} * (SUM(tm.reb) + SUM(op.reb)), 0)"
    ),
    # Steals per opponent possession, blocks per opponent two-point attempt.
    "stl_pct": (
        f"100.0 * (SUM(pg.stl) * {_TM_MP5}) / NULLIF({_PLAYER_MP} * {_OPP_POSS}, 0)"
    ),
    "blk_pct": (
        f"100.0 * (SUM(pg.blk) * {_TM_MP5}) "
        f"/ NULLIF({_PLAYER_MP} * (SUM(op.fga) - SUM(op.fg3a)), 0)"
    ),
    # Turnovers as a share of possessions used.
    "tov_pct": (
        "100.0 * SUM(pg.tov) / NULLIF(SUM(pg.fga) + 0.44 * SUM(pg.fta) "
        "+ SUM(pg.tov), 0)"
    ),
}

# Advanced stats need the team/opponent joins; nothing else does.
def _needs_team_join(stats: list[str]) -> bool:
    return any(s in ADVANCED_STATS for s in stats)


# ---------------------------------------------------------------------------
# Derived counts
#
# Counts of games meeting a per-game condition. They are only meaningful as
# totals — "double-doubles per 36 minutes" is not a thing — so per-mode never
# touches them, the same way it never touches a percentage.
# ---------------------------------------------------------------------------

_DD_CATS = ["pts", "reb", "ast", "stl", "blk"]
_CAT_COUNT = " + ".join(
    f"(CASE WHEN pg.{c} >= 10 THEN 1 ELSE 0 END)" for c in _DD_CATS
)

DERIVED_COUNTS = {
    "double_doubles": f"SUM(CASE WHEN ({_CAT_COUNT}) >= 2 THEN 1 ELSE 0 END)",
    "triple_doubles": f"SUM(CASE WHEN ({_CAT_COUNT}) >= 3 THEN 1 ELSE 0 END)",
}


def all_stats() -> set[str]:
    """Every column name a query may ask for."""
    return (set(COUNTING_STATS) | set(RATE_STATS) | set(ADVANCED_STATS)
            | set(DERIVED_COUNTS))


# Columns that always exist in a result, so they are always valid to sort on.
ALWAYS_PRESENT = {"gp", "season", "player", "team"}

POSSESSIONS = "(SUM(pg.fga) - SUM(pg.oreb) + SUM(pg.tov) + 0.44 * SUM(pg.fta))"

DEFAULT_STATS = ["pts", "reb", "ast"]


@dataclass
class StatQuery:
    """A declarative filter. Maps to SQL in one place, so callers never
    concatenate strings and injection is impossible by construction."""

    entity: Literal["player", "team"] = "player"
    stats: list[str] = field(default_factory=lambda: list(DEFAULT_STATS))
    per_mode: PerMode = "per_game"
    season: int | None = None
    season_range: tuple[int, int] | None = None
    season_type: str = "Regular Season"
    player: str | None = None          # name substring, case-insensitive
    team: str | None = None            # abbreviation, e.g. 'BOS'
    min_games: int = 0
    min_minutes: float = 0.0
    group_by_season: bool = True       # False = career totals across seasons
    order_by: str | None = None        # defaults to the first requested stat
    descending: bool = True
    limit: int = 50
    # Adds a season-total column beside each counting stat. A leaderboard
    # showing 30.1 PPG is more useful when it also says 2,370 points, since a
    # high average over 12 games is a different thing from one over 79.
    include_totals: bool = False


def _per_mode_expr(sql_sum: str, per_mode: PerMode) -> str:
    if per_mode == "totals":
        return sql_sum
    if per_mode == "per_game":
        return f"CAST({sql_sum} AS REAL) / NULLIF(COUNT(*), 0)"
    if per_mode == "per36":
        return f"CAST({sql_sum} AS REAL) / NULLIF(SUM(pg.min), 0) * 36"
    if per_mode == "per100":
        return f"CAST({sql_sum} AS REAL) / NULLIF({POSSESSIONS}, 0) * 100"
    raise ValueError(f"unknown per_mode: {per_mode}")


def build_sql(q: StatQuery) -> tuple[str, list]:
    """Turn a StatQuery into (sql, params). Exposed so you can inspect or
    log the generated SQL — useful when a number looks wrong."""
    known = all_stats()
    if not q.stats:
        raise ValueError("pick at least one column to show")
    unknown = [s for s in q.stats if s not in known]
    if unknown:
        raise ValueError(
            f"unknown stat(s): {unknown}. available: {sorted(known)}"
        )

    select, params = [], []

    if q.entity == "player":
        # A player traded mid-season has rows for both teams. Rather than
        # splitting him into two rows, list the teams he appeared for in this
        # grouping: "BOS,LAL". GROUP_CONCAT(DISTINCT …) in SQLite always uses
        # a comma, which is fine here.
        select += ["pg.player_id", "p.full_name AS player",
                   "GROUP_CONCAT(DISTINCT t.abbreviation) AS team"]
        group = ["pg.player_id", "p.full_name"]
    else:
        select += ["pg.team_id", "t.abbreviation AS team"]
        group = ["pg.team_id", "t.abbreviation"]

    if q.group_by_season:
        select.append("pg.season")
        group.append("pg.season")

    select.append("COUNT(*) AS gp")

    for stat in q.stats:
        if stat in RATE_STATS:
            select.append(f"ROUND({RATE_STATS[stat]}, 4) AS {stat}")
        elif stat in ADVANCED_STATS:
            select.append(f"ROUND({ADVANCED_STATS[stat]}, 1) AS {stat}")
        elif stat in DERIVED_COUNTS:
            # A count of games, so per-mode never applies.
            select.append(f"{DERIVED_COUNTS[stat]} AS {stat}")
        else:
            expr = _per_mode_expr(COUNTING_STATS[stat], q.per_mode)
            rounding = 0 if q.per_mode == "totals" else 2
            select.append(f"ROUND({expr}, {rounding}) AS {stat}")
            # The season total beside the rate, so a 30 PPG line over 12 games
            # can't be mistaken for one over 79.
            if q.include_totals and q.per_mode != "totals":
                select.append(f"{COUNTING_STATS[stat]} AS {stat}_total")

    where = ["pg.season_type = ?"]
    params.append(q.season_type)

    if q.season is not None:
        where.append("pg.season = ?")
        params.append(q.season)
    elif q.season_range is not None:
        where.append("pg.season BETWEEN ? AND ?")
        params += [q.season_range[0], q.season_range[1]]

    if q.player:
        where.append("LOWER(p.full_name) LIKE ?")
        params.append(f"%{q.player.lower()}%")

    if q.team:
        where.append("t.abbreviation = ?")
        params.append(q.team.upper())

    having, having_params = [], []
    if q.min_games:
        having.append("COUNT(*) >= ?")
        having_params.append(q.min_games)
    if q.min_minutes:
        having.append("SUM(pg.min) >= ?")
        having_params.append(q.min_minutes)

    # Sorting by a column that isn't in the SELECT is a SQL error, which is
    # what happens when the UI drops the column it was ordering by. Fall back
    # to the first visible column instead of failing the whole query.
    sortable = set(q.stats) | ALWAYS_PRESENT
    if q.include_totals and q.per_mode != "totals":
        sortable |= {f"{s}_total" for s in q.stats if s in COUNTING_STATS}

    order = q.order_by if q.order_by in sortable else q.stats[0]
    direction = "DESC" if q.descending else "ASC"

    # Advanced stats need the player's team box score and the opponent's for
    # each game. Joined only when asked for, so ordinary queries stay cheap.
    team_joins = ""
    if _needs_team_join(q.stats):
        team_joins = (
            "\n        LEFT JOIN team_games tm "
            "ON tm.game_id = pg.game_id AND tm.team_id = pg.team_id"
            "\n        LEFT JOIN team_games op "
            "ON op.game_id = pg.game_id AND op.team_id = tm.opp_team_id"
        )

    sql = f"""
        SELECT {", ".join(select)}
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        JOIN teams   t ON t.team_id   = pg.team_id{team_joins}
        WHERE {" AND ".join(where)}
        GROUP BY {", ".join(group)}
        {"HAVING " + " AND ".join(having) if having else ""}
        ORDER BY {order} {direction} NULLS LAST
        LIMIT ?
    """
    return sql, params + having_params + [q.limit]


def run(conn: sqlite3.Connection, q: StatQuery) -> list[dict]:
    sql, params = build_sql(q)
    return [dict(r) for r in conn.execute(sql, params)]


def query(conn: sqlite3.Connection, **kwargs) -> list[dict]:
    """Convenience wrapper: query(conn, player='curry', per_mode='per36')."""
    return run(conn, StatQuery(**kwargs))
