#!/usr/bin/env python3
"""Walk-forward backtest: how wrong is the projection, really?

This is the part that keeps the tool honest. Any model can produce a confident
number; this measures whether that number holds up on games the model has not
seen, using only data available before each game.

    python scripts/backtest.py --season 2024 --stat pts
    python scripts/backtest.py --season 2024 --stat pts --min-games 40 --line-offset 0

What it reports:

    MAE / RMSE      typical miss, in points. Compare against the naive
                    baseline (a flat season average) printed alongside. If the
                    model can't beat the naive baseline, it is adding nothing.

    Bias            mean signed error. Near zero is good; a large value means
                    the model is systematically high or low.

    Calibration     bucket every over/under probability the model produced and
                    check how often those games actually went over. If the
                    model says 70% and those games go over 70% of the time, it
                    is calibrated. A model can be well calibrated and still be
                    useless for betting — calibration is necessary, not
                    sufficient, and it says nothing about beating a price.

No odds are involved anywhere in here. This measures accuracy against outcomes,
not profit against a market.
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nbastats import db, project  # noqa: E402
from nbastats.project import DECAY  # noqa: E402

MIN_HISTORY = 10          # games needed before the model is allowed to project


class OpponentStrength:
    """Opponent factors built from games played BEFORE a given date.

    Using full-season defensive numbers would leak the future into every
    projection and flatter the results. This keeps a running tally instead, so
    a factor asked for on January 5th only knows about games through the 4th.
    """

    MIN_GAMES = 5     # below this, a team's defensive average is noise

    def __init__(self, conn, season: int, stat: str, season_type: str):
        rows = conn.execute(
            f"""SELECT tg.game_date AS date, t.abbreviation AS team,
                       opp.{stat} AS allowed
                FROM team_games tg
                JOIN teams t ON t.team_id = tg.team_id
                JOIN team_games opp ON opp.game_id = tg.game_id
                                   AND opp.team_id = tg.opp_team_id
                WHERE tg.season = ? AND tg.season_type = ?
                ORDER BY tg.game_date ASC""",
            (season, season_type),
        ).fetchall()

        # Per team: parallel arrays of date and cumulative (sum, count).
        self.by_team: dict[str, list[tuple[str, float, int]]] = {}
        league: list[tuple[str, float, int]] = []
        l_sum = l_n = 0
        for r in rows:
            if r["allowed"] is None:
                continue
            t = r["team"]
            prev = self.by_team.get(t)
            s, n = (prev[-1][1], prev[-1][2]) if prev else (0.0, 0)
            self.by_team.setdefault(t, []).append(
                (r["date"], s + r["allowed"], n + 1)
            )
            l_sum += r["allowed"]
            l_n += 1
            league.append((r["date"], l_sum, l_n))
        self.league = league

    @staticmethod
    def _before(series, date):
        """Cumulative (sum, count) over entries strictly before `date`."""
        lo, hi = 0, len(series)
        while lo < hi:                       # rightmost entry < date
            mid = (lo + hi) // 2
            if series[mid][0] < date:
                lo = mid + 1
            else:
                hi = mid
        if lo == 0:
            return 0.0, 0
        return series[lo - 1][1], series[lo - 1][2]

    def factor(self, opponent: str, date: str) -> float:
        series = self.by_team.get(opponent)
        if not series:
            return 1.0
        t_sum, t_n = self._before(series, date)
        if t_n < self.MIN_GAMES:
            return 1.0
        l_sum, l_n = self._before(self.league, date)
        if l_n < self.MIN_GAMES:
            return 1.0
        league_avg = l_sum / l_n
        if league_avg <= 0:
            return 1.0
        return (t_sum / t_n) / league_avg


def walk_forward(conn, season: int, stat: str, min_games: int,
                 lookback: int, season_type: str = "Regular Season") -> list[dict]:
    """For each game, project from earlier games only, then score it."""
    players = conn.execute(
        """SELECT pg.player_id, p.full_name, COUNT(*) AS gp
           FROM player_games pg JOIN players p ON p.player_id = pg.player_id
           WHERE pg.season = ? AND pg.season_type = ? AND pg.min > 0
           GROUP BY pg.player_id HAVING gp >= ?""",
        (season, season_type, min_games),
    ).fetchall()

    print(f"backtesting {len(players)} players with {min_games}+ games…")

    strength = OpponentStrength(conn, season, stat, season_type)
    results = []

    for i, p in enumerate(players):
        if i % 50 == 0 and i:
            print(f"  {i}/{len(players)}")

        log = conn.execute(
            f"""SELECT pg.game_date, pg.min, pg.{stat} AS value,
                       (SELECT t2.abbreviation FROM team_games tg
                          JOIN teams t2 ON t2.team_id = tg.opp_team_id
                         WHERE tg.game_id = pg.game_id AND tg.team_id = pg.team_id) AS opponent
                FROM player_games pg
                WHERE pg.player_id = ? AND pg.season = ? AND pg.season_type = ?
                  AND pg.min > 0
                ORDER BY pg.game_date ASC""",
            (p["player_id"], season, season_type),
        ).fetchall()

        for idx in range(MIN_HISTORY, len(log)):
            history = log[max(0, idx - lookback):idx]     # strictly earlier games
            actual = log[idx]["value"]
            if actual is None:
                continue

            # Same arithmetic as project(), applied to the truncated history.
            recent = list(reversed(history))
            weights = [DECAY ** k for k in range(len(recent))]
            wsum = sum(weights)
            w_min = sum(w * (g["min"] or 0) for w, g in zip(weights, recent))
            w_val = sum(w * (g["value"] or 0) for w, g in zip(weights, recent))
            if w_min <= 0:
                continue

            base = (w_val / w_min) * (w_min / wsum)
            opp_factor = strength.factor(log[idx]["opponent"], log[idx]["game_date"])
            projection = base * opp_factor

            vals = [g["value"] or 0 for g in history]
            n = len(vals)
            mean = sum(vals) / n
            sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / (n - 1)) if n > 1 else 0.0

            results.append({
                "player": p["full_name"],
                "actual": actual,
                "projection": projection,
                "base": base,           # before the opponent adjustment
                "naive": mean,          # flat average of the same window
                "sd": sd,
                # newest-first, to feed the empirical probability
                "history": list(reversed(vals)),
            })

    return results


def summarize(results: list[dict], stat: str) -> None:
    if not results:
        print("no games to score — try a lower --min-games")
        return

    n = len(results)
    err = [r["projection"] - r["actual"] for r in results]
    naive_err = [r["naive"] - r["actual"] for r in results]

    base_err = [r["base"] - r["actual"] for r in results]

    mae = sum(abs(e) for e in err) / n
    rmse = math.sqrt(sum(e * e for e in err) / n)
    bias = sum(err) / n
    naive_mae = sum(abs(e) for e in naive_err) / n
    base_mae = sum(abs(e) for e in base_err) / n

    print(f"\n{'=' * 58}")
    print(f"{n:,} player-games scored on {stat}")
    print(f"{'=' * 58}")
    print(f"  MAE            {mae:6.3f}")
    print(f"  RMSE           {rmse:6.3f}")
    print(f"  Bias           {bias:+6.3f}   (negative = projecting low)")
    print(f"\n  Against simpler models (lower MAE is better):")
    print(f"    flat average of the window     {naive_mae:6.3f}")
    print(f"    rate × minutes, no opponent    {base_mae:6.3f}")
    print(f"    full model                     {mae:6.3f}")

    lift = (naive_mae - mae) / naive_mae * 100 if naive_mae else 0
    verdict = "better than" if lift > 0 else "WORSE than"
    print(f"\n  vs flat average  {lift:+6.2f}%  {verdict} doing nothing clever")
    if lift <= 0:
        print("    The extra machinery is not paying for itself on this data.")
        print("    That is a real result, not a bug — report it as one.")

    # Calibration against a line set at each projection, the hardest case.
    # Both methods are scored so the difference is visible: the normal
    # approximation assumes a symmetric spread around the mean, the empirical
    # one reads the probability off the player's own distribution and so keeps
    # the right skew in scoring.
    from statistics import NormalDist
    from nbastats.project import adaptive_prior, empirical_over_probability

    # Lines are tested at several distances from the projection, measured in
    # standard deviations. Scoring only at the projection flatters any method
    # that refuses to commit: that is the one spot where which side the result
    # lands on really is close to a coin flip. A real user types lines away
    # from the center too, and a method that still says "45%" when the line is
    # two standard deviations below the projection is useless.
    OFFSETS = [-1.5, -0.75, 0.0, 0.75, 1.5]

    def calibrate(prob_fn, offset=0.0):
        buckets = {}
        for r in results:
            if r["sd"] <= 0:
                continue
            raw = r["projection"] + offset * r["sd"]
            line = round(raw * 2) / 2                  # nearest half point
            if line < 0.5:
                continue                               # a line below zero is not a line
            p_over = prob_fn(r, line)
            b = min(int(p_over * 10) / 10, 0.9)
            buckets.setdefault(b, []).append(1 if r["actual"] > line else 0)
        return buckets

    def weighted_miss(buckets, floor=30):
        total = weighted = 0
        for b, hits in buckets.items():
            if len(hits) < floor:
                continue
            pred, act, n = b + 0.05, sum(hits) / len(hits), len(hits)
            weighted += abs(pred - act) * n
            total += n
        return (weighted / total) if total else 0.0

    def show(title, buckets):
        """Report each bucket, and a games-weighted average miss.

        Weighting matters: an unweighted "worst bucket" number is decided by
        whichever bucket holds fifty games, which is noise. The weighted
        average is what you actually experience across a season.
        """
        print(f"\n  {title}")
        print(f"    {'predicted':>10}  {'actual':>8}  {'gap':>7}  {'games':>8}")
        total_games = weighted = 0
        for b in sorted(buckets):
            hits = buckets[b]
            if len(hits) < 30:
                continue
            pred, act, n = b + 0.05, sum(hits) / len(hits), len(hits)
            weighted += abs(pred - act) * n
            total_games += n
            flag = "  (thin)" if n < 200 else ""
            print(f"    {pred:>9.0%}  {act:>8.1%}  {pred - act:>+7.1%}  {n:>8,}{flag}")
        avg = weighted / total_games if total_games else 0
        print(f"    weighted average miss: {avg:.1%}")
        return avg

    # Several candidates, scored the same way, so the data picks rather than
    # the author. BASE_RATE is deliberately included: if a single constant
    # beats every clever method, that is the most important thing to know,
    # because it means none of them discriminate.
    BASE_RATE = 0.45

    candidates = [
        ("normal approximation",
         lambda r, line: 1 - NormalDist().cdf((line - r["projection"]) / r["sd"])),
        ("empirical, light shrinkage (prior=1)",
         lambda r, line: empirical_over_probability(r["history"], line, prior=1)),
        ("empirical, heavy shrinkage (prior=12)",
         lambda r, line: empirical_over_probability(r["history"], line, prior=12)),
        ("empirical, heavy shrinkage toward 45%",
         lambda r, line: empirical_over_probability(
             r["history"], line, prior=12, prior_center=BASE_RATE)),
        ("adaptive shrinkage (current default)",
         lambda r, line: empirical_over_probability(
             r["history"], line,
             prior=adaptive_prior(line, r["projection"], r["sd"]),
             prior_center=BASE_RATE)),
        (f"constant {BASE_RATE:.0%} (predicts nothing)",
         lambda r, line: BASE_RATE),
    ]

    # Detail tables at the projection, the regime people quote most.
    for name, fn in candidates:
        show(f"Calibration at the projection, {name}:", calibrate(fn, 0.0))

    # Then the same methods across the full range of line placements.
    print(f"\n  {'=' * 62}")
    print("  Calibration miss by how far the line sits from the projection")
    print("  (in standard deviations; lower is better)")
    header = "".join(f"{o:>+8.2f}" for o in OFFSETS)
    print(f"\n    {'method':<40}{header}{'   mean':>9}")
    print(f"    {'-' * (40 + 8 * len(OFFSETS) + 9)}")

    table = []
    for name, fn in candidates:
        per_offset = [weighted_miss(calibrate(fn, o)) for o in OFFSETS]
        mean_miss = sum(per_offset) / len(per_offset)
        table.append((mean_miss, name, per_offset))
        cells = "".join(f"{m:>8.1%}" for m in per_offset)
        print(f"    {name[:40]:<40}{cells}{mean_miss:>9.1%}")

    table.sort()
    best_mean, best_name, best_cols = table[0]
    print(f"\n  Best across all line placements: {best_name} ({best_mean:.1%}).")

    at_line = {name: cols[OFFSETS.index(0.0)] for _, name, cols in table}
    winner_at_line = min(at_line, key=at_line.get)
    if winner_at_line != best_name:
        print(f"  Note: {winner_at_line} wins at the projection ("
              f"{at_line[winner_at_line]:.1%}) but not overall — a method can")
        print(f"  look good there by refusing to commit, then fall apart on")
        print(f"  lines further out. Judge on the mean, not the middle column.")

    print(f"\n  Every projection above used only games played before the game")
    print(f"  being scored, opponent strength included. Calibration says the")
    print(f"  stated uncertainty is honest. It does not mean the model can")
    print(f"  beat a priced market — nothing here was measured against odds.\n")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, required=True)
    ap.add_argument("--stat", default="pts", choices=project.PROJECTABLE)
    ap.add_argument("--min-games", type=int, default=40)
    ap.add_argument("--lookback", type=int, default=project.DEFAULT_LOOKBACK)
    ap.add_argument("--season-type", default="Regular Season")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    conn = db.connect(args.db)
    have = conn.execute(
        "SELECT COUNT(*) FROM player_games WHERE season = ?", (args.season,)
    ).fetchone()[0]
    if not have:
        print(f"no data for {args.season}. Run scripts/backfill.py first.")
        return 1

    results = walk_forward(conn, args.season, args.stat,
                           args.min_games, args.lookback, args.season_type)
    summarize(results, args.stat)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
