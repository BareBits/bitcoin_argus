"""The time-series history store + the sampler's counter/delta logic."""

from __future__ import annotations

import time

import pytest

from argus.web import history, sampler


# --- store: write / rollup / prune / load -----------------------------------


def _rows():
    return [
        history.SampleRow(
            net="regtest", bucket="bitcoind", cpu=0.5, ram=100, disk=2000, rx=1000, tx=200
        ),
        history.SampleRow(
            net=history.HOST_KEY, bucket=history.HOST_KEY, cpu=1.2, ram=9, disk=5, rx=9, tx=3
        ),
    ]


def test_write_rollup_prune_and_load(tmp_path):
    db = str(tmp_path / "m.db")
    conn = history.connect(db)
    base = 1_700_000_000  # fixed, hour-aligned-ish anchor
    for k in range(6):
        history.write_samples(conn, base + k * 60, 60, _rows())
    history.rollup(conn, base + 6 * 60)

    # raw load returns the per-(net,bucket) entries with parallel arrays.
    p = history.load_series(3600, 24, path=db, now=base + 6 * 60)
    assert p["tier"] == "raw"
    bc = p["series"]["regtest"]["bitcoind"]
    assert len(bc["ts"]) == 6
    assert bc["cpu"][0] == 0.5 and bc["rx"][0] == 1000 and bc["dt"][0] == 60
    assert history.HOST_KEY in p["series"]

    # hour rollup sums rx (6 * 1000) and dt (6 * 60), keeps cpu average.
    conn2 = history.connect(db)
    row = conn2.execute(
        "SELECT rx_bytes, dt, cpu_avg, disk FROM metric "
        "WHERE tier='hour' AND net='regtest' AND bucket='bitcoind'"
    ).fetchone()
    assert row["rx_bytes"] == 6000
    assert row["dt"] == 360
    assert row["cpu_avg"] == pytest.approx(0.5)
    assert row["disk"] == 2000  # gauge: last reading in the hour
    conn.close()
    conn2.close()


def test_tier_selection_by_range():
    assert history.tier_for_range(3600, 24) == "raw"
    assert history.tier_for_range(24 * 3600, 24) == "raw"
    assert history.tier_for_range(3 * 86400, 24) == "hour"
    assert history.tier_for_range(365 * 86400, 24) == "day"


def test_prune_drops_aged_rows(tmp_path):
    db = str(tmp_path / "p.db")
    conn = history.connect(db)
    now = 1_700_000_000
    old = now - 48 * 3600  # older than the 24h raw horizon
    history.write_samples(conn, old, 60, _rows())
    history.write_samples(conn, now, 60, _rows())
    history.prune(conn, now, raw_hours=24, hourly_days=3, daily_days=365)
    remaining = conn.execute(
        "SELECT COUNT(*) c FROM metric WHERE tier='raw'"
    ).fetchone()["c"]
    # Only the recent tick survives (2 buckets); the 48h-old one is pruned.
    assert remaining == 2
    conn.close()


# --- sampler: net counter diffing -------------------------------------------


def test_delta_handles_baseline_and_reset():
    # No previous value -> 0 (we have no interval to attribute yet).
    assert sampler.Sampler._delta(500, None) == 0
    # Normal monotonic increase.
    assert sampler.Sampler._delta(1500, 1000) == 500
    # Counter reset (container restarted): current < last -> treat as from zero.
    assert sampler.Sampler._delta(300, 1000) == 300


def test_host_net_parses_and_skips_virtual(tmp_path, monkeypatch):
    proc = tmp_path / "proc" / "net"
    proc.mkdir(parents=True)
    # Header (2 lines) + eth0 (real) + lo + docker0 + veth123 (all skipped).
    (proc / "dev").write_text(
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets\n"
        "  eth0: 1000 5 0 0 0 0 0 0 2000 7 0 0 0 0 0 0\n"
        "    lo: 9999 1 0 0 0 0 0 0 9999 1 0 0 0 0 0 0\n"
        "docker0: 4444 1 0 0 0 0 0 0 4444 1 0 0 0 0 0 0\n"
        "veth9: 7777 1 0 0 0 0 0 0 7777 1 0 0 0 0 0 0\n"
    )
    monkeypatch.setenv("HOST_ROOT", str(tmp_path))
    monkeypatch.setenv("HISTORY_DB", str(tmp_path / "s.db"))
    s = sampler.Sampler()
    assert s._host_net() == (1000, 2000)  # only eth0 counted


# --- sampler: a full tick against a fake docker client ----------------------


class _FakeContainer:
    def __init__(self, name, rx, tx):
        self.name, self.rx, self.tx = name, rx, tx

    def stats(self, stream=False):
        return {
            "memory_stats": {"usage": 1000, "stats": {"inactive_file": 0}},
            "cpu_stats": {
                "cpu_usage": {"total_usage": 200},
                "system_cpu_usage": 1000,
                "online_cpus": 2,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 0},
                "system_cpu_usage": 0,
            },
            "networks": {"eth0": {"rx_bytes": self.rx, "tx_bytes": self.tx}},
        }


class _FakeClient:
    def __init__(self, containers):
        self._containers = containers
        self.containers = self

    def list(self):
        return self._containers

    def df(self):
        return {
            "Volumes": [
                {
                    "Name": "argus-regtest_bitcoind_data",
                    "UsageData": {"Size": 12_345},
                }
            ],
            "Containers": [],
        }


def test_sampler_tick_writes_reset_corrected_deltas(tmp_path, monkeypatch):
    monkeypatch.setenv("HISTORY_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("HOST_ROOT", str(tmp_path))  # no /proc -> host net skipped
    s = sampler.Sampler()
    cont = _FakeContainer("argus-regtest-bitcoind", rx=1000, tx=100)
    client = _FakeClient([cont])

    base = 1_700_000_000
    # First tick: no baseline, so the net delta is 0 (but CPU/disk are recorded).
    s.tick(client, now=base)
    # Second tick (60s later): counters advanced by 500/50 -> the recorded delta.
    cont.rx, cont.tx = 1500, 150
    s.tick(client, now=base + 60)

    p = history.load_series(3600, 24, path=str(tmp_path / "t.db"), now=base + 60)
    bc = p["series"]["regtest"]["bitcoind"]
    assert bc["rx"][0] == 0 and bc["rx"][1] == 500
    assert bc["tx"][1] == 50
    assert bc["cpu"][1] == pytest.approx(0.4)  # 200/1000 * 2 cores
    assert bc["disk"][1] == 12_345
    # The synthetic host series is recorded too.
    assert history.HOST_KEY in p["series"]
