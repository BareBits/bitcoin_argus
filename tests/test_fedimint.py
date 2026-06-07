"""Fedimint federation + Lightning gateway: config, ports, and generation."""

from __future__ import annotations

import dataclasses

import pytest
import yaml

from argus.config import ConfigError
from argus.constants import FEDIMINT_SUPPORTED_CHAINS, NETWORK_SPECS
from argus.generate import generate
from argus.ports import allocate
from helpers import BITCART_OFF, make, validated


def _gen(tmp_path, data):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    return out, sec


def _compose(out, net):
    return yaml.safe_load((out / net / "docker-compose.yml").read_text())


# --- config / validation -----------------------------------------------------


def test_fedimint_on_by_default():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    net = cfg.networks["regtest"]
    spec = NETWORK_SPECS["regtest"]
    assert net.fedimint.enabled is True
    assert net.fedimint_enabled(spec) is True
    assert net.fedimint_guardian_count(spec) == 1  # default single guardian


def test_fedimint_disabled_removes_everything(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"enabled": False}}}))
    services = set(_compose(out, "regtest")["services"])
    assert not any(s.startswith(("fedimintd", "gatewayd", "fedimint-")) for s in services)
    # And no fedimint ports are allocated.
    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"enabled": False}}}))
    assert not any(k.startswith(("fedimintd_", "gatewayd_")) for k in allocate(cfg)["regtest"])


def test_guardians_range_rejected():
    # Field-level bound => pydantic ValidationError (not the semantic ConfigError).
    for bad in (0, 4):
        with pytest.raises(Exception):
            validated(make({"regtest": {
                "enabled": True, "bitcart": BITCART_OFF,
                "fedimint": {"guardians": bad}}}))


def test_guardians_capped_by_ring_nodes():
    # Only node 1 present (ring + extra nodes off) => at most 1 guardian.
    with pytest.raises(ConfigError) as e:
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF,
            "lnd": {"channels": {"enabled": False},
                    "secondary": {"enabled": False},
                    "tertiary": {"enabled": False}},
            "fedimint": {"guardians": 2}}}))
    assert "available ring LND node" in str(e.value)


def test_three_guardians_ok_with_full_ring(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"guardians": 3}}}))
    services = set(_compose(out, "regtest")["services"])
    assert {"fedimintd", "fedimintd2", "fedimintd3"} <= services
    assert {"gatewayd", "gatewayd2", "gatewayd3"} <= services


def test_federation_name_validation():
    # Field-level validator => pydantic ValidationError (not the semantic ConfigError).
    with pytest.raises(Exception):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF,
            "fedimint": {"federation_name": "bad\nname"}}}))


def test_capability_guard_unsupported_chain():
    # Every real chain is supported; a hypothetical chain is not (the guard hook).
    assert set(FEDIMINT_SUPPORTED_CHAINS) == {"regtest", "test", "testnet4", "signet"}
    for spec in NETWORK_SPECS.values():
        assert spec.chain in FEDIMINT_SUPPORTED_CHAINS
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    bogus = dataclasses.replace(NETWORK_SPECS["regtest"], chain="future-chain")
    net = cfg.networks["regtest"]
    assert net.fedimint_supported(bogus) is False
    assert net.fedimint_enabled(bogus) is False  # auto-disabled, not an error


# --- ports -------------------------------------------------------------------


def test_fedimint_port_offsets():
    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"guardians": 3}}}))
    p = allocate(cfg)["regtest"]
    # Guardian 0 strided sub-range, gateway 0 sub-range.
    assert p["fedimintd_0_api_public"] == 30500
    assert p["fedimintd_0_api"] == 30501
    assert p["fedimintd_0_ui"] == 30502
    assert p["fedimintd_1_api_public"] == 30504
    assert p["fedimintd_2_api_public"] == 30508
    assert p["gatewayd_0_api"] == 30520
    assert p["gatewayd_0_api_public"] == 30521
    assert p["gatewayd_2_api"] == 30524
    # Globally unique across the block.
    vals = list(p.values())
    assert len(vals) == len(set(vals))


