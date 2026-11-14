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
