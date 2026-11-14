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


# ---------------------------------------------------------------------------
# Advanced stats and season totals
# ---------------------------------------------------------------------------

def test_advanced_stats_require_team_join(conn):
    """The SQL must pull in team and opponent box scores when asked."""
    sql, _ = build_sql(StatQuery(stats=["usg_pct"]))
    assert "team_games tm" in sql
    assert "team_games op" in sql


def test_ordinary_query_skips_team_join(conn):
    """Queries that don't need team context shouldn't pay for the joins."""
    sql, _ = build_sql(StatQuery(stats=["pts"]))
    assert "team_games" not in sql


def test_advanced_stats_run(conn):
    rows = run(conn, StatQuery(stats=["usg_pct", "ast_ratio", "tov_pct"],
                               season=2020))
    assert rows
    for r in rows:
        for k in ("usg_pct", "ast_ratio", "tov_pct"):
            assert k in r


def test_ast_ratio_is_self_contained(conn):
    """ast_ratio needs no team data, so it works even without team_games rows."""
    rows = run(conn, StatQuery(player="beta", season=2020, stats=["ast_ratio"]))
    # Beta over 4 games: 40 fga, 16 fta, 22 ast (10 + 4 + 4 + 4), 12 tov
    # 100 * 22 / (40 + 0.44*16 + 22 + 12) = 2200 / 81.04 = 27.15
    assert rows[0]["ast_ratio"] == pytest.approx(27.15, abs=0.1)


def test_advanced_stat_is_not_rescaled_by_per_mode(conn):
    a = run(conn, StatQuery(player="beta", season=2020, stats=["ast_ratio"],
                            per_mode="totals"))
    b = run(conn, StatQuery(player="beta", season=2020, stats=["ast_ratio"],
                            per_mode="per36"))
    assert a[0]["ast_ratio"] == b[0]["ast_ratio"]


def test_unknown_stat_message_lists_advanced(conn):
    with pytest.raises(ValueError, match="usg_pct"):
        run(conn, StatQuery(stats=["not_real"]))


def test_include_totals_adds_total_column(conn):
    rows = run(conn, StatQuery(season=2020, stats=["pts"], per_mode="per_game",
                               include_totals=True))
    assert "pts_total" in rows[0]
    alpha = next(r for r in rows if r["player"] == "Alpha Scorer")
    assert alpha["pts"] == 25.0 and alpha["pts_total"] == 100


def test_include_totals_noop_in_totals_mode(conn):
    """In totals mode the column already IS the total — don't duplicate it."""
    rows = run(conn, StatQuery(season=2020, stats=["pts"], per_mode="totals",
                               include_totals=True))
    assert "pts_total" not in rows[0]


def test_include_totals_skips_rate_stats(conn):
    rows = run(conn, StatQuery(season=2020, stats=["fg_pct"],
                               include_totals=True))
    assert "fg_pct_total" not in rows[0]


# ---------------------------------------------------------------------------
# Sorting robustness and derived counts as columns
# ---------------------------------------------------------------------------

def test_order_by_dropped_column_falls_back(conn):
    """Deselecting the sorted column must not produce invalid SQL.

    This was a real bug: the UI dropped PTS but kept ORDER BY pts, and SQLite
    failed the whole query with 'no such column'.
    """
    rows = run(conn, StatQuery(stats=["reb"], order_by="pts", season=2020))
    assert rows
    sql, _ = build_sql(StatQuery(stats=["reb"], order_by="pts"))
    assert "ORDER BY reb" in sql


def test_order_by_valid_column_is_respected(conn):
    sql, _ = build_sql(StatQuery(stats=["pts", "reb"], order_by="reb"))
    assert "ORDER BY reb" in sql


def test_order_by_always_present_column(conn):
    """gp and season are always in the result, so they're always sortable."""
    for col in ("gp", "season"):
        sql, _ = build_sql(StatQuery(stats=["pts"], order_by=col))
        assert f"ORDER BY {col}" in sql


def test_order_by_totals_column(conn):
    sql, _ = build_sql(StatQuery(stats=["pts"], order_by="pts_total",
                                 include_totals=True))
    assert "ORDER BY pts_total" in sql


def test_empty_stats_rejected(conn):
    with pytest.raises(ValueError, match="at least one column"):
        run(conn, StatQuery(stats=[]))


def test_derived_counts_as_columns(conn):
    rows = run(conn, StatQuery(stats=["double_doubles", "triple_doubles"],
                               season=2020))
    beta = next(r for r in rows if r["player"] == "Beta Allrounder")
    assert beta["double_doubles"] == 4
    assert beta["triple_doubles"] == 1


def test_derived_counts_ignore_per_mode(conn):
    """A double-double count is a count; per-36 must not rescale it."""
    a = run(conn, StatQuery(stats=["double_doubles"], season=2020,
                            per_mode="per_game"))
    b = run(conn, StatQuery(stats=["double_doubles"], season=2020,
                            per_mode="per36"))
    assert a == b


def test_derived_counts_sortable(conn):
    rows = run(conn, StatQuery(stats=["double_doubles"], season=2020,
                               order_by="double_doubles"))
    counts = [r["double_doubles"] for r in rows]
    assert counts == sorted(counts, reverse=True)


# ---------------------------------------------------------------------------
# Team column
# ---------------------------------------------------------------------------

def test_player_rows_carry_team(conn):
    rows = run(conn, StatQuery(season=2020, stats=["pts"]))
    assert all("team" in r for r in rows)
    alpha = next(r for r in rows if r["player"] == "Alpha Scorer")
    assert alpha["team"] == "BOS"


def test_team_column_sits_next_to_player(conn):
    rows = run(conn, StatQuery(season=2020, stats=["pts"]))
    keys = list(rows[0].keys())
    assert keys.index("team") == keys.index("player") + 1


def test_traded_player_lists_both_teams(conn):
    """One row per player-season, with every team he appeared for."""
    conn.execute(
        """INSERT INTO player_games (game_id, player_id, team_id, season,
           season_type, game_date, min, fgm, fga, fg3m, fg3a, ftm, fta,
           oreb, dreb, reb, ast, stl, blk, tov, pf, pts, plus_minus)
           VALUES ('0022000003', 1001, 1610612747, 2020, 'Regular Season',
                   '2021-01-05', 30, 5, 10, 1, 3, 2, 2, 1, 3, 4, 2, 1, 0, 1, 2, 13, 0)
           ON CONFLICT(game_id, player_id) DO UPDATE SET team_id = 1610612747"""
    )
    conn.commit()
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"]))
    teams = rows[0]["team"].split(",")
    assert set(teams) == {"BOS", "LAL"}


def test_team_filter_still_works_with_team_column(conn):
    rows = run(conn, StatQuery(team="LAL", season=2020, stats=["pts"]))
    assert all("LAL" in r["team"] for r in rows)
