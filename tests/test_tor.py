"""Tor onion layer: routing, generated artifacts, and LND advertisement."""

from __future__ import annotations

import stat

import yaml

from argus.config import load_config
from argus.generate import generate
from argus.ports import allocate
from argus.tor import onion_routes, render_torrc, socks_open_to_containers
from helpers import BITCART_OFF, make


def _cfg(networks: dict, tor: dict | None = None):
    data = make(networks, tor={"enabled": True, **(tor or {})})
    return data


def _gen(tmp_path, data):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    return out, sec


# --- routing ---------------------------------------------------------------


def test_routes_use_clearnet_port_numbers_and_backend_targets(tmp_path):
    data = _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    cfg = load_config(str(cfgp))
    pm = allocate(cfg)
    routes = {r.service: r for r in onion_routes(cfg, pm)}

    # HTTP service: virtual port == clearnet public port; target == backend loopback.
    mp = routes["mempool explorer"]
    assert mp.virtual_port == pm["regtest"]["mempool_public"]
    assert mp.target_port == pm["regtest"]["mempool_web"]
    assert mp.virtual_port != mp.target_port  # genuinely the backend, not Caddy

    # TCP service: virtual and target are the same published port.
    lnd = routes["LND (argus1) P2P"]
    assert lnd.virtual_port == lnd.target_port == pm["regtest"]["lnd_p2p"]

    # The dashboard is install-wide (net_key None) on onion port 80.
    dash = routes["Dashboard"]
    assert dash.net_key is None and dash.virtual_port == 80


def test_expose_toggles_drop_categories(tmp_path):
    data = _cfg(
        {"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
        tor={"expose_lnd_p2p": False, "expose_electrum": False},
    )
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    cfg = load_config(str(cfgp))
    services = {r.service for r in onion_routes(cfg, allocate(cfg))}
    assert not any("LND" in s for s in services)
    assert not any("Electrum" in s for s in services)
    assert "mempool explorer" in services  # web still exposed
    assert not socks_open_to_containers(cfg)  # no LND onion => SOCKS stays local


def test_operator_only_ports_never_exposed(tmp_path):
    data = _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    cfg = load_config(str(cfgp))
    pm = allocate(cfg)
    exposed = {r.virtual_port for r in onion_routes(cfg, pm)}
    # RPC, gRPC, REST, Fulcrum admin, mempool API/DB must not be onion-routed.
    for closed in ("bitcoind_rpc", "lnd_grpc", "lnd_rest", "fulcrum_0_admin",
                   "mempool_api", "mempool_db"):
        assert pm["regtest"][closed] not in exposed


def test_bitcoind_p2p_onion_follows_clearnet_gate(tmp_path):
    data = _cfg({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "bitcoind": {"p2p_public": False},
    }})
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    cfg = load_config(str(cfgp))
    services = {r.service for r in onion_routes(cfg, allocate(cfg))}
    assert "Bitcoin Core P2P" not in services  # closed on clearnet => closed on onion


def test_torrc_unique_virtual_ports(tmp_path):
    # Two networks: their disjoint port blocks must not collide in one HS.
    data = _cfg({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "signet": {"enabled": True, "bitcart": BITCART_OFF, "mempool": {"enabled": True}},
    })
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    cfg = load_config(str(cfgp))
    pm = allocate(cfg)
    vports = [r.virtual_port for r in onion_routes(cfg, pm)]
    assert len(vports) == len(set(vports))

    torrc = render_torrc(cfg, pm)
    assert "HiddenServiceVersion 3" in torrc
    assert torrc.count("HiddenServicePort ") == len(vports)


# --- generated artifacts ---------------------------------------------------


def test_generates_shared_tor_and_keys(tmp_path):
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    tor_dir = out / "shared-tor"
    assert (tor_dir / "torrc").is_file()
    assert (tor_dir / "docker-compose.yml").is_file()
    assert (tor_dir / "entrypoint.sh").is_file()

    compose = yaml.safe_load((tor_dir / "docker-compose.yml").read_text())
    assert compose["services"]["tor"]["network_mode"] == "host"

    host = (tor_dir / "keys" / "hostname").read_text().strip()
    assert host.endswith(".onion")
    # The secret key must be private (0600); the public key/hostname may be world-readable.
    mode = stat.S_IMODE((tor_dir / "keys" / "hs_ed25519_secret_key").stat().st_mode)
    assert mode == 0o600


def test_tor_disabled_emits_nothing(tmp_path):
    out, sec = tmp_path / "gen", tmp_path / "sec"
    cfgp = tmp_path / "c.yaml"
    cfgp.write_text(yaml.safe_dump(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})))
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    assert not (out / "shared-tor").exists()
    assert not (sec / "tor").exists()  # no onion seed created when Tor is off


