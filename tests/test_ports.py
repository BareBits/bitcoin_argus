"""Deterministic port allocation + collision detection."""

from __future__ import annotations

import pytest

from argus.ports import PortAllocationError, allocate, block_base
from helpers import BITCART_OFF, BITCART_OK, make, validated


def test_block_base_ordering():
    assert block_base("regtest") == 30000
    assert block_base("signet") == 33000
    assert block_base("custom-signet") == 35000


def test_default_offsets():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    p = allocate(cfg)["regtest"]
    assert p["bitcoind_rpc"] == 30001
    assert p["lnd_p2p"] == 30010
    assert p["lnd2_p2p"] == 30013
    assert p["lnd2_rest"] == 30014
    assert p["lnd2_grpc"] == 30015
    assert p["fulcrum_0_electrum_tcp"] == 30040
    assert p["cashu_public"] == 30100
    assert p["cashu_wallet_public"] == 30101
    assert p["cashu_wallet_backend"] == 30111
    assert p["mempool_public"] == 30300


def test_two_networks_disjoint_no_collision():
    cfg = validated(make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "signet": {"enabled": True, "bitcart": BITCART_OFF},
    }))
    pm = allocate(cfg)
    assert pm["signet"]["lnd_p2p"] == 33010
    all_ports = [v for net in pm.values() for v in net.values()]
    assert len(all_ports) == len(set(all_ports))  # globally unique


def test_collision_via_override_raises():
    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "ports": {"lnd_rest": 30010},  # == lnd_p2p
    }}))
    with pytest.raises(PortAllocationError, match="claimed by both"):
        allocate(cfg)


def test_unknown_override_raises():
    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "ports": {"nope": 40000}}}))
    with pytest.raises(PortAllocationError, match="unknown port"):
        allocate(cfg)


def test_multiple_fulcrum_instances_distinct():
    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "indexers": [{"name": "fulcrum-1"}, {"name": "fulcrum-2"}]}}))
    p = allocate(cfg)["regtest"]
    # stride of 4 between instances
    assert p["fulcrum_0_electrum_tcp"] == 30040
    assert p["fulcrum_1_electrum_tcp"] == 30044
    assert p["fulcrum_0_electrum_ssl"] == 30041


def test_btclnd_bases_only_when_bitcart_enabled():
    on = allocate(validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OK}})))
    assert on["regtest"]["bitcart_btclnd_p2p_base"] == 30400
    assert on["regtest"]["bitcart_btclnd_grpc_base"] == 30450

    off = allocate(validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})))
    assert "bitcart_btclnd_p2p_base" not in off["regtest"]


def test_bitcart_port_override():
    cfg = validated(make({"regtest": {
        "enabled": True,
        "bitcart": {**BITCART_OK, "ports": {"store": 31999}}}}))
    assert allocate(cfg)["regtest"]["bitcart_store_public"] == 31999
