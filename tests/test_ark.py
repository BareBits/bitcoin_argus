"""Ark ASP (captaind + Core Lightning bridge): config, ports, and generation."""

from __future__ import annotations

import pytest
import yaml

from argus.config import ConfigError
from argus.constants import (
    ARK_NETWORK_KEY,
    ARK_SUPPORTED_CHAINS,
    NETWORK_SPECS,
)
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


# Ark needs the LND ring's nodes present; the default network has them on, so most
# tests just disable Bitcart to stay light. Disabling the ring (channels) but
# keeping node 1 is still a valid Ark target (argus1 always exists).
_ARK_NET = {"enabled": True, "bitcart": BITCART_OFF}


# --- config / validation -----------------------------------------------------


def test_ark_on_by_default():
    cfg = validated(make({"regtest": _ARK_NET}))
    net = cfg.networks["regtest"]
    spec = NETWORK_SPECS["regtest"]
    assert net.ark.enabled is True
    assert net.ark_enabled(spec) is True
    # Default channel target is argus1 (the node Cashu/Fedimint also use).
    assert net.ark_channel_target(spec) == ("argus1", "lnd", "lnd_data")
    assert net.ark.channel.channel_btc == 0.1


def test_ark_supported_on_every_current_chain():
    # Every chain Argus ships maps to a captaind/CLN network, so Ark is never
    # auto-disabled today (the guard exists only for a future, unknown chain).
    chains = {spec.chain for spec in NETWORK_SPECS.values()}
    assert chains <= ARK_SUPPORTED_CHAINS
    for spec in NETWORK_SPECS.values():
        cfg = validated(make({"regtest": _ARK_NET}))
        assert cfg.networks["regtest"].ark_supported(spec) is True


def test_ark_disabled_removes_everything(tmp_path):
    data = make({"regtest": {**_ARK_NET, "ark": {"enabled": False}}})
    out, _ = _gen(tmp_path, data)
    services = set(_compose(out, "regtest")["services"])
    assert not any(
        s in ("cln", "captaind", "ark-setup", "ark-channel") for s in services
    )
    # No ark services, build context, or per-network config dir are written.
    # (The ark_* host ports stay reserved in the fixed offset map for collision
    # checks, exactly like lnd2/lnd3's ports when those nodes are off.)
    assert not (out / "ark-cln").exists()
    assert not (out / "regtest" / "ark").exists()


def test_ark_target_node_must_exist():
    # Target argus2 but the secondary node is off (ring also off so the ring's own
    # "needs all three" rule doesn't fire first) => a clear Ark-specific error.
    with pytest.raises(ConfigError) as e:
        validated(make({"regtest": {
            **_ARK_NET,
            "lnd": {"channels": {"enabled": False},
                    "secondary": {"enabled": False},
                    "tertiary": {"enabled": False}},
            "ark": {"channel": {"target_node": "argus2"}}}}))
    assert "target_node" in str(e.value) and "argus2" in str(e.value)


def test_ark_target_argus2_ok_when_secondary_on():
    # With the ring on (default), argus2 exists, so targeting it validates.
    cfg = validated(make({"regtest": {
        **_ARK_NET, "ark": {"channel": {"target_node": "argus2"}}}}))
    spec = NETWORK_SPECS["regtest"]
    assert cfg.networks["regtest"].ark_channel_target(spec) == (
        "argus2", "lnd2", "lnd2_data")


def test_ark_alias_too_long_rejected():
    with pytest.raises(Exception):
        validated(make({"regtest": {
            **_ARK_NET, "ark": {"cln_alias": "x" * 33}}}))


# --- ports -------------------------------------------------------------------


def test_ark_ports_allocated():
    cfg = validated(make({"regtest": _ARK_NET}))
    ports = allocate(cfg)["regtest"]
    base = 30000  # regtest block base
    assert ports["ark_captaind_public"] == base + 600
    assert ports["ark_captaind"] == base + 601
    assert ports["ark_captaind_admin"] == base + 602
    assert ports["ark_cln_p2p"] == base + 603
    # No collisions across the whole allocation.
    allp = [p for net in allocate(cfg).values() for p in net.values()]
    assert len(allp) == len(set(allp))


# --- generation --------------------------------------------------------------


