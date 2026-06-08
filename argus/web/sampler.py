"""The metrics-history sampler: a small long-running loop that records per-service
resource usage over time.

It runs as its OWN container (a sibling of the dashboard built from the same
image — see :mod:`argus.web_gen`), so a sampler bug can never take the main page
down, mirroring how the faucet is isolated. Every ``SAMPLE_INTERVAL_SECONDS`` it:

1. reads each running container's CPU/RAM/net via the read-only docker-socket
   proxy (the same surface the live dashboard uses — no extra permissions);
2. turns the cumulative per-container net counters into per-interval byte deltas
   (reset-corrected across container restarts) and sums them into buckets;
3. samples whole-host CPU/RAM/disk/net as a synthetic ``__host__`` series;
4. samples per-bucket disk usage on a slower cadence (``/system/df`` makes the
   daemon ``du`` every volume, so it is throttled to ``DISK_SAMPLE_INTERVAL``);
5. writes one raw row per bucket, then rolls up + prunes the tiers.

All of it is best-effort: any failing piece is skipped and the loop continues.
"""

from __future__ import annotations

import os
import time

from . import history, metrics


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except ValueError:
        return default


class Sampler:
    def __init__(self) -> None:
        self.interval = _env_int("SAMPLE_INTERVAL_SECONDS", 60)
        self.disk_interval = _env_int("DISK_SAMPLE_INTERVAL_SECONDS", 900)
        self.raw_hours = _env_int("RAW_RETENTION_HOURS", 24)
        self.hourly_days = _env_int("HOURLY_RETENTION_DAYS", 3)
        self.daily_days = _env_int("DAILY_RETENTION_DAYS", 365)
        self.host_root = os.environ.get("HOST_ROOT", "/host")

        self.conn = history.connect()
        # Per-container cumulative net counters from the previous tick, so we can
        # diff them into per-interval deltas. Keyed by container name.
        self._last_rx: dict[str, int] = {}
        self._last_tx: dict[str, int] = {}
        # Last known per-(net,bucket) disk gauge, carried forward between the
        # throttled df samples so every raw row has a disk figure.
        self._last_disk: dict[tuple[str, str], int] = {}
        self._last_df_ts = 0.0
        # Whole-host net counters from the previous tick.
        self._host_rx: int | None = None
        self._host_tx: int | None = None
        self._last_tick = 0.0

    # -- net counter diffing -------------------------------------------------
    @staticmethod
    def _delta(cur: int, last: int | None) -> int:
        """Bytes moved since ``last``. No baseline yet -> 0; a counter reset
        (cur < last, i.e. the container restarted) -> treat ``cur`` as the delta
        from zero rather than going negative."""
        if last is None:
            return 0
        if cur < last:
            return cur
        return cur - last

    # -- host series ---------------------------------------------------------
    def _host_net(self) -> tuple[int, int] | None:
        """Cumulative host rx/tx from ``<host>/proc/net/dev``, summed over physical
        interfaces (loopback and docker/bridge/veth interfaces are skipped so we
        don't double-count traffic already seen on container veths)."""
        path = os.path.join(self.host_root, "proc", "net", "dev")
        try:
            with open(path) as fh:
                lines = fh.readlines()[2:]  # two header rows
        except Exception:
            return None
        rx = tx = 0
        for line in lines:
            iface, _, data = line.partition(":")
            iface = iface.strip()
            if not data or iface == "lo" or iface.startswith(
                ("docker", "br-", "veth")
            ):
                continue
            fields = data.split()
            if len(fields) < 9:
                continue
            try:
                rx += int(fields[0])
                tx += int(fields[8])
            except ValueError:
                continue
        return rx, tx

    def _host_row(self, dt: int) -> history.SampleRow:
        cpu = 0.0
        ram = 0
        disk: int | None = None
        try:
            import psutil

            # cpu_percent(interval=None) is the busy share since our previous
            # call (~one tick); convert to cores-used.
            cpu = (psutil.cpu_percent(interval=None) / 100.0) * (
                psutil.cpu_count() or 1
            )
            ram = int(psutil.virtual_memory().used)
        except Exception:
            pass
        try:
            import shutil

            disk = int(shutil.disk_usage(self.host_root).used)
        except Exception:
            disk = None

        rx_d = tx_d = 0
        counters = self._host_net()
        if counters is not None:
            rx, tx = counters
            rx_d = self._delta(rx, self._host_rx)
            tx_d = self._delta(tx, self._host_tx)
            self._host_rx, self._host_tx = rx, tx

        return history.SampleRow(
            net=history.HOST_KEY,
            bucket=history.HOST_KEY,
            cpu=cpu,
            ram=ram,
            disk=disk,
            rx=rx_d,
            tx=tx_d,
        )

    # -- disk (throttled) ----------------------------------------------------
    def _refresh_disk(self, client, now: float) -> None:
        if now - self._last_df_ts < self.disk_interval and self._last_disk:
            return
        try:
            df = client.df()
        except Exception:
            return
        fresh: dict[tuple[str, str], int] = {}
        for vol in df.get("Volumes") or []:
            net, bucket = metrics.classify(vol.get("Name", ""))
            if net is None or bucket is None:
                continue
            size = (vol.get("UsageData") or {}).get("Size", 0) or 0
            if size > 0:
                fresh[(net, bucket)] = fresh.get((net, bucket), 0) + int(size)
        for cont in df.get("Containers") or []:
            names = cont.get("Names") or []
            name = (names[0] if names else "").lstrip("/")
            net, bucket = metrics.classify(name)
            if net is None or bucket is None:
                continue
            fresh[(net, bucket)] = fresh.get((net, bucket), 0) + int(
                cont.get("SizeRw", 0) or 0
            )
        if fresh:
            self._last_disk = fresh
            self._last_df_ts = now

    # -- one tick ------------------------------------------------------------
    def tick(self, client, now: float | None = None) -> None:
        now = time.time() if now is None else now
        dt = int(now - self._last_tick) if self._last_tick else self.interval
        dt = max(1, dt)
        ts = int(now)

        samples = metrics.per_container_samples(client, client.containers.list())

        # Aggregate CPU/RAM and reset-corrected net deltas into buckets.
        cpu: dict[tuple[str, str], float] = {}
        ram: dict[tuple[str, str], int] = {}
        rx: dict[tuple[str, str], int] = {}
        tx: dict[tuple[str, str], int] = {}
        seen_names: set[str] = set()
        for s in samples:
            key = (s.net, s.bucket)
            cpu[key] = cpu.get(key, 0.0) + s.cpu
            ram[key] = ram.get(key, 0) + s.ram
            if s.has_net:
                seen_names.add(s.name)
                rx[key] = rx.get(key, 0) + self._delta(s.rx, self._last_rx.get(s.name))
                tx[key] = tx.get(key, 0) + self._delta(s.tx, self._last_tx.get(s.name))
                self._last_rx[s.name] = s.rx
                self._last_tx[s.name] = s.tx
        # Forget counters for containers that are gone (so a future container that
        # reuses the name doesn't diff against a stale, larger value).
        for gone in set(self._last_rx) - seen_names:
            self._last_rx.pop(gone, None)
            self._last_tx.pop(gone, None)

        self._refresh_disk(client, now)

        keys = set(cpu) | set(self._last_disk)
        rows = [
            history.SampleRow(
                net=net,
                bucket=bucket,
                cpu=cpu.get((net, bucket), 0.0),
                ram=ram.get((net, bucket), 0),
                disk=self._last_disk.get((net, bucket)),
                rx=rx.get((net, bucket), 0),
                tx=tx.get((net, bucket), 0),
            )
            for (net, bucket) in keys
        ]
        rows.append(self._host_row(dt))

        history.write_samples(self.conn, ts, dt, rows)
        history.rollup(self.conn, ts)
        history.prune(
            self.conn, ts, self.raw_hours, self.hourly_days, self.daily_days
        )
        self._last_tick = now

    # -- loop ----------------------------------------------------------------
    def run(self) -> None:
        import docker

        print(
            f"[argus-sampler] interval={self.interval}s disk={self.disk_interval}s "
            f"retention raw={self.raw_hours}h hour={self.hourly_days}d "
            f"day={self.daily_days}d",
            flush=True,
        )
        client = None
        while True:
            start = time.time()
            try:
                if client is None:
                    client = docker.from_env()  # honours DOCKER_HOST (proxy)
                self.tick(client)
            except Exception as exc:  # never let a bad tick kill the loop
                print(f"[argus-sampler] tick error: {exc}", flush=True)
                client = None  # force a reconnect next time
            elapsed = time.time() - start
            time.sleep(max(1.0, self.interval - elapsed))


def main() -> None:
    Sampler().run()


if __name__ == "__main__":
    main()