# --- generation --------------------------------------------------------------


def test_auto_funded_network_has_fund_sidecar(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    c = _compose(out, "regtest")
    assert {"fedimintd", "gatewayd", "fedimint-setup", "fedimint-gateways",
            "fedimint-fund"} <= set(c["services"])
    fund = c["services"]["fedimint-fund"]["environment"]
    assert fund["CAN_MINE"] == "1" and fund["FUNDING_WALLET"] == "miner"


def test_external_funded_network_skips_fund_sidecar(tmp_path):
    # Public signet isn't mined by Argus => external funding => no fund sidecar,
    # but the federation + gateways are still deployed.
    out, _ = _gen(tmp_path, make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    services = set(_compose(out, "signet")["services"])
    assert {"fedimintd", "gatewayd", "fedimint-setup", "fedimint-gateways"} <= services
    assert "fedimint-fund" not in services


def test_guardian_wiring(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    g = _compose(out, "regtest")["services"]["fedimintd"]["environment"]
    assert g["FM_BITCOIN_NETWORK"] == "regtest"
    assert g["FM_BITCOIND_URL"] == "http://bitcoind:18443"
    assert g["FM_BITCOIND_USERNAME"] == "${RPC_USER}"
    # ssl off in tests => ws:// advertised API in the invite code.
    assert g["FM_API_URL"] == "ws://x.com:30500"


def test_gateway_wiring(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    gw = _compose(out, "regtest")["services"]["gatewayd"]
    env = gw["environment"]
    assert env["FM_GATEWAY_NETWORK"] == "regtest"
    assert env["FM_GATEWAY_LIGHTNING_MODULE_MODE"] == "LNv1"
    assert env["FM_LND_RPC_ADDR"] == "https://lnd:10009"
    assert env["FM_LND_MACAROON"] == "/lnd/data/chain/bitcoin/regtest/admin.macaroon"
    assert "lnd_data:/lnd:ro" in gw["volumes"]
    # bcrypt hash is derived at runtime by the entrypoint (no plaintext leak).
    assert any("create-password-hash" in part for part in gw["entrypoint"])


def test_gateway_paired_with_distinct_ring_nodes(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"guardians": 3}}}))
    svcs = _compose(out, "regtest")["services"]
    assert svcs["gatewayd"]["environment"]["FM_LND_RPC_ADDR"] == "https://lnd:10009"
    assert svcs["gatewayd2"]["environment"]["FM_LND_RPC_ADDR"] == "https://lnd2:10009"
    assert svcs["gatewayd3"]["environment"]["FM_LND_RPC_ADDR"] == "https://lnd3:10009"


def test_setup_endpoints_in_peer_order(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"guardians": 3}}}))
    env = _compose(out, "regtest")["services"]["fedimint-setup"]["environment"]
    assert env["ENDPOINTS"] == "ws://fedimintd:8174 ws://fedimintd2:8174 ws://fedimintd3:8174"
    assert env["GUARDIANS"] == "3"
    assert env["FEDERATION_NAME"] == "Argus regtest"


def test_caddy_fronts_guardian_and_gateway(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    caddy = (out / "shared" / "Caddyfile").read_text()
    # Public listeners for the guardian + gateway APIs, proxying to their backends.
    assert "x.com:30500 {" in caddy and "127.0.0.1:30501" in caddy
    assert "x.com:30521 {" in caddy and "127.0.0.1:30520" in caddy


def test_firewall_opens_public_apis(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    fw = (out / "firewall.sh").read_text()
    assert "ufw allow 30500/tcp" in fw and "guardian0 api" in fw
    assert "ufw allow 30521/tcp" in fw and "gateway0 api" in fw


def test_secrets_persisted(tmp_path):
    _, sec = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    env = (sec / "regtest" / "secrets.env").read_text()
    assert "FEDIMINT_GUARDIAN_PASSWORD=" in env
    assert "FEDIMINT_GATEWAY_PASSWORD=" in env
