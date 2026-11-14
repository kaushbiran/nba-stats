"""Query layer tests. Every expected value is hand-computable from conftest."""

import pytest

from nbastats.query import StatQuery, build_sql, run


def test_per_game_math(conn):
    """Alpha scored 25 in each of 4 games in 2020 -> 25.0 ppg."""
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"],
                               per_mode="per_game"))
    assert len(rows) == 1
    assert rows[0]["gp"] == 4
    assert rows[0]["pts"] == 25.0


def test_totals_math(conn):
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"],
                               per_mode="totals"))
    assert rows[0]["pts"] == 100


def test_per36_math(conn):
    """100 pts in 144 minutes -> 100/144*36 = 25.0 (he played 36 min/game)."""
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"],
                               per_mode="per36"))
    assert rows[0]["pts"] == pytest.approx(25.0, abs=0.01)


def test_per100_math(conn):
    """Possessions = fga - oreb + tov + 0.44*fta
                   = 80 - 4 + 8 + 0.44*8 = 87.52
       100 pts / 87.52 * 100 = 114.26"""
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"],
                               per_mode="per100"))
    assert rows[0]["pts"] == pytest.approx(114.26, abs=0.05)


def test_rate_stats_not_rescaled(conn):
    """fg_pct must be identical regardless of per_mode — it is a ratio."""
    totals = run(conn, StatQuery(player="alpha", season=2020, stats=["fg_pct"],
                                 per_mode="totals"))
    per36 = run(conn, StatQuery(player="alpha", season=2020, stats=["fg_pct"],
                                per_mode="per36"))
    assert totals[0]["fg_pct"] == per36[0]["fg_pct"] == pytest.approx(0.5)


def test_rate_from_summed_components_not_averaged(conn):
    """Alpha 2021: 10/20 and 10/20 -> 20/40 = .500, not an average of games."""
    rows = run(conn, StatQuery(player="alpha", season=2021, stats=["fg_pct"]))
    assert rows[0]["fg_pct"] == pytest.approx(0.5)


def test_season_range_filter(conn):
    rows = run(conn, StatQuery(player="alpha", season_range=(2020, 2021),
                               stats=["pts"], per_mode="totals"))
    assert len(rows) == 2
    assert {r["season"] for r in rows} == {2020, 2021}


def test_career_aggregation(conn):
    """group_by_season=False collapses 100 + 60 = 160 career points."""
    rows = run(conn, StatQuery(player="alpha", stats=["pts"],
                               per_mode="totals", group_by_season=False))
    assert len(rows) == 1
    assert rows[0]["pts"] == 160
    assert rows[0]["gp"] == 6


def test_min_games_filter(conn):
    """Alpha's 2021 season has only 2 games; min_games=4 excludes it."""
    rows = run(conn, StatQuery(player="alpha", stats=["pts"], min_games=4))
    assert all(r["season"] == 2020 for r in rows)


def test_team_filter(conn):
    rows = run(conn, StatQuery(team="LAL", season=2020, stats=["pts"]))
    assert len(rows) == 1
    assert rows[0]["player"] == "Gamma Bench"


def test_ordering_and_limit(conn):
    rows = run(conn, StatQuery(season=2020, stats=["pts"], per_mode="per_game",
                               limit=2))
    assert len(rows) == 2
    assert rows[0]["pts"] >= rows[1]["pts"]
    assert rows[0]["player"] == "Alpha Scorer"


def test_ascending_order(conn):
    rows = run(conn, StatQuery(season=2020, stats=["pts"], descending=False))
    assert rows[0]["player"] == "Gamma Bench"


def test_unknown_stat_raises(conn):
    with pytest.raises(ValueError, match="unknown stat"):
        run(conn, StatQuery(stats=["not_a_real_stat"]))


def test_sql_is_parameterized(conn):
    """A malicious player name must land in params, never in the SQL text."""
    sql, params = build_sql(StatQuery(player="'; DROP TABLE players; --"))
    assert "DROP TABLE" not in sql
    assert any("drop table" in str(p).lower() for p in params)
    # and it must execute harmlessly
    assert run(conn, StatQuery(player="'; DROP TABLE players; --")) == []
    assert conn.execute("SELECT COUNT(*) FROM players").fetchone()[0] == 3


def test_division_by_zero_yields_null_not_error(conn):
    """A player with zero attempts gets NULL, not a ZeroDivisionError.

    This is what the NULLIF(...) guards in query.py are for. A DNP or a
    pure-defense bench game would otherwise crash the whole query.
    """
    conn.execute(
        """INSERT INTO players (player_id, full_name) VALUES (1004, 'Delta Noshot')"""
    )
    conn.execute(
        """INSERT INTO player_games (game_id, player_id, team_id, season,
           season_type, game_date, min, fgm, fga, fg3m, fg3a, ftm, fta,
           oreb, dreb, reb, ast, stl, blk, tov, pf, pts, plus_minus)
           VALUES ('0022000001', 1004, 1610612747, 2020, 'Regular Season',
                   '2020-12-22', 0, 0, 0, 0, 0, 0, 0,
                   0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"""
    )
    conn.commit()

    rows = run(conn, StatQuery(player="delta", season=2020,
                               stats=["fg_pct", "ts_pct"]))
    assert len(rows) == 1
    assert rows[0]["fg_pct"] is None
    assert rows[0]["ts_pct"] is None


def test_per36_with_zero_minutes_is_null(conn):
    """Dividing by zero minutes must not raise either."""
    conn.execute("INSERT INTO players (player_id, full_name) VALUES (1005, 'Eps Zero')")
    conn.execute(
        """INSERT INTO player_games (game_id, player_id, team_id, season,
           season_type, game_date, min, fgm, fga, fg3m, fg3a, ftm, fta,
           oreb, dreb, reb, ast, stl, blk, tov, pf, pts, plus_minus)
           VALUES ('0022000002', 1005, 1610612747, 2020, 'Regular Season',
                   '2020-12-25', 0, 0, 0, 0, 0, 0, 0,
                   0, 0, 0, 0, 0, 0, 0, 0, 0, 0)"""
    )
    conn.commit()
    rows = run(conn, StatQuery(player="eps", season=2020, stats=["pts"],
                               per_mode="per36"))
    assert rows[0]["pts"] is None
