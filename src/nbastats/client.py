"""Thin wrapper around nba_api with rate limiting, retries and raw caching.

stats.nba.com is undocumented, throttled, and hostile to cloud IP ranges. Every
request goes through here so the politeness rules live in exactly one place.

The cache is the important part: raw JSON is written to data/raw/ before it is
parsed. When (not if) a parser bug turns up, re-parsing is a local operation
instead of a 30-season re-pull.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from .db import RAW_DIR

log = logging.getLogger(__name__)

# stats.nba.com throttles bursts. One second between calls is the commonly
# used floor; raise it if you start seeing timeouts.
REQUEST_DELAY_SECONDS = 1.0
MAX_RETRIES = 4
BACKOFF_BASE = 2.0
TIMEOUT_SECONDS = 60

_last_request_at = 0.0


def _throttle() -> None:
    """Sleep so that consecutive requests are at least REQUEST_DELAY apart."""
    global _last_request_at
    elapsed = time.time() - _last_request_at
    if elapsed < REQUEST_DELAY_SECONDS:
        time.sleep(REQUEST_DELAY_SECONDS - elapsed)
    _last_request_at = time.time()


def season_str(season: int) -> str:
    """2015 -> '2015-16'. NBA.com wants the hyphenated form."""
    return f"{season}-{str(season + 1)[-2:]}"


def _cache_path(name: str, season: int, season_type: str) -> Path:
    safe_type = season_type.replace(" ", "_").lower()
    return RAW_DIR / f"{season}" / f"{name}__{safe_type}.json"


def fetch_league_game_log(
    season: int,
    season_type: str = "Regular Season",
    player_or_team: str = "P",
    use_cache: bool = True,
) -> list[dict]:
    """Pull one season's game log.

    player_or_team: 'P' for one row per player per game, 'T' for one row per
    team per game. Two calls per season covers everything we store.

    Returns a list of dicts (header names mapped onto row values).
    """
    name = "player_gamelog" if player_or_team == "P" else "team_gamelog"
    cache = _cache_path(name, season, season_type)

    if use_cache and cache.exists():
        log.info("cache hit %s", cache)
        return json.loads(cache.read_text())

    # Imported lazily so the query layer works with no network dependency.
    from nba_api.stats.endpoints import leaguegamelog

    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            _throttle()
            log.info(
                "fetching %s %s %s (attempt %d)",
                name, season_str(season), season_type, attempt + 1,
            )
            resp = leaguegamelog.LeagueGameLog(
                season=season_str(season),
                season_type_all_star=season_type,
                player_or_team_abbreviation=player_or_team,
                timeout=TIMEOUT_SECONDS,
            )
            rows = resp.league_game_log.get_dict()
            records = [
                dict(zip(rows["headers"], row)) for row in rows["data"]
            ]
            cache.parent.mkdir(parents=True, exist_ok=True)
            cache.write_text(json.dumps(records))
            return records
        except Exception as exc:  # noqa: BLE001 - we retry on anything
            last_error = exc
            wait = BACKOFF_BASE ** attempt
            log.warning("request failed (%s), retrying in %.0fs", exc, wait)
            time.sleep(wait)

    raise RuntimeError(
        f"failed to fetch {name} for {season_str(season)} {season_type} "
        f"after {MAX_RETRIES} attempts: {last_error}"
    )


def fetch_static_reference() -> tuple[list[dict], list[dict]]:
    """Player and team reference data. Ships with nba_api, so no HTTP."""
    from nba_api.stats.static import players as static_players
    from nba_api.stats.static import teams as static_teams

    return static_players.get_players(), static_teams.get_teams()
