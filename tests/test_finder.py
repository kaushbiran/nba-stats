"""Game finder tests.

Every expected number here is checkable by hand against the fixture in
conftest.py. See that file for the full line-by-line layout; the facts these
tests lean on:

    6 games total: 4 in season 2020, 2 in season 2021.
    Boston is home in games 1, 3 and 5 and wins every home game 110-105.
    Alpha  (BOS) played all 6: 25 pts in each 2020 game, then 40 and 20.
    Beta   (BOS) played the 4 games of 2020: 12 pts / 11 reb every time,
           with 10 assists in the first one only — one triple-double.
    Gamma  (LAL) played the 4 games of 2020: 6 pts each.
"""

from __future__ import annotations

import re

import pytest

from nbastats import finder


# ---------------------------------------------------------------------------
# Parsing conditions
#
# This is the only place user text enters the query, so it is tested hardest.
# ---------------------------------------------------------------------------

def test_parse_single_condition():
    assert finder.parse_conditions("pts>=50") == [("pts", ">=", 50.0)]


def test_parse_multiple_conditions():
    assert finder.parse_conditions("pts>=40,reb>=10") == [
        ("pts", ">=", 40.0),
        ("reb", ">=", 10.0),
    ]


def test_parse_tolerates_whitespace_and_case():
    assert finder.parse_conditions("  PTS >= 30 , REB>=5 ") == [
        ("pts", ">=", 30.0),
        ("reb", ">=", 5.0),
    ]


def test_parse_empty_is_no_conditions():
    assert finder.parse_conditions(None) == []
    assert finder.parse_conditions("") == []
    assert finder.parse_conditions("   ") == []


@pytest.mark.parametrize("raw,op", [
    ("pts>=10", ">="), ("pts>10", ">"), ("pts=10", "="),
    ("pts==10", "="), ("pts<=10", "<="), ("pts<10", "<"),
    ("pts!=10", "<>"), ("pts<>10", "<>"),
])
def test_parse_every_operator(raw, op):
    assert finder.parse_conditions(raw) == [("pts", op, 10.0)]


def test_two_character_operators_win():
    """">=" must not be read as ">" with a value of "=50"."""
    assert finder.parse_conditions("pts>=50") == [("pts", ">=", 50.0)]
    assert finder.parse_conditions("pts<=50") == [("pts", "<=", 50.0)]


def test_parse_accepts_negative_and_decimal_values():
    assert finder.parse_conditions("plus_minus<=-15") == [
        ("plus_minus", "<=", -15.0)
    ]
    assert finder.parse_conditions("ts_pct>=0.65") == [("ts_pct", ">=", 0.65)]


def test_parse_rejects_unknown_field():
    with pytest.raises(ValueError, match="can't filter on 'dunks'"):
        finder.parse_conditions("dunks>=5")


def test_parse_rejects_missing_operator():
    with pytest.raises(ValueError, match="no comparison"):
        finder.parse_conditions("pts")


def test_parse_rejects_non_numeric_value():
    with pytest.raises(ValueError, match="isn't a number"):
        finder.parse_conditions("pts>=lots")


def test_parse_caps_condition_count():
    too_many = ",".join(["pts>=1"] * (finder.MAX_CONDITIONS + 1))
    with pytest.raises(ValueError, match="is the limit"):
        finder.parse_conditions(too_many)


@pytest.mark.parametrize("payload", [
    "pts); DROP TABLE player_games; --",      # caught: no operator at all
    "pts>=1); DROP TABLE player_games; --",   # caught: value isn't a number
    "pts; DROP TABLE x--=1",                  # caught: field not in allowlist
    "1=1",                                    # caught: field not in allowlist
])
def test_sql_injection_attempts_are_rejected(payload):
    """The defence is an allowlist, not quoting.

    A field name has to match GAME_FIELDS, an operator has to match OPERATORS,
    and the value has to parse as a float — so there is no path by which text
    becomes SQL. Which of the three guards catches a given payload doesn't
    matter; that all of them refuse it does.
    """
    with pytest.raises(ValueError):
        finder.parse_conditions(payload)


def test_injection_payload_leaves_the_database_intact(conn):
    """Belt and braces: run one through the whole query path and check the
    table is still there afterwards."""
    before = finder.count(conn, finder.GameQuery())
    with pytest.raises(ValueError):
        finder.find(conn, conditions=finder.parse_conditions(
            "pts>=1); DROP TABLE player_games; --"))
    assert finder.count(conn, finder.GameQuery()) == before


