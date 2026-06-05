"""Tests for the dashboard: config schema, metrics attribution, cache TTL,
inventory view model, generation artifacts, Caddy/firewall wiring, and routes."""

from __future__ import annotations

import sys
import types
from urllib.parse import quote

import pytest
import yaml

from argus.config import load_config
from argus.firewall import render_firewall
from argus.ports import allocate
from argus.shared import render_caddyfile, web_public_port
from argus.web import cache, metrics
from argus.web.inventory import build_donations, build_sections
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


def test_web_operator_and_contact_defaults():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert cfg.web.operator_name == "BareBits"
    assert cfg.web.operator_url == "https://getbarebits.com"
    assert cfg.web.contact_email == "sales@getbarebits.com"


def test_web_operator_and_contact_overridable():
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {
        "operator_name": "Acme Labs",
        "operator_url": "https://acme.example",
        "contact_email": "hi@acme.example",
    }
    cfg = validated(data)
    assert cfg.web.operator_name == "Acme Labs"
    assert cfg.web.operator_url == "https://acme.example"
    assert cfg.web.contact_email == "hi@acme.example"


@pytest.mark.parametrize(
    "bad", ["nope", "no@domain", "two@@at.com", "has space@x.com", "a@b<c>.com", ""]
)
def test_web_contact_email_validated(bad):
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {"contact_email": bad}
    with pytest.raises(Exception):
        validated(data)


# --- metrics name attribution ----------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("argus-regtest-bitcoind", ("regtest", "bitcoind")),
        ("argus-regtest-miner", ("regtest", "miner")),
        ("argus-signet-lnd", ("signet", "lnd")),
        ("argus-regtest-lnd2", ("regtest", "lnd2")),  # second node, own bucket
        ("argus-regtest-lnd2-nodeinfo", ("regtest", "lnd2")),
        ("argus-regtest-lnd-setup", ("regtest", "lnd")),  # funding sidecar
        ("argus-signet-fulcrum-1", ("signet", "fulcrum")),
        ("argus-regtest-mempool-db", ("regtest", "mempool")),
        ("argus-regtest-mempool-web", ("regtest", "mempool")),
        ("argus-regtest-cashu", ("regtest", "cashu")),
        # The web wallet gets its own bucket, not folded into "cashu".
        ("argus-regtest-cashu-wallet", ("regtest", "cashu-wallet")),
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
    # The mint links to /v1/info (its root 404s — it's an API, not a page).
    assert cashu.links and cashu.links[0].url.endswith("/v1/info")
    # The cashu.me wallet row links to the wallet, pre-pointed at this mint via
    # the ?mint= deep-link (URL-encoded mint URL).
    wallet = next(s for s in rt.services if s.bucket == "cashu-wallet")
    assert wallet.name == "cashu.me (web wallet)"
    assert wallet.audience == "Visitor"
    wlink = wallet.links[0].url
    assert ":30101/?mint=" in wlink
    assert quote("http://x.com:30100/", safe="") in wlink
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


def test_build_sections_reset_info():
    cfg = validated(
        make(
            {
                "regtest": {"enabled": True, "bitcart": BITCART_OFF},
                "custom-signet": {"enabled": True, "bitcart": BITCART_OFF},
                "testnet4": {"enabled": False, "bitcart": BITCART_OFF},
            }
        )
    )
    port_map = allocate(cfg)
    metrics = {
        "usage": {},
        "host": {},
        "reset": {
            # regtest has a live size -> a countdown; custom-signet has none yet.
            "regtest": {
                "size_on_disk": 5_000_000_000,
                "limit_bytes": 30 * 1024**3,
                "block_interval_seconds": 60,
                "max_size_gb": 30,
            }
        },
    }
    by_key = {s.key: s for s in build_sections(cfg, port_map, metrics)}

    rt = by_key["regtest"].reset
    assert rt is not None and rt.known and rt.eta_text  # "X days, Y hours"
    assert rt.cap_text == "30"

    cs = by_key["custom-signet"].reset
    assert cs is not None and not cs.known and cs.eta_text is None

    # Disabled / non-mined networks carry no reset banner.
    assert by_key["testnet4"].reset is None


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


def test_lnd_link_falls_back_to_mempool_space():
    # signet/testnet3/testnet4 have no local mempool by default, so the LND row
    # links out to the matching public mempool.space Lightning node page.
    cfg = validated(make({
        "signet": {"enabled": True, "bitcart": BITCART_OFF},
        "testnet3": {"enabled": True, "bitcart": BITCART_OFF},
        "testnet4": {"enabled": True, "bitcart": BITCART_OFF},
    }))
    port_map = allocate(cfg)
    pk = "02" + "ab" * 32
    metrics = {"usage": {}, "host": {},
               "lnd": {"signet": pk, "testnet3": pk, "testnet4": pk}}
    sections = {s.key: s for s in build_sections(cfg, port_map, metrics)}

    expected = {
        "signet": f"https://mempool.space/signet/lightning/node/{pk}",
        "testnet3": f"https://mempool.space/testnet/lightning/node/{pk}",
        "testnet4": f"https://mempool.space/testnet4/lightning/node/{pk}",
    }
    for key, url in expected.items():
        lnd = next(s for s in sections[key].services if s.bucket == "lnd")
        assert [l.url for l in lnd.links] == [url]
        assert lnd.links[0].label == "Node on mempool.space"


