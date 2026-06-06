"""Shared helpers for the test suite."""

from __future__ import annotations

from argus.config import ArgusConfig

# A bitcart block that satisfies validation (admin email + cashout for liquidity).
BITCART_OK = {
    "admin_email": "admin@example.com",
    "liquidity": {"cashout_lightning_address": "pay@example.com"},
}
# Disable bitcart to keep a network minimal in tests that don't exercise it.
BITCART_OFF = {"enabled": False}

# The storefront services (CashuPayServer + WooCommerce) default ON in production
# but are heavy and need an admin email; tests keep networks minimal by defaulting
# them OFF (mirroring BITCART_OFF) unless a test sets them explicitly.
CASHUPAYSERVER_OFF = {"enabled": False}
WOOCOMMERCE_OFF = {"enabled": False}


def make(networks: dict, **global_over) -> dict:
    """Build a raw config dict with a sane default global block.

    Storefront services default OFF here so the many minimal-network tests stay
    minimal; a test that exercises them passes an explicit ``cashupayserver`` /
    ``woocommerce`` block, which is left untouched.
    """
    g = {"hostname": "x.com", "ssl_enabled": False}
    g.update(global_over)
    for net in networks.values():
        if isinstance(net, dict):
            net.setdefault("cashupayserver", {"enabled": False})
            net.setdefault("woocommerce", {"enabled": False})
    return {"global": g, "networks": networks}


def validated(data: dict) -> ArgusConfig:
    """Validate a config dict exactly as load_config does (sans file/YAML)."""
    cfg = ArgusConfig.model_validate(data)
    cfg._validate_semantics()
    return cfg