def test_a_value_cannot_smuggle_sql_through_the_float_cast(conn):
    """Even a well-formed field and operator can't carry a payload: the value
    is bound as a parameter, and anything non-numeric is refused outright."""
    with pytest.raises(ValueError, match="isn't a number"):
        finder.parse_conditions("pts>=0 OR 1=1")


def test_describe_conditions_reads_as_a_sentence():
    conds = finder.parse_conditions("pts>=40,reb>=10")
    assert finder.describe_conditions(conds) == "pts ≥ 40 and reb ≥ 10"


def test_describe_drops_trailing_zeros():
    assert finder.describe_conditions(
        finder.parse_conditions("pts>=50")) == "pts ≥ 50"


# ---------------------------------------------------------------------------
# Finding games
# ---------------------------------------------------------------------------

def test_no_conditions_returns_every_player_game(conn):
    # 6 Alpha + 4 Beta + 4 Gamma = 14 player-games in the fixture.
    assert finder.count(conn, finder.GameQuery()) == 14


def test_single_threshold(conn):
    """Only Alpha's 40-point game in 2021 clears 40."""
    rows = finder.find(conn, conditions=[("pts", ">=", 40)])
    assert len(rows) == 1
    assert rows[0]["player"] == "Alpha Scorer"
    assert rows[0]["pts"] == 40
    assert rows[0]["game_date"] == "2021-10-20"


def test_two_conditions_must_hold_in_the_same_game(conn):
    """Beta has 12 points and 11 rebounds every game, so all four match…"""
    both = finder.find(conn, conditions=[("pts", ">=", 12), ("reb", ">=", 11)])
    assert len(both) == 4
    assert {r["player"] for r in both} == {"Beta Allrounder"}

    # …but nobody pairs 25 points with 11 rebounds. Alpha scores 25 with 5
    # rebounds; Beta rebounds 11 with 12 points. Separately yes, together no.
    assert finder.find(
        conn, conditions=[("pts", ">=", 25), ("reb", ">=", 11)]) == []


def test_triple_double_via_category_count(conn):
    """Beta's opening game is 12 / 11 / 10 — the fixture's only one."""
    rows = finder.find(conn, conditions=[("cats", ">=", 3)])
    assert len(rows) == 1
    r = rows[0]
    assert (r["player"], r["pts"], r["reb"], r["ast"]) == (
        "Beta Allrounder", 12, 11, 10)
    assert r["cats"] == 3


def test_double_doubles_are_the_same_field_at_two(conn):
    """All four of Beta's games are double-doubles; no one else has any."""
    rows = finder.find(conn, conditions=[("cats", ">=", 2)])
    assert len(rows) == 4
    assert {r["player"] for r in rows} == {"Beta Allrounder"}


def test_player_substring_filter(conn):
    rows = finder.find(conn, player="gamma")
    assert len(rows) == 4
    assert {r["player"] for r in rows} == {"Gamma Bench"}


def test_player_ids_filter_is_exact(conn):
    rows = finder.find(conn, player_ids=[1001])
    assert len(rows) == 6
    assert {r["player"] for r in rows} == {"Alpha Scorer"}


def test_team_and_opponent_filters(conn):
    assert finder.count(conn, finder.GameQuery(team="BOS")) == 10  # Alpha+Beta
    assert finder.count(conn, finder.GameQuery(team="LAL")) == 4   # Gamma
    # Boston only ever plays the Lakers in this fixture.
    assert finder.count(conn, finder.GameQuery(team="BOS", opponent="LAL")) == 10
    assert finder.count(conn, finder.GameQuery(team="BOS", opponent="BOS")) == 0


def test_venue_filter_splits_the_schedule(conn):
    home = finder.count(conn, finder.GameQuery(player="alpha", venue="home"))
    away = finder.count(conn, finder.GameQuery(player="alpha", venue="away"))
    assert (home, away) == (3, 3)
    assert home + away == finder.count(conn, finder.GameQuery(player="alpha"))


def test_result_filter(conn):
    """Boston wins its three home games and loses its three road games."""
    assert finder.count(conn, finder.GameQuery(player="alpha", result="W")) == 3
    assert finder.count(conn, finder.GameQuery(player="alpha", result="L")) == 3


def test_season_and_range_filters(conn):
    assert finder.count(conn, finder.GameQuery(player="alpha", season=2020)) == 4
    assert finder.count(conn, finder.GameQuery(player="alpha", season=2021)) == 2
    assert finder.count(
        conn, finder.GameQuery(player="alpha", season_range=(2020, 2021))) == 6