def test_local_mempool_link_wins_over_mempool_space():
    # When a network DOES run a local mempool, its row points there, not out.
    cfg = validated(make({"signet": {
        "enabled": True, "bitcart": BITCART_OFF, "mempool": {"enabled": True}}}))
    port_map = allocate(cfg)
    pk = "02" + "ab" * 32
    metrics = {"usage": {}, "host": {}, "lnd": {"signet": pk}}
    section = next(s for s in build_sections(cfg, port_map, metrics) if s.key == "signet")
    lnd = next(s for s in section.services if s.bucket == "lnd")
    assert lnd.links[0].label == "Node on mempool"
    assert "mempool.space" not in lnd.links[0].url
    assert f"/lightning/node/{pk}" in lnd.links[0].url


def test_private_signets_not_linked_to_mempool_space():
    # The custom signets share chain="signet" but are private — they must never
    # be mapped to the public explorer (the map is keyed by network key).
    from argus.web.content import MEMPOOL_SPACE_LN_NODE

    assert "mutinynet" not in MEMPOOL_SPACE_LN_NODE
    assert "custom-signet" not in MEMPOOL_SPACE_LN_NODE
    assert "regtest" not in MEMPOOL_SPACE_LN_NODE


def test_second_lnd_node_rows_and_uris():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    port_map = allocate(cfg)
    pk1, pk2 = "02" + "ab" * 32, "03" + "cd" * 32
    metrics = {"usage": {}, "host": {}, "lnd": {"regtest": pk1}, "lnd2": {"regtest": pk2}}
    section = next(s for s in build_sections(cfg, port_map, metrics) if s.key == "regtest")

    # Two LND rows, one per node, on distinct usage buckets.
    buckets = [s.bucket for s in section.services]
    assert "lnd" in buckets and "lnd2" in buckets
    node2 = next(s for s in section.services if s.bucket == "lnd2")
    assert any(p.label == "P2P" and p.port == port_map["regtest"]["lnd2_p2p"]
               for p in node2.ports)

    # Both connect URIs are offered, argus1 first.
    connects = [a for a in section.attach if "lncli connect" in a.command]
    assert any(pk1 in a.command for a in connects)
    assert any(pk2 in a.command for a in connects)
    assert pk1 in connects[0].command  # argus1 at the top


def test_regtest_mining_recipe_present_and_gated():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    port_map = allocate(cfg)
    section = next(
        s for s in build_sections(cfg, port_map, {"usage": {}, "host": {}})
        if s.key == "regtest"
    )
    mine = next(a for a in section.attach if "generatetoaddress" in a.command)
    assert mine.audience == "visitor"
    assert f":{port_map['regtest']['bitcoind_p2p']}" in mine.command
    assert "mine only the blocks you need" in mine.note.lower()

    # With the P2P port loopback-only, the mining recipe is withheld.
    cfg2 = validated(make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "bitcoind": {"p2p_public": False}}}))
    section2 = next(
        s for s in build_sections(cfg2, allocate(cfg2), {"usage": {}, "host": {}})
        if s.key == "regtest"
    )
    assert not any("generatetoaddress" in a.command for a in section2.attach)


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


# --- donations --------------------------------------------------------------


def test_build_donations_only_enabled_in_order():
    cfg = validated(make({
        "signet": {"enabled": True, "bitcart": BITCART_OFF},
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "testnet4": {"enabled": False, "bitcart": BITCART_OFF},
    }))
    metrics_payload = {"donations": {
        "regtest": {"address": "bcrt1qabc", "total_received": "1.50000000",
                    "balance": "0.25000000"},
    }}
    rows = build_donations(cfg, metrics_payload)
    # Disabled networks are excluded; enabled ones follow the recommended order
    # (regtest before signet).
    assert [r.key for r in rows] == ["regtest", "signet"]

    rt = rows[0]
    assert rt.address == "bcrt1qabc"
    assert rt.total_received == "1.50000000" and rt.balance == "0.25000000"

    # A network with no sidecar report yet renders with empty figures (the
    # template shows "pending…"), not an error.
    sg = rows[1]
    assert sg.address is None and sg.total_received is None and sg.balance is None


def test_build_donations_tolerates_missing_key():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert build_donations(cfg, {}) and build_donations(cfg, {})[0].address is None


