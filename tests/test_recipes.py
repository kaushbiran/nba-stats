"""Recipe tests — the derived stats that need game-level data."""

import pytest

from nbastats import recipes


def test_double_doubles(conn):
    """Beta had 12 pts / 11 reb in all 4 games of 2020."""
    rows = recipes.double_doubles(conn, player="beta", season=2020)
    assert len(rows) == 1
    assert rows[0]["double_doubles"] == 4


def test_triple_double_counted_once(conn):
    """Only game 1 had 12/11/10; the other three had 4 assists."""
    rows = recipes.double_doubles(conn, player="beta", season=2020)
    assert rows[0]["triple_doubles"] == 1


def test_triple_doubles_listing(conn):
    rows = recipes.triple_doubles(conn, player="beta")
    assert len(rows) == 1
    assert rows[0]["pts"] == 12 and rows[0]["reb"] == 11 and rows[0]["ast"] == 10


def test_alpha_has_no_double_doubles(conn):
    """25 pts but only 5 reb / 3 ast — scoring alone is not a double-double."""
    rows = recipes.double_doubles(conn, player="alpha")
    assert rows == [] or all(r["double_doubles"] == 0 for r in rows)


def test_single_digit_scoring_seasons(conn):
    """Gamma averaged 6.0 ppg over 4 games."""
    rows = recipes.single_digit_scoring_seasons(conn, min_games=4)
    names = {r["player"] for r in rows}
    assert "Gamma Bench" in names
    assert "Alpha Scorer" not in names
    gamma = next(r for r in rows if r["player"] == "Gamma Bench")
    assert gamma["ppg"] == 6.0


def test_single_digit_respects_min_games(conn):
    """Raising the games floor above the fixture should empty the result."""
    assert recipes.single_digit_scoring_seasons(conn, min_games=58) == []


def test_threshold_games(conn):
    """Alpha has exactly one 40-point game (2021 game 1)."""
    rows = recipes.games_above_threshold(conn, stat="pts", threshold=40)
    assert len(rows) == 1
    assert rows[0]["player"] == "Alpha Scorer"
    assert rows[0]["games"] == 1
    assert rows[0]["best"] == 40


def test_threshold_boundary_is_inclusive(conn):
    """>= 25 must include the 25-point games, not exclude them."""
    rows = recipes.games_above_threshold(conn, stat="pts", threshold=25, season=2020)
    alpha = next(r for r in rows if r["player"] == "Alpha Scorer")
    assert alpha["games"] == 4


def test_threshold_rejects_bad_stat(conn):
    with pytest.raises(ValueError):
        recipes.games_above_threshold(conn, stat="height")


def test_averages_club(conn):
    """Alpha 2020: 25.0 ppg but only 5 rpg — fails a 20/10/5 filter."""
    assert recipes.season_averages_club(conn, pts=20, reb=10, ast=5,
                                        min_games=4) == []
    # Lower the bar to something he clears
    rows = recipes.season_averages_club(conn, pts=20, reb=5, ast=3, min_games=4)
    assert any(r["player"] == "Alpha Scorer" for r in rows)


def test_iron_man(conn):
    rows = recipes.iron_man_seasons(conn, min_games=4)
    assert {r["player"] for r in rows} == {"Alpha Scorer", "Beta Allrounder",
                                           "Gamma Bench"}


def test_team_records(conn):
    """Home team wins all 6 fixture games; each team hosts 3."""
    rows = recipes.team_records(conn, season=2020)
    for r in rows:
        assert r["wins"] + r["losses"] == 4
        assert r["wins"] == r["home_wins"]  # all wins came at home
        assert r["away_wins"] == 0


def test_team_ratings_shape(conn):
    rows = recipes.team_ratings(conn, season=2020)
    assert len(rows) == 2
    for r in rows:
        assert r["ortg"] > 0 and r["drtg"] > 0
        assert r["net_rtg"] == pytest.approx(r["ortg"] - r["drtg"], abs=0.2)


