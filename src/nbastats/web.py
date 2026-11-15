"""Stat Merchant's local web UI. Standard library only — no FastAPI, no extra installs.

Run with:  nbastats serve

Binds to 127.0.0.1 by default, so it is reachable only from this machine. The
database is read-only here: the API can query it, never write to it.
"""

from __future__ import annotations

import http.server
import json
import socketserver
import threading
import webbrowser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db, finder, project, recipes
from .query import (ADVANCED_STATS, COUNTING_STATS, DERIVED_COUNTS,
                    RATE_STATS, StatQuery, count, run)

STATIC_DIR = Path(__file__).parent / "static"


def _meta(conn) -> dict:
    """Everything the front end needs to build its controls."""
    seasons = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT season FROM player_games ORDER BY season DESC"
        )
    ]
    types = [
        r[0] for r in conn.execute(
            "SELECT DISTINCT season_type FROM player_games ORDER BY season_type"
        )
    ]
    counts = db.table_counts(conn)
    return {
        "seasons": seasons,
        "season_types": types,
        "counting_stats": sorted(COUNTING_STATS),
        "rate_stats": sorted(RATE_STATS),
        "advanced_stats": sorted(ADVANCED_STATS),
        "derived_counts": sorted(DERIVED_COUNTS),
        "recipes": [
            {"name": n, "summary": (f.__doc__ or "").strip().split("\n")[0]}
            for n, f in sorted(recipes.RECIPES.items())
        ],
        "counts": counts,
        "empty": counts.get("player_games", 0) == 0,
        "projectable": project.PROJECTABLE,
        # Game finder: what a condition can test, and the one-click examples.
        "finder_fields": sorted(finder.GAME_FIELDS),
        "finder_rate_fields": sorted(finder.RATE_FIELDS),
        "finder_groups": finder.GROUPS,
        "finder_presets": [
            {"name": name, **spec} for name, spec in finder.PRESETS.items()
        ],
    }


