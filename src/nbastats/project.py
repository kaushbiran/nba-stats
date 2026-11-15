"""Player prop projections, with the uncertainty left visible.

This module projects a stat line for a player and reports how wide the
distribution around it is. It deliberately does NOT output picks.

WHAT THE MODEL KNOWS
    per-minute production, weighted toward recent games
    projected minutes, weighted the same way
    how much of that stat the opponent allows relative to league average
    the player's own home/away split
    how much that player's game-to-game results actually vary

WHAT IT DOES NOT KNOW
    injuries, rest decisions, minutes restrictions, foul trouble
    lineup changes, trades, a coach's rotation plan
    anything that happened after the last ingested game

That second list is most of what moves a real line, which is why the output
here is a distribution to reason about and not a recommendation. A sportsbook
prices all of it; this model prices none of it.

METHOD
    rate      = Σ(w·stat) / Σ(w·minutes)      recency-weighted per-minute rate
    minutes   = Σ(w·minutes) / Σ(w)           recency-weighted minutes
    opp_adj   = opponent allowed / league average for that stat
    home_adj  = player's home or away mean / overall mean, pulled toward 1
    projection = rate × minutes × opp_adj × home_adj

Weights decay geometrically: the most recent game counts 1.0, the one before
0.94, and so on. With the default half-life that means a game 11 back carries
about half the weight of the latest one.
"""

from __future__ import annotations

import math
import sqlite3
from statistics import NormalDist

REG = "Regular Season"

PROJECTABLE = ["pts", "reb", "ast", "stl", "blk", "fg3m", "tov",
               "oreb", "dreb", "fgm", "fga", "ftm"]

DEFAULT_LOOKBACK = 20
DECAY = 0.94          # per-game weight decay; ~11-game half-life

# How often a player actually clears a line set at his own projection. Not 50%:
# scoring is right-skewed (a floor at zero, a long tail upward), so the mean
# sits above the median. Measured at 45.8% over 18,475 player-games.
SKEWED_BASE_RATE = 0.45

# Weight given to that base rate, in games, for a line sitting right at the
# projection. A 20-game window carries about 12 units of decayed weight, so
# this is roughly a 50/50 blend at the center.
EMPIRICAL_PRIOR = 6.0

# How fast that shrinkage falls off as the line moves away from the
# projection, in standard deviations.
#
# Flat shrinkage fails in both directions, and the backtest only caught one of
# them. At the projection, heavy shrinkage wins — a backtest on 18,475
# player-games found a constant 45% beat every method that looked at the data,
# because which side a centered line falls on really is near a coin flip.
# But that backtest never tested lines away from the center. With a flat
# prior of 12, a line that all 20 recent games cleared still reports 72%,
# which is plainly wrong.
#
# So the prior decays with distance: heavy shrinkage where the answer is
# genuinely uncertain, and none where the game log settles it.
PRIOR_FALLOFF_SDS = 1.0

# The prior never reaches zero. Twenty games is a small sample however lopsided
# it looks, and a stat tool reporting 100% is making a promise the data cannot
# keep — a player can get hurt, rest, or foul out in the first quarter.
MIN_PRIOR = 0.3


def adaptive_prior(line: float, projection: float, sd: float) -> float:
    """Shrinkage weight for this line: strong at the center, minimal at the tails."""
    if sd <= 0:
        return EMPIRICAL_PRIOR
    d = abs(line - projection) / sd
    decayed = EMPIRICAL_PRIOR * math.exp(-(d ** 2) / (2 * PRIOR_FALLOFF_SDS ** 2))
    return max(MIN_PRIOR, decayed)

# How far a home/away split is trusted. Splits over 20-40 games are noisy, so
# the adjustment is shrunk toward 1.0 rather than applied at face value.
SPLIT_SHRINK = 0.5
MAX_SPLIT_ADJ = 0.10   # cap the home/away effect at ±10%


class ProjectionError(ValueError):
    """Raised when there isn't enough data to project honestly."""


def seasonlabel(year: int) -> str:
    """2023 -> '2023-24', the way a season is actually written."""
    return f"{year}-{str(year + 1)[-2:]}"


# ---------------------------------------------------------------------------
# Odds helpers
# ---------------------------------------------------------------------------

