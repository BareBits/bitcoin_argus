"""Resolve effective resource settings for a network.

Precedence per value: explicit per-network knob > explicit global knob >
profile bundle (per-network profile > global profile > default) > built-in.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import ArgusConfig
from .constants import (
    DEFAULT_LOG_MAX_FILE,
    DEFAULT_LOG_MAX_SIZE,
    DEFAULT_RESOURCE_PROFILE,
    RESOURCE_KNOBS,
    RESOURCE_PROFILES,
)


@dataclass(frozen=True)
class ResolvedResources:
    bitcoind_dbcache: int
    bitcoind_maxmempool: int
    fulcrum_db_mem: int
    fulcrum_db_max_open_files: int
    mempool_mariadb_buffer_mb: int
    log_rotation: bool
    log_max_size: str
    log_max_file: int


def _pick(net_val, global_val, fallback):
    if net_val is not None:
        return net_val
    if global_val is not None:
        return global_val
    return fallback


def resolve(cfg: ArgusConfig, net_key: str) -> ResolvedResources:
    g = cfg.global_.resources
    n = cfg.networks[net_key].resources
    profile = n.profile or g.profile or DEFAULT_RESOURCE_PROFILE
    base = RESOURCE_PROFILES[profile]
    knobs = {k: _pick(getattr(n, k), getattr(g, k), base[k]) for k in RESOURCE_KNOBS}
    return ResolvedResources(
        **knobs,
        log_rotation=_pick(n.log_rotation, g.log_rotation, True),
        log_max_size=_pick(n.log_max_size, g.log_max_size, DEFAULT_LOG_MAX_SIZE),
        log_max_file=_pick(n.log_max_file, g.log_max_file, DEFAULT_LOG_MAX_FILE),
    )


def log_options(res: ResolvedResources) -> dict:
    """The compose ``logging:`` block for json-file rotation."""
    return {
        "driver": "json-file",
        "options": {"max-size": res.log_max_size, "max-file": str(res.log_max_file)},
    }


def global_log(cfg: ArgusConfig) -> tuple[bool, dict]:
    """(rotation_on, logging-block) from global resources — for the shared layer."""
    g = cfg.global_.resources
    rotation = g.log_rotation if g.log_rotation is not None else True
    block = {
        "driver": "json-file",
        "options": {
            "max-size": g.log_max_size or DEFAULT_LOG_MAX_SIZE,
            "max-file": str(g.log_max_file or DEFAULT_LOG_MAX_FILE),
        },
    }
    return rotation, block
