"""Web API tests — exercise the handler functions directly, no socket needed."""

import pytest

from nbastats import web


def test_meta_reports_seasons_and_stats(conn):
    m = web._meta(conn)
    assert m["seasons"] == [2021, 2020]           # newest first
    assert m["season_types"] == ["Regular Season"]
    assert "pts" in m["counting_stats"]
    assert "fg_pct" in m["rate_stats"]
    assert m["empty"] is False
    assert any(r["name"] == "double-doubles" for r in m["recipes"])


def test_meta_flags_empty_database(tmp_path):
    from nbastats import db
    empty = db.connect(tmp_path / "empty.db")
    db.init_schema(empty)
    assert web._meta(empty)["empty"] is True


def test_search_matches_substring(conn):
    players = web._search_players(conn, "alpha")
    assert len(players) == 1
    assert players[0]["full_name"] == "Alpha Scorer"
    assert players[0]["gp"] == 6


def test_search_prefers_prefix_match(conn):
    """'be' matches Beta at the start and nothing else; prefix wins ordering."""
    players = web._search_players(conn, "be")
    assert players[0]["full_name"] == "Beta Allrounder"


def test_search_empty_term_returns_nothing(conn):
    assert web._search_players(conn, "") == []
    assert web._search_players(conn, "   ") == []


def test_search_reports_career_span(conn):
    p = web._search_players(conn, "alpha")[0]
    assert p["first_season"] == 2020
    assert p["last_season"] == 2021


def test_query_handler_parses_params(conn):
    out = web._handle_query(conn, {
        "player": ["alpha"], "season": ["2020"],
        "stats": ["pts,reb"], "per_mode": ["per_game"],
    })
    assert out["per_mode"] == "per_game"
    assert out["rows"][0]["pts"] == 25.0


def test_query_handler_career_flag(conn):
    out = web._handle_query(conn, {
        "player": ["alpha"], "stats": ["pts"],
        "per_mode": ["totals"], "career": ["1"],
    })
    assert len(out["rows"]) == 1
    assert out["rows"][0]["pts"] == 160


def test_query_handler_caps_limit(conn):
    """A hand-edited URL must not be able to request a million rows."""
    out = web._handle_query(conn, {"limit": ["999999"], "stats": ["pts"]})
    assert len(out["rows"]) <= 500


def test_query_handler_rejects_bad_stat(conn):
    with pytest.raises(ValueError):
        web._handle_query(conn, {"stats": ["nonsense"]})


def test_recipe_handler_runs(conn):
    out = web._handle_recipe(conn, "double-doubles", {"player": ["beta"]})
    assert out["recipe"] == "double-doubles"
    assert out["rows"][0]["double_doubles"] == 4


def test_recipe_handler_drops_unaccepted_params(conn):
    """min_games is passed by the UI for every recipe; recipes that don't take
    it must ignore it rather than raise TypeError."""
    out = web._handle_recipe(conn, "team-records",
                             {"min_games": ["58"], "threshold": ["40"]})
    assert isinstance(out["rows"], list)


def test_recipe_handler_unknown_name(conn):
    with pytest.raises(KeyError):
        web._handle_recipe(conn, "does-not-exist", {})


def test_teams_listing(conn):
    teams = web._teams(conn)
    assert {t["abbreviation"] for t in teams} == {"BOS", "LAL"}


def test_static_index_exists():
    index = web.STATIC_DIR / "index.html"
    assert index.exists(), "the UI page is missing from the package"
    assert "<title>" in index.read_text()


# ---------------------------------------------------------------------------
# The `stats` parameter: absent, empty, and populated are three states
# ---------------------------------------------------------------------------

def test_stats_absent_uses_the_defaults(conn):
    assert web._stats_param({}) == ["pts", "reb", "ast"]


def test_stats_empty_means_no_columns(conn):
    """Not the same as absent. The Clear all button sends `stats=`, and
    quietly restoring the defaults would make it look broken."""
    assert web._stats_param({"stats": [""]}) == []


def test_stats_parsing_trims_and_drops_blanks(conn):
    assert web._stats_param({"stats": [" pts , ,reb "]}) == ["pts", "reb"]


def test_query_with_no_stats_returns_identity_columns(conn):
    out = web._handle_query(conn, {"stats": [""], "season": ["2020"]})
    assert set(out["rows"][0]) == {"player_id", "player", "team",
                                   "season", "gp"}
    assert out["rows"][0]["gp"] == 4


def test_query_with_no_stats_sorts_by_games_played(conn):
    """There is no stats[0] to fall back on, so the sort key has to be gp."""
    out = web._handle_query(conn, {"stats": [""]})
    counts = [r["gp"] for r in out["rows"]]
    assert counts == sorted(counts, reverse=True)


def test_compare_with_no_stats(conn):
    out = web._handle_compare(conn, {"p": ["1001~career~"], "stats": [""]})
    assert "gp" in out["rows"][0]
    assert "pts" not in out["rows"][0]


def test_blank_values_survive_query_string_parsing():
    """parse_qs drops blank values by default, which would make `stats=`
    invisible to the handler and the Clear all button a no-op."""
    from urllib.parse import parse_qs
    assert "stats" not in parse_qs("stats=&season=2023")
    kept = parse_qs("stats=&season=2023", keep_blank_values=True)
    assert kept["stats"] == [""]
    assert web._stats_param(kept) == []


def test_handler_reads_blank_values(conn):
    """The server must pass keep_blank_values through, or the above is moot."""
    import inspect
    src = inspect.getsource(web)
    assert "keep_blank_values=True" in src