def american_to_implied(odds: int | float) -> float:
    """American odds -> implied probability, vig included.

    -110 -> 0.5238,  +150 -> 0.40
    """
    odds = float(odds)
    if odds == 0:
        raise ValueError("american odds cannot be 0")
    if odds < 0:
        return -odds / (-odds + 100.0)
    return 100.0 / (odds + 100.0)


def devig(over_odds: float, under_odds: float) -> dict:
    """Strip the vig from a two-way market by normalizing to 1.

    Both sides' implied probabilities sum to more than 1; the excess is the
    book's margin. Dividing each by the total gives the book's actual estimate
    of each outcome, which is the number worth comparing a model against.
    """
    p_over = american_to_implied(over_odds)
    p_under = american_to_implied(under_odds)
    total = p_over + p_under
    return {
        "implied_over": round(p_over, 4),
        "implied_under": round(p_under, 4),
        "overround": round(total, 4),
        "hold_pct": round((total - 1) / total * 100, 2),
        "fair_over": round(p_over / total, 4),
        "fair_under": round(p_under / total, 4),
    }


def parlay_math(legs: int, odds_per_leg: float = -110,
                true_prob: float | None = None) -> dict:
    """What a parlay actually costs, as a function of leg count.

    Included because the number is the whole argument: the house edge on a
    parlay compounds with every leg added, even when each leg is fairly priced.
    """
    if legs < 1:
        raise ValueError("a parlay needs at least one leg")
    dec_leg = (1 + 100 / -odds_per_leg) if odds_per_leg < 0 else (1 + odds_per_leg / 100)
    payout_decimal = dec_leg ** legs
    implied = 1 / payout_decimal
    p = american_to_implied(odds_per_leg) if true_prob is None else true_prob
    # Vig-free per-leg probability, assuming a symmetric two-way market.
    fair_leg = p / (2 * p) if true_prob is None else true_prob
    fair_parlay = fair_leg ** legs
    return {
        "legs": legs,
        "odds_per_leg": odds_per_leg,
        "payout_multiple": round(payout_decimal, 3),
        "implied_probability": round(implied, 4),
        "fair_probability": round(fair_parlay, 4),
        "house_edge_pct": round((implied - fair_parlay) / implied * 100, 2),
    }


# ---------------------------------------------------------------------------
# Data gathering
# ---------------------------------------------------------------------------

def _resolve_player(conn, name: str) -> dict:
    row = conn.execute(
        """SELECT p.player_id, p.full_name, COUNT(*) AS gp
           FROM player_games pg JOIN players p ON p.player_id = pg.player_id
           WHERE LOWER(p.full_name) LIKE ?
           GROUP BY p.player_id
           ORDER BY (CASE WHEN LOWER(p.full_name) LIKE ? THEN 0 ELSE 1 END), gp DESC
           LIMIT 1""",
        (f"%{name.lower()}%", f"{name.lower()}%"),
    ).fetchone()
    if row is None:
        raise ProjectionError(f"no player matching {name!r} in the loaded seasons")
    return dict(row)


def game_log(conn, player_id: int, stat: str, season: int | None = None,
             season_type: str = REG, limit: int = DEFAULT_LOOKBACK) -> list[dict]:
    """Most recent games first."""
    where = ["pg.player_id = ?", "pg.season_type = ?", "pg.min > 0"]
    params: list = [player_id, season_type]
    if season is not None:
        where.append("pg.season = ?")
        params.append(season)

    rows = conn.execute(
        f"""SELECT pg.game_date, pg.season, pg.min, pg.{stat} AS value, pg.wl,
                   t.abbreviation AS team,
                   (SELECT t2.abbreviation FROM team_games tg
                      JOIN teams t2 ON t2.team_id = tg.opp_team_id
                     WHERE tg.game_id = pg.game_id AND tg.team_id = pg.team_id) AS opponent,
                   (SELECT tg.is_home FROM team_games tg
                     WHERE tg.game_id = pg.game_id AND tg.team_id = pg.team_id) AS is_home
            FROM player_games pg
            JOIN teams t ON t.team_id = pg.team_id
            WHERE {" AND ".join(where)}
            ORDER BY pg.game_date DESC
            LIMIT ?""",
        params + [limit],
    )
    return [dict(r) for r in rows]


