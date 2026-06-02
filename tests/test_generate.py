"""End-to-end generation: config -> compose / conf / env / Caddyfile / firewall."""

from __future__ import annotations

import yaml

from argus.generate import generate
from helpers import BITCART_OFF, BITCART_OK, make


def _gen(tmp_path, data):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    return out, sec


def _read(p):
    return p.read_text()


def test_regtest_compose_structure(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OK}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    services = set(compose["services"])
    assert {"bitcoind", "lnd", "fulcrum-1", "cashu", "miner",
            "mempool-db", "mempool-api", "mempool-web"} <= services


def test_bitcoind_rpc_bound_to_loopback(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    ports = compose["services"]["bitcoind"]["ports"]
    assert any(p.startswith("127.0.0.1:30001:") for p in ports)
    # RPC must never be published on 0.0.0.0
    assert not any(p.startswith("30001:") or p.startswith("0.0.0.0:30001") for p in ports)


def test_bitcoin_conf_contents(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OK}}))
    conf = _read(out / "regtest" / "bitcoin" / "bitcoin.conf")
    assert "chain=regtest" in conf
    assert "blockfilterindex=1" in conf  # bitcart on => BIP157
    assert "zmqpubrawblock=tcp://0.0.0.0:28332" in conf
    assert "zmqpubrawtx=tcp://0.0.0.0:28333" in conf  # distinct from block port


def test_mutinynet_conf_has_challenge_and_blocktime(tmp_path):
    out, _ = _gen(tmp_path, make(
        {"mutinynet": {"enabled": True, "bitcart": BITCART_OK}},
        bitcoind_knots_image="reg/btc:1"))
    conf = _read(out / "mutinynet" / "bitcoin" / "bitcoin.conf")
    assert "signetchallenge=" in conf
    assert "signetblocktime=30" in conf
    assert "addnode=45.79.52.207:38333" in conf


def test_cashu_wired_to_lnd(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    env = compose["services"]["cashu"]["environment"]
    assert env["MINT_BACKEND_BOLT11_SAT"] == "LndRestWallet"
    assert "/bitcoin/regtest/admin.macaroon" in env["MINT_LND_REST_MACAROON"]


def test_mempool_network_mapping(tmp_path):
    out, _ = _gen(tmp_path, make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "testnet4": {"enabled": True, "bitcart": BITCART_OFF,
                     "mempool": {"enabled": True}},
    }))
    rt = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    t4 = yaml.safe_load(_read(out / "testnet4" / "docker-compose.yml"))
    assert rt["services"]["mempool-api"]["environment"]["MEMPOOL_NETWORK"] == "mainnet"
    assert t4["services"]["mempool-api"]["environment"]["MEMPOOL_NETWORK"] == "testnet4"


def test_caddyfile_ssl_off(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    caddy = _read(out / "shared" / "Caddyfile")
    assert "auto_https off" in caddy
    assert "http://x.com:30100" in caddy  # cashu site, plain http


def test_caddyfile_ssl_on(tmp_path):
    out, _ = _gen(tmp_path, make(
        {"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
        ssl_enabled=True, acme_email="ops@x.com"))
    caddy = _read(out / "shared" / "Caddyfile")
    assert "email ops@x.com" in caddy
    assert "x.com:30100 {" in caddy  # managed TLS (no http:// scheme)
    assert "http://x.com:30100" not in caddy


def test_firewall_script(tmp_path):
    out, _ = _gen(tmp_path, make(
        {"regtest": {"enabled": True, "bitcart": BITCART_OK}},
        ssl_enabled=True, acme_email="ops@x.com"))
    fw = _read(out / "firewall.sh")
    assert "ufw allow 22/tcp" in fw
    assert "ufw allow 80/tcp" in fw  # ssl on
    assert "ufw allow 30040/tcp" in fw  # electrum
    assert "ufw allow 30400:30404/tcp" in fw  # btclnd p2p pool (default size 5)


def test_bitcart_env(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OK}}))
    env = dict(
        line.split("=", 1)
        for line in _read(out / "regtest" / "bitcart" / "bitcart.env").splitlines()
        if "=" in line
    )
    assert env["DEPLOY_NAME"] == "argus-bitcart-regtest"
    assert env["BITCART_REVERSEPROXY"] == "none"
    assert env["BTCLND_NEUTRINO_PEERS"] == "bitcoind:18444"
    assert env["BITCART_ADMIN_API_URL"] == "http://x.com:30202"  # ssl off => http
    assert env["LIQUIDITYHELPER_LIQUIDITY_DISABLED"] == "False"


def test_resource_knobs_in_confs(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    bconf = _read(out / "regtest" / "bitcoin" / "bitcoin.conf")
    assert "dbcache=300" in bconf and "maxmempool=100" in bconf  # medium profile
    fconf = _read(out / "regtest" / "fulcrum-1" / "fulcrum.conf")
    assert "db_mem = 600" in fconf and "db_max_open_files = 200" in fconf
    assert "db.bolt.auto-compact=true" in _read(out / "regtest" / "lnd" / "lnd.conf")


def test_lnd_hygiene_toggle_off(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "lnd": {"auto_compact": False}}}))
    assert "auto-compact" not in _read(out / "regtest" / "lnd" / "lnd.conf")


def test_mempool_statistics_off_and_buffer(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    d = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert d["services"]["mempool-api"]["environment"]["STATISTICS_ENABLED"] == "false"
    assert "--innodb-buffer-pool-size=128M" in d["services"]["mempool-db"]["command"]


def test_log_rotation_default_on(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    d = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert d["services"]["bitcoind"]["logging"]["options"]["max-size"] == "10m"


def test_log_rotation_off(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF, "resources": {"log_rotation": False}}}))
    d = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert "logging" not in d["services"]["bitcoind"]


def test_secrets_are_idempotent(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}})
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    first = (sec / "regtest" / "secrets.env").read_text()
    generate(str(cfgp), output_dir=out, secrets_dir=sec)  # regenerate
    assert (sec / "regtest" / "secrets.env").read_text() == first
