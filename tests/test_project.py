"""Projection tests.

Two fixtures here. The small one from conftest covers error handling; the
`league` fixture below builds a synthetic season with known true rates, so the
projection can be checked against a number we chose on purpose rather than
against whatever the model happens to output.
"""

import math
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nbastats import db, project  # noqa: E402
from nbastats.project import (  # noqa: E402
    ProjectionError, american_to_implied, compare_to_line, devig, parlay_math,
)


# ---------------------------------------------------------------------------
# Odds math — checkable by hand
# ---------------------------------------------------------------------------

def test_american_to_implied_negative():
    assert american_to_implied(-110) == pytest.approx(0.5238, abs=1e-4)
    assert american_to_implied(-200) == pytest.approx(0.6667, abs=1e-4)


def test_american_to_implied_positive():
    assert american_to_implied(150) == pytest.approx(0.40)
    assert american_to_implied(100) == pytest.approx(0.50)


def test_zero_odds_rejected():
    with pytest.raises(ValueError):
        american_to_implied(0)


def test_devig_symmetric_market_is_coinflip():
    d = devig(-110, -110)
    assert d["fair_over"] == pytest.approx(0.5)
    assert d["hold_pct"] == pytest.approx(4.55, abs=0.01)


def test_devig_fair_probabilities_sum_to_one():
    d = devig(-140, +120)
    assert d["fair_over"] + d["fair_under"] == pytest.approx(1.0, abs=1e-4)


def test_devig_overround_exceeds_one():
    """Both sides always imply more than 100% — that excess is the margin."""
    assert devig(-110, -110)["overround"] > 1.0


def test_parlay_edge_grows_with_legs():
    edges = [parlay_math(n)["house_edge_pct"] for n in range(1, 6)]
    assert edges == sorted(edges), "house edge must increase with each leg"
    assert edges[0] == pytest.approx(4.55, abs=0.01)
    assert edges[3] == pytest.approx(16.98, abs=0.05)


def test_parlay_payout_matches_compounding():
    """A 4-leg -110 parlay pays 13.283x, i.e. 12.283-to-1."""
    assert parlay_math(4)["payout_multiple"] == pytest.approx(1.909090**4, abs=0.01)


def test_parlay_requires_a_leg():
    with pytest.raises(ValueError):
        parlay_math(0)


# ---------------------------------------------------------------------------
# A synthetic league with known true rates
# ---------------------------------------------------------------------------

TEAM_IDS = {"AAA": 101, "BBB": 102, "CCC": 103, "DDD": 104}

# CCC is a sieve, DDD is a wall. Used to check the opponent adjustment.
DEFENSE = {"AAA": 1.00, "BBB": 1.00, "CCC": 1.25, "DDD": 0.75}