def _opponent_factor(conn, stat: str, opponent: str | None, season: int,
                     season_type: str = REG) -> tuple[float, dict]:
    """How much of `stat` this opponent allows, relative to league average.

    Above 1.0 means they give up more than average, so the projection goes up.
    Only team-level box stats can be measured this way; for a stat the team
    table doesn't carry, the factor is 1.0 and the detail says so.
    """
    if not opponent:
        return 1.0, {"applied": False, "reason": "no opponent given"}
    if stat not in {"pts", "reb", "ast", "stl", "blk", "fg3m", "tov",
                    "oreb", "dreb", "fgm", "fga", "ftm"}:
        return 1.0, {"applied": False, "reason": f"{stat} not tracked at team level"}

    # What each team allows per game = the stat posted by their opponents.
    rows = conn.execute(
        f"""SELECT t.abbreviation AS team,
                   AVG(opp.{stat}) AS allowed
            FROM team_games tg
            JOIN teams t ON t.team_id = tg.team_id
            JOIN team_games opp ON opp.game_id = tg.game_id
                               AND opp.team_id = tg.opp_team_id
            WHERE tg.season = ? AND tg.season_type = ?
            GROUP BY tg.team_id""",
        (season, season_type),
    ).fetchall()

    if not rows:
        return 1.0, {"applied": False, "reason": "no team data for that season"}

    allowed = {r["team"]: r["allowed"] for r in rows if r["allowed"] is not None}
    if opponent.upper() not in allowed:
        return 1.0, {"applied": False,
                     "reason": f"{opponent.upper()} has no games in {season}"}

    league_avg = sum(allowed.values()) / len(allowed)
    opp_allowed = allowed[opponent.upper()]
    if league_avg <= 0:
        return 1.0, {"applied": False, "reason": "league average is zero"}

    factor = opp_allowed / league_avg
    return factor, {
        "applied": True,
        "opponent": opponent.upper(),
        "allowed_per_game": round(opp_allowed, 2),
        "league_average": round(league_avg, 2),
        "factor": round(factor, 4),
    }


def _home_away_factor(conn, player_id: int, stat: str, season: int | None,
                      is_home: bool | None, season_type: str = REG):
    """The player's own split, shrunk toward neutral because splits are noisy."""
    if is_home is None:
        return 1.0, {"applied": False, "reason": "venue not specified"}

    where = ["pg.player_id = ?", "pg.season_type = ?", "pg.min > 0"]
    params: list = [player_id, season_type]
    if season is not None:
        where.append("pg.season = ?")
        params.append(season)

    row = conn.execute(
        f"""SELECT AVG(CASE WHEN tg.is_home = 1 THEN pg.{stat} END) AS home_avg,
                   AVG(CASE WHEN tg.is_home = 0 THEN pg.{stat} END) AS away_avg,
                   AVG(pg.{stat}) AS overall,
                   SUM(CASE WHEN tg.is_home = 1 THEN 1 ELSE 0 END) AS n_home,
                   SUM(CASE WHEN tg.is_home = 0 THEN 1 ELSE 0 END) AS n_away
            FROM player_games pg
            JOIN team_games tg ON tg.game_id = pg.game_id AND tg.team_id = pg.team_id
            WHERE {" AND ".join(where)}""",
        params,
    ).fetchone()

    side = row["home_avg"] if is_home else row["away_avg"]
    n = row["n_home"] if is_home else row["n_away"]
    overall = row["overall"]

    if not side or not overall or overall == 0 or (n or 0) < 5:
        return 1.0, {"applied": False, "reason": "not enough games on that side"}

    raw = side / overall
    shrunk = 1 + (raw - 1) * SPLIT_SHRINK
    capped = max(1 - MAX_SPLIT_ADJ, min(1 + MAX_SPLIT_ADJ, shrunk))
    return capped, {
        "applied": True,
        "venue": "home" if is_home else "away",
        "games": n,
        "raw_ratio": round(raw, 4),
        "factor": round(capped, 4),
    }


# ---------------------------------------------------------------------------
# The projection
# ---------------------------------------------------------------------------

def latest_season(conn, season_type: str = REG) -> int | None:
    """The newest season in the database — the only one worth projecting."""
    row = conn.execute(
        "SELECT MAX(season) AS s FROM player_games WHERE season_type = ?",
        (season_type,),
    ).fetchone()
    return row["s"] if row else None