def test_date_range_filter(conn):
    rows = finder.find(conn, player="alpha",
                       date_from="2021-01-01", date_to="2021-01-31")
    assert [r["game_date"] for r in rows] == ["2021-01-10", "2021-01-05"]


def test_margin_is_signed_from_the_players_side(conn):
    """Boston wins home games by 5 and loses road games by 5."""
    won = finder.find(conn, player="alpha", result="W", stats=["pts", "margin"])
    lost = finder.find(conn, player="alpha", result="L", stats=["pts", "margin"])
    assert {r["margin"] for r in won} == {5}
    assert {r["margin"] for r in lost} == {-5}


def test_rate_condition_uses_a_share_not_a_percentage(conn):
    """Alpha goes 10-for-20 every game: 0.5 exactly."""
    assert finder.count(
        conn, finder.GameQuery(player="alpha",
                               conditions=[("fg_pct", ">=", 0.5)])) == 6
    assert finder.count(
        conn, finder.GameQuery(player="alpha",
                               conditions=[("fg_pct", ">", 0.5)])) == 0
    # The trap this guards against: typing 50 when the scale is 0–1.
    assert finder.count(
        conn, finder.GameQuery(player="alpha",
                               conditions=[("fg_pct", ">=", 50)])) == 0


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------

def test_game_context_columns_are_always_present(conn):
    row = finder.find(conn, player="alpha", season=2021)[0]
    for col in ("game_date", "player", "team", "opp", "wl", "score", "season"):
        assert col in row, col


def test_opponent_column_reads_like_a_schedule(conn):
    rows = finder.find(conn, player="alpha", season=2021)
    # Game 5 is at home to the Lakers, game 6 away. Newest first.
    assert [r["opp"] for r in rows] == ["@ LAL", "vs LAL"]


def test_score_is_the_final_from_this_players_side(conn):
    won = finder.find(conn, player="alpha", result="W")[0]
    lost = finder.find(conn, player="alpha", result="L")[0]
    assert won["score"] == "110-105"
    assert lost["score"] == "105-110"


def test_filtered_field_is_added_as_a_column(conn):
    """Filter on threes and the threes show up, even unasked."""
    q = finder.GameQuery(stats=["pts"], conditions=[("fg3m", ">=", 1)])
    assert finder.shown_columns(q) == ["pts", "fg3m"]
    assert "fg3m" in finder.run(conn, q)[0]


def test_filtered_field_is_not_duplicated(conn):
    q = finder.GameQuery(stats=["pts", "reb"], conditions=[("pts", ">=", 1)])
    assert finder.shown_columns(q) == ["pts", "reb"]


def test_unknown_display_column_is_rejected(conn):
    # Usage rate needs season-level team totals, so it is not a game field.
    with pytest.raises(ValueError, match="unknown column"):
        finder.find(conn, stats=["usg_pct"])


def test_bad_venue_and_result_are_rejected(conn):
    with pytest.raises(ValueError, match="home.*away"):
        finder.find(conn, venue="sideways")
    with pytest.raises(ValueError, match="W.*L"):
        finder.find(conn, result="tie")


# ---------------------------------------------------------------------------
# Ordering and paging
# ---------------------------------------------------------------------------

def test_default_order_is_newest_first(conn):
    dates = [r["game_date"] for r in finder.find(conn, player="alpha")]
    assert dates == sorted(dates, reverse=True)


def test_order_by_a_stat(conn):
    rows = finder.find(conn, player="alpha", order_by="pts")
    assert [r["pts"] for r in rows] == [40, 25, 25, 25, 25, 20]


def test_order_ascending(conn):
    rows = finder.find(conn, player="alpha", order_by="pts", descending=False)
    assert [r["pts"] for r in rows] == [20, 25, 25, 25, 25, 40]


def test_order_by_a_column_that_is_not_shown_falls_back(conn):
    """The UI can drop the column it was sorting by; that must not 500."""
    rows = finder.find(conn, player="alpha", stats=["reb"], order_by="pts")
    assert len(rows) == 6                      # fell back to the date
    assert "pts" not in rows[0]


def test_order_by_season_is_not_ambiguous(conn):
    """`season` exists on player_games and both joined team_games rows."""
    rows = finder.find(conn, player="alpha", order_by="season")
    assert [r["season"] for r in rows] == [2021, 2021, 2020, 2020, 2020, 2020]


