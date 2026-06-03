"""Collect live per-service resource usage from the Docker daemon.

The dashboard never talks to the Docker socket directly. It reaches a read-only
``docker-socket-proxy`` sidecar over TCP (``DOCKER_HOST``), which exposes only
the handful of GET endpoints we need (containers, volumes, ``/system/df``). Every
call is defensive: if the proxy is unavailable or an endpoint is blocked, the
affected numbers come back as ``None`` and the page renders "n/a" rather than
failing.

Containers and volumes are attributed to a ``(network, service-bucket)`` pair by
name, matching the conventions the generators use:

* per-network services -> ``argus-<net>-<svc>`` containers / ``argus-<net>_<vol>``
  volumes (compose prefixes volumes with the project name);
* Bitcart -> ``argus-bitcart-<net>-*`` (deployed by the BareBits installer under
  its own ``DEPLOY_NAME`` project).
"""

from __future__ import annotations

import io
import json
import os
import tarfile
from dataclasses import dataclass, field

# Where the LND node-info sidecar writes the identity pubkey (see builders/lnd.py).
_LND_NODEINFO_PATH = "/home/lnd/.lnd/argus_nodeinfo.json"

from ..constants import NETWORK_ORDER

# Buckets we aggregate usage into. A network's row in the page asks for usage by
# one of these (fulcrum indexers use their own name-derived bucket, see below).
# "lnd2" precedes "lnd" so the optional second node + its sidecar are attributed
# separately (substring match would otherwise fold "lnd2" into "lnd").
_KEYWORD_BUCKETS = ("bitcoind", "miner", "lnd2", "lnd", "cashu", "mempool", "fulcrum")


def _split_net(rest: str) -> tuple[str | None, str]:
    """Peel a known network key off the front of ``rest`` (after stripping the
    ``argus-``/``argus-bitcart-`` prefix). Longest match wins so ``custom-signet``
    is not shadowed by a hypothetical shorter key."""
    for net in sorted(NETWORK_ORDER, key=len, reverse=True):
        if rest == net:
            return net, ""
        for sep in ("-", "_"):
            if rest.startswith(net + sep):
                return net, rest[len(net) + 1 :]
    return None, rest


def bucket_for(tail: str) -> str:
    """Map a service-name tail (e.g. ``fulcrum-1``, ``mempool-db``) to its bucket.

    Used both when classifying live containers/volumes and when a page row asks
    for its usage, so the two always agree on the key.
    """
    t = tail.lower()
    for key in _KEYWORD_BUCKETS:
        if key in t:
            return key
    if "mariadb" in t or t.endswith("_db") or t.endswith("-db"):
        return "mempool"
    return tail or "other"


def classify(name: str) -> tuple[str | None, str | None]:
    """Return ``(net_key, bucket)`` for a container or volume name, or
    ``(None, None)`` if it is not one of ours."""
    for prefix in ("argus-", "argus_"):
        if name.startswith(prefix):
            rest = name[len(prefix) :]
            break
    else:
        return None, None

    bucket_override: str | None = None
    for bprefix in ("bitcart-", "bitcart_"):
        if rest.startswith(bprefix):
            bucket_override = "bitcart"
            rest = rest[len(bprefix) :]
            break

    net, tail = _split_net(rest)
    if net is None:
        return None, None
    return net, (bucket_override or bucket_for(tail))


@dataclass
class Usage:
    """Aggregated usage for one ``(network, bucket)`` pair, in bytes."""

    ram: int = 0
    disk: int = 0

    def as_dict(self) -> dict[str, int]:
        return {"ram": self.ram, "disk": self.disk}


@dataclass
class MetricsResult:
    # usage[net_key][bucket] -> {"ram": int, "disk": int}
    usage: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    host: dict[str, int | None] = field(default_factory=dict)
    # lnd[net_key] / lnd2[net_key] -> identity pubkey (hex), when discoverable.
    lnd: dict[str, str] = field(default_factory=dict)
    lnd2: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "usage": self.usage,
            "host": self.host,
            "lnd": self.lnd,
            "lnd2": self.lnd2,
            "errors": self.errors,
        }


def _lnd_pubkey(client, net_key: str, service: str = "lnd") -> str | None:
    """Read an LND identity pubkey from the node-info file via the Docker API.

    Uses get_archive (a GET, allowed by the read-only socket proxy) so the
    dashboard needs no per-network volume or network wiring. ``service`` is the
    compose service name ("lnd" or the optional second node "lnd2"). Returns None
    if the network/node isn't deployed yet or the file isn't there.
    """
    try:
        ct = client.containers.get(f"argus-{net_key}-{service}")
        bits, _ = ct.get_archive(_LND_NODEINFO_PATH)
        tf = tarfile.open(fileobj=io.BytesIO(b"".join(bits)))
        member = tf.extractfile(tf.getmembers()[0])
        data = json.loads(member.read().decode())
        pubkey = data.get("identity_pubkey")
        return pubkey or None
    except Exception:
        return None


