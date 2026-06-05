"""Explorer links for a faucet payout's transaction and destination address.

Prefers the network's own self-hosted mempool explorer when it runs one;
otherwise a known public explorer; regtest/custom-signet have no explorer, so
they get no link and the caller shows the bare txid/address.
"""

from __future__ import annotations

from ..config import ArgusConfig
from ..constants import NETWORK_SPECS

# Public explorers for networks without a self-hosted mempool. regtest and
# custom-signet are intentionally absent — there is no public explorer for them.
_PUBLIC_EXPLORER: dict[str, str] = {
    "testnet3": "https://mempool.space/testnet",
    "testnet4": "https://mempool.space/testnet4",
    "signet": "https://mempool.space/signet",
    "mutinynet": "https://mempool.mutinynet.com",
}


def explorer_base(cfg: ArgusConfig, net_key: str, ports: dict[str, int]) -> str | None:
    """Base URL (no trailing slash) for the best explorer for ``net_key``, or
    ``None`` if no explorer is available."""
    net = cfg.networks.get(net_key)
    if net is None:
        return None
    spec = NETWORK_SPECS[net_key]
    if net.mempool_enabled(spec) and "mempool_public" in ports:
        scheme = "https" if (cfg.global_.ssl_enabled and net.mempool.ssl) else "http"
        return f"{scheme}://{cfg.global_.hostname}:{ports['mempool_public']}"
    return _PUBLIC_EXPLORER.get(net_key)


def tx_url(base: str | None, txid: str) -> str | None:
    return f"{base}/tx/{txid}" if base else None


def address_url(base: str | None, address: str) -> str | None:
    return f"{base}/address/{address}" if base else None
