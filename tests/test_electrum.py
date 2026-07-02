"""Electrum submarine-swap provider + its local Nostr relay: config, ports, gen."""

from __future__ import annotations

import pytest
import yaml

from argus.config import ConfigError, ElectrumSwapsCfg
from argus.constants import (
    ELECTRUM_SWAP_KINDS,
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


# A minimal network with Electrum on (it is on by default; Bitcart off to stay
# light). The default indexer + argus1 ring node satisfy Electrum's requirements.
_NET = {"enabled": True, "bitcart": BITCART_OFF}


# --- config / validation -----------------------------------------------------


def test_electrum_on_by_default():
    assert ElectrumSwapsCfg().enabled is True
    cfg = validated(make({"regtest": _NET}))
    net = cfg.networks["regtest"]
    spec = NETWORK_SPECS["regtest"]
    assert net.electrum_enabled(spec) is True
    assert net.electrum_relay_enabled(spec) is True
    # Single wallet: 5 BTC reserve + one 0.5 BTC channel into argus1, 50/50.
    assert net.electrum.reserve_btc == 5.0
    assert net.electrum.channel_btc == 0.5
    assert net.electrum_channel_target(spec) == ("argus1", "lnd", "lnd_data")
    assert net.electrum.fee_millionths == 5000
    assert net.electrum.pow_target == 0
    assert net.electrum.relay.retention_hours == 24


def test_electrum_funding_mode_tracks_mining():
    spec = NETWORK_SPECS["regtest"]
    # regtest mines by default => auto funding.
    cfg = validated(make({"regtest": _NET}))
    assert cfg.networks["regtest"].electrum_funding_mode(spec) == "auto"
    # A non-mined network (public signet) funds externally.
    sspec = NETWORK_SPECS["signet"]
    cfg2 = validated(make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert cfg2.networks["signet"].electrum_funding_mode(sspec) == "external"


def test_electrum_needs_an_indexer():
    # No Fulcrum indexer => Electrum has no Electrum server to talk to => error.
    with pytest.raises(ConfigError) as e:
        validated(make({"regtest": {
            **_NET, "indexers": [], "mempool": {"enabled": False}}}))
    assert "electrum" in str(e.value) and "indexer" in str(e.value)


def test_electrum_target_node_must_exist():
    # Target argus2 but the secondary node is off (ring off too, so the ring's own
    # "needs all three" rule doesn't fire first) => a clear Electrum-specific error.
    with pytest.raises(ConfigError) as e:
        validated(make({"regtest": {
            **_NET,
            "lnd": {"channels": {"enabled": False},
                    "secondary": {"enabled": False},
                    "tertiary": {"enabled": False}},
            "electrum": {"target_node": "argus2"}}}))
    assert "target_node" in str(e.value) and "argus2" in str(e.value)


def test_electrum_funding_auto_requires_mineable():
    # Forcing auto funding on a network Argus can't mine is rejected.
    with pytest.raises(ConfigError) as e:
        validated(make({"signet": {
            **_NET, "electrum": {"funding": "auto"}}}))
    assert "electrum.funding" in str(e.value)


# --- ports -------------------------------------------------------------------


def test_electrum_ports_allocated():
    cfg = validated(make({"regtest": _NET}))
    ports = allocate(cfg)["regtest"]
    base = 30000
    assert ports["electrum_rpc"] == base + 700
    assert ports["nostr_relay_public"] == base + 701
    assert ports["nostr_relay_backend"] == base + 702
    allp = [p for net in allocate(cfg).values() for p in net.values()]
    assert len(allp) == len(set(allp))  # no collisions across the allocation


# --- generation --------------------------------------------------------------


def test_electrum_services_and_volumes(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _NET}))
    c = _compose(out, "regtest")
    svc = c["services"]
    assert {"electrum", "electrum-fund", "nostr-relay", "nostr-relay-sweeper"} <= set(svc)
    # Electrum builds from the shared context and runs the provisioning entrypoint.
    assert svc["electrum"]["build"]["context"] == "../electrum"
    assert svc["electrum"]["entrypoint"] == [
        "/bin/sh", "/scripts/electrum-entrypoint.sh"]
    # It reads the target ring node's data volume read-only (for its pubkey).
    assert "lnd_data:/lnd:ro" in svc["electrum"]["volumes"]
    # JSON-RPC is loopback only (swaps ride nostr, not this port).
    assert all(p.startswith("127.0.0.1:") for p in svc["electrum"]["ports"])
    # The relay uses the upstream image directly (no build context).
    assert svc["nostr-relay"]["image"] == "${NOSTR_RELAY_IMAGE}"
    assert "build" not in svc["nostr-relay"]
    assert all(p.startswith("127.0.0.1:") for p in svc["nostr-relay"]["ports"])
    assert {"electrum_data", "electrum_state", "nostr_relay_data"} <= set(c["volumes"])
    # No strfry-style build context is written (relay is a stock image).
    assert not (out / "nostr-relay").exists()
    # The shared Electrum image context is written once, pinning the version.
    df = (out / "electrum" / "Dockerfile").read_text()
    assert "download.electrum.org/4.7.2/Electrum-4.7.2.tar.gz" in df
    assert "[crypto]" in df and "ELECTRUM_ECC_DONT_COMPILE=1" in df


