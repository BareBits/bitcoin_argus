"""A tiny tiered time-series store for container resource history (SQLite).

The dashboard's live snapshot answers "what is each service using *right now*"; this
module answers "how has it trended". A separate :mod:`argus.web.sampler` process
writes one row per ``(network, bucket)`` every sample interval; the dashboard's
``/stats`` page reads them back. Both talk to the same SQLite file on a shared
volume, in WAL mode so the reader never blocks the writer.

Storage is three tiers in one table, distinguished by ``tier``:

* ``raw``  — one row per sample interval, kept ~24h (burst-level detail);
* ``hour`` — raw rolled up per clock-hour, kept ~3 days;
* ``day``  — hours rolled up per clock-day, kept ~365 days.

Each row's ``rx_bytes``/``tx_bytes`` are the bytes moved *within that row's
interval* (a delta, not a cumulative counter), so summing them gives total
bandwidth and dividing by ``dt`` gives the average speed over the interval —
which is what the speed and bandwidth graphs need. ``cpu``/``ram`` are gauges
(cores / bytes) stored as the interval's dt-weighted average plus its peak;
``disk`` is a slow gauge stored as the interval's last reading.

The two synthetic keys ``net == bucket == HOST_KEY`` hold whole-host totals.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass

HOST_KEY = "__host__"

_DEFAULT_DB_PATH = "/history/metrics.db"

# Tier bucket sizes (seconds). raw has no fixed size (it is per-sample).
HOUR = 3600
DAY = 86400


@dataclass
class SampleRow:
    """One ``(net, bucket)`` measurement to persist for a single interval.

    ``rx``/``tx`` are per-interval byte *deltas* (already reset-corrected by the
    sampler); ``cpu`` is cores-used, ``ram``/``disk`` are bytes."""

    net: str
    bucket: str
    cpu: float = 0.0
    ram: int = 0
    disk: int | None = None
    rx: int = 0
    tx: int = 0


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the history DB with WAL + a busy timeout, and
    ensure the schema exists. Safe to call from both writer and reader. The path
    defaults to ``$HISTORY_DB`` (read at call time, not import) so tests and the
    container env alike are honoured."""
    db_path = path or os.environ.get("HISTORY_DB", _DEFAULT_DB_PATH)
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # WAL lets the dashboard read while the sampler writes; NORMAL sync is plenty
    # durable for monitoring data and avoids fsync on every interval.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS metric (
            tier     TEXT    NOT NULL,
            ts       INTEGER NOT NULL,
            net      TEXT    NOT NULL,
            bucket   TEXT    NOT NULL,
            cpu_avg  REAL    NOT NULL DEFAULT 0,
            cpu_max  REAL    NOT NULL DEFAULT 0,
            ram_avg  INTEGER NOT NULL DEFAULT 0,
            ram_max  INTEGER NOT NULL DEFAULT 0,
            disk     INTEGER,
            rx_bytes INTEGER NOT NULL DEFAULT 0,
            tx_bytes INTEGER NOT NULL DEFAULT 0,
            dt       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (tier, ts, net, bucket)
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS metric_tier_ts ON metric (tier, ts)"
    )
    conn.commit()


