"""Synthetic fixture database.

Tests run against hand-built rows with known answers, never against the live
API. The API is slow, rate-limited and changes without notice; a test suite
that depends on it fails for reasons that have nothing to do with your code.

The fixture is deliberately tiny and hand-checkable: three players, two teams,
two seasons. Every expected value in test_query.py can be verified with a
calculator.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nbastats import db  # noqa: E402

TEAMS = [
    (1610612738, "BOS", "Boston Celtics"),
    (1610612747, "LAL", "Los Angeles Lakers"),
]

PLAYERS = [
    (1001, "Alpha Scorer"),      # high volume
    (1002, "Beta Allrounder"),   # double-double machine
    (1003, "Gamma Bench"),       # single-digit scorer
]


def _game_rows():
    """Generate games, team_games and player_games with known totals.

    Season 2020: 4 games.  Season 2021: 2 games.

    Alpha   2020: 4 games, 25 pts each      -> 100 pts, 25.0 ppg
            2021: 2 games, 40 and 20        ->  60 pts, 30.0 ppg
    Beta    2020: 4 games, 12 pts / 11 reb  ->  4 double-doubles
            one of which is 12/11/10 ast    ->  1 triple-double
    Gamma   2020: 4 games, 6 pts each       ->  24 pts, 6.0 ppg (single digit)
    """
    games, team_games, player_games = [], [], []

    schedule = [
        # (game_id, season, date, home_id, away_id)
        ("0022000001", 2020, "2020-12-22", 1610612738, 1610612747),
        ("0022000002", 2020, "2020-12-25", 1610612747, 1610612738),
        ("0022000003", 2020, "2021-01-05", 1610612738, 1610612747),
        ("0022000004", 2020, "2021-01-10", 1610612747, 1610612738),
        ("0022100001", 2021, "2021-10-20", 1610612738, 1610612747),
        ("0022100002", 2021, "2021-10-25", 1610612747, 1610612738),
    ]

    for i, (gid, season, date, home, away) in enumerate(schedule):
        home_pts, away_pts = 110, 105
        games.append((gid, season, "Regular Season", date, home, away, home_pts, away_pts))

        for team_id, is_home, pts, wl in [
            (home, 1, home_pts, "W"), (away, 0, away_pts, "L")
        ]:
            opp = away if is_home else home
            team_games.append((
                gid, team_id, opp, season, "Regular Season", date, is_home, wl,
                240.0,  # min
                40, 88, 12, 33, 18, 22,   # fgm fga fg3m fg3a ftm fta
                10, 34, 44, 25, 8, 5, 14, 20, pts,
                (home_pts - away_pts) if is_home else (away_pts - home_pts),
            ))

        # Alpha: BOS, 25 ppg in 2020; 40 then 20 in 2021
        if season == 2020:
            alpha_pts = 25
        else:
            alpha_pts = 40 if gid.endswith("1") else 20
        player_games.append((
            gid, 1001, 1610612738, season, "Regular Season", date,
            "W" if home == 1610612738 else "L", 36.0,
            10, 20, 3, 8, 2, 2,           # fgm fga fg3m fg3a ftm fta
            1, 4, 5, 3, 1, 0, 2, 2, alpha_pts, 5,
        ))

        # Beta: BOS, 12/11 every game; 10 assists in game 1 only
        if season == 2020:
            beta_ast = 10 if gid == "0022000001" else 4
            player_games.append((
                gid, 1002, 1610612738, season, "Regular Season", date,
                "W" if home == 1610612738 else "L", 34.0,
                5, 10, 0, 1, 2, 4,
                3, 8, 11, beta_ast, 2, 1, 3, 3, 12, 2,
            ))

            # Gamma: LAL bench, 6 ppg
            player_games.append((
                gid, 1003, 1610612747, season, "Regular Season", date,
                "L" if home == 1610612738 else "W", 18.0,
                3, 9, 0, 2, 0, 0,
                1, 2, 3, 1, 0, 0, 1, 2, 6, -3,
            ))

    return games, team_games, player_games


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "test.db")
    db.init_schema(connection)

    connection.executemany(
        "INSERT INTO teams (team_id, abbreviation, full_name) VALUES (?, ?, ?)",
        TEAMS,
    )
    connection.executemany(
        "INSERT INTO players (player_id, full_name) VALUES (?, ?)", PLAYERS
    )

    games, team_games, player_games = _game_rows()

    connection.executemany(
        """INSERT INTO games (game_id, season, season_type, game_date,
                              home_team_id, away_team_id, home_pts, away_pts)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        games,
    )
    connection.executemany(
        f"""INSERT INTO team_games VALUES ({", ".join("?" * 25)})""", team_games
    )
    connection.executemany(
        f"""INSERT INTO player_games VALUES ({", ".join("?" * 24)})""", player_games
    )
    connection.commit()
    yield connection
    connection.close()
