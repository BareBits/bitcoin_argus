"""A tiny SQLite-backed cache (via peewee) for the metrics snapshot.

Computing per-container stats and volume sizes is relatively expensive (and the
daemon has to ``du`` volumes for ``/system/df``), so the spec requires it run at
most once per hour. We store the whole collected snapshot as one JSON row and
serve it until it ages past the TTL, then recompute lazily on the next request.
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable

from peewee import CharField, FloatField, Model, SqliteDatabase, TextField

CACHE_TTL_SECONDS = 3600  # at most one recompute per hour, per the spec

_DB_PATH = os.environ.get("CACHE_DB", "/data/cache.db")
database = SqliteDatabase(None)


class Snapshot(Model):
    """One cached payload, keyed by ``scope`` (we use a single "metrics" scope)."""

    scope = CharField(unique=True)
    payload = TextField()
    created_at = FloatField()

    class Meta:
        database = database


def init_db(path: str | None = None) -> None:
    """Bind and create the schema. Safe to call more than once."""
    db_path = path or _DB_PATH
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    database.init(db_path)
    if database.is_closed():
        database.connect(reuse_if_open=True)
    database.create_tables([Snapshot])


def _now() -> float:
    return time.time()


def get_or_refresh(
    refresh: Callable[[], dict],
    *,
    scope: str = "metrics",
    ttl: int = CACHE_TTL_SECONDS,
    force: bool = False,
) -> tuple[dict, float]:
    """Return ``(payload, age_seconds)``.

    Serves the stored snapshot while it is younger than ``ttl``; otherwise calls
    ``refresh()``, stores the result, and returns it. ``force=True`` always
    recomputes.
    """
    row = Snapshot.get_or_none(Snapshot.scope == scope)
    now = _now()
    if row is not None and not force and (now - row.created_at) < ttl:
        return json.loads(row.payload), now - row.created_at

    payload = refresh()
    encoded = json.dumps(payload)
    if row is None:
        Snapshot.create(scope=scope, payload=encoded, created_at=now)
    else:
        row.payload = encoded
        row.created_at = now
        row.save()
    return payload, 0.0