def write_samples(
    conn: sqlite3.Connection,
    ts: int,
    dt: int,
    rows: list[SampleRow],
) -> None:
    """Persist a batch of raw samples taken at ``ts`` covering ``dt`` seconds.

    A raw row stores the gauge as both its (single-sample) average and peak so the
    rollups can keep a peak column without special-casing the raw tier."""
    conn.executemany(
        """
        INSERT OR REPLACE INTO metric
            (tier, ts, net, bucket, cpu_avg, cpu_max, ram_avg, ram_max,
             disk, rx_bytes, tx_bytes, dt)
        VALUES ('raw', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                ts,
                r.net,
                r.bucket,
                r.cpu,
                r.cpu,
                r.ram,
                r.ram,
                r.disk,
                r.rx,
                r.tx,
                dt,
            )
            for r in rows
        ],
    )
    conn.commit()


def _rollup_into(
    conn: sqlite3.Connection, src_tier: str, dst_tier: str, span: int, since: int
) -> None:
    """Aggregate ``src_tier`` rows whose bucket-floor is >= ``since`` into
    ``dst_tier`` rows floored to ``span`` seconds.

    Re-aggregating the recent window every tick (rather than only sealed buckets)
    keeps the *current*, still-growing bucket fresh and is idempotent thanks to
    INSERT OR REPLACE on the (tier, ts, net, bucket) key. The dt-weighted averages
    survive a second pass because each source row carries its own ``dt``. ``disk``
    is the reading at the latest source ts in the bucket (a gauge, not a sum)."""
    conn.execute(
        f"""
        INSERT OR REPLACE INTO metric
            (tier, ts, net, bucket, cpu_avg, cpu_max, ram_avg, ram_max,
             disk, rx_bytes, tx_bytes, dt)
        SELECT
            '{dst_tier}',
            (ts / {span}) * {span} AS bts,
            net, bucket,
            CASE WHEN SUM(dt) > 0 THEN SUM(cpu_avg * dt) / SUM(dt) ELSE 0 END,
            MAX(cpu_max),
            CASE WHEN SUM(dt) > 0 THEN SUM(ram_avg * dt) / SUM(dt) ELSE 0 END,
            MAX(ram_max),
            (SELECT m2.disk FROM metric m2
                WHERE m2.tier = '{src_tier}' AND m2.net = m.net
                  AND m2.bucket = m.bucket
                  AND (m2.ts / {span}) * {span} = (m.ts / {span}) * {span}
                  AND m2.disk IS NOT NULL
                ORDER BY m2.ts DESC LIMIT 1),
            SUM(rx_bytes), SUM(tx_bytes), SUM(dt)
        FROM metric m
        WHERE tier = '{src_tier}' AND ts >= ?
        GROUP BY bts, net, bucket
        """,
        (since,),
    )
    conn.commit()


def rollup(conn: sqlite3.Connection, now: int | None = None) -> None:
    """Refresh the hour and day tiers from the tiers below them.

    Only the trailing two buckets of each destination tier are recomputed (the
    current, still-filling one plus the one just sealed), so the work per tick is
    tiny regardless of how much history is retained."""
    now = int(now if now is not None else time.time())
    _rollup_into(conn, "raw", "hour", HOUR, now - 2 * HOUR)
    _rollup_into(conn, "hour", "day", DAY, now - 2 * DAY)


def prune(
    conn: sqlite3.Connection,
    now: int | None,
    raw_hours: int,
    hourly_days: int,
    daily_days: int,
) -> None:
    """Delete rows past each tier's retention horizon."""
    now = int(now if now is not None else time.time())
    conn.execute(
        "DELETE FROM metric WHERE tier='raw' AND ts < ?",
        (now - raw_hours * HOUR,),
    )
    conn.execute(
        "DELETE FROM metric WHERE tier='hour' AND ts < ?",
        (now - hourly_days * DAY,),
    )
    conn.execute(
        "DELETE FROM metric WHERE tier='day' AND ts < ?",
        (now - daily_days * DAY,),
    )
    conn.commit()


# Range (seconds) -> the finest tier that still covers it without returning an
# unwieldy number of points. Ordered coarsest-last; the first whose window is
# large enough wins.
def tier_for_range(range_seconds: int, raw_hours: int) -> str:
    """Pick the tier to serve a requested look-back of ``range_seconds``."""
    if range_seconds <= raw_hours * HOUR:
        return "raw"
    if range_seconds <= 3 * DAY:
        return "hour"
    return "day"


def load_series(
    range_seconds: int,
    raw_hours: int,
    *,
    path: str | None = None,
    now: int | None = None,
) -> dict:
    """Build the ``/stats`` chart payload for the last ``range_seconds``.

    Picks the finest tier that covers the range and returns one entry per
    ``(net, bucket)``. ``rx``/``tx`` are per-interval byte deltas and ``dt`` the
    interval length, so the page derives speed (``rx/dt``) and cumulative
    bandwidth (running sum of ``rx``) itself. Opens its own short-lived
    connection so it is safe to call per request alongside the writer (WAL)."""
    now = int(now if now is not None else time.time())
    tier = tier_for_range(range_seconds, raw_hours)
    since = now - range_seconds
    conn = connect(path)
    try:
        rows = query(conn, tier, since, now)
    finally:
        conn.close()

    series: dict[str, dict[str, dict[str, list]]] = {}
    for r in rows:
        entry = series.setdefault(r["net"], {}).setdefault(
            r["bucket"],
            {"ts": [], "dt": [], "cpu": [], "ram": [], "disk": [], "rx": [], "tx": []},
        )
        entry["ts"].append(r["ts"])
        entry["dt"].append(r["dt"])
        entry["cpu"].append(round(r["cpu_avg"], 4))
        entry["ram"].append(int(r["ram_avg"]))
        entry["disk"].append(r["disk"])
        entry["rx"].append(int(r["rx_bytes"]))
        entry["tx"].append(int(r["tx_bytes"]))
    return {
        "range_seconds": range_seconds,
        "tier": tier,
        "host_key": HOST_KEY,
        "series": series,
    }


def query(
    conn: sqlite3.Connection, tier: str, since: int, until: int | None = None
) -> list[sqlite3.Row]:
    """Return all rows of ``tier`` with ``since <= ts (<= until)``, oldest first."""
    until = int(until if until is not None else time.time())
    cur = conn.execute(
        """
        SELECT ts, net, bucket, cpu_avg, cpu_max, ram_avg, ram_max,
               disk, rx_bytes, tx_bytes, dt
        FROM metric
        WHERE tier = ? AND ts >= ? AND ts <= ?
        ORDER BY ts ASC
        """,
        (tier, since, until),
    )
    return cur.fetchall()