def project(conn: sqlite3.Connection, player: str, stat: str = "pts",
            opponent: str | None = None, season: int | None = None,
            is_home: bool | None = None, lookback: int = DEFAULT_LOOKBACK,
            line: float | None = None, season_type: str = REG,
            allow_past_seasons: bool = False) -> dict:
    """Project one stat for one player, with its distribution.

    Only the newest season in the database can be projected. Projecting a
    finished season is backwards — those games already happened, and dressing a
    settled result up as a forecast is exactly the kind of false confidence
    this module exists to avoid. `allow_past_seasons=True` overrides it for
    backtesting, where the point is to compare against known outcomes.
    """
    if stat not in PROJECTABLE:
        raise ProjectionError(
            f"can't project {stat!r}; try one of: {', '.join(PROJECTABLE)}"
        )

    p = _resolve_player(conn, player)
    newest = latest_season(conn, season_type)

    if season is None:
        row = conn.execute(
            """SELECT MAX(season) AS s FROM player_games
               WHERE player_id = ? AND season_type = ?""",
            (p["player_id"], season_type),
        ).fetchone()
        season = row["s"]
        if season is None:
            raise ProjectionError(f"{p['full_name']} has no {season_type} games")

    if not allow_past_seasons and newest is not None and season < newest:
        raise ProjectionError(
            f"{seasonlabel(season)} is finished, so there is nothing to project. "
            f"Projections are only available for {seasonlabel(newest)}, the most "
            f"recent season loaded."
        )

    log = game_log(conn, p["player_id"], stat, season, season_type, lookback)
    if len(log) < 5:
        raise ProjectionError(
            f"only {len(log)} game(s) for {p['full_name']} in {season}; "
            "need at least 5 to project"
        )

    # Recency-weighted rate and minutes.
    weights = [DECAY ** i for i in range(len(log))]
    wsum = sum(weights)
    w_minutes = sum(w * (g["min"] or 0) for w, g in zip(weights, log))
    w_value = sum(w * (g["value"] or 0) for w, g in zip(weights, log))

    if w_minutes <= 0:
        raise ProjectionError("no recorded minutes in the lookback window")

    per_minute = w_value / w_minutes
    proj_minutes = w_minutes / wsum

    opp_factor, opp_detail = _opponent_factor(conn, stat, opponent, season, season_type)
    home_factor, home_detail = _home_away_factor(
        conn, p["player_id"], stat, season, is_home, season_type
    )

    base = per_minute * proj_minutes
    projection = base * opp_factor * home_factor

    # Spread, measured from what actually happened rather than assumed.
    values = [g["value"] or 0 for g in log]
    n = len(values)
    mean = sum(values) / n
    variance = sum((v - mean) ** 2 for v in values) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(variance)

    out = {
        "player": p["full_name"],
        "player_id": p["player_id"],
        "stat": stat,
        "season": season,
        "projection": round(projection, 2),
        "baseline": round(base, 2),
        "recent_mean": round(mean, 2),
        "std_dev": round(sd, 2),
        "games_used": n,
        "projected_minutes": round(proj_minutes, 1),
        "per_minute_rate": round(per_minute, 4),
        "range_68": [round(projection - sd, 1), round(projection + sd, 1)],
        "range_95": [round(projection - 2 * sd, 1), round(projection + 2 * sd, 1)],
        "adjustments": {"opponent": opp_detail, "venue": home_detail},
        "game_log": log,
        "caveats": [
            "Box scores only: no injury, rest, or rotation information.",
            "Assumes a normal-ish spread, which fits points better than "
            "low-count stats like blocks or steals.",
            f"Based on {n} games; a short sample moves a lot.",
        ],
    }

    if line is not None:
        out["line_analysis"] = compare_to_line(projection, sd, values, line)
    return out


