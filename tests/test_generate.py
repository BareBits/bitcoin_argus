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


def test_bitcoind_p2p_published_and_firewalled(tmp_path):
    # With the auto-channel feature off, regtest P2P is published + firewalled.
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "lnd": {"channels": {"enabled": False}}}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    ports = compose["services"]["bitcoind"]["ports"]
    # P2P (30000 -> internal 18444) published publicly (no 127.0.0.1 prefix).
    assert any(p == "30000:18444" for p in ports)
    assert "listen=1" in _read(out / "regtest" / "bitcoin" / "bitcoin.conf")
    fw = _read(out / "firewall.sh")
    assert "ufw allow 30000/tcp" in fw and "bitcoind p2p" in fw


def test_regtest_p2p_self_gated_by_default(tmp_path):
    # Auto-channels default on for regtest: the P2P port is published normally,
    # but bitcoind runs a self-gate wrapper that keeps its inbound listener closed
    # until the lnd-channels marker appears, then restarts with P2P open. No
    # operator step / open-mining.sh.
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    bitcoind = compose["services"]["bitcoind"]
    assert "30000:18444" in bitcoind["ports"]  # published normally
    # Wrapper entrypoint + shared state volume (read-only) to watch the marker.
    assert bitcoind["entrypoint"] == ["/bin/sh", "/scripts/bitcoind-gate.sh"]
    assert "lnd_setup_state:/state:ro" in bitcoind["volumes"]
    gate = _read(out / "regtest" / "bitcoin" / "bitcoind-gate.sh")
    assert "-listen=0" in gate and "/state/channels" in gate
    fw = _read(out / "firewall.sh")
    assert "ufw allow 30000/tcp" in fw  # auto-opening port is firewalled open
    assert not (out / "open-mining.sh").exists()


