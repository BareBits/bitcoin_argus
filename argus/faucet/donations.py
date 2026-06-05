"""The "donate / recycle" reminder the faucet shows for its network: the same
on-chain donation address and donate Lightning Address the main dashboard shows.

The on-chain address is read from the per-network donations sidecar via the
read-only docker socket proxy (the same ``get_archive`` trick the dashboard's
metrics use); the Lightning Address is computed from config. Both are best-effort
— a missing address just renders as unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config import ArgusConfig


@dataclass(frozen=True)
class DonationReminder:
    address: str | None
    lightning_address: str | None


def donation_reminder(cfg: ArgusConfig, net_key: str) -> DonationReminder:
    """The donation address + donate Lightning Address for ``net_key``."""
    lnurl = cfg.web.lnurl
    ln_addr: str | None = None
    # Mirror the dashboard: only advertise the clearnet Lightning Address when SSL
    # is on (wallets need https to fetch the LNURL endpoint).
    if cfg.web.enabled and lnurl.enabled and cfg.global_.ssl_enabled:
        enabled_keys = [k for k, _ in cfg.enabled_networks()]
        default_net = lnurl.default_network or (
            enabled_keys[0] if enabled_keys else None
        )
        local = "donate" if net_key == default_net else f"donate-{net_key}"
        ln_addr = f"{local}@{cfg.global_.hostname}"
    return DonationReminder(
        address=_read_onchain_address(net_key), lightning_address=ln_addr
    )


def _read_onchain_address(net_key: str) -> str | None:
    """The network's public donation address, via the read-only socket proxy."""
    try:
        import docker

        from ..web import metrics as web_metrics

        client = docker.from_env()  # honours DOCKER_HOST (the socket proxy)
        info = web_metrics._donation_info(client, net_key)
        return (info or {}).get("address")
    except Exception:
        return None
