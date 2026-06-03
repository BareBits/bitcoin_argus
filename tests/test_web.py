"""Tests for the dashboard: config schema, metrics attribution, cache TTL,
inventory view model, generation artifacts, Caddy/firewall wiring, and routes."""

from __future__ import annotations

import sys
import types

import pytest
import yaml

from argus.config import load_config
from argus.firewall import render_firewall
from argus.ports import allocate
from argus.shared import render_caddyfile, web_public_port
from argus.web import cache, metrics
from argus.web.inventory import build_sections
from argus.web_gen import generate_web

from helpers import BITCART_OFF, make, validated


# --- config schema ----------------------------------------------------------


def test_web_defaults_present():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert cfg.web.enabled is True
    assert cfg.web.default_theme == "hacker"
    assert set(cfg.web.themes) == {"hacker", "game", "bootstrap"}


def test_web_default_theme_must_exist():
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {"default_theme": "nope", "themes": {"hacker": "themes/hacker.css"}}
    with pytest.raises(Exception):
        validated(data)


def test_web_port_range_rejected():
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {"port": 99999}
    with pytest.raises(Exception):
        validated(data)


# --- metrics name attribution ----------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("argus-regtest-bitcoind", ("regtest", "bitcoind")),
        ("argus-regtest-miner", ("regtest", "miner")),
        ("argus-signet-lnd", ("signet", "lnd")),
        ("argus-signet-fulcrum-1", ("signet", "fulcrum")),
        ("argus-regtest-mempool-db", ("regtest", "mempool")),
        ("argus-regtest-mempool-web", ("regtest", "mempool")),
        ("argus-regtest-cashu", ("regtest", "cashu")),
        ("argus-custom-signet-bitcoind", ("custom-signet", "bitcoind")),
        ("argus-bitcart-regtest-btclnd-1", ("regtest", "bitcart")),
        ("argus-regtest_bitcoind_data", ("regtest", "bitcoind")),  # volume form
        ("argus-custom-signet_fulcrum_1_data", ("custom-signet", "fulcrum")),
        ("some-unrelated-container", (None, None)),
    ],
)
def test_classify(name, expected):
    assert metrics.classify(name) == expected


def test_container_memory_subtracts_cache():
    stats = {"memory_stats": {"usage": 1000, "stats": {"inactive_file": 400}}}
    assert metrics._container_memory_bytes(stats) == 600


def test_collect_with_fake_docker(monkeypatch):
    class FakeContainer:
        def __init__(self, name, mem):
            self.name = name
            self._mem = mem

        def stats(self, stream=False):
            return {"memory_stats": {"usage": self._mem, "stats": {"inactive_file": 0}}}

    class FakeClient:
        def containers(self):  # not used; replaced below
            ...

        def df(self):
            return {
                "Volumes": [
                    {"Name": "argus-regtest_bitcoind_data", "UsageData": {"Size": 5000}},
                    {"Name": "argus-signet_cashu_data", "UsageData": {"Size": 200}},
                ],
                "Containers": [],
            }

    client = FakeClient()
    client.containers = types.SimpleNamespace(
        list=lambda: [
            FakeContainer("argus-regtest-bitcoind", 300),
            FakeContainer("argus-bitcart-signet-btclnd-1", 150),
            FakeContainer("unrelated", 999),
        ]
    )
    fake_docker = types.ModuleType("docker")
    fake_docker.from_env = lambda: client
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    result = metrics.collect().as_dict()
    usage = result["usage"]
    assert usage["regtest"]["bitcoind"]["ram"] == 300
    assert usage["regtest"]["bitcoind"]["disk"] == 5000
    assert usage["signet"]["bitcart"]["ram"] == 150
    assert "unrelated" not in usage
    assert "host" in result


# --- cache TTL --------------------------------------------------------------


def test_cache_ttl(tmp_path, monkeypatch):
    cache.init_db(str(tmp_path / "c.db"))
    cache.Snapshot.delete().execute()
    calls = {"n": 0}

    def refresh():
        calls["n"] += 1
        return {"v": calls["n"]}

    payload, age = cache.get_or_refresh(refresh, scope="t", ttl=100)
    assert payload == {"v": 1} and age == 0.0
    # Within TTL: served from cache, refresh not called again.
    payload, age = cache.get_or_refresh(refresh, scope="t", ttl=100)
    assert payload == {"v": 1} and calls["n"] == 1
    # force recomputes.
    payload, _ = cache.get_or_refresh(refresh, scope="t", ttl=100, force=True)
    assert payload == {"v": 2}
    # Simulate expiry by pretending now is far in the future.
    monkeypatch.setattr(cache, "_now", lambda: 10**12)
    payload, _ = cache.get_or_refresh(refresh, scope="t", ttl=100)
    assert payload == {"v": 3}


# --- inventory --------------------------------------------------------------


def _cfg_two_nets():
    return validated(
        make(
            {
                "regtest": {"enabled": True, "bitcart": BITCART_OFF},
                "testnet4": {"enabled": False, "bitcart": BITCART_OFF},
            }
        )
    )