def test_ark_services_and_volumes(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _ARK_NET}))
    c = _compose(out, "regtest")
    svcs = c["services"]
    assert {"cln", "captaind", "ark-setup", "ark-channel"} <= set(svcs)
    # CLN builds from the shared context, keeps hostname "cln" (TLS SAN), and
    # publishes only its P2P port.
    assert svcs["cln"]["build"]["context"] == "../ark-cln"
    assert svcs["cln"]["hostname"] == "cln"
    assert any(":9735" in p and p.startswith("0.0.0.0:") for p in svcs["cln"]["ports"])
    # captaind uses the configured image, reads CLN certs read-only, gRPC loopback.
    assert svcs["captaind"]["image"] == "${ARK_CAPTAIND_IMAGE}"
    assert "ark_cln_data:/data/cln:ro" in svcs["captaind"]["volumes"]
    assert all(p.startswith("127.0.0.1:") for p in svcs["captaind"]["ports"])
    # The channel sidecar mounts the target ring node's volume read-only.
    assert "lnd_data:/lnd:ro" in svcs["ark-channel"]["volumes"]
    # Volumes declared.
    assert {"ark_cln_data", "ark_captaind_data", "ark_captaind_pg", "ark_state"} <= set(
        c["volumes"]
    )
    # Shared CLN build context written once with the hold plugin pinned.
    df = (out / "ark-cln" / "Dockerfile").read_text()
    assert "BoltzExchange/hold.git" in df and "v0.3.3" in df
    assert (out / "ark-cln" / "cln_start.sh").exists()


def test_ark_captaind_toml_regtest(tmp_path):
    out, sec = _gen(tmp_path, make({"regtest": _ARK_NET}))
    toml = (out / "regtest" / "ark" / "captaind.toml").read_text()
    assert 'network = "regtest"' in toml
    # bitcoind points at the network's node with the generated RPC creds.
    assert 'url = "bitcoind:18443"' in toml
    assert 'rpc_user = "argus_regtest"' in toml
    secrets = dict(
        line.split("=", 1)
        for line in (sec / "regtest" / "secrets.env").read_text().splitlines()
        if "=" in line
    )
    assert f'rpc_pass = "{secrets["RPC_PASSWORD"]}"' in toml
    # CLN gRPC + hold cert paths point at the regtest CLN dir.
    assert 'server_cert_path = "/data/cln/regtest/ca.pem"' in toml
    assert 'hold_invoice.uri = "https://cln:9988"' in toml
    assert "[[cln_array]]" in toml


def test_ark_captaind_toml_signet_network_mapping(tmp_path):
    # A custom signet reports chain="signet"; captaind + CLN both use "signet",
    # and the cert paths must live under /data/cln/signet/.
    out, _ = _gen(tmp_path, make({"custom-signet-short": _ARK_NET}))
    toml = (out / "custom-signet-short" / "ark" / "captaind.toml").read_text()
    assert ARK_NETWORK_KEY["signet"] == "signet"
    assert 'network = "signet"' in toml
    assert 'client_cert_path = "/data/cln/signet/client.pem"' in toml


def test_ark_caddy_and_firewall(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _ARK_NET}))
    ports = allocate(validated(make({"regtest": _ARK_NET})))["regtest"]
    caddy = (out / "shared" / "Caddyfile").read_text()
    # captaind's gRPC is fronted over h2c on its public port.
    assert f"h2c://127.0.0.1:{ports['ark_captaind']}" in caddy
    fw = (out / "firewall.sh").read_text()
    assert f"ufw allow {ports['ark_captaind_public']}/tcp" in fw
    assert f"ufw allow {ports['ark_cln_p2p']}/tcp" in fw


def test_ark_credentials_info_row(tmp_path):
    from pathlib import Path

    from argus.credentials import build_credentials

    data = make({"regtest": _ARK_NET})
    _, sec = _gen(tmp_path, data)
    cfg = validated(data)
    rows = [
        c
        for c in build_credentials(cfg, allocate(cfg), Path(sec), only="regtest")
        if "Ark" in c.component
    ]
    assert len(rows) == 1
    row = rows[0]
    assert row.info_only is True and row.password is None
    assert row.login_url and row.login_url.endswith(":30600/")
    assert any("ark-setup" in n for n in row.notes)
