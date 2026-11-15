"""The /api/project handler."""

import pytest

from nbastats import web
from nbastats.project import ProjectionError

# Reuse the synthetic league from the projection tests.
from test_project import league  # noqa: F401


def test_project_endpoint_returns_distribution(league):
    out = web._handle_project(league, {"player": ["Star"], "stat": ["pts"]})
    assert out["player"] == "Star Player"
    assert out["projection"] > 0
    assert out["std_dev"] > 0
    assert len(out["range_68"]) == 2


def test_project_endpoint_requires_player(league):
    with pytest.raises(ValueError, match="player is required"):
        web._handle_project(league, {"stat": ["pts"]})


def test_project_endpoint_passes_opponent(league):
    soft = web._handle_project(league, {"player": ["Star"], "opponent": ["CCC"]})
    tough = web._handle_project(league, {"player": ["Star"], "opponent": ["DDD"]})
    assert soft["projection"] > tough["projection"]


def test_project_endpoint_parses_venue(league):
    out = web._handle_project(league, {"player": ["Star"], "venue": ["home"]})
    v = out["adjustments"]["venue"]
    assert v["applied"] is False or v["venue"] == "home"


def test_project_endpoint_line_analysis(league):
    out = web._handle_project(league, {"player": ["Star"], "line": ["25.5"]})
    assert out["line_analysis"]["line"] == 25.5


def test_project_endpoint_blank_params_ignored(league):
    """The UI sends empty strings for unset selects; they must not break."""
    out = web._handle_project(league, {
        "player": ["Star"], "opponent": [""], "venue": [""], "line": [""],
    })
    assert out["projection"] > 0


def test_project_endpoint_unknown_player_is_value_error(league):
    """ProjectionError subclasses ValueError so the server answers 400."""
    with pytest.raises(ValueError):
        web._handle_project(league, {"player": ["Nobody At All"]})
    assert issubclass(ProjectionError, ValueError)


def test_meta_lists_projectable_stats(conn):
    assert "pts" in web._meta(conn)["projectable"]


# ---------------------------------------------------------------------------
# Player comparison with per-entry scopes
# ---------------------------------------------------------------------------

def test_scopes_lists_seasons_and_teams(conn):
    s = web._player_scopes(conn, 1001)
    assert s["player"] == "Alpha Scorer"
    assert [x["season"] for x in s["seasons"]] == [2021, 2020]
    assert {t["team"] for t in s["teams"]} == {"BOS"}


def test_scopes_unknown_player(conn):
    with pytest.raises(ValueError, match="no player"):
        web._player_scopes(conn, 99999)


def test_parse_entry_defaults_to_career(conn):
    assert web._parse_entry("1001") == {
        "player_id": 1001, "scope": "career", "value": ""}


def test_parse_entry_team_and_season(conn):
    assert web._parse_entry("1001~team~BOS")["value"] == "BOS"
    assert web._parse_entry("1001~season~2020")["scope"] == "season"


def test_parse_entry_rejects_junk(conn):
    for bad in ("", "abc~team~BOS", "1001~nonsense~x"):
        with pytest.raises(ValueError):
            web._parse_entry(bad)


def test_compare_career_scope(conn):
    out = web._handle_compare(conn, {"p": ["1001"], "stats": ["pts"],
                                     "per_mode": ["totals"]})
    assert len(out["rows"]) == 1
    r = out["rows"][0]
    assert r["player"] == "Alpha Scorer" and r["counting"] == "career"
    assert r["pts"] == 160          # both seasons combined


def test_compare_season_scope(conn):
    out = web._handle_compare(conn, {"p": ["1001~season~2020"],
                                     "stats": ["pts"], "per_mode": ["totals"]})
    r = out["rows"][0]
    assert r["counting"] == "2020-21"
    assert r["pts"] == 100


def test_compare_team_scope(conn):
    """The feature this was built for: a player's line for one team only."""
    out = web._handle_compare(conn, {"p": ["1003~team~LAL"], "stats": ["pts"],
                                     "per_mode": ["totals"]})
    r = out["rows"][0]
    assert r["counting"] == "LAL"
    assert r["pts"] == 24


def test_compare_same_player_two_scopes(conn):
    """Comparing one player's stints against each other must keep both rows."""
    out = web._handle_compare(conn, {
        "p": ["1001~season~2020", "1001~season~2021"],
        "stats": ["pts"], "per_mode": ["totals"]})
    assert [r["counting"] for r in out["rows"]] == ["2020-21", "2021-22"]
    assert [r["pts"] for r in out["rows"]] == [100, 60]


def test_compare_rows_share_a_column_shape(conn):
    """Columns have to line up even when one entry matched nothing."""
    out = web._handle_compare(conn, {
        "p": ["1001", "1001~team~ZZZ"], "stats": ["pts", "reb"]})
    assert list(out["rows"][0].keys()) == list(out["rows"][1].keys())
    assert "no games" in out["rows"][1]["counting"]
    assert out["rows"][1]["pts"] is None


def test_compare_team_column_not_duplicated(conn):
    """A team-scoped row leaves Team blank, since Counting already says it."""
    out = web._handle_compare(conn, {"p": ["1001~team~BOS"], "stats": ["pts"]})
    assert out["rows"][0]["team"] == ""
    career = web._handle_compare(conn, {"p": ["1001"], "stats": ["pts"]})
    assert career["rows"][0]["team"] == "BOS"


def test_compare_requires_an_entry(conn):
    with pytest.raises(ValueError, match="at least one player"):
        web._handle_compare(conn, {})


def test_compare_caps_entries(conn):
    with pytest.raises(ValueError, match="six"):
        web._handle_compare(conn, {"p": ["1001"] * 7})