def test_onion_seed_is_stable_across_regen(tmp_path):
    data = _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    out, sec = _gen(tmp_path, data)
    first = (out / "shared-tor" / "keys" / "hostname").read_text()
    # Regenerate into a fresh output dir but the SAME secrets dir.
    cfgp = tmp_path / "config.yaml"
    generate(str(cfgp), output_dir=tmp_path / "gen2", secrets_dir=sec)
    second = (tmp_path / "gen2" / "shared-tor" / "keys" / "hostname").read_text()
    assert first == second  # onion address is stable (idempotent secret)


# --- LND advertisement + wiring --------------------------------------------


def test_lnd_tor_split_primary_inbound_secondary_dials(tmp_path):
    """Both nodes advertise the onion (reachable inbound over Tor), but only the
    secondary runs in Tor mode (dials .onion peers); the primary is clearnet-only
    outbound."""
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    onion = (out / "shared-tor" / "keys" / "hostname").read_text().strip()
    cfg = load_config(str(tmp_path / "config.yaml"))
    pm = allocate(cfg)["regtest"]
    conf1 = (out / "regtest" / "lnd" / "lnd.conf").read_text()
    conf2 = (out / "regtest" / "lnd2" / "lnd.conf").read_text()

    # Both nodes advertise clearnet + onion externalips (reachable inbound).
    assert f"externalip=x.com:{pm['lnd_p2p']}" in conf1
    assert f"externalip={onion}:{pm['lnd_p2p']}" in conf1
    assert f"externalip={onion}:{pm['lnd2_p2p']}" in conf2

    # Primary: NOT in Tor mode (clearnet-only outbound).
    assert "[Tor]" not in conf1 and "tor.active=true" not in conf1
    # Secondary: Tor mode (can dial .onion peers); advertises the shared onion but
    # mints none (no tor.v3).
    assert "[Tor]" in conf2 and "tor.active=true" in conf2
    assert "tor.skip-proxy-for-clearnet-targets=true" in conf2
    assert "tor.v3" not in conf2

    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    assert "extra_hosts" not in compose["services"]["lnd"]
    assert "argus-tor-host:host-gateway" in compose["services"]["lnd2"]["extra_hosts"]
    assert socks_open_to_containers(cfg)  # the secondary dials over Tor


def test_tor_mode_node_waits_for_socks_proxy(tmp_path):
    """The Tor-mode secondary gets a one-shot sidecar that blocks its start until
    the shared-tor SOCKS proxy is reachable, so the per-net stack can be brought up
    before/with shared-tor without the node crash-looping on config validation."""
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    services = compose["services"]

    # The primary (clearnet-only) has no wait sidecar; the secondary does.
    assert "lnd-tor-wait" not in services
    wait = services["lnd2-tor-wait"]
    assert "argus-tor-host:host-gateway" in wait["extra_hosts"]
    cmd = " ".join(wait["entrypoint"])
    assert "nc -z" in cmd and "argus-tor-host 9050" in cmd
    assert "$" not in cmd  # Compose interpolation must not mangle it
    # lnd2 won't start until the wait completes.
    assert services["lnd2"]["depends_on"]["lnd2-tor-wait"] == {
        "condition": "service_completed_successfully"
    }


def test_no_tor_wait_sidecar_when_tor_off(tmp_path):
    """With Tor off, the secondary isn't in Tor mode, so there's no wait sidecar."""
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})))
    out = tmp_path / "gen"
    generate(str(cfgp), output_dir=out, secrets_dir=tmp_path / "sec")
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    assert "lnd2-tor-wait" not in compose["services"]
    assert "lnd2" in compose["services"]  # the node itself still exists


def test_channel_sidecar_connects_by_resolved_ip(tmp_path):
    """The auto-channel sidecar dials siblings by resolved IP so the Tor-mode
    secondary doesn't route a private hostname through Tor (which would fail)."""
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    ch = (out / "regtest" / "lnd_setup" / "channels.sh").read_text()
    assert "getent hosts" in ch
    assert "$(addr lnd2)" in ch and "$(addr lnd)" in ch


def test_no_onion_externalip_when_lnd_not_exposed(tmp_path):
    out, _ = _gen(
        tmp_path,
        _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
             tor={"expose_lnd_p2p": False}),
    )
    conf = (out / "regtest" / "lnd" / "lnd.conf").read_text()
    assert ".onion" not in conf
    assert "[Tor]" not in conf


def test_firewall_allows_socks_only_from_docker_range(tmp_path):
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    fw = (out / "firewall.sh").read_text()
    assert "from 172.16.0.0/12 to any port 9050" in fw
    # The onion itself opens no inbound port.
    assert "9050/tcp" not in fw  # not a blanket allow, only the scoped 'from' rule


def test_web_dashboard_receives_onion_env(tmp_path):
    out, _ = _gen(tmp_path, _cfg({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    onion = (out / "shared-tor" / "keys" / "hostname").read_text().strip()
    compose = yaml.safe_load((out / "web" / "docker-compose.yml").read_text())
    assert compose["services"]["web"]["environment"]["ONION_HOSTNAME"] == onion
