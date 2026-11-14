"""Parse raw NBA.com game logs into the normalized tables."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from .client import fetch_league_game_log, fetch_static_reference

log = logging.getLogger(__name__)


def _num(value, cast=int):
    """NBA.com uses nulls liberally (MIN for DNPs, PLUS_MINUS pre-1997)."""
    if value is None or value == "":
        return None
    try:
        return cast(value)
    except (TypeError, ValueError):
        return None


def _iso_date(raw: str) -> str:
    """GAME_DATE arrives as 'YYYY-MM-DDT00:00:00' or 'YYYY-MM-DD'."""
    return str(raw)[:10]


def _parse_matchup(matchup: str) -> tuple[str, str, bool]:
    """'LAL vs. BOS' -> ('LAL', 'BOS', True);  'LAL @ BOS' -> (..., False).

    Home/away is encoded only in this string, so it is the single source of
    truth for reconstructing one games row from two team rows.
    """
    if " vs. " in matchup:
        left, right = matchup.split(" vs. ")
        return left.strip(), right.strip(), True
    if " @ " in matchup:
        left, right = matchup.split(" @ ")
        return left.strip(), right.strip(), False
    raise ValueError(f"unrecognized matchup string: {matchup!r}")


# ---------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------

def load_reference(conn: sqlite3.Connection) -> None:
    """Upsert players and teams from nba_api's bundled static data."""
    players, teams = fetch_static_reference()

    conn.executemany(
        """INSERT INTO players (player_id, full_name, first_name, last_name, is_active)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(player_id) DO UPDATE SET
             full_name = excluded.full_name,
             is_active = excluded.is_active""",
        [
            (p["id"], p["full_name"], p.get("first_name"), p.get("last_name"),
             int(bool(p.get("is_active"))))
            for p in players
        ],
    )

    conn.executemany(
        """INSERT INTO teams (team_id, abbreviation, full_name, nickname, city, state, year_founded)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(team_id) DO UPDATE SET
             abbreviation = excluded.abbreviation,
             full_name    = excluded.full_name""",
        [
            (t["id"], t["abbreviation"], t["full_name"], t.get("nickname"),
             t.get("city"), t.get("state"), t.get("year_founded"))
            for t in teams
        ],
    )
    conn.commit()
    log.info("reference loaded: %d players, %d teams", len(players), len(teams))


# ---------------------------------------------------------------------------
# Season ingest
# ---------------------------------------------------------------------------

def _upsert_games(conn, team_rows, season: int, season_type: str) -> int:
    """Collapse two team rows per game into one games row.

    Also backfills any team that appears in a game log but is missing from the
    static teams table (defunct franchises: Sonics, Bobcats, New Jersey Nets).
    """
    abbrev_to_id, games = {}, {}

    for r in team_rows:
        abbrev_to_id[r["TEAM_ABBREVIATION"]] = r["TEAM_ID"]

    # Defunct franchises aren't in nba_api's static list but do appear in old
    # game logs, so insert a placeholder to keep the foreign keys valid.
    existing = {row[0] for row in conn.execute("SELECT team_id FROM teams")}
    missing = [
        (tid, abbrev, abbrev)
        for abbrev, tid in abbrev_to_id.items()
        if tid not in existing
    ]
    if missing:
        conn.executemany(
            "INSERT OR IGNORE INTO teams (team_id, abbreviation, full_name) VALUES (?, ?, ?)",
            missing,
        )
        log.info("inserted %d historical teams not in static data", len(missing))

    for r in team_rows:
        gid = r["GAME_ID"]
        _, opp_abbrev, is_home = _parse_matchup(r["MATCHUP"])
        entry = games.setdefault(
            gid,
            {"game_id": gid, "season": season, "season_type": season_type,
             "game_date": _iso_date(r["GAME_DATE"]),
             "home_team_id": None, "away_team_id": None,
             "home_pts": None, "away_pts": None},
        )
        if is_home:
            entry["home_team_id"] = r["TEAM_ID"]
            entry["home_pts"] = _num(r.get("PTS"))
            entry["away_team_id"] = entry["away_team_id"] or abbrev_to_id.get(opp_abbrev)
        else:
            entry["away_team_id"] = r["TEAM_ID"]
            entry["away_pts"] = _num(r.get("PTS"))
            entry["home_team_id"] = entry["home_team_id"] or abbrev_to_id.get(opp_abbrev)

    conn.executemany(
        """INSERT INTO games (game_id, season, season_type, game_date,
                              home_team_id, away_team_id, home_pts, away_pts)
           VALUES (:game_id, :season, :season_type, :game_date,
                   :home_team_id, :away_team_id, :home_pts, :away_pts)
           ON CONFLICT(game_id) DO UPDATE SET
             home_pts = excluded.home_pts,
             away_pts = excluded.away_pts""",
        list(games.values()),
    )
    return len(games)


_BOX_FIELDS = ["FGM", "FGA", "FG3M", "FG3A", "FTM", "FTA", "OREB", "DREB",
               "REB", "AST", "STL", "BLK", "TOV", "PF", "PTS", "PLUS_MINUS"]


