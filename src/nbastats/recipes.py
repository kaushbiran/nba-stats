"""Derived / "random" stats — the questions that need game-level data.

Each recipe is a thin function over player_games. This is the extension point:
a contributor adds a new stat question as one function plus one test, without
touching the query engine.

Why these can't come from season averages: "how many double-doubles" requires
inspecting each individual game, and "single-digit scoring seasons" requires a
season aggregate filtered by a threshold. Season-average tables throw away the
first and can't express the second.
"""

from __future__ import annotations

import sqlite3

REG = "Regular Season"

# The five categories that count toward a double-double / triple-double.
_DD_CATEGORIES = ["pts", "reb", "ast", "stl", "blk"]
_CATEGORY_COUNT = " + ".join(f"(CASE WHEN pg.{c} >= 10 THEN 1 ELSE 0 END)"
                             for c in _DD_CATEGORIES)


def _rows(conn, sql, params=()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params)]


# ---------------------------------------------------------------------------
# Game-level achievements
# ---------------------------------------------------------------------------

def double_doubles(conn: sqlite3.Connection, player: str | None = None,
                   season: int | None = None, season_type: str = REG,
                   limit: int = 50) -> list[dict]:
    """Count games with >=10 in at least two of pts/reb/ast/stl/blk."""
    where, params = ["pg.season_type = ?"], [season_type]
    if player:
        where.append("LOWER(p.full_name) LIKE ?")
        params.append(f"%{player.lower()}%")
    if season is not None:
        where.append("pg.season = ?")
        params.append(season)

    return _rows(conn, f"""
        SELECT p.full_name AS player, pg.season,
               SUM(CASE WHEN ({_CATEGORY_COUNT}) >= 2 THEN 1 ELSE 0 END) AS double_doubles,
               SUM(CASE WHEN ({_CATEGORY_COUNT}) >= 3 THEN 1 ELSE 0 END) AS triple_doubles,
               COUNT(*) AS gp
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE {" AND ".join(where)}
        GROUP BY pg.player_id, pg.season
        HAVING double_doubles > 0
        ORDER BY double_doubles DESC
        LIMIT ?
    """, params + [limit])


def triple_doubles(conn, player=None, season=None, season_type=REG, limit=50):
    """Games with >=10 in at least three categories, listed individually."""
    where, params = ["pg.season_type = ?", f"({_CATEGORY_COUNT}) >= 3"], [season_type]
    if player:
        where.append("LOWER(p.full_name) LIKE ?")
        params.append(f"%{player.lower()}%")
    if season is not None:
        where.append("pg.season = ?")
        params.append(season)

    return _rows(conn, f"""
        SELECT p.full_name AS player, pg.season, pg.game_date,
               pg.pts, pg.reb, pg.ast, pg.stl, pg.blk
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE {" AND ".join(where)}
        ORDER BY pg.game_date DESC
        LIMIT ?
    """, params + [limit])


def games_above_threshold(conn, stat: str = "pts", threshold: int = 40,
                          season: int | None = None, season_type: str = REG,
                          limit: int = 50) -> list[dict]:
    """How many times each player hit N+ of a stat in a single game.

    Covers 40-point games, 20-rebound games, 5-block games, and so on.
    """
    if stat not in {"pts", "reb", "ast", "stl", "blk", "fg3m", "tov"}:
        raise ValueError(f"unsupported stat for threshold query: {stat}")

    where, params = ["pg.season_type = ?", f"pg.{stat} >= ?"], [season_type, threshold]
    if season is not None:
        where.append("pg.season = ?")
        params.append(season)

    return _rows(conn, f"""
        SELECT p.full_name AS player, COUNT(*) AS games,
               MAX(pg.{stat}) AS best, MIN(pg.season) AS first_season,
               MAX(pg.season) AS last_season
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE {" AND ".join(where)}
        GROUP BY pg.player_id
        ORDER BY games DESC
        LIMIT ?
    """, params + [limit])


# ---------------------------------------------------------------------------
# Season-level achievements
# ---------------------------------------------------------------------------

def single_digit_scoring_seasons(conn, min_games: int = 58,
                                 season_type: str = REG,
                                 limit: int = 100) -> list[dict]:
    """Seasons where a player averaged under 10.0 points per game.

    min_games defaults to 58 (roughly 70% of an 82-game season) to filter out
    players who appeared in a handful of games. Adjust for lockout seasons:
    1998-99 was 50 games and 2011-12 was 66.
    """
    return _rows(conn, """
        SELECT p.full_name AS player, pg.season, COUNT(*) AS gp,
               ROUND(CAST(SUM(pg.pts) AS REAL) / COUNT(*), 2) AS ppg,
               SUM(pg.pts) AS total_pts
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE pg.season_type = ?
        GROUP BY pg.player_id, pg.season
        HAVING gp >= ? AND ppg < 10.0
        ORDER BY ppg DESC
        LIMIT ?
    """, (season_type, min_games, limit))