@pytest.fixture
def league(tmp_path):
    """One 2022 season: 4 teams, a double round robin, two tracked players.

    Star scores at a true rate of 0.75 pts/min over 34 minutes (25.5 a game)
    before any opponent effect. Role plays 20 minutes at 0.25 (5.0 a game).
    """
    conn = db.connect(tmp_path / "league.db")
    db.init_schema(conn)
    rng = random.Random(42)

    for ab, tid in TEAM_IDS.items():
        conn.execute("INSERT INTO teams (team_id, abbreviation, full_name) VALUES (?,?,?)",
                     (tid, ab, ab))
    conn.execute("INSERT INTO players (player_id, full_name) VALUES (9001, 'Star Player')")
    conn.execute("INSERT INTO players (player_id, full_name) VALUES (9002, 'Role Player')")

    names = list(TEAM_IDS)
    schedule = []
    gid = 0
    # Each ordered pair meets 6 times -> 36 games apiece for the home team set.
    for _ in range(6):
        for home in names:
            for away in names:
                if home == away:
                    continue
                gid += 1
                schedule.append((f"00{gid:08d}", home, away))

    day = 0
    for game_id, home, away in schedule:
        day += 1
        date = f"2022-11-{(day % 28) + 1:02d}"
        # games first: team_games and player_games both reference it.
        conn.execute(
            """INSERT INTO games (game_id, season, season_type, game_date,
               home_team_id, away_team_id, home_pts, away_pts)
               VALUES (?,?,?,?,?,?,?,?)""",
            (game_id, 2022, "Regular Season", date,
             TEAM_IDS[home], TEAM_IDS[away], 110, 108),
        )
        for team, opp, is_home in [(home, away, 1), (away, home, 0)]:
            # Team scoring scales with how leaky the OPPONENT is.
            pts = int(rng.gauss(110 * DEFENSE[opp], 6))
            conn.execute(
                f"""INSERT INTO team_games (game_id, team_id, opp_team_id, season,
                    season_type, game_date, is_home, wl, min, fgm, fga, fg3m, fg3a,
                    ftm, fta, oreb, dreb, reb, ast, stl, blk, tov, pf, pts, plus_minus)
                    VALUES ({",".join("?" * 25)})""",
                (game_id, TEAM_IDS[team], TEAM_IDS[opp], 2022, "Regular Season",
                 date, is_home, "W", 240.0, 40, 88, 12, 33, 18, 22,
                 10, 34, 44, 25, 8, 5, 14, 20, pts, 0),
            )
        # Our two players both play for AAA.
        if "AAA" not in (home, away):
            continue
        opp = away if home == "AAA" else home
        for pid, rate, mins in [(9001, 0.75, 34), (9002, 0.25, 20)]:
            minutes = max(8.0, rng.gauss(mins, 3))
            pts = max(0, int(rng.gauss(rate * minutes * DEFENSE[opp], 5)))
            conn.execute(
                f"""INSERT INTO player_games (game_id, player_id, team_id, season,
                    season_type, game_date, wl, min, fgm, fga, fg3m, fg3a, ftm, fta,
                    oreb, dreb, reb, ast, stl, blk, tov, pf, pts, plus_minus)
                    VALUES ({",".join("?" * 24)})""",
                (game_id, pid, TEAM_IDS["AAA"], 2022, "Regular Season", date, "W",
                 minutes, pts // 2, 15, 2, 6, 2, 3, 1, 4, 5,
                 max(0, int(rng.gauss(5, 2))), 1, 0, 2, 2, pts, 0),
            )
    conn.commit()
    yield conn
    conn.close()


def test_projection_lands_near_true_rate(league):
    """True mean is ~25.5 against an average defense; allow sampling noise."""
    out = project.project(league, "Star", stat="pts", season=2022)
    assert 21 <= out["projection"] <= 30
    assert out["games_used"] == project.DEFAULT_LOOKBACK


