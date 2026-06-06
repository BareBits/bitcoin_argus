"""End-to-end generation: config -> compose / conf / env / Caddyfile / firewall."""

from __future__ import annotations

import json

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


def test_ring_nodes_and_sidecars(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    svcs = compose["services"]
    # Three-node ring: all nodes, their reporters, funding, ring opener, rebalancer.
    assert {"lnd", "lnd2", "lnd3", "lnd-nodeinfo", "lnd2-nodeinfo", "lnd3-nodeinfo",
            "lnd-setup", "lnd-channels", "lnd-rebalancer"} <= set(svcs)
    # Third node: P2P public (30016), gRPC/REST loopback.
    assert "30016:9735" in svcs["lnd3"]["ports"]
    assert "127.0.0.1:30018:10009" in svcs["lnd3"]["ports"]
    # Aliases distinguish the three nodes.
    assert "alias=argus1" in _read(out / "regtest" / "lnd" / "lnd.conf")
    assert "alias=argus2" in _read(out / "regtest" / "lnd2" / "lnd.conf")
    assert "alias=argus3" in _read(out / "regtest" / "lnd3" / "lnd.conf")
    # Setup (bitcoind image) funds; channels + rebalancer (LND image).
    assert svcs["lnd-setup"]["image"] == "${BITCOIND_IMAGE}"
    assert svcs["lnd-channels"]["image"] == "${LND_IMAGE}"
    assert svcs["lnd-rebalancer"]["image"] == "${LND_IMAGE}"
    assert svcs["lnd-setup"]["environment"]["FUNDING_WALLET"] == "miner"
    # Auto funding mode on regtest; ring opener depends on all three nodes.
    assert svcs["lnd-channels"]["environment"]["FUNDING"] == "auto"
    assert "lnd3" in svcs["lnd-channels"]["depends_on"]
    assert "lnd_setup_state" in compose["volumes"]
    # Ring opener script wires all three hops and does the initial rebalance.
    ring = _read(out / "regtest" / "lnd_setup" / "channels.sh")
    assert "ring_l1_l2" in ring and "ring_l2_l3" in ring and "ring_l3_l1" in ring
    assert "allow_self_payment" in ring


def test_external_funding_omits_setup_sidecar(tmp_path):
    # On a non-mineable net the ring funds externally: no lnd-setup, and the ring
    # opener runs in external mode (waits for coins sent to each node).
    out, _ = _gen(tmp_path, make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "signet" / "docker-compose.yml"))
    svcs = compose["services"]
    assert {"lnd", "lnd2", "lnd3", "lnd-channels", "lnd-rebalancer"} <= set(svcs)
    assert "lnd-setup" not in svcs
    assert svcs["lnd-channels"]["environment"]["FUNDING"] == "external"


def test_single_lnd_when_ring_off(tmp_path):
    # Opting out of the ring (channels + both extra nodes off) leaves one node and
    # no funding/channel orchestration.
    out, _ = _gen(tmp_path, make({"signet": {
        "enabled": True, "bitcart": BITCART_OFF,
        "lnd": {"channels": {"enabled": False},
                "secondary": {"enabled": False},
                "tertiary": {"enabled": False}}}}))
    compose = yaml.safe_load(_read(out / "signet" / "docker-compose.yml"))
    svcs = set(compose["services"])
    assert "lnd" in svcs
    assert not ({"lnd2", "lnd3", "lnd-setup", "lnd-channels", "lnd-rebalancer"} & svcs)
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
    # Now a long-running reporter (identity + address + liquidity snapshot).
    assert side["restart"] == "unless-stopped"
    # Runs from a mounted script (not an inline entrypoint) so compose's variable
    # interpolation can't mangle the shell ``$1``/``$A`` references.
    assert side["entrypoint"] == ["/bin/sh", "/scripts/nodeinfo.sh"]
    assert any("nodeinfo.sh:/scripts/nodeinfo.sh:ro" in v for v in side["volumes"])
    assert side["environment"]["RPCSERVER"] == "lnd:10009"
    assert side["environment"]["ALIAS"] == "argus1"
    script = _read(out / "regtest" / "lnd" / "nodeinfo.sh")
    assert "argus_nodeinfo.json" in script and "newaddress p2wkh" in script
    # Writes the liquidity snapshot the operator dashboard reads.
    assert "argus_liquidity.json" in script


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


def test_cashu_wallet_deployed_by_default(tmp_path):
    # The cashu.me web wallet rides along with the mint: a per-network container
    # off the shared build context, plus the shared build context itself.
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    svc = compose["services"]["cashu-wallet"]
    assert svc["container_name"] == "argus-regtest-cashu-wallet"
    assert svc["image"] == "argus-cashu-wallet:local"
    assert svc["build"]["context"] == "../cashu-wallet"
    # Closed to the internet (Caddy fronts it); served on the wallet backend port.
    assert svc["ports"] == ["127.0.0.1:30111:80"]
    # The shared build context is generated once, with a pinned source ref.
    dockerfile = _read(out / "cashu-wallet" / "Dockerfile")
    assert "git clone https://github.com/cashubtc/cashu.me" in dockerfile
    assert "ARG CASHU_WALLET_REF" in dockerfile
    assert svc["build"]["args"]["CASHU_WALLET_REF"]  # ref injected from config
    assert (out / "cashu-wallet" / "nginx.conf").exists()


def test_cashu_wallet_fronted_and_firewalled(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    caddy = _read(out / "shared" / "Caddyfile")
    # Wallet public port proxies to its loopback backend (plain http, ssl off).
    assert "http://x.com:30101" in caddy
    assert "reverse_proxy 127.0.0.1:30111" in caddy
    fw = _read(out / "firewall.sh")
    assert "ufw allow 30101/tcp" in fw and "cashu wallet" in fw


def test_cashu_wallet_can_be_disabled(tmp_path):
    # wallet: false keeps the mint but drops the wallet container, Caddy site,
    # firewall rule, and the shared build context (nothing references it).
    out, _ = _gen(tmp_path, make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "cashu": {"enabled": True, "wallet": False}}}))
    compose = yaml.safe_load(_read(out / "regtest" / "docker-compose.yml"))
    assert "cashu" in compose["services"]
    assert "cashu-wallet" not in compose["services"]
    assert not (out / "cashu-wallet").exists()
    assert "30101" not in _read(out / "shared" / "Caddyfile")
    assert "30101" not in _read(out / "firewall.sh")


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


def test_bitcart_products_seeded(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OK}}))
    pdir = out / "regtest" / "bitcart" / "products"
    manifest = json.loads(_read(pdir / "manifest.json"))
    assert {m["name"] for m in manifest} == {
        "Hal Finney", "Gavin Andresen", "Satoshi Nakamoto"}
    # Every manifest image and the seeding script are staged alongside it.
    for m in manifest:
        assert (pdir / m["image"]).is_file()
    seed = pdir / "seed-products.py"
    assert seed.is_file() and (seed.stat().st_mode & 0o111)  # executable
    # The wrapper invokes the seeder after deploy.
    wrapper = _read(out / "regtest" / "bitcart" / "deploy-bitcart.sh")
    assert "products/seed-products.py" in wrapper


def test_bitcart_products_absent_when_disabled(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert not (out / "regtest" / "bitcart").exists()


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
