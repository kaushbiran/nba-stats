"""The /api/finder endpoint: parameter handling and the response contract.

The handler is exercised directly, no socket needed. Parameters arrive the way
parse_qs produces them — every value a list — because that is what the real
handler is handed.
"""

from __future__ import annotations

import pytest

from nbastats import finder, web


def test_meta_advertises_the_finder(conn):
    m = web._meta(conn)
    assert "pts" in m["finder_fields"]
    assert "cats" in m["finder_fields"]
    # Season-level metrics must not be offered as game filters.
    assert "usg_pct" not in m["finder_fields"]
    assert "double_doubles" not in m["finder_fields"]
    assert "ts_pct" in m["finder_rate_fields"]
    assert m["finder_presets"], "no presets for the UI to show"


def test_meta_presets_carry_what_the_ui_needs(conn):
    for spec in web._meta(conn)["finder_presets"]:
        assert spec["name"] and spec["label"] and spec["conditions"]
        assert spec["help"]


def test_finder_defaults_to_every_game(conn):
    out = web._handle_finder(conn, {})
    assert out["total"] == 14
    assert out["conditions"] == ""
    assert out["columns"] == ["pts", "reb", "ast"]


def test_finder_applies_a_condition(conn):
    out = web._handle_finder(conn, {"where": ["pts>=40"]})
    assert out["total"] == 1
    assert out["rows"][0]["pts"] == 40
    assert out["conditions"] == "pts ≥ 40"


def test_finder_echoes_the_conditions_that_ran(conn):
    """The heading is built from this, so it has to describe the real filter
    rather than whatever the UI believes it sent."""
    out = web._handle_finder(conn, {"where": ["pts>=12,reb>=11"]})
    assert out["conditions"] == "pts ≥ 12 and reb ≥ 11"


def test_finder_adds_the_filtered_column(conn):
    out = web._handle_finder(conn, {"where": ["stl>=1"], "stats": ["pts"]})
    assert out["columns"] == ["pts", "stl"]
    assert "stl" in out["rows"][0]


def test_finder_context_columns_are_in_every_row(conn):
    row = web._handle_finder(conn, {"where": ["pts>=40"]})["rows"][0]
    for col in ("game_date", "player", "team", "opp", "wl", "score"):
        assert col in row, col


def test_finder_filters_combine(conn):
    out = web._handle_finder(conn, {
        "where": ["pts>=25"], "team": ["BOS"], "venue": ["home"],
        "result": ["W"], "season": ["2020"],
    })
    # Alpha's three home games in 2020 are all wins at 25 points; one of them
    # falls in 2021, so the season filter leaves two.
    assert out["total"] == 2
    assert {r["wl"] for r in out["rows"]} == {"W"}
    assert {r["opp"] for r in out["rows"]} == {"vs LAL"}


def test_finder_season_range(conn):
    out = web._handle_finder(conn, {
        "player": ["alpha"], "season_from": ["2020"], "season_to": ["2020"],
    })
    assert out["total"] == 4


def test_finder_player_ids_is_exact(conn):
    out = web._handle_finder(conn, {"player_ids": ["1003"]})
    assert {r["player"] for r in out["rows"]} == {"Gamma Bench"}


def test_finder_date_range(conn):
    out = web._handle_finder(conn, {
        "date_from": ["2021-10-01"], "date_to": ["2021-10-31"],
    })
    assert {r["game_date"] for r in out["rows"]} == {"2021-10-20", "2021-10-25"}


def test_finder_sorting(conn):
    desc = web._handle_finder(conn, {"player": ["alpha"], "order_by": ["pts"]})
    asc = web._handle_finder(conn, {"player": ["alpha"], "order_by": ["pts"],
                                    "asc": ["1"]})
    assert desc["rows"][0]["pts"] == 40
    assert asc["rows"][0]["pts"] == 20
    assert desc["ascending"] is False
    assert asc["ascending"] is True


def test_finder_paging_reports_the_full_total(conn):
    """The table says "2 of 6", so the total must ignore the limit."""
    out = web._handle_finder(conn, {"player": ["alpha"], "limit": ["2"]})
    assert len(out["rows"]) == 2
    assert out["total"] == 6
    assert out["limit"] == 2
    assert out["offset"] == 0


def test_finder_offset(conn):
    page2 = web._handle_finder(conn, {
        "player": ["alpha"], "order_by": ["pts"], "limit": ["2"],
        "offset": ["2"],
    })
    assert page2["offset"] == 2
    assert len(page2["rows"]) == 2


def test_finder_caps_the_limit(conn):
    """A hand-edited URL must not be able to pull the whole table."""
    out = web._handle_finder(conn, {"limit": ["999999"]})
    assert out["limit"] <= 500


def test_finder_clamps_negative_offset(conn):
    out = web._handle_finder(conn, {"offset": ["-10"]})
    assert out["offset"] == 0


def test_finder_blank_params_are_treated_as_absent(conn):
    """Empty selects post as "", which must not become a filter."""
    out = web._handle_finder(conn, {
        "where": [""], "team": [""], "opponent": [""], "venue": [""],
        "result": [""], "season": [""], "player": [""],
    })
    assert out["total"] == 14


@pytest.mark.parametrize("params,match", [
    ({"where": ["pts"]}, "no comparison"),
    ({"where": ["dunks>=5"]}, "can't filter on"),
    ({"where": ["pts>=soon"]}, "isn't a number"),
    ({"venue": ["sideways"]}, "home"),
    ({"result": ["tie"]}, "W"),
    ({"stats": ["usg_pct"]}, "unknown column"),
])
def test_finder_rejects_bad_input(conn, params, match):
    """Each of these surfaces as a 400 with the message, not a 500."""
    with pytest.raises(ValueError, match=match):
        web._handle_finder(conn, params)


def test_finder_presets_all_work_through_the_handler(conn):
    """Every preset button has to produce a valid request end to end."""
    for spec in web._meta(conn)["finder_presets"]:
        params = {"where": [spec["conditions"]],
                  "order_by": [spec.get("order_by", finder.DEFAULT_ORDER)]}
        if spec.get("result"):
            params["result"] = [spec["result"]]
        if spec.get("venue"):
            params["venue"] = [spec["venue"]]
        out = web._handle_finder(conn, params)
        assert "rows" in out and "total" in out, spec["name"]


def test_finder_with_no_stat_columns(conn):
    """Clearing every column leaves the game context, which stands alone."""
    out = web._handle_finder(conn, {"stats": [""], "player": ["alpha"]})
    assert out["columns"] == []
    row = out["rows"][0]
    assert set(row) == {"game_id", "game_date", "season", "player", "team",
                        "opp", "wl", "score"}


def test_finder_condition_field_survives_an_empty_stat_list(conn):
    """Filtering on points with no columns chosen still shows the points —
    otherwise the result would give no clue why those games matched."""
    out = web._handle_finder(conn, {"stats": [""],
                                    "where": ["pts>=40"]})
    assert out["columns"] == ["pts"]
    assert out["rows"][0]["pts"] == 40


def test_finder_route_is_registered():
    """A handler nothing routes to is a feature nobody can reach."""
    source = (web.__file__ and open(web.__file__).read()) or ""
    assert '"/api/finder"' in source
