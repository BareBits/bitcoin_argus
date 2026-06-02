"""Resource resolution precedence: explicit > profile (net > global) > default."""

from __future__ import annotations

import pytest

from argus.resources import resolve
from helpers import BITCART_OFF, make, validated


def _net():
    return {"enabled": True, "bitcart": BITCART_OFF}


def test_default_profile_is_medium():
    cfg = validated(make({"regtest": _net()}))
    r = resolve(cfg, "regtest")
    assert r.bitcoind_dbcache == 300
    assert r.fulcrum_db_mem == 600
    assert r.mempool_mariadb_buffer_mb == 128
    assert r.log_rotation is True
    assert (r.log_max_size, r.log_max_file) == ("10m", 3)


def test_global_profile_low():
    cfg = validated(make({"regtest": _net()}, resources={"profile": "low"}))
    r = resolve(cfg, "regtest")
    assert r.bitcoind_dbcache == 100
    assert r.fulcrum_db_mem == 400


def test_per_network_profile_overrides_global():
    cfg = validated(make(
        {"regtest": _net(),
         "signet": {**_net(), "resources": {"profile": "high"}}},
        resources={"profile": "low"}))
    assert resolve(cfg, "regtest").bitcoind_dbcache == 100   # global low
    assert resolve(cfg, "signet").bitcoind_dbcache == 1000   # per-net high


def test_explicit_knob_overrides_profile():
    cfg = validated(make({"regtest": {
        **_net(), "resources": {"profile": "high", "bitcoind_dbcache": 42}}}))
    r = resolve(cfg, "regtest")
    assert r.bitcoind_dbcache == 42      # explicit wins
    assert r.bitcoind_maxmempool == 300  # from high profile


def test_global_knob_applies_when_net_unset():
    cfg = validated(make({"regtest": _net()},
                         resources={"profile": "low", "fulcrum_db_mem": 1234}))
    assert resolve(cfg, "regtest").fulcrum_db_mem == 1234


def test_log_rotation_toggle_and_overrides():
    cfg = validated(make({"regtest": {**_net(), "resources": {
        "log_rotation": False, "log_max_size": "5m", "log_max_file": 2}}}))
    r = resolve(cfg, "regtest")
    assert r.log_rotation is False
    assert (r.log_max_size, r.log_max_file) == ("5m", 2)


def test_invalid_profile_rejected():
    with pytest.raises(Exception):
        validated(make({"regtest": {**_net(), "resources": {"profile": "ultra"}}}))