def test_electrum_relay_config_kind_allowlist(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _NET}))
    conf = (out / "regtest" / "nostr-relay" / "config.toml").read_text()
    # The whole point: accept ONLY the two Electrum swap kinds.
    assert "event_kind_allowlist = [30315, 25582]" in conf
    assert list(ELECTRUM_SWAP_KINDS) == [30315, 25582]
    assert 'address = "0.0.0.0"' in conf and "port = 7777" in conf
    # The retention sweeper deletes events older than 24h (86400s).
    sweep = (out / "regtest" / "nostr-relay" / "retention-sweep.sh").read_text()
    assert "RETENTION_SECONDS=86400" in sweep


def test_electrum_entrypoint_runs_daemon_foreground(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _NET}))
    ep = (out / "regtest" / "electrum" / "electrum-entrypoint.sh").read_text()
    # The daemon must NOT be detached (-d would fork and kill the container); it is
    # backgrounded as a shell job and waited on, so the script stays PID 1.
    assert "$E daemon -d" not in ep
    assert "$E daemon &" in ep and 'wait "$DAEMON_PID"' in ep
    # It enables the swap server plugin + points at the local relay.
    assert "plugins.swapserver.enabled true" in ep
    # It opens one channel with a push to land 50/50.
    assert "--push_amount" in ep


def test_electrum_relays_env_points_at_local_relay(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _NET}))
    c = _compose(out, "regtest")
    env = c["services"]["electrum"]["environment"]
    assert env["NOSTR_RELAYS"] == "ws://nostr-relay:7777"
    # reserve + channel is what the funder must supply.
    assert env["NEED_BTC"] == "5.5"
    assert env["CHANNEL_BTC"] == "0.5" and env["PUSH_BTC"] == "0.25"


def test_electrum_caddy_and_firewall(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": _NET}))
    ports = allocate(validated(make({"regtest": _NET})))["regtest"]
    # Caddy fronts the relay's WebSocket (backend on loopback) as a public site.
    caddy = (out / "shared" / "Caddyfile").read_text()
    assert f"127.0.0.1:{ports['nostr_relay_backend']}" in caddy
    fw = (out / "firewall.sh").read_text()
    assert f"ufw allow {ports['nostr_relay_public']}/tcp" in fw


def test_electrum_external_funding_omits_fund_sidecar(tmp_path):
    # A non-mined network funds externally: no fund sidecar, Electrum waits for coins.
    out, _ = _gen(tmp_path, make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    svc = _compose(out, "signet")["services"]
    assert "electrum" in svc
    assert "electrum-fund" not in svc


def test_electrum_disabled_removes_everything(tmp_path):
    data = make({"regtest": {**_NET, "electrum": {"enabled": False}}})
    out, _ = _gen(tmp_path, data)
    c = _compose(out, "regtest")
    svc = set(c["services"])
    assert not any(
        s in ("electrum", "electrum-fund", "nostr-relay", "nostr-relay-sweeper")
        for s in svc
    )
    assert not (out / "electrum").exists()
    assert not (out / "regtest" / "electrum").exists()
    assert not (out / "regtest" / "nostr-relay").exists()


def test_electrum_relay_disabled_keeps_wallet(tmp_path):
    # Relay off but Electrum on: the wallet stays, no relay/sweeper, and it has no
    # local relay to announce on (operator would set extra_relays).
    data = make({"regtest": {**_NET, "electrum": {"relay": {"enabled": False}}}})
    out, _ = _gen(tmp_path, data)
    c = _compose(out, "regtest")
    svc = set(c["services"])
    assert "electrum" in svc
    assert "nostr-relay" not in svc and "nostr-relay-sweeper" not in svc
    assert c["services"]["electrum"]["environment"]["NOSTR_RELAYS"] == ""