def test_collect_reads_donation_file(monkeypatch):
    # The dashboard reads the donations JSON from the sidecar via get_archive
    # (a GET allowed by the read-only socket proxy), exactly like LND pubkeys.
    import io
    import json
    import tarfile

    def fake_archive(payload: dict):
        raw = json.dumps(payload).encode()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tf:
            info = tarfile.TarInfo("donations.json")
            info.size = len(raw)
            tf.addfile(info, io.BytesIO(raw))
        return [buf.getvalue()], {}

    class FakeContainer:
        def __init__(self, name):
            self.name = name

        def get_archive(self, path):
            assert path == "/state/donations.json"
            return fake_archive({"address": "bcrt1qxyz",
                                 "total_received": "2.00000000",
                                 "balance": "0.40000000"})

    class FakeClient:
        def df(self):
            return {"Volumes": [], "Containers": []}

    client = FakeClient()
    client.containers = types.SimpleNamespace(
        list=lambda: [],
        get=lambda name: FakeContainer(name) if name.endswith("-donations")
        else (_ for _ in ()).throw(Exception("no such container")),
    )
    fake_docker = types.ModuleType("docker")
    fake_docker.from_env = lambda: client
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    result = metrics.collect(["regtest"]).as_dict()
    assert result["donations"]["regtest"] == {
        "address": "bcrt1qxyz",
        "total_received": "2.00000000",
        "balance": "0.40000000",
    }


# --- "which network" picker columns + Tor accessibility ---------------------


def test_when_to_use_columns_enabled_plus_local():
    from argus.web.content import when_to_use_columns

    # Local-regtest column always leads; then only the ENABLED networks, in order.
    cols = when_to_use_columns(["signet", "regtest"])
    keys = [c.key for c in cols]
    assert keys[0] == "local-regtest"
    assert keys[1:] == ["regtest", "signet"]  # canonical VARIANT_ORDER
    assert all(len(c.reasons) == 5 for c in cols)  # exactly five reasons each
    local = cols[0]
    assert local.title == "Regtest on your machine"


def test_build_sections_onion_population():
    cfg = validated(make(
        {"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
        tor={"enabled": True},
    ))
    port_map = allocate(cfg)
    onion = "abc234def567ghi890jkl123mno456pqr789stu012vwx345yz678abd.onion"
    section = next(
        s for s in build_sections(cfg, port_map, {"usage": {}, "host": {}}, onion)
        if s.key == "regtest"
    )
    # Each public HTTP service link carries an onion equivalent on the same port.
    mempool = next(s for s in section.services if s.name == "mempool explorer")
    port = port_map["regtest"]["mempool_public"]
    assert mempool.links[0].onion_url == f"http://{onion}:{port}/"
    # Operator-only services (no public link) have no link to carry an onion URL.
    core = next(s for s in section.services if s.name == "Bitcoin Core node")
    assert core.links == []


def test_build_sections_no_onion_without_hostname():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    port_map = allocate(cfg)
    section = next(
        s for s in build_sections(cfg, port_map, {"usage": {}, "host": {}})
        if s.key == "regtest"
    )
    # Tor off => no link carries an onion URL.
    assert all(
        link.onion_url is None for svc in section.services for link in svc.links
    )


def test_onion_lnd_connect_line_present():
    cfg = validated(make(
        {"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
        tor={"enabled": True},
    ))
    port_map = allocate(cfg)
    pk = "02" + "ab" * 32
    onion = "abc234def567ghi890jkl123mno456pqr789stu012vwx345yz678abd.onion"
    section = next(
        s for s in build_sections(
            cfg, port_map, {"usage": {}, "host": {}, "lnd": {"regtest": pk}}, onion)
        if s.key == "regtest"
    )
    # Clearnet connect and its onion variant now live in ONE attach item:
    # the clearnet URI in .command, the onion URI in .command_onion.
    p2p = port_map["regtest"]["lnd_p2p"]
    lnd = next(
        a for a in section.attach
        if "lncli connect" in a.command and f":{p2p}" in a.command
    )
    assert f"{pk}@{cfg.global_.hostname}:{p2p}" in lnd.command  # clearnet URI
    assert f"{pk}@{onion}:{p2p}" in lnd.command_onion  # onion URI, same container


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
    for path in ("/", "/tos", "/privacy", "/contact", "/healthz"):
        assert client.get(path).status_code == 200


def test_contact_page_shows_email_and_copy(client):
    body = client.get("/contact").get_data(as_text=True)
    assert "mailto:sales@getbarebits.com" in body
    assert "We welcome any and all testing feedback" in body


def test_footer_uses_configured_operator(tmp_path):
    from argus.web.app import create_app

    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    data["web"] = {"operator_name": "Acme Labs", "operator_url": "https://acme.example"}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    app = create_app(config_path=str(cfg_path), cache_db=str(tmp_path / "c.db"))
    body = app.test_client().get("/").get_data(as_text=True)
    assert "Acme Labs" in body and "https://acme.example" in body
    # Footer also links to the contact page.
    assert 'href="/contact"' in body


def test_index_renders_network_tabs(client):
    # Each configured network gets a radio + a labelled tab/panel; the first
    # enabled network's radio is checked by default.
    body = client.get("/").get_data(as_text=True)
    assert 'id="nettab-regtest"' in body
    assert 'for="nettab-regtest"' in body
    assert 'id="netpanel-regtest"' in body
    # The per-network panel-reveal rule is generated into the page <style>.
    assert "#nettab-regtest:checked ~ .net-panels #netpanel-regtest" in body
    # The default (first enabled) network's radio is checked.
    assert "checked" in body


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
