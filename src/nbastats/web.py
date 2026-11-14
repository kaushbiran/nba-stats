"""NBA Watcher's local web UI. Standard library only — no FastAPI, no extra installs.

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

from . import db, project, recipes
from .query import (ADVANCED_STATS, COUNTING_STATS, DERIVED_COUNTS,
                    RATE_STATS, StatQuery, run)

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
    }


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


def _handle_query(conn, params: dict) -> dict:
    def one(key, default=None):
        value = params.get(key, [default])[0]
        return value if value not in ("", None) else default

    stats = one("stats", "pts,reb,ast").split(",")
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
        team=one("team"),
        min_games=int(one("min_games", 0) or 0),
        min_minutes=float(one("min_minutes", 0) or 0),
        group_by_season=one("career") != "1",
        order_by=one("order_by"),
        descending=one("asc") != "1",
        limit=min(int(one("limit", 50) or 50), 500),
        include_totals=one("include_totals") == "1",
    )
    return {"rows": run(conn, q), "stats": q.stats, "per_mode": q.per_mode}


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

        params = parse_qs(parsed.query)
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
        print(f"NBA Watcher is running at {url}")
        print("Press Ctrl+C to stop.")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")
