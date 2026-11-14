"""Ingest parser tests.

These feed the parsers rows shaped exactly like NBA.com's leaguegamelog
response, without touching the network. The point is to pin down the fiddly
parts: MATCHUP parsing, two-team-rows-to-one-game collapsing, null handling
and idempotency.
"""

import pytest

from nbastats import ingest


# A realistic pair of team rows for one game, with NBA.com's column names.
def _team_row(game_id, team_id, abbrev, matchup, pts, wl):
    return {
        "SEASON_ID": "22020", "TEAM_ID": team_id, "TEAM_ABBREVIATION": abbrev,
        "TEAM_NAME": abbrev, "GAME_ID": game_id,
        "GAME_DATE": "2020-12-22T00:00:00", "MATCHUP": matchup, "WL": wl,
        "MIN": 240, "FGM": 40, "FGA": 88, "FG_PCT": 0.455, "FG3M": 12,
        "FG3A": 33, "FG3_PCT": 0.364, "FTM": 18, "FTA": 22, "FT_PCT": 0.818,
        "OREB": 10, "DREB": 34, "REB": 44, "AST": 25, "STL": 8, "BLK": 5,
        "TOV": 14, "PF": 20, "PTS": pts, "PLUS_MINUS": 5, "VIDEO_AVAILABLE": 1,
    }


def _player_row(game_id, player_id, name, team_id, abbrev, matchup, pts, minutes=36):
    return {
        "SEASON_ID": "22020", "PLAYER_ID": player_id, "PLAYER_NAME": name,
        "TEAM_ID": team_id, "TEAM_ABBREVIATION": abbrev, "TEAM_NAME": abbrev,
        "GAME_ID": game_id, "GAME_DATE": "2020-12-22T00:00:00",
        "MATCHUP": matchup, "WL": "W", "MIN": minutes,
        "FGM": 10, "FGA": 20, "FG_PCT": 0.5, "FG3M": 3, "FG3A": 8,
        "FG3_PCT": 0.375, "FTM": 2, "FTA": 2, "FT_PCT": 1.0,
        "OREB": 1, "DREB": 4, "REB": 5, "AST": 3, "STL": 1, "BLK": 0,
        "TOV": 2, "PF": 2, "PTS": pts, "PLUS_MINUS": 5, "VIDEO_AVAILABLE": 1,
    }


# ---------------------------------------------------------------------------
# MATCHUP parsing — home/away lives only in this string
# ---------------------------------------------------------------------------

def test_parse_matchup_home():
    assert ingest._parse_matchup("BOS vs. LAL") == ("BOS", "LAL", True)


def test_parse_matchup_away():
    assert ingest._parse_matchup("LAL @ BOS") == ("LAL", "BOS", False)


def test_parse_matchup_rejects_garbage():
    with pytest.raises(ValueError):
        ingest._parse_matchup("BOS LAL")


# ---------------------------------------------------------------------------
# Null handling — NBA.com returns nulls for DNPs and pre-1997 plus/minus
# ---------------------------------------------------------------------------

def test_num_handles_none_and_empty():
    assert ingest._num(None) is None
    assert ingest._num("") is None
    assert ingest._num("17") == 17
    assert ingest._num("36.5", float) == 36.5


def test_num_survives_unparseable_value():
    assert ingest._num("not a number") is None


def test_iso_date_strips_time_component():
    assert ingest._iso_date("2020-12-22T00:00:00") == "2020-12-22"
    assert ingest._iso_date("2020-12-22") == "2020-12-22"


# ---------------------------------------------------------------------------
# Game collapsing
# ---------------------------------------------------------------------------

def test_two_team_rows_collapse_to_one_game(conn):
    rows = [
        _team_row("0022000099", 1610612738, "BOS", "BOS vs. LAL", 110, "W"),
        _team_row("0022000099", 1610612747, "LAL", "LAL @ BOS", 105, "L"),
    ]
    n = ingest._upsert_games(conn, rows, 2020, "Regular Season")
    conn.commit()

    assert n == 1
    game = conn.execute(
        "SELECT * FROM games WHERE game_id = '0022000099'"
    ).fetchone()
    assert game["home_team_id"] == 1610612738
    assert game["away_team_id"] == 1610612747
    assert game["home_pts"] == 110
    assert game["away_pts"] == 105


def test_ingest_is_idempotent(conn):
    """Re-ingesting the same rows must not duplicate anything."""
    rows = [
        _team_row("0022000099", 1610612738, "BOS", "BOS vs. LAL", 110, "W"),
        _team_row("0022000099", 1610612747, "LAL", "LAL @ BOS", 105, "L"),
    ]
    before = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    for _ in range(3):
        ingest._upsert_games(conn, rows, 2020, "Regular Season")
        ingest._upsert_team_games(conn, rows, 2020, "Regular Season")
    conn.commit()

    assert conn.execute("SELECT COUNT(*) FROM games").fetchone()[0] == before + 1
    assert conn.execute(
        "SELECT COUNT(*) FROM team_games WHERE game_id = '0022000099'"
    ).fetchone()[0] == 2


def test_unknown_player_is_backfilled(conn):
    """A player missing from static reference data gets a stub row, so the
    foreign key holds instead of the whole season failing to insert."""
    ingest._upsert_games(conn, [
        _team_row("0022000099", 1610612738, "BOS", "BOS vs. LAL", 110, "W"),
        _team_row("0022000099", 1610612747, "LAL", "LAL @ BOS", 105, "L"),
    ], 2020, "Regular Season")

    rows = [_player_row("0022000099", 9999, "Obscure Rookie",
                        1610612738, "BOS", "BOS vs. LAL", 8)]
    ingest._upsert_player_games(conn, rows, 2020, "Regular Season")
    conn.commit()

    player = conn.execute(
        "SELECT full_name FROM players WHERE player_id = 9999"
    ).fetchone()
    assert player["full_name"] == "Obscure Rookie"
    assert conn.execute(
        "SELECT pts FROM player_games WHERE player_id = 9999"
    ).fetchone()["pts"] == 8


def test_defunct_team_is_backfilled(conn):
    """Seattle SuperSonics appear in old game logs but not in static data."""
    rows = [
        _team_row("0020700099", 1610612760, "SEA", "SEA vs. LAL", 100, "L"),
        _team_row("0020700099", 1610612747, "LAL", "LAL @ SEA", 110, "W"),
    ]
    ingest._upsert_games(conn, rows, 2007, "Regular Season")
    conn.commit()

    team = conn.execute(
        "SELECT abbreviation FROM teams WHERE team_id = 1610612760"
    ).fetchone()
    assert team["abbreviation"] == "SEA"


# ---------------------------------------------------------------------------
# Bookkeeping — what makes the backfill resumable
# ---------------------------------------------------------------------------

def test_is_complete_false_before_ingest(conn):
    assert ingest.is_complete(conn, 2020, "Regular Season") is False


def test_mark_complete_then_skip(conn):
    ingest._mark(conn, 2020, "Regular Season", "complete",
                 rows_player=100, rows_team=20)
    assert ingest.is_complete(conn, 2020, "Regular Season") is True
    # Different season type is tracked separately
    assert ingest.is_complete(conn, 2020, "Playoffs") is False


def test_failed_status_is_not_complete(conn):
    ingest._mark(conn, 2020, "Regular Season", "failed", error="timeout")
    assert ingest.is_complete(conn, 2020, "Regular Season") is False
    status = ingest.ingest_status(conn)
    assert status[0]["error"] == "timeout"