def test_custom_signet_p2p_not_gated(tmp_path):
    # Custom-signet is never gated (outsiders can't mine it), so bitcoind uses the
    # plain image entrypoint (no self-gate wrapper).
    out, _ = _gen(tmp_path, make({"custom-signet": {
        "enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "custom-signet" / "docker-compose.yml"))
    bitcoind = compose["services"]["bitcoind"]
    assert "35000:38333" in bitcoind["ports"]
    assert "entrypoint" not in bitcoind
    assert not (out / "open-mining.sh").exists()


def test_secondary_lnd_and_channel_sidecars(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    svcs = compose["services"]
    assert {"lnd", "lnd2", "lnd-nodeinfo", "lnd2-nodeinfo",
            "lnd-setup", "lnd-channels"} <= set(svcs)
    # Second node: P2P public (30013), gRPC/REST loopback.
    assert "30013:9735" in svcs["lnd2"]["ports"]
    assert "127.0.0.1:30015:10009" in svcs["lnd2"]["ports"]
    # Aliases/colors distinguish the two nodes.
    assert "alias=argus1" in _read(out / "regtest" / "lnd" / "lnd.conf")
    assert "alias=argus2" in _read(out / "regtest" / "lnd2" / "lnd.conf")
    # Setup sidecar (bitcoind image) funds; channels sidecar (LND image) opens.
    assert svcs["lnd-setup"]["image"] == "${BITCOIND_IMAGE}"
    assert svcs["lnd-channels"]["image"] == "${LND_IMAGE}"
    assert svcs["lnd-setup"]["environment"]["FUNDING_WALLET"] == "miner"
    assert "lnd_setup_state" in compose["volumes"]


def test_single_lnd_when_secondary_off(tmp_path):
    out, _ = _gen(tmp_path, make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "signet" / "docker-compose.yml"))
    svcs = set(compose["services"])
    assert "lnd" in svcs
    assert not ({"lnd2", "lnd-setup", "lnd-channels"} & svcs)
    # No second node => no funding/channel orchestration, no open-mining.
    assert not (out / "open-mining.sh").exists()


def test_lnd_discovery_knobs(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    conf = _read(out / "regtest" / "lnd" / "lnd.conf")
    # Discoverable + open to large channels so peers can easily open to us.
    assert "externalip=x.com:30010" in conf
    # Wumbo auto-on for 10 BTC channels, emitted in its own [protocol] section.
    assert "[protocol]\nprotocol.wumbo-channels=true" in conf
    assert "accept-amp=true" in conf
    assert "color=#3399ff" in conf


def test_bitcoind_p2p_loopback_when_not_public(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "bitcoind": {"p2p_public": False}}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    ports = compose["services"]["bitcoind"]["ports"]
    # Bound to loopback, never on 0.0.0.0, and not in the firewall.
    assert "127.0.0.1:30000:18444" in ports
    assert "30000:18444" not in ports
    assert "bitcoind p2p" not in _read(out / "firewall.sh")


def test_lnd_nodeinfo_sidecar(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert "lnd-nodeinfo" in compose["services"]
    side = compose["services"]["lnd-nodeinfo"]
    assert side["depends_on"]["lnd"]["condition"] == "service_healthy"
    assert side["restart"] == "on-failure"
    # Runs from a mounted script (not an inline entrypoint) so compose's variable
    # interpolation can't mangle the shell ``$i``/``$A`` references.
    assert side["entrypoint"] == ["/bin/sh", "/scripts/nodeinfo.sh"]
    assert any("nodeinfo.sh:/scripts/nodeinfo.sh:ro" in v for v in side["volumes"])
    assert side["environment"]["RPCSERVER"] == "lnd:10009"
    # Channels on for regtest => the node writes a funding address too.
    assert side["environment"]["WRITE_ADDR"] == "1"
    script = _read(out / "regtest" / "lnd" / "nodeinfo.sh")
    assert "argus_nodeinfo.json" in script and "newaddress p2wkh" in script


def test_donations_sidecar_reuses_mining_wallet(tmp_path):
    # regtest + custom-signet mine into their own wallet; the donations sidecar
    # must reuse it (never recreate — that would race the miner / drop the signer
    # key) and the donation address goes into that same single wallet.
    out, _ = _gen(tmp_path, make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "custom-signet": {"enabled": True, "bitcart": BITCART_OFF},
    }))
    for net, wallet, flag in (("regtest", "miner", "-regtest"),
                              ("custom-signet", "signer", "-signet")):
        compose = yaml.safe_load(_read(out / net / "docker-compose.yml"))
        assert "donations" in compose["services"]
        d = compose["services"]["donations"]
        assert d["depends_on"]["bitcoind"]["condition"] == "service_healthy"
        assert d["entrypoint"] == ["/bin/sh", "/scripts/donations.sh"]
        assert d["user"] == "0:0"  # writes the root-owned state volume
        env = d["environment"]
        assert env["WALLET"] == wallet
        assert env["CREATE_WALLET"] == "0"  # reuse, don't recreate
        assert env["CHAIN_FLAG"] == flag
        assert "donations_state:/state" in d["volumes"]
        assert "donations_state" in compose["volumes"]
        script = _read(out / net / "donations" / "donations.sh")
        assert "getreceivedbyaddress" in script and "getbalance" in script


def test_donations_sidecar_creates_wallet_on_non_mined(tmp_path):
    # signet has no miner, so bitcoind has no wallet — the sidecar creates a plain
    # 'donation' wallet itself.
    out, _ = _gen(tmp_path, make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "signet" / "docker-compose.yml"))
    env = compose["services"]["donations"]["environment"]
    assert env["WALLET"] == "donation"
    assert env["CREATE_WALLET"] == "1"
    assert env["CHAIN_FLAG"] == "-signet"


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
    rt_api = rt["services"]["mempool-api"]["environment"]
    rt_web = rt["services"]["mempool-web"]["environment"]
    t4_web = t4["services"]["mempool-web"]["environment"]
    # regtest runs in the mainnet slot (network="") so the Lightning nav stays
    # enabled (regtest is hardcoded out of mempool's lightning-network list).
    assert rt_api["MEMPOOL_NETWORK"] == "mainnet"
    assert "ROOT_NETWORK" not in rt_web
    assert rt_web["BACKEND_MAINNET_HTTP_HOST"] == "mempool-api"
    # A real testnet uses its native slot, served at root, with mainnet hidden so
    # the selector lists only that network.
    assert t4["services"]["mempool-api"]["environment"]["MEMPOOL_NETWORK"] == "testnet4"
    assert t4_web["ROOT_NETWORK"] == "testnet4"
    assert t4_web["TESTNET4_ENABLED"] == "true"
    assert t4_web["MAINNET_ENABLED"] == "false"


def test_mempool_mainnet_slot_injects_warning_banner(tmp_path):
    # regtest (mainnet slot) gets an injected nginx sub_filter banner, since
    # mempool shows no built-in test-coin warning for mainnet.
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    web = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-web"]
    assert "./mempool/web-banner.sh:/web-banner.sh:ro" in web["volumes"]
    assert web["command"] == ["/bin/sh", "/web-banner.sh"]
    script = _read(out / "regtest" / "mempool" / "web-banner.sh")
    assert "sub_filter" in script and "no real value" in script
    assert "Argus regtest" in script


def test_mempool_native_slot_has_no_banner(tmp_path):
    # A real testnet uses mempool's built-in warning, so no banner is injected.
    out, _ = _gen(tmp_path, make({
        "regtest": {"enabled": False},
        "testnet4": {"enabled": True, "bitcart": BITCART_OFF,
                     "mempool": {"enabled": True}},
    }))
    web = yaml.safe_load(_read(out / "testnet4" / "docker-compose.yml"))[
        "services"]["mempool-web"]
    assert "command" not in web
    assert "volumes" not in web


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
    # Cross-frontend origins so the store can reach the checkout app (admin port);
    # bare host:port (the frontends add the scheme). Without these checkout 404s.
    assert env["BITCART_STORE_HOST"] == "x.com:30200"
    assert env["BITCART_ADMIN_HOST"] == "x.com:30201"


def test_bitcart_cross_host_ssl(tmp_path):
    # The host vars are bare host:port regardless of SSL (scheme added by the
    # frontend); the API URL still carries the scheme.
    out, _ = _gen(tmp_path, make(
        {"regtest": {"enabled": True, "bitcart": BITCART_OK}},
        ssl_enabled=True, acme_email="ops@x.com"))
    env = dict(
        line.split("=", 1)
        for line in _read(out / "regtest" / "bitcart" / "bitcart.env").splitlines()
        if "=" in line
    )
    assert env["BITCART_ADMIN_HOST"] == "x.com:30201"
    assert env["BITCART_STORE_HOST"] == "x.com:30200"
    assert env["BITCART_ADMIN_API_URL"] == "https://x.com:30202"


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


def test_mempool_statistics_default_on_and_buffer(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    d = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert d["services"]["mempool-api"]["environment"]["STATISTICS_ENABLED"] == "true"
    assert "--innodb-buffer-pool-size=128M" in d["services"]["mempool-db"]["command"]


def test_mempool_statistics_can_be_disabled(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "mempool": {"statistics": False}}}))
    d = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert d["services"]["mempool-api"]["environment"]["STATISTICS_ENABLED"] == "false"


def test_mempool_lightning_wired_to_lnd(tmp_path):
    out, _ = _gen(tmp_path, make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "signet": {"enabled": True, "bitcart": BITCART_OFF,
                   "mempool": {"enabled": True}},
    }))
    api = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-api"]
    env = api["environment"]
    assert env["LIGHTNING_ENABLED"] == "true"
    assert env["LIGHTNING_BACKEND"] == "lnd"
    assert env["LND_REST_API_URL"] == "https://lnd:8080"
    assert env["LND_TLS_CERT_PATH"] == "/lnd-data/tls.cert"
    # regtest -> LND's bitcoin/regtest macaroon dir; readonly (not admin).
    assert env["LND_MACAROON_PATH"] == (
        "/lnd-data/data/chain/bitcoin/regtest/readonly.macaroon")
    assert "lnd_data:/lnd-data:ro" in api["volumes"]
    assert api["user"] == "0:0"  # so it can read LND's 0600 macaroon
    assert "lnd" in api["depends_on"]

    # The frontend needs its own LIGHTNING flag to show the /lightning section.
    web = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-web"]
    assert web["environment"]["LIGHTNING"] == "true"

    # The macaroon dir tracks LND's network key (signet here, not the chain name).
    s_api = yaml.safe_load(_read(out / "signet" / "docker-compose.yml"))[
        "services"]["mempool-api"]
    assert s_api["environment"]["LND_MACAROON_PATH"] == (
        "/lnd-data/data/chain/bitcoin/signet/readonly.macaroon")


def test_mempool_lightning_off(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "mempool": {"lightning": False}}}))
    api = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-api"]
    assert "LIGHTNING_ENABLED" not in api["environment"]
    assert "volumes" not in api
    assert "user" not in api
    assert "lnd" not in api["depends_on"]
    web = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-web"]
    assert "LIGHTNING" not in web["environment"]


def test_mempool_web_restarts_with_api(tmp_path):
    # The frontend's nginx caches the API's resolved IP at startup, so Compose
    # must recreate the frontend whenever it recreates the API (new IP) — else
    # /api calls 502 until a manual restart.
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    web = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))[
        "services"]["mempool-web"]
    assert web["depends_on"]["mempool-api"]["restart"] is True


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