def _container_memory_bytes(stats: dict) -> int:
    """Working-set memory: total usage minus reclaimable page cache."""
    mem = stats.get("memory_stats", {}) or {}
    usage = mem.get("usage", 0) or 0
    detail = mem.get("stats", {}) or {}
    # cgroup v2 exposes inactive_file; v1 exposes total_inactive_file / cache.
    cache = (
        detail.get("inactive_file")
        or detail.get("total_inactive_file")
        or detail.get("cache")
        or 0
    )
    return max(usage - cache, 0)


def _host_metrics() -> tuple[dict[str, int | None], list[str]]:
    """Whole-host disk and memory totals (bytes).

    Disk is read from ``HOST_ROOT`` (the host filesystem mounted read-only) so it
    reflects the real machine, not the container's overlay. Memory is read via
    psutil, which reads ``/proc/meminfo`` (the host's, absent a memory cgroup cap).
    """
    host: dict[str, int | None] = {
        "disk_total": None,
        "disk_used": None,
        "disk_free": None,
        "mem_total": None,
        "mem_used": None,
        "mem_free": None,
    }
    errors: list[str] = []

    try:
        import shutil

        total, used, free = shutil.disk_usage(os.environ.get("HOST_ROOT", "/"))
        host["disk_total"], host["disk_used"], host["disk_free"] = total, used, free
    except Exception as exc:  # pragma: no cover - environment dependent
        errors.append(f"host disk: {exc}")

    try:
        import psutil

        vm = psutil.virtual_memory()
        host["mem_total"] = int(vm.total)
        host["mem_used"] = int(vm.total - vm.available)
        host["mem_free"] = int(vm.available)
    except Exception as exc:  # pragma: no cover - environment dependent
        errors.append(f"host memory: {exc}")

    return host, errors


def _add(usage: dict[str, dict[str, Usage]], net: str, bucket: str) -> Usage:
    return usage.setdefault(net, {}).setdefault(bucket, Usage())


def collect(net_keys: list[str] | None = None) -> MetricsResult:
    """Gather live usage from the Docker API (via the socket proxy) plus host
    totals and per-network LND pubkeys. Always returns a result; failures are
    recorded in ``errors``."""
    usage: dict[str, dict[str, Usage]] = {}
    lnd: dict[str, str] = {}
    lnd2: dict[str, str] = {}
    errors: list[str] = []

    try:
        import docker
    except Exception as exc:  # pragma: no cover - import guard
        errors.append(f"docker SDK unavailable: {exc}")
        host, herrs = _host_metrics()
        return MetricsResult(usage={}, host=host, errors=errors + herrs)

    client = None
    try:
        client = docker.from_env()  # honours DOCKER_HOST (the socket proxy)
    except Exception as exc:
        errors.append(f"docker connect: {exc}")

    if client is not None:
        # RAM: one-shot stats per running container.
        try:
            for c in client.containers.list():
                net, bucket = classify(c.name)
                if net is None or bucket is None:
                    continue
                try:
                    stats = c.stats(stream=False)
                except Exception:
                    continue
                _add(usage, net, bucket).ram += _container_memory_bytes(stats)
        except Exception as exc:
            errors.append(f"container stats: {exc}")

        # Disk: volume sizes (+ container writable layers) from /system/df.
        try:
            df = client.df()
            for vol in df.get("Volumes") or []:
                net, bucket = classify(vol.get("Name", ""))
                if net is None or bucket is None:
                    continue
                size = (vol.get("UsageData") or {}).get("Size", 0) or 0
                if size > 0:
                    _add(usage, net, bucket).disk += int(size)
            for cont in df.get("Containers") or []:
                names = cont.get("Names") or []
                name = (names[0] if names else "").lstrip("/")
                net, bucket = classify(name)
                if net is None or bucket is None:
                    continue
                _add(usage, net, bucket).disk += int(cont.get("SizeRw", 0) or 0)
        except Exception as exc:
            errors.append(f"disk usage (system/df): {exc}")

        # LND identity pubkeys (per network, both nodes), best-effort.
        for net_key in net_keys or []:
            pubkey = _lnd_pubkey(client, net_key)
            if pubkey:
                lnd[net_key] = pubkey
            pubkey2 = _lnd_pubkey(client, net_key, service="lnd2")
            if pubkey2:
                lnd2[net_key] = pubkey2

    host, herrs = _host_metrics()
    serialisable = {
        net: {bucket: u.as_dict() for bucket, u in buckets.items()}
        for net, buckets in usage.items()
    }
    return MetricsResult(
        usage=serialisable, host=host, lnd=lnd, lnd2=lnd2, errors=errors + herrs
    )