def test_projection_reports_spread(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    assert out["std_dev"] > 0
    lo, hi = out["range_68"]
    assert lo < out["projection"] < hi
    assert out["range_95"][0] < lo and hi < out["range_95"][1]


def test_weak_defense_raises_projection(league):
    """CCC allows 25% more than average, DDD 25% less."""
    soft = project.project(league, "Star", stat="pts", opponent="CCC", season=2022)
    tough = project.project(league, "Star", stat="pts", opponent="DDD", season=2022)
    assert soft["projection"] > tough["projection"]
    assert soft["adjustments"]["opponent"]["factor"] > 1.1
    assert tough["adjustments"]["opponent"]["factor"] < 0.9


def test_opponent_factor_relative_to_league_average(league):
    out = project.project(league, "Star", stat="pts", opponent="CCC", season=2022)
    d = out["adjustments"]["opponent"]
    assert d["applied"] is True
    assert d["allowed_per_game"] > d["league_average"]


def test_unknown_opponent_is_reported_not_silently_ignored(league):
    out = project.project(league, "Star", stat="pts", opponent="ZZZ", season=2022)
    d = out["adjustments"]["opponent"]
    assert d["applied"] is False
    assert "ZZZ" in d["reason"]
    # and the projection is unchanged by it
    assert out["projection"] == pytest.approx(out["baseline"], abs=0.01)


def test_role_player_projects_lower(league):
    star = project.project(league, "Star", stat="pts", season=2022)
    role = project.project(league, "Role", stat="pts", season=2022)
    assert role["projection"] < star["projection"]
    assert role["projected_minutes"] < star["projected_minutes"]


def test_venue_adjustment_is_capped(league):
    """Even a lopsided split can't move the projection more than 10%."""
    home = project.project(league, "Star", stat="pts", season=2022, is_home=True)
    away = project.project(league, "Star", stat="pts", season=2022, is_home=False)
    for out in (home, away):
        f = out["adjustments"]["venue"]
        if f["applied"]:
            assert 0.90 <= f["factor"] <= 1.10


def test_line_analysis_two_estimates(league):
    out = project.project(league, "Star", stat="pts", season=2022, line=25.5)
    la = out["line_analysis"]
    assert 0 <= la["model_over_probability"] <= 1
    assert 0 <= la["empirical_over_rate"] <= 1
    assert la["model_over_probability"] + la["model_under_probability"] == pytest.approx(1.0)
    assert "-" in la["empirical_record"]


def test_line_far_above_projection_is_unlikely(league):
    out = project.project(league, "Star", stat="pts", season=2022, line=60)
    assert out["line_analysis"]["model_over_probability"] < 0.02


def test_line_far_below_projection_is_likely(league):
    out = project.project(league, "Star", stat="pts", season=2022, line=2)
    assert out["line_analysis"]["model_over_probability"] > 0.98


def test_compare_to_market_reports_gap(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    values = [g["value"] for g in out["game_log"]]
    cmp = project.compare_to_market(out["projection"], out["std_dev"], values,
                                    25.5, -110, -110)
    assert cmp["market"]["fair_over"] == pytest.approx(0.5)
    assert cmp["model_vs_market_over"] == pytest.approx(
        cmp["model_over_probability"] - 0.5, abs=1e-4
    )


def test_game_log_is_newest_first(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    dates = [g["game_date"] for g in out["game_log"]]
    assert dates == sorted(dates, reverse=True)


def test_game_log_carries_opponent(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    assert all(g["opponent"] in TEAM_IDS for g in out["game_log"])


def test_caveats_always_present(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    assert len(out["caveats"]) >= 3
    assert any("injury" in c.lower() for c in out["caveats"])


# ---------------------------------------------------------------------------
# Refusing to project when it would be dishonest
# ---------------------------------------------------------------------------

def test_unknown_player_raises(league):
    with pytest.raises(ProjectionError, match="no player matching"):
        project.project(league, "Nobody At All", stat="pts")


def test_unsupported_stat_raises(league):
    with pytest.raises(ProjectionError, match="can't project"):
        project.project(league, "Star", stat="height")


def test_too_few_games_raises(conn):
    """The small fixture has 4-6 games per player; the floor is 5 per season."""
    with pytest.raises(ProjectionError, match="need at least 5"):
        project.project(conn, "alpha", stat="pts", season=2021)


def test_lookback_limits_sample(league):
    out = project.project(league, "Star", stat="pts", season=2022, lookback=8)
    assert out["games_used"] == 8
    assert len(out["game_log"]) == 8


# ---------------------------------------------------------------------------
# Projections are for the current season only
# ---------------------------------------------------------------------------

def test_past_season_refused(conn):
    """conftest has 2020 and 2021; 2020 is finished, so it can't be projected."""
    with pytest.raises(ProjectionError, match="finished"):
        project.project(conn, "alpha", stat="pts", season=2020)


def test_past_season_message_names_both_seasons(conn):
    try:
        project.project(conn, "alpha", stat="pts", season=2020)
    except ProjectionError as exc:
        assert "2020-21" in str(exc)
        assert "2021-22" in str(exc)
    else:
        pytest.fail("expected a ProjectionError")


def test_backtest_override_allows_past_seasons(league):
    """allow_past_seasons exists so the backtest can score known outcomes."""
    out = project.project(league, "Star", stat="pts", season=2022,
                          allow_past_seasons=True)
    assert out["projection"] > 0


def test_newest_season_still_projects(league):
    out = project.project(league, "Star", stat="pts", season=2022)
    assert out["season"] == 2022


def test_latest_season_helper(conn):
    assert project.latest_season(conn) == 2021


def test_season_label_format():
    assert project.seasonlabel(2023) == "2023-24"
    assert project.seasonlabel(1999) == "1999-00"
