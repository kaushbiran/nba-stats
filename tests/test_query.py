"""Query layer tests. Every expected value is hand-computable from conftest."""

import pytest

from nbastats.query import StatQuery, build_sql, count, run


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
    """Per 100 TEAM possessions while on the floor, not per 100 of the
    player's own used possessions.

    Alpha, 2020: 100 points in 4 games, 36 minutes each (144 total).
    Boston's team line each game: fga 88, oreb 10, tov 14, fta 22, min 240.

        team poss/game = 88 - 10 + 14 + 0.44*22       = 101.68
        team poss      = 101.68 * 4                   = 406.72
        floor share    = 144 / (960 / 5)              = 0.75
        poss on floor  = 406.72 * 0.75                = 305.04
        per 100        = 100 / 305.04 * 100           = 32.78

    An earlier version divided by the player's own possessions and returned
    114.26 — a figure that would mean one player outscoring a whole team.
    """
    rows = run(conn, StatQuery(player="alpha", season=2020, stats=["pts"],
                               per_mode="per100"))
    assert rows[0]["pts"] == pytest.approx(32.78, abs=0.05)


def test_per100_is_in_a_plausible_range(conn):
    """A guard against the class of bug this replaced: no real player scores
    anywhere near 100 points per 100 possessions."""
    rows = run(conn, StatQuery(season=2020, stats=["pts"], per_mode="per100"))
    assert rows, "expected some rows"
    for r in rows:
        assert 0 <= r["pts"] < 70, f"{r['player']} at {r['pts']} per 100"


def test_per100_joins_team_games(conn):
    """The possession estimate needs the team box score."""
    sql, _ = build_sql(StatQuery(stats=["pts"], per_mode="per100"))
    assert "team_games tm" in sql


def test_other_per_modes_skip_team_join(conn):
    for mode in ("totals", "per_game", "per36"):
        sql, _ = build_sql(StatQuery(stats=["pts"], per_mode=mode))
        assert "team_games" not in sql, f"{mode} should not need the join"


def test_per100_scales_with_minutes(conn):
    """Two players on the same team: fewer minutes means fewer possessions on
    the floor, so per-100 is not just a rescaled per-game number."""
    rows = run(conn, StatQuery(season=2020, stats=["pts"], per_mode="per100"))
    by_name = {r["player"]: r["pts"] for r in rows}
    # Gamma plays 18 min to Alpha's 36 and scores 6 to Alpha's 25.
    # Per game that's a 4.2x gap; per 100 it narrows, because Gamma's
    # possessions-on-floor denominator is half the size.
    assert by_name["Alpha Scorer"] / by_name["Gamma Bench"] < 4.2


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
    """gp and season are always in the result, so they're always sortable.

    season comes out table-qualified because team_games also has a column by
    that name once advanced stats join it in.
    """
    sql, _ = build_sql(StatQuery(stats=["pts"], order_by="gp"))
    assert "ORDER BY gp" in sql
    sql, _ = build_sql(StatQuery(stats=["pts"], order_by="season"))
    assert "ORDER BY pg.season" in sql


def test_order_by_totals_column(conn):
    sql, _ = build_sql(StatQuery(stats=["pts"], order_by="pts_total",
                                 include_totals=True))
    assert "ORDER BY pts_total" in sql


def test_no_stats_gives_the_identity_columns(conn):
    """Clearing every column is a deliberate choice, not an error.

    What comes back is who, which team, which season and how many games — a
    clean list to add columns back onto.
    """
    rows = run(conn, StatQuery(stats=[]))
    assert rows, "no stats should still return the players"
    assert set(rows[0]) == {"player_id", "player", "team", "season", "gp"}
    # Careers rather than seasons: the season column goes, the rest stay.
    career = run(conn, StatQuery(stats=[], group_by_season=False))
    assert set(career[0]) == {"player_id", "player", "team", "gp"}


def test_no_stats_still_counts_games(conn):
    """Alpha played 4 games in 2020, Beta 4, Gamma 4."""
    rows = run(conn, StatQuery(stats=[], season=2020))
    assert {r["player"]: r["gp"] for r in rows} == {
        "Alpha Scorer": 4, "Beta Allrounder": 4, "Gamma Bench": 4}