def test_build_sections_enabled_and_disabled():
    cfg = _cfg_two_nets()
    port_map = allocate(cfg)
    fake_metrics = {
        "usage": {"regtest": {"bitcoind": {"ram": 100, "disk": 4000}}},
        "host": {},
    }
    sections = build_sections(cfg, port_map, fake_metrics)
    by_key = {s.key: s for s in sections}

    rt = by_key["regtest"]
    assert rt.enabled and rt.services
    bitcoind = next(s for s in rt.services if s.bucket == "bitcoind")
    assert bitcoind.ram == 100 and bitcoind.disk == 4000
    assert rt.ram_total == 100 and rt.disk_total == 4000
    assert rt.attach  # attach recipes present

    # Audience: services with a public port are visitor-facing; closed-only ones
    # are operator-only.
    assert bitcoind.audience == "Visitor"  # public P2P port
    assert any(p.label == "P2P" and p.public for p in bitcoind.ports)
    cashu = next(s for s in rt.services if s.bucket == "cashu")
    assert cashu.audience == "Visitor"
    miner = next(s for s in rt.services if s.bucket == "miner")
    assert miner.audience == "Operator only"  # block production is operator-controlled

    # Attach recipes carry an audience; the Bitcoin Core RPC one is operator-only
    # and must not suggest a visitor SSH tunnel.
    auds = {a.audience for a in rt.attach}
    assert auds == {"visitor", "operator"}
    rpc = next(a for a in rt.attach if a.audience == "operator")
    assert "Operator-only" in rpc.note and "ssh -" not in rpc.command.lower()

    t4 = by_key["testnet4"]
    assert not t4.enabled and not t4.services  # disabled: section but no table


def test_lnd_pubkey_uri_and_mempool_link():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    port_map = allocate(cfg)
    pk = "02" + "ab" * 32
    metrics = {"usage": {}, "host": {}, "lnd": {"regtest": pk}}
    section = next(s for s in build_sections(cfg, port_map, metrics) if s.key == "regtest")

    # LND row links to its node page on the mempool explorer.
    lnd = next(s for s in section.services if s.bucket == "lnd")
    assert any(f"/lightning/node/{pk}" in link.url for link in lnd.links)

    # The connection URI (pubkey@host:p2p) is offered in the attach section.
    uri_cmd = next(a for a in section.attach if "lncli connect" in a.command)
    assert f"{pk}@{cfg.global_.hostname}:{port_map['regtest']['lnd_p2p']}" in uri_cmd.command


def test_no_lnd_link_or_uri_without_pubkey():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    port_map = allocate(cfg)
    section = next(
        s for s in build_sections(cfg, port_map, {"usage": {}, "host": {}, "lnd": {}})
        if s.key == "regtest"
    )
    lnd = next(s for s in section.services if s.bucket == "lnd")
    assert lnd.links == []
    assert not any("lncli connect" in a.command for a in section.attach)


# --- generation -------------------------------------------------------------


def _write_cfg(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def test_generate_web_artifacts(tmp_path):
    cfg_path = _write_cfg(tmp_path)
    cfg = load_config(cfg_path)
    out = tmp_path / "generated"
    web_dir = generate_web(cfg, out, cfg_path)
    assert web_dir is not None
    for f in ("Dockerfile", "docker-compose.yml", ".env", "config.yaml"):
        assert (web_dir / f).is_file()
    # The source is copied in so the image build context is self-contained.
    assert (web_dir / "argus" / "web" / "app.py").is_file()
    compose = yaml.safe_load((web_dir / "docker-compose.yml").read_text())
    assert set(compose["services"]) == {"web", "socket-proxy"}
    # socket-proxy is read-only on the docker socket.
    assert "/var/run/docker.sock:/var/run/docker.sock:ro" in (
        compose["services"]["socket-proxy"]["volumes"]
    )


def test_generate_web_disabled_returns_none(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {"enabled": False}
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    cfg = load_config(p)
    assert generate_web(cfg, tmp_path / "g", p) is None


# --- Caddy + firewall wiring -----------------------------------------------


def test_caddy_root_site_default():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}, hostname="x.com"))
    out = render_caddyfile(cfg, allocate(cfg))
    # ssl off (test helper) => http root, no port.
    assert "http://x.com {" in out
    assert "reverse_proxy 127.0.0.1:29080" in out


def test_caddy_custom_port_and_ssl():
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}, hostname="x.com", ssl_enabled=True)
    data["web"] = {"port": 8443}
    cfg = validated(data)
    out = render_caddyfile(cfg, allocate(cfg))
    assert "x.com:8443 {" in out
    assert web_public_port(cfg) == 8443


def test_firewall_opens_web_port_when_needed():
    # ssl off + default web port 80 => explicit rule (80/443 not auto-added).
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    fw = render_firewall(cfg, allocate(cfg))
    assert "ufw allow 80/tcp comment 'argus dashboard'" in fw


# --- routes -----------------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from argus.web.app import create_app

    cfg_path = _write_cfg(tmp_path)
    app = create_app(config_path=str(cfg_path), cache_db=str(tmp_path / "cache.db"))
    return app.test_client()


def test_routes_ok(client):
    for path in ("/", "/tos", "/privacy", "/healthz"):
        assert client.get(path).status_code == 200


def test_theme_cookie_set(client):
    resp = client.get("/?theme=game")
    assert resp.status_code == 200
    assert "theme=game" in resp.headers.get("Set-Cookie", "")
    # Unknown theme falls back, no cookie set.
    resp = client.get("/?theme=bogus")
    assert "Set-Cookie" not in resp.headers or "theme=" not in resp.headers["Set-Cookie"]


def test_no_forbidden_words(client):
    body = client.get("/").get_data(as_text=True).lower()
    assert "uplink" not in body and "nintendo" not in body
