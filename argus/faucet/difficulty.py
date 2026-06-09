"""Chain-data oracle for the value-pegged PoW regime.

The value-pegged difficulty (testnet3) is anchored to the cost of actually
mining the requested amount, which needs the network's *current block subsidy* —
derived from the tip height. We read that from the network's own self-hosted
mempool explorer API (reachable in-container at ``argus-<net>-mempool-api:8999``
because the faucet joins each faucet network), not Core RPC, so no node
credentials need to live in the faucet container.

Per the design, value-pegged PoW is only offered where this data is available:
if the mempool API can't be reached and we have never cached a subsidy, the
caller disables PoW for that network. The subsidy steps only every 210,000
blocks, so once read it is cached and a transient API blip reuses the last known
value rather than dropping the feature.
"""

from __future__ import annotations

import os
import threading
import time

# Bitcoin's halving schedule: 50 BTC, halving every 210,000 blocks, to zero after
# 64 halvings. Identical across mainnet/testnet3.
_INITIAL_SUBSIDY_SAT = 50 * 100_000_000
_HALVING_INTERVAL = 210_000
_MAX_HALVINGS = 64

# In-container mempool API endpoint template; overridable for tests/non-standard
# deployments.
_API_TEMPLATE = os.environ.get(
    "FAUCET_MEMPOOL_API", "http://argus-{net}-mempool-api:8999"
)
# How long a fetched subsidy is treated as fresh before re-reading the tip.
_CACHE_TTL = 600.0

_lock = threading.Lock()
# net_key -> (fetched_at, subsidy_sat)
_cache: dict[str, tuple[float, int]] = {}


def subsidy_for_height(height: int) -> int:
    """The block subsidy (sats) at ``height`` on the standard halving schedule."""
    halvings = height // _HALVING_INTERVAL
    if halvings >= _MAX_HALVINGS:
        return 0
    return _INITIAL_SUBSIDY_SAT >> halvings


def _fetch_height(net_key: str, timeout: float) -> int | None:
    """Read the tip height from the network's mempool API, or None on any error."""
    import requests

    url = _API_TEMPLATE.format(net=net_key) + "/api/blocks/tip/height"
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
        return int(resp.text.strip())
    except Exception:
        return None


def block_subsidy_sat(
    net_key: str, *, now: float | None = None, timeout: float = 5.0
) -> int | None:
    """Current block subsidy (sats) for ``net_key``, or ``None`` if it can't be
    determined and was never cached.

    Cached for ``_CACHE_TTL`` seconds; on a fetch failure the last known value is
    reused (the subsidy is near-constant), so only a network that has *never*
    been reachable returns ``None``.
    """
    when = time.time() if now is None else now
    with _lock:
        cached = _cache.get(net_key)
    if cached is not None and when - cached[0] < _CACHE_TTL:
        return cached[1]

    height = _fetch_height(net_key, timeout)
    if height is None:
        return cached[1] if cached is not None else None

    subsidy = subsidy_for_height(height)
    with _lock:
        _cache[net_key] = (when, subsidy)
    return subsidy


def _reset_cache() -> None:
    """Test hook: drop the subsidy cache."""
    with _lock:
        _cache.clear()
