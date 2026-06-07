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


def test_core_services_present(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    c = _compose(out, "regtest")
    assert {"fedimintd", "gatewayd", "fedimint-setup", "fedimint-gateways"} <= set(c["services"])


def test_float_funded_from_gateway_onchain_all_networks(tmp_path):
    # Funding pegs the ecash float in from each gateway's own (ring-funded) LND
    # on-chain wallet via pegin-from-onchain, so it's identical on mined (regtest)
    # and non-mined (signet) networks -- no separate bitcoind fund sidecar, no
    # auto/external split.
    for net in ("regtest", "signet"):
        d = tmp_path / net
        d.mkdir()
        out, _ = _gen(d, make({net: {"enabled": True, "bitcart": BITCART_OFF}}))
        c = _compose(out, net)
        assert "fedimint-fund" not in c["services"]
        gw = c["services"]["fedimint-gateways"]["environment"]
        assert gw["FLOAT_MSAT"] == "50000000000"  # 0.5 BTC default, in msats
        script = (out / net / "fedimint" / "gateways.sh").read_text()
        # float from the gateway's own on-chain wallet: get a peg-in address, send
        # to it, then recheck to claim (deposits are not auto-claimed).
        assert "onchain send" in script and "pegin-recheck" in script
        assert "/ui/wallet/create" in script  # creates the gateway wallet/mnemonic


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
    # gatewayd requires a Bitcoin backend even in LND mode.
    assert env["FM_BITCOIND_URL"] == "http://bitcoind:18443"
    assert "lnd_data:/lnd:ro" in gw["volumes"]
    # bcrypt hash is derived at runtime by a mounted entry script (no plaintext
    # leak, and no Compose $-interpolation of the shell vars).
    assert gw["entrypoint"] == ["/bin/sh", "/scripts/gateway-entry.sh"]
    assert any("gateway-entry.sh" in v for v in gw["volumes"])
    entry = (out / "regtest" / "fedimint" / "gateway-entry.sh").read_text()
    assert "create-password-hash" in entry and "exec gatewayd lnd" in entry


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


def test_dashboard_lists_fedimint_with_backing_lnd():
    from argus.web.inventory import build_sections

    cfg = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "fedimint": {"guardians": 3}}}))
    rt = next(s for s in build_sections(cfg, allocate(cfg), {"usage": {}, "host": {}})
              if s.key == "regtest")
    names = [s.name for s in rt.services]
    assert "Fedimint guardian 1" in names
    # The gateway rows name the LND node backing each (gateway i -> argus_i).
    for nm in ("Fedimint gateway (argus1)", "Fedimint gateway (argus2)",
               "Fedimint gateway (argus3)"):
        assert nm in names
    gw = next(s for s in rt.services if s.name == "Fedimint gateway (argus1)")
    assert gw.audience == "Visitor" and gw.links  # public API + a Gateway UI link


def test_dashboard_surfaces_invite_code():
    from argus.web.inventory import build_sections

    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    # The metrics collector supplies the live invite code per network.
    metrics = {"usage": {}, "host": {}, "fedimint": {"regtest": "fed11invitexyz"}}
    rt = next(s for s in build_sections(cfg, allocate(cfg), metrics) if s.key == "regtest")
    guardian = next(s for s in rt.services if s.name == "Fedimint guardian")
    assert guardian.invite_code == "fed11invitexyz"
    # QR renders when the optional qrcode lib is present; else degrades to text-only.
    try:
        import qrcode  # noqa: F401

        assert guardian.invite_qr and "<svg" in guardian.invite_qr
    except ImportError:
        assert guardian.invite_qr is None
    # The code lives on the leader guardian only, not the gateway rows.
    gw = next(s for s in rt.services if s.name.startswith("Fedimint gateway"))
    assert gw.invite_code is None


def test_dashboard_single_guardian_unnumbered():
    from argus.web.inventory import build_sections

    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    rt = next(s for s in build_sections(cfg, allocate(cfg), {"usage": {}, "host": {}})
              if s.key == "regtest")
    names = [s.name for s in rt.services]
    assert "Fedimint guardian" in names  # no number when there is just one
    assert "Fedimint gateway (argus1)" in names


def test_secrets_persisted(tmp_path):
    _, sec = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    env = (sec / "regtest" / "secrets.env").read_text()
    assert "FEDIMINT_GUARDIAN_PASSWORD=" in env
    assert "FEDIMINT_GATEWAY_PASSWORD=" in env


def test_credentials_surface_fedimint_logins(tmp_path):
    from argus.credentials import build_credentials, format_credentials

    out, sec = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    creds = build_credentials(
        validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})),
        allocate(validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))),
        sec,
    )
    gw = next(c for c in creds if c.component == "Fedimint gateway (argus1) UI")
    guardian = next(c for c in creds if "guardian" in c.component)
    # Passwords come from the persisted secrets (the real gateway-cli/UI logins).
    fed_gw = (sec / "regtest" / "secrets.env").read_text()
    assert gw.password and gw.password in fed_gw
    assert guardian.password and gw.password != guardian.password
    # And they render in the printed/file output.
    text = format_credentials(creds)
    assert "Fedimint gateway (argus1) UI" in text and "Password:" in text
