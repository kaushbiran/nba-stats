"""NBA Watcher: query completed NBA seasons from a local database."""

from .db import connect, init_schema
from .query import StatQuery, query, run, build_sql
from . import recipes

__version__ = "0.1.0"

__all__ = [
    "connect", "init_schema",
    "StatQuery", "query", "run", "build_sql",
    "recipes",
]