def test_registry_covers_all_recipes(conn):
    """Every recipe in the registry must be callable with just a connection."""
    for name, fn in recipes.RECIPES.items():
        result = fn(conn)
        assert isinstance(result, list), f"{name} did not return a list"


def test_triple_double_leaders_shows_counts(conn):
    """The leaderboard must rank by count, not list individual games."""
    rows = recipes.triple_double_leaders(conn)
    assert rows
    assert "triple_doubles" in rows[0]
    beta = next(r for r in rows if r["player"] == "Beta Allrounder")
    assert beta["triple_doubles"] == 1
    assert beta["double_doubles"] == 4


def test_triple_double_leaders_excludes_zero(conn):
    rows = recipes.triple_double_leaders(conn)
    assert all(r["triple_doubles"] > 0 for r in rows)


def test_triple_double_leaders_sorted_descending(conn):
    rows = recipes.triple_double_leaders(conn)
    counts = [r["triple_doubles"] for r in rows]
    assert counts == sorted(counts, reverse=True)


def test_registry_maps_triple_doubles_to_leaderboard(conn):
    """The preset chip must get counts; the game list lives under its own name."""
    assert recipes.RECIPES["triple-doubles"] is recipes.triple_double_leaders
    assert recipes.RECIPES["triple-double-games"] is recipes.triple_doubles


def test_triple_double_games_still_lists_games(conn):
    rows = recipes.RECIPES["triple-double-games"](conn, player="beta")
    assert len(rows) == 1
    assert "game_date" in rows[0]


# ---------------------------------------------------------------------------
# Every player recipe names a team
#
# These rows sit in the same table as everything else, so a row without a
# team badge is the odd one out. Team also comes first, matching the column
# order the rest of the app uses.
# ---------------------------------------------------------------------------

PLAYER_RECIPES = [
    ("double-doubles", {}),
    ("triple-doubles", {}),
    ("triple-double-games", {}),
    ("threshold-games", {"stat": "pts", "threshold": 20}),
    ("single-digit-scoring", {"min_games": 1}),
    ("averages-club", {"pts": 1, "reb": 1, "ast": 0, "min_games": 1}),
    ("iron-man", {"min_games": 1}),
]


@pytest.mark.parametrize("name,kwargs", PLAYER_RECIPES)
def test_player_recipe_includes_a_team(conn, name, kwargs):
    rows = recipes.RECIPES[name](conn, **kwargs)
    assert rows, f"{name} returned nothing, so this proves nothing"
    assert "team" in rows[0], f"{name} has no team column"
    assert rows[0]["team"], f"{name} has an empty team"


@pytest.mark.parametrize("name,kwargs", PLAYER_RECIPES)
def test_player_recipe_puts_team_first(conn, name, kwargs):
    rows = recipes.RECIPES[name](conn, **kwargs)
    keys = list(rows[0])
    assert keys[0] == "team", f"{name} starts with {keys[0]!r}, not team"
    assert keys[1] == "player", f"{name} has {keys[1]!r} where player should be"


def test_team_recipes_already_lead_with_team(conn):
    for name in ("team-ratings", "team-records"):
        rows = recipes.RECIPES[name](conn)
        assert list(rows[0])[0] == "team", name


def test_traded_player_shows_both_teams(conn):
    """A season-grouped row covers every team the player appeared for, the
    same way the main query does."""
    rows = recipes.RECIPES["iron-man"](conn, min_games=1)
    alpha = [r for r in rows if r["player"] == "Alpha Scorer"]
    assert alpha
    assert all(r["team"] == "BOS" for r in alpha)


def test_game_level_recipe_names_one_team(conn):
    """A single game has exactly one team, so no comma-joined list."""
    for row in recipes.RECIPES["triple-double-games"](conn):
        assert "," not in row["team"], row
