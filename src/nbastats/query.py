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
    unknown = [s for s in q.stats if s not in COUNTING_STATS and s not in RATE_STATS]
    if unknown:
        raise ValueError(
            f"unknown stat(s): {unknown}. "
            f"available: {sorted(set(COUNTING_STATS) | set(RATE_STATS))}"
        )

    select, params = [], []

    if q.entity == "player":
        select += ["pg.player_id", "p.full_name AS player"]
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
        else:
            expr = _per_mode_expr(COUNTING_STATS[stat], q.per_mode)
            rounding = 0 if q.per_mode == "totals" else 2
            select.append(f"ROUND({expr}, {rounding}) AS {stat}")

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

    order = q.order_by or q.stats[0]
    direction = "DESC" if q.descending else "ASC"

    sql = f"""
        SELECT {", ".join(select)}
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        JOIN teams   t ON t.team_id   = pg.team_id
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