def _player_scopes(conn, player_id: int, season_type: str = "Regular Season") -> dict:
    """The ways one player's career can be sliced: by season, or by team.

    Team scope is the point of the comparison feature — "Jordan on the
    Wizards" is a different player from "Jordan on the Bulls", and most stats
    sites make you do that subtraction yourself.
    """
    row = conn.execute(
        "SELECT full_name FROM players WHERE player_id = ?", (player_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"no player with id {player_id}")

    seasons = [
        dict(r) for r in conn.execute(
            """SELECT season, COUNT(*) AS gp
               FROM player_games
               WHERE player_id = ? AND season_type = ?
               GROUP BY season ORDER BY season DESC""",
            (player_id, season_type),
        )
    ]
    teams = [
        dict(r) for r in conn.execute(
            """SELECT t.abbreviation AS team, COUNT(*) AS gp,
                      MIN(pg.season) AS first_season,
                      MAX(pg.season) AS last_season
               FROM player_games pg
               JOIN teams t ON t.team_id = pg.team_id
               WHERE pg.player_id = ? AND pg.season_type = ?
               GROUP BY pg.team_id
               ORDER BY gp DESC""",
            (player_id, season_type),
        )
    ]
    return {"player_id": player_id, "player": row["full_name"],
            "seasons": seasons, "teams": teams}


def _parse_entry(raw: str) -> dict:
    """`<player_id>~<scope>~<value>` — e.g. 893~team~WAS, 2544~season~2012."""
    parts = raw.split("~")
    if not parts or not parts[0].strip().isdigit():
        raise ValueError(f"bad comparison entry: {raw!r}")
    scope = parts[1] if len(parts) > 1 and parts[1] else "career"
    value = parts[2] if len(parts) > 2 else ""
    if scope not in ("career", "season", "team"):
        raise ValueError(f"unknown scope {scope!r}; use career, season or team")
    return {"player_id": int(parts[0]), "scope": scope, "value": value}


DEFAULT_STATS = "pts,reb,ast"


def _stats_param(params: dict, default: str = DEFAULT_STATS) -> list[str]:
    """Parse the `stats` parameter into a list of column names.

    Absent means "use the default". Present but empty means "no stat columns
    at all" — the Clear all button in the UI — and the two are deliberately
    not the same thing. Treating `stats=` as absent would silently restore
    the defaults and make that button appear to do nothing.
    """
    raw = params["stats"][0] if "stats" in params else default
    return [s.strip() for s in raw.split(",") if s.strip()]


def _handle_compare(conn, params: dict) -> dict:
    """One aggregated row per player-and-scope, so rows line up for comparison.

    Each entry collapses to a single row (group_by_season off) because the
    whole point is putting "Harden on the Rockets" next to "Jordan on the
    Wizards" as two comparable lines.
    """
    raw_entries = params.get("p", [])
    if not raw_entries:
        raise ValueError("pick at least one player to compare")
    if len(raw_entries) > 6:
        raise ValueError("comparing more than six entries gets unreadable")

    stats = _stats_param(params)
    per_mode = params.get("per_mode", ["per_game"])[0]
    season_type = params.get("season_type", ["Regular Season"])[0]

    rows = []
    for raw in raw_entries:
        e = _parse_entry(raw)
        q = StatQuery(
            stats=stats,
            per_mode=per_mode,
            season_type=season_type,
            player_ids=[e["player_id"]],
            season=int(e["value"]) if e["scope"] == "season" and e["value"] else None,
            team=e["value"] if e["scope"] == "team" else None,
            group_by_season=False,
            limit=1,
        )
        result = run(conn, q)
        name = conn.execute(
            "SELECT full_name FROM players WHERE player_id = ?",
            (e["player_id"],),
        ).fetchone()
        display = name["full_name"] if name else f"#{e['player_id']}"

        # Every entry produces a row with the same keys in the same order, so
        # the columns line up even when one of them matched nothing.
        row = {"player": display, "counting": _scope_label(e),
               "team": "", "gp": 0}
        for s in stats:
            row[s] = None

        if result:
            got = result[0]
            # Only worth showing when the scope doesn't already name the team.
            # For a career row it says which teams; for a team row it would
            # just repeat the Counting column.
            row["team"] = got.get("team", "") if e["scope"] != "team" else ""
            row["gp"] = got.get("gp", 0)
            for s in stats:
                row[s] = got.get(s)
        else:
            # Kept rather than dropped, so a filter that matched nothing is
            # visible instead of silently vanishing from the comparison.
            row["counting"] += " — no games"
        rows.append(row)

    return {"rows": rows, "stats": stats, "per_mode": per_mode}


def _scope_label(entry: dict) -> str:
    if entry["scope"] == "career":
        return "career"
    if entry["scope"] == "season":
        year = int(entry["value"])
        return f"{year}-{str(year + 1)[-2:]}"
    return entry["value"].upper()


def _handle_project(conn, params: dict) -> dict:
    def one(key, default=None):
        v = params.get(key, [default])[0]
        return v if v not in ("", None) else default

    player = one("player")
    if not player:
        raise ValueError("a player is required")

    is_home = one("venue")
    return project.project(
        conn,
        player=player,
        stat=one("stat", "pts"),
        opponent=one("opponent"),
        season=int(one("season")) if one("season") else None,
        is_home=None if is_home is None else is_home == "home",
        lookback=int(one("lookback", project.DEFAULT_LOOKBACK)),
        line=float(one("line")) if one("line") else None,
        season_type=one("season_type", "Regular Season"),
    )


def _search_players(conn, term: str, limit: int = 8) -> list[dict]:
    """Autocomplete over players who actually appear in the data.

    Ranked so that a name starting with the term beats one merely containing
    it, then by games played, so typing 'cur' surfaces Stephen Curry rather
    than someone with 11 career games.
    """
    if not term.strip():
        return []
    like = f"%{term.lower()}%"
    prefix = f"{term.lower()}%"
    rows = conn.execute(
        """
        SELECT p.player_id, p.full_name,
               COUNT(*) AS gp,
               MIN(pg.season) AS first_season,
               MAX(pg.season) AS last_season
        FROM player_games pg
        JOIN players p ON p.player_id = pg.player_id
        WHERE LOWER(p.full_name) LIKE ?
        GROUP BY p.player_id
        ORDER BY (CASE WHEN LOWER(p.full_name) LIKE ? THEN 0 ELSE 1 END),
                 gp DESC
        LIMIT ?
        """,
        (like, prefix, limit),
    )
    return [dict(r) for r in rows]


def _teams(conn) -> list[dict]:
    rows = conn.execute(
        """SELECT DISTINCT t.abbreviation, t.full_name
           FROM team_games tg JOIN teams t ON t.team_id = tg.team_id
           ORDER BY t.abbreviation"""
    )
    return [dict(r) for r in rows]


def _int_list(raw: str | None, limit: int = 8) -> list[int] | None:
    """Parse "123,456" into ids, dropping anything that isn't a number.

    Capped so a hand-edited URL can't build an enormous IN clause.
    """
    if not raw:
        return None
    out = []
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.append(int(part))
    return out[:limit] or None


def _handle_query(conn, params: dict) -> dict:
    def one(key, default=None):
        value = params.get(key, [default])[0]
        return value if value not in ("", None) else default

    stats = _stats_param(params)
    season_range = None
    if one("season_from") and one("season_to"):
        season_range = (int(one("season_from")), int(one("season_to")))

    q = StatQuery(
        stats=[s.strip() for s in stats if s.strip()],
        per_mode=one("per_mode", "per_game"),
        season=int(one("season")) if one("season") else None,
        season_range=season_range,
        season_type=one("season_type", "Regular Season"),
        player=one("player"),
        player_ids=_int_list(one("player_ids")),
        team=one("team"),
        min_games=int(one("min_games", 0) or 0),
        min_minutes=float(one("min_minutes", 0) or 0),
        group_by_season=one("career") != "1",
        order_by=one("order_by"),
        descending=one("asc") != "1",
        limit=min(int(one("limit", 50) or 50), 500),
        offset=max(0, int(one("offset", 0) or 0)),
        include_totals=one("include_totals") == "1",
    )
    return {
        "rows": run(conn, q),
        "stats": q.stats,
        "per_mode": q.per_mode,
        # How many rows match before the limit, so the table can number rows
        # by their real rank even when sorted from the bottom up.
        "total": count(conn, q),
        "ascending": not q.descending,
        "offset": q.offset,
        "limit": q.limit,
    }


def _handle_finder(conn, params: dict) -> dict:
    """Individual games matching a set of conditions.

    Unlike /api/query this returns one row per game, so the response carries
    the game context columns (date, opponent, result, score) as well as the
    stats, and `columns` says which stat columns came back — the query may
    have added one for a field that was filtered on.
    """
    def one(key, default=None):
        value = params.get(key, [default])[0]
        return value if value not in ("", None) else default

    stats = _stats_param(params)
    season_range = None
    if one("season_from") and one("season_to"):
        season_range = (int(one("season_from")), int(one("season_to")))

    q = finder.GameQuery(
        stats=stats,
        conditions=finder.parse_conditions(one("where")),
        season=int(one("season")) if one("season") else None,
        season_range=season_range,
        season_type=one("season_type", "Regular Season"),
        player=one("player"),
        player_ids=_int_list(one("player_ids")),
        team=one("team"),
        opponent=one("opponent"),
        venue=one("venue"),
        result=one("result"),
        date_from=one("date_from"),
        date_to=one("date_to"),
        order_by=one("order_by", finder.DEFAULT_ORDER),
        descending=one("asc") != "1",
        limit=min(int(one("limit", 100) or 100), 500),
        offset=max(0, int(one("offset", 0) or 0)),
    )
    return {
        "rows": finder.run(conn, q),
        "columns": finder.shown_columns(q),
        "total": finder.count(conn, q),
        # Echoed back so the heading can state the filter that actually ran,
        # rather than the UI's idea of what it asked for.
        "conditions": finder.describe_conditions(q.conditions),
        "ascending": not q.descending,
        "offset": q.offset,
        "limit": q.limit,
    }


def _handle_recipe(conn, name: str, params: dict) -> dict:
    import inspect

    fn = recipes.RECIPES.get(name)
    if fn is None:
        raise KeyError(name)

    accepted = inspect.signature(fn).parameters
    kwargs = {}
    for key, cast in [("player", str), ("season", int), ("stat", str),
                      ("threshold", int), ("min_games", int), ("limit", int),
                      ("season_type", str)]:
        raw = params.get(key, [None])[0]
        if raw not in (None, "") and key in accepted:
            kwargs[key] = cast(raw)
    if "limit" in accepted and "limit" not in kwargs:
        kwargs["limit"] = 50
    return {"rows": fn(conn, **kwargs), "recipe": name}


class Handler(http.server.SimpleHTTPRequestHandler):
    db_path: str | None = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, fmt, *args):
        # Quiet by default; a request log per keystroke of autocomplete is noise.
        pass

    def _json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, default=str).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        # keep_blank_values, because for one parameter an empty value is a
        # real answer: `stats=` means "no stat columns", which is not the
        # same as leaving stats out and getting the defaults. Every other
        # reader treats "" as absent anyway, so nothing else sees a
        # difference.
        params = parse_qs(parsed.query, keep_blank_values=True)
        # A fresh connection per request: SQLite objects are not safe to share
        # across threads, and opening one is microseconds.
        conn = db.connect(self.db_path)

        try:
            if parsed.path == "/api/meta":
                return self._json(_meta(conn))
            if parsed.path == "/api/search":
                term = params.get("q", [""])[0]
                return self._json({"players": _search_players(conn, term)})
            if parsed.path == "/api/teams":
                return self._json({"teams": _teams(conn)})
            if parsed.path == "/api/query":
                return self._json(_handle_query(conn, params))
            if parsed.path == "/api/project":
                return self._json(_handle_project(conn, params))
            if parsed.path == "/api/scopes":
                pid = params.get("player_id", [""])[0]
                if not pid.isdigit():
                    raise ValueError("player_id is required")
                return self._json(_player_scopes(
                    conn, int(pid),
                    params.get("season_type", ["Regular Season"])[0]))
            if parsed.path == "/api/compare":
                return self._json(_handle_compare(conn, params))
            if parsed.path == "/api/finder":
                return self._json(_handle_finder(conn, params))
            if parsed.path.startswith("/api/recipe/"):
                name = parsed.path.rsplit("/", 1)[-1]
                return self._json(_handle_recipe(conn, name, params))
            return self._json({"error": "not found"}, 404)

        except KeyError as exc:
            return self._json({"error": f"unknown recipe: {exc}"}, 404)
        except ValueError as exc:
            return self._json({"error": str(exc)}, 400)
        except Exception as exc:  # noqa: BLE001
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        finally:
            conn.close()


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def serve(port: int = 8000, db_path: str | None = None,
          open_browser: bool = True, host: str = "127.0.0.1") -> None:
    handler = type("BoundHandler", (Handler,), {"db_path": db_path})

    with _Server((host, port), handler) as httpd:
        url = f"http://{host}:{port}"
        print(f"Stat Merchant is running at {url}")
        print("Press Ctrl+C to stop.")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