def _box(r: dict) -> tuple:
    return tuple(_num(r.get(f)) for f in _BOX_FIELDS)


def _upsert_player_games(conn, rows, season: int, season_type: str) -> int:
    known = {row[0] for row in conn.execute("SELECT player_id FROM players")}
    unknown = []
    payload = []

    for r in rows:
        pid = r["PLAYER_ID"]
        if pid not in known:
            unknown.append((pid, r.get("PLAYER_NAME", f"Unknown {pid}")))
            known.add(pid)
        payload.append(
            (r["GAME_ID"], pid, r["TEAM_ID"], season, season_type,
             _iso_date(r["GAME_DATE"]), r.get("WL"), _num(r.get("MIN"), float))
            + _box(r)
        )

    if unknown:
        conn.executemany(
            "INSERT OR IGNORE INTO players (player_id, full_name) VALUES (?, ?)",
            unknown,
        )

    conn.executemany(
        f"""INSERT INTO player_games
            (game_id, player_id, team_id, season, season_type, game_date, wl, min,
             {", ".join(f.lower() for f in _BOX_FIELDS)})
            VALUES ({", ".join("?" * (8 + len(_BOX_FIELDS)))})
            ON CONFLICT(game_id, player_id) DO UPDATE SET
              pts = excluded.pts, reb = excluded.reb, ast = excluded.ast,
              min = excluded.min""",
        payload,
    )
    return len(payload)


def _upsert_team_games(conn, rows, season: int, season_type: str) -> int:
    abbrev_to_id = {r["TEAM_ABBREVIATION"]: r["TEAM_ID"] for r in rows}
    payload = []

    for r in rows:
        _, opp_abbrev, is_home = _parse_matchup(r["MATCHUP"])
        payload.append(
            (r["GAME_ID"], r["TEAM_ID"], abbrev_to_id.get(opp_abbrev),
             season, season_type, _iso_date(r["GAME_DATE"]),
             int(is_home), r.get("WL"), _num(r.get("MIN"), float))
            + _box(r)
        )

    conn.executemany(
        f"""INSERT INTO team_games
            (game_id, team_id, opp_team_id, season, season_type, game_date,
             is_home, wl, min, {", ".join(f.lower() for f in _BOX_FIELDS)})
            VALUES ({", ".join("?" * (9 + len(_BOX_FIELDS)))})
            ON CONFLICT(game_id, team_id) DO UPDATE SET
              pts = excluded.pts, plus_minus = excluded.plus_minus""",
        payload,
    )
    return len(payload)


def ingest_season(
    conn: sqlite3.Connection,
    season: int,
    season_type: str = "Regular Season",
    force: bool = False,
) -> dict:
    """Ingest one season. Idempotent: skips seasons already marked complete."""
    if not force and is_complete(conn, season, season_type):
        log.info("skipping %s %s (already complete)", season, season_type)
        return {"season": season, "season_type": season_type, "status": "skipped"}

    _mark(conn, season, season_type, "pending")
    try:
        team_rows = fetch_league_game_log(season, season_type, player_or_team="T")
        player_rows = fetch_league_game_log(season, season_type, player_or_team="P")

        if not team_rows:
            raise ValueError("empty team game log — season may not exist")

        n_games = _upsert_games(conn, team_rows, season, season_type)
        n_team = _upsert_team_games(conn, team_rows, season, season_type)
        n_player = _upsert_player_games(conn, player_rows, season, season_type)
        conn.commit()

        _mark(conn, season, season_type, "complete",
              rows_player=n_player, rows_team=n_team)
        log.info("ingested %s %s: %d games, %d player-games",
                 season, season_type, n_games, n_player)
        return {"season": season, "season_type": season_type, "status": "complete",
                "games": n_games, "player_games": n_player, "team_games": n_team}

    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        _mark(conn, season, season_type, "failed", error=str(exc))
        log.error("failed %s %s: %s", season, season_type, exc)
        return {"season": season, "season_type": season_type,
                "status": "failed", "error": str(exc)}


# ---------------------------------------------------------------------------
# Bookkeeping — what makes the backfill resumable
# ---------------------------------------------------------------------------

def _mark(conn, season, season_type, status, rows_player=None, rows_team=None,
          error=None) -> None:
    conn.execute(
        """INSERT INTO ingest_log (season, season_type, status, rows_player,
                                   rows_team, error, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(season, season_type) DO UPDATE SET
             status = excluded.status, rows_player = excluded.rows_player,
             rows_team = excluded.rows_team, error = excluded.error,
             updated_at = excluded.updated_at""",
        (season, season_type, status, rows_player, rows_team, error,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def is_complete(conn, season: int, season_type: str) -> bool:
    row = conn.execute(
        "SELECT status FROM ingest_log WHERE season = ? AND season_type = ?",
        (season, season_type),
    ).fetchone()
    return row is not None and row["status"] == "complete"


def ingest_status(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM ingest_log ORDER BY season, season_type"
    )]