def empirical_over_probability(values: list[float], line: float,
                               decay: float = DECAY,
                               prior: float = 1.0,
                               prior_center: float = 0.5) -> float:
    """Share of recent games that cleared the line, weighted toward recent ones.

    Replaces a normal approximation around the projected mean. Scoring is
    right-skewed — a player cannot go below zero but can go off for 50 — so the
    mean sits above the median, and assuming a symmetric spread around the mean
    overstates the chance of going over. A backtest showed exactly that: lines
    the model put at 55% hit only 46.8% of the time, while the 45% bucket was
    accurate. Reading the probability off the real distribution keeps the skew
    instead of assuming it away.

    `prior` is how many games' worth of weight to put on `prior_center`. A
    small prior lets a 20-game sample speak for itself; a large one holds the
    estimate near the center unless the evidence is strong. This matters more
    than it sounds: a backtest on real data showed the lightly-smoothed version
    spreading its predictions from 25% to 65% while the actual rate stayed flat
    near 45%, i.e. producing confident variation that predicted nothing. Too
    little shrinkage trades the normal approximation's bias for worse variance.

    `values` is newest-first, matching the game log.
    """
    if not values:
        return prior_center
    weights = [decay ** i for i in range(len(values))]
    total = sum(weights)
    w_over = sum(w for w, v in zip(weights, values) if v > line)
    w_push = sum(w for w, v in zip(weights, values) if v == line)
    return ((w_over + 0.5 * w_push + prior * prior_center)
            / (total + prior))


def compare_to_line(projection: float, sd: float, values: list[float],
                    line: float) -> dict:
    """Three reads on an over/under, reported side by side.

    The headline number is a normal approximation around the projection. That
    choice was settled by backtest, not by preference, and it took two wrong
    turns to get there:

    1. The normal method's calibration at a centered line looked bad — lines
       it put at 55% hit only 46.8% of the time. Scoring is right-skewed, so a
       symmetric spread around the mean overstates the over. Replacing it with
       the player's own empirical distribution seemed obviously correct.

    2. That was worse (5.5% vs 4.2% weighted miss). The empirical estimate
       spread predictions from 25% to 65% while the true rate sat flat near
       45% — confident variation that predicted nothing. Shrinking it heavily
       helped at the center, and a constant 45% that looked at nothing at all
       scored best of everything.

    3. Which was an artifact of the measurement: the backtest only ever placed
       the line AT the projection, the one spot where refusing to commit is
       free. Scored across lines from -1.5 to +1.5 standard deviations out,
       the constant is the worst method by a mile (26.4% mean) and the plain
       normal approximation is the best (3.2%).

    So the known flaw in step 1 is real and still here: at a centered line
    this runs about 8 points hot on the over. It is reported rather than
    hidden, and the empirical figure sits beside it so the gap is visible.
    Everything that fixed that flaw made the overall answer worse.
    """
    over_hits = sum(1 for v in values if v > line)
    push = sum(1 for v in values if v == line)
    n = len(values)

    if sd > 0:
        over = 1 - NormalDist().cdf((line - projection) / sd)
    else:
        over = 1.0 if projection > line else 0.0

    empirical = empirical_over_probability(
        values, line,
        prior=adaptive_prior(line, projection, sd),
        prior_center=SKEWED_BASE_RATE,
    )

    # How far the line sits from the projection decides whether the known bias
    # applies: it shows up near the center and fades at the tails.
    centered = sd > 0 and abs(line - projection) < 0.5 * sd

    return {
        "line": line,
        "over_probability": round(over, 4),
        "under_probability": round(1 - over, 4),
        "empirical_probability": round(empirical, 4),
        "empirical_over_rate": round(over_hits / n, 4) if n else None,
        "empirical_record": f"{over_hits}-{n - over_hits - push}" +
                            (f"-{push} push" if push else ""),
        "games": n,
        "centered": centered,
        "method": "normal approximation around the projection",
        "note": "Backtested as the best-calibrated of six methods across the "
                "range of line placements. Near the projection it runs about "
                "8 points hot on the over, because scoring is right-skewed. "
                "It knows nothing about injuries or rest, and it is not a "
                "forecast.",
    }


def compare_to_market(projection: float, sd: float, values: list[float],
                      line: float, over_odds: float, under_odds: float) -> dict:
    """Put the model's number next to the book's, with the vig removed.

    The gap is not an edge. A book's line reflects injury news, minutes
    projections and money flow that this model cannot see, so when they
    disagree the base rate strongly favors the book being right.
    """
    model = compare_to_line(projection, sd, values, line)
    market = devig(over_odds, under_odds)
    return {
        **model,
        "market": market,
        "model_vs_market_over": round(
            model["over_probability"] - market["fair_over"], 4
        ),
        "interpretation": (
            "A positive number means this model is higher on the over than the "
            "vig-free market. Treat it as a prompt to find what the market "
            "knows that the model doesn't, not as an edge."
        ),
    }