def test_no_stats_orders_by_games_played(conn):
    """With no stat to sort by, the fallback has to be a column that exists —
    reaching for stats[0] would raise IndexError."""
    sql, _ = build_sql(StatQuery(stats=[]))
    assert "ORDER BY gp" in sql
    rows = run(conn, StatQuery(stats=[], group_by_season=False))
    counts = [r["gp"] for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_no_stats_with_a_season_breakdown(conn):
    rows = run(conn, StatQuery(stats=[], player="alpha", group_by_season=True))
    assert {r["season"]: r["gp"] for r in rows} == {2020: 4, 2021: 2}


def test_unknown_stat_still_rejected_when_list_is_short(conn):
    """Allowing an empty list must not weaken the allowlist."""
    with pytest.raises(ValueError, match="unknown stat"):
        run(conn, StatQuery(stats=["dunks"]))


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


# ---------------------------------------------------------------------------
# Row counts, for ranking a page that may be truncated
# ---------------------------------------------------------------------------

def test_count_matches_row_count_when_not_truncated(conn):
    q = StatQuery(season=2020, stats=["pts"])
    assert count(conn, q) == len(run(conn, q))


def test_count_ignores_limit(conn):
    """The limit must not change how many rows the filters actually match."""
    full = count(conn, StatQuery(season=2020, stats=["pts"]))
    assert count(conn, StatQuery(season=2020, stats=["pts"], limit=1)) == full
    assert len(run(conn, StatQuery(season=2020, stats=["pts"], limit=1))) == 1


def test_count_respects_min_games(conn):
    loose = count(conn, StatQuery(season=2020, stats=["pts"], min_games=1))
    strict = count(conn, StatQuery(season=2020, stats=["pts"], min_games=4))
    assert strict <= loose


def test_count_respects_player_filter(conn):
    assert count(conn, StatQuery(player="alpha", stats=["pts"],
                                 group_by_season=False)) == 1


def test_count_respects_season_filter(conn):
    one = count(conn, StatQuery(season=2021, stats=["pts"]))
    both = count(conn, StatQuery(season_range=(2020, 2021), stats=["pts"]))
    assert both > one


def test_count_career_mode_collapses_seasons(conn):
    by_season = count(conn, StatQuery(stats=["pts"], group_by_season=True))
    career = count(conn, StatQuery(stats=["pts"], group_by_season=False))
    assert career < by_season


# ---------------------------------------------------------------------------
# Advanced stats in a single player's season-by-season view
# ---------------------------------------------------------------------------

def test_advanced_stats_sorted_by_season(conn):
    """Regression: the advanced-stat joins bring team_games into the query,
    which also has a `season` column, so a bare ORDER BY season was ambiguous
    and SQLite refused the whole query. The player view always sorts by
    season, so advanced stats were broken there specifically."""
    rows = run(conn, StatQuery(player="alpha", stats=["usg_pct"],
                               order_by="season", descending=False))
    assert len(rows) == 2
    assert [r["season"] for r in rows] == [2020, 2021]
    assert all(r["usg_pct"] is not None for r in rows)


def test_order_by_unselected_column_falls_back(conn):
    """game_date isn't selected by a grouped query, so it can't be the sort
    key; the fallback picks the first visible column instead of erroring."""
    sql, _ = build_sql(StatQuery(stats=["usg_pct"], order_by="game_date"))
    assert "ORDER BY usg_pct" in sql


def test_season_is_qualified_in_order_by(conn):
    sql, _ = build_sql(StatQuery(stats=["usg_pct"], order_by="season"))
    assert "ORDER BY pg.season" in sql


def test_advanced_stats_in_career_mode(conn):
    rows = run(conn, StatQuery(player="alpha", stats=["usg_pct"],
                               group_by_season=False))
    assert len(rows) == 1
    assert rows[0]["usg_pct"] is not None


def test_mixed_regular_and_advanced_by_season(conn):
    rows = run(conn, StatQuery(player="alpha", stats=["pts", "usg_pct"],
                               order_by="season"))
    assert all(r["pts"] is not None and r["usg_pct"] is not None for r in rows)


def test_every_advanced_stat_survives_season_sort(conn):
    """Each one individually, since they have different join dependencies."""
    from nbastats.query import ADVANCED_STATS
    for stat in ADVANCED_STATS:
        rows = run(conn, StatQuery(player="beta", stats=[stat],
                                   order_by="season"))
        assert rows, f"{stat} returned nothing"


# ---------------------------------------------------------------------------
# Paging
# ---------------------------------------------------------------------------

def test_offset_skips_rows(conn):
    all_rows = run(conn, StatQuery(season=2020, stats=["pts"]))
    skipped = run(conn, StatQuery(season=2020, stats=["pts"], offset=1))
    assert len(skipped) == len(all_rows) - 1
    assert skipped[0]["player"] == all_rows[1]["player"]


def test_offset_with_limit_pages_cleanly(conn):
    """Two pages of one row must cover the same players as one page of two."""
    page1 = run(conn, StatQuery(season=2020, stats=["pts"], limit=1, offset=0))
    page2 = run(conn, StatQuery(season=2020, stats=["pts"], limit=1, offset=1))
    both = run(conn, StatQuery(season=2020, stats=["pts"], limit=2))
    assert [page1[0]["player"], page2[0]["player"]] == [r["player"] for r in both]


def test_offset_past_the_end_is_empty_not_an_error(conn):
    assert run(conn, StatQuery(season=2020, stats=["pts"], offset=10_000)) == []


def test_negative_offset_is_clamped(conn):
    a = run(conn, StatQuery(season=2020, stats=["pts"], offset=-5))
    b = run(conn, StatQuery(season=2020, stats=["pts"], offset=0))
    assert a == b


def test_count_ignores_offset(conn):
    """Total has to describe the whole result set, or page numbers drift."""
    q0 = StatQuery(season=2020, stats=["pts"])
    q1 = StatQuery(season=2020, stats=["pts"], offset=2, limit=1)
    assert count(conn, q0) == count(conn, q1)


def test_page_count_follows_min_games(conn):
    """Raising the games floor must shrink the result set, not leave empty
    pages behind — the pager is derived from this number.

    Across both seasons the fixture has four player-seasons; Alpha's 2021 is
    only two games, so a floor of three drops exactly that one.
    """
    loose = count(conn, StatQuery(stats=["pts"], min_games=1))
    strict = count(conn, StatQuery(stats=["pts"], min_games=3))
    assert loose == 4
    assert strict == 3