def season_averages_club(conn, pts: float = 20, reb: float = 10, ast: float = 5,
                         min_games: int = 58, season_type: str = REG,
                         limit: int = 50) -> list[dict]:
    """Seasons meeting all three per-game thresholds at once.

    The classic 20/10/5 query; change the thresholds for any other club.
    """
    return _rows(conn, """
        SELECT p.full_name AS player, pg.season, COUNT(*) AS gp,
               ROUND(CAST(SUM(pg.pts) AS REAL) / COUNT(*), 1) AS ppg,
               ROUND(CAST(SUM(pg.reb) AS REAL) / COUNT(*), 1) AS rpg,
               ROUND(CAST(SUM(pg.ast) AS REAL) / COUNT(*), 1) AS apg
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE pg.season_type = ?
        GROUP BY pg.player_id, pg.season
        HAVING gp >= ? AND ppg >= ? AND rpg >= ? AND apg >= ?
        ORDER BY ppg DESC
        LIMIT ?
    """, (season_type, min_games, pts, reb, ast, limit))


def iron_man_seasons(conn, min_games: int = 82, season_type: str = REG,
                     limit: int = 50) -> list[dict]:
    """Players who appeared in every game (or at least min_games) of a season."""
    return _rows(conn, """
        SELECT p.full_name AS player, pg.season, COUNT(*) AS gp,
               ROUND(SUM(pg.min), 0) AS total_min
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE pg.season_type = ?
        GROUP BY pg.player_id, pg.season
        HAVING gp >= ?
        ORDER BY gp DESC, total_min DESC
        LIMIT ?
    """, (season_type, min_games, limit))


# ---------------------------------------------------------------------------
# Team-level
# ---------------------------------------------------------------------------

def team_ratings(conn, season: int | None = None, season_type: str = REG) -> list[dict]:
    """Offensive and defensive rating (points per 100 possessions) by team.

    Possessions are estimated per the formula documented in query.py. DRtg uses
    the opponent's points scored against this team.
    """
    where, params = ["tg.season_type = ?"], [season_type]
    if season is not None:
        where.append("tg.season = ?")
        params.append(season)

    return _rows(conn, f"""
        WITH poss AS (
            SELECT tg.team_id, tg.season, tg.game_id,
                   (tg.fga - tg.oreb + tg.tov + 0.44 * tg.fta) AS possessions,
                   tg.pts AS pts_for
            FROM team_games tg
            WHERE {" AND ".join(where)}
        ),
        opp AS (
            SELECT tg.game_id, tg.opp_team_id AS team_id, tg.pts AS pts_against
            FROM team_games tg
            WHERE {" AND ".join(where)}
        )
        SELECT t.abbreviation AS team, poss.season,
               COUNT(*) AS gp,
               ROUND(SUM(poss.pts_for) * 100.0 / NULLIF(SUM(poss.possessions), 0), 1) AS ortg,
               ROUND(SUM(opp.pts_against) * 100.0 / NULLIF(SUM(poss.possessions), 0), 1) AS drtg,
               ROUND((SUM(poss.pts_for) - SUM(opp.pts_against)) * 100.0
                     / NULLIF(SUM(poss.possessions), 0), 1) AS net_rtg
        FROM poss
        JOIN opp   ON opp.game_id = poss.game_id AND opp.team_id = poss.team_id
        JOIN teams t ON t.team_id = poss.team_id
        GROUP BY poss.team_id, poss.season
        ORDER BY net_rtg DESC
    """, params + params)


def team_records(conn, season: int | None = None, season_type: str = REG) -> list[dict]:
    """Win-loss records, including home/away splits."""
    where, params = ["tg.season_type = ?"], [season_type]
    if season is not None:
        where.append("tg.season = ?")
        params.append(season)

    return _rows(conn, f"""
        SELECT t.abbreviation AS team, tg.season,
               SUM(CASE WHEN tg.wl = 'W' THEN 1 ELSE 0 END) AS wins,
               SUM(CASE WHEN tg.wl = 'L' THEN 1 ELSE 0 END) AS losses,
               ROUND(CAST(SUM(CASE WHEN tg.wl = 'W' THEN 1 ELSE 0 END) AS REAL)
                     / NULLIF(COUNT(*), 0), 3) AS win_pct,
               SUM(CASE WHEN tg.wl = 'W' AND tg.is_home = 1 THEN 1 ELSE 0 END) AS home_wins,
               SUM(CASE WHEN tg.wl = 'W' AND tg.is_home = 0 THEN 1 ELSE 0 END) AS away_wins
        FROM team_games tg
        JOIN teams t ON t.team_id = tg.team_id
        WHERE {" AND ".join(where)}
        GROUP BY tg.team_id, tg.season
        ORDER BY win_pct DESC
    """, params)


# Registry so the CLI (and any future web layer) can list what exists.
RECIPES = {
    "double-doubles": double_doubles,
    "triple-doubles": triple_doubles,
    "threshold-games": games_above_threshold,
    "single-digit-scoring": single_digit_scoring_seasons,
    "averages-club": season_averages_club,
    "iron-man": iron_man_seasons,
    "team-ratings": team_ratings,
    "team-records": team_records,
}