def test_paging_covers_every_row_exactly_once(conn):
    """A tie on the sort key must not let a row repeat or go missing.

    Alpha has four games at 25 points. Without a unique tiebreaker in the
    ORDER BY, SQLite may return those four in a different order per query,
    and paging would then skip and duplicate rows.
    """
    seen = []
    for offset in range(0, 6, 2):
        page = finder.find(conn, player="alpha", order_by="pts",
                           limit=2, offset=offset)
        seen += [r["game_id"] for r in page]
    assert len(seen) == 6
    assert len(set(seen)) == 6


def test_count_ignores_limit_and_offset(conn):
    q = finder.GameQuery(player="alpha", limit=2)
    assert len(finder.run(conn, q)) == 2
    assert finder.count(conn, q) == 6


def test_limit_and_offset_walk_the_result(conn):
    first = finder.find(conn, player="alpha", order_by="pts", limit=1)
    second = finder.find(conn, player="alpha", order_by="pts",
                         limit=1, offset=1)
    assert first[0]["pts"] == 40
    assert second[0]["pts"] == 25


def test_negative_offset_is_clamped(conn):
    assert len(finder.find(conn, player="alpha", offset=-5)) == 6


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

def test_every_preset_parses_and_runs(conn):
    """A preset with a typo in it would be a broken button in the UI."""
    for name, spec in finder.PRESETS.items():
        conds = finder.parse_conditions(spec["conditions"])
        assert conds, f"{name} has no conditions"
        q = finder.GameQuery(
            conditions=conds,
            result=spec.get("result"),
            venue=spec.get("venue"),
            order_by=spec.get("order_by", finder.DEFAULT_ORDER),
        )
        finder.run(conn, q)       # must not raise
        finder.count(conn, q)


def test_preset_order_by_is_a_real_field(conn):
    for name, spec in finder.PRESETS.items():
        key = spec.get("order_by", finder.DEFAULT_ORDER)
        assert key in finder.GAME_FIELDS or key in finder.CONTEXT_COLUMNS, (
            f"{name} sorts by {key!r}, which is neither a stat nor a context "
            f"column"
        )


def test_triple_double_preset_finds_the_fixtures_triple_double(conn):
    spec = finder.PRESETS["triple-doubles"]
    rows = finder.find(conn, conditions=finder.parse_conditions(
        spec["conditions"]))
    assert len(rows) == 1
    assert rows[0]["player"] == "Beta Allrounder"


def test_preset_labels_and_help_are_present(conn):
    for name, spec in finder.PRESETS.items():
        assert spec.get("label"), f"{name} has no label"
        assert spec.get("help"), f"{name} has no explanation"


def test_every_preset_belongs_to_a_declared_group(conn):
    """The UI renders group by group; one with a stray group name would be
    silently dropped off the page."""
    for name, spec in finder.PRESETS.items():
        assert spec.get("group") in finder.GROUPS, (
            f"{name} is in group {spec.get('group')!r}, which is not in GROUPS"
        )


def test_every_group_has_presets_in_it(conn):
    used = {spec["group"] for spec in finder.PRESETS.values()}
    for group in finder.GROUPS:
        assert group in used, f"group {group!r} would render empty"


def test_preset_help_avoids_abbreviations(conn):
    """These lines are the whole point of the named searches: they are what a
    casual fan reads instead of decoding `pts>=50`. An abbreviation in one
    defeats the purpose."""
    # Whole words only: "rebounds" is the plain English these stand in for.
    jargon = ["pts", "reb", "ast", "stl", "blk", "tov", "fgm", "fga",
              "fg3m", "ts%", "efg%", "usg%", "per 36", "per-36"]
    symbols = [">=", "<=", "≥", "≤"]
    for name, spec in finder.PRESETS.items():
        low = spec["help"].lower()
        for word in jargon:
            assert not re.search(rf"\b{re.escape(word)}\b", low), (
                f"{name} help says {word!r}: {spec['help']}")
        for sym in symbols:
            assert sym not in low, f"{name} help uses {sym!r}: {spec['help']}"


def test_preset_help_is_one_short_line(conn):
    for name, spec in finder.PRESETS.items():
        assert len(spec["help"]) <= 110, f"{name} help is too long to sit on a card"
        assert spec["help"].endswith("."), f"{name} help is not a sentence"


def test_preset_labels_are_short_enough_for_a_card(conn):
    for name, spec in finder.PRESETS.items():
        assert len(spec["label"]) <= 28, f"{name} label will wrap badly"
