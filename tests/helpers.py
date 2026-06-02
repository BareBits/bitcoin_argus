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


def make(networks: dict, **global_over) -> dict:
    """Build a raw config dict with a sane default global block."""
    g = {"hostname": "x.com", "ssl_enabled": False}
    g.update(global_over)
    return {"global": g, "networks": networks}


def validated(data: dict) -> ArgusConfig:
    """Validate a config dict exactly as load_config does (sans file/YAML)."""
    cfg = ArgusConfig.model_validate(data)
    cfg._validate_semantics()
    return cfg
