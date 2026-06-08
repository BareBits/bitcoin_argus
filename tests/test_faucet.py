"""Tests for the testnet faucet: config wiring, the approval/address/store
helpers, the generated artefacts (web service, Caddy route, reset hook), and the
Flask app end-to-end with a stubbed LND node."""

from __future__ import annotations

import pytest
import yaml

from argus.config import ConfigError, load_config
from argus.faucet import store
from argus.faucet.addresses import is_valid_address
from argus.faucet.app import _btc_to_sats, _sats_to_btc, create_app
from argus.faucet.approval import FaucetContext, get, is_registered, names
from argus.ports import allocate
from argus.reset import generate_reset
from argus.shared import render_caddyfile
from argus.web_gen import generate_web
from helpers import make, validated

# Known-valid example addresses by family (verified against BIP173/350 + base58).
ADDR_REGTEST_P2WPKH = "bcrt1qqqqsyqcyq5rqwzqfpg9scrgwpugpzysnard0ew"
ADDR_TB_P2WPKH = "tb1qqqqsyqcyq5rqwzqfpg9scrgwpugpzysnl25zw8"
ADDR_TB_P2TR = "tb1pqqqsyqcyq5rqwzqfpg9scrgwpugpzysnzs23v9ccrydpk8qarc0slua5fd"
ADDR_TESTNET_P2PKH = "mipcBbFg9gMiCh81Kj8tqqdgoZub1ZJRfn"
ADDR_TESTNET_P2SH = "2N1SP7r92ZZJvYKG2oNtzPwYnzw62up7mTo"
ADDR_MAINNET_BECH32 = "bc1qw508d6qejxtdg4y5r3zarvary0c5xw7kv8f3t4"
ADDR_MAINNET_P2PKH = "1BvBMSEYstWetqTFn5Au4m4GFg7xJaNVN2"


# --- approval functions -----------------------------------------------------


def _ctx(amount_raw: str) -> FaucetContext:
    return FaucetContext(
        net_key="regtest", chain="regtest", address="x",
        amount_raw=amount_raw, balance_sat=None,
    )


@pytest.mark.parametrize(
    "amount,approved",
    [
        ("0.5", True),
        ("0.00000001", True),
        ("0.999999999", True),   # policy accepts; sat-conversion guard rejects later
        ("1", False),
        ("1.5", False),
        ("0", False),
        ("-1", False),
        ("abc", False),
        ("nan", False),
        ("inf", False),
        ("", False),
    ],
)
def test_max_one_btc_policy(amount, approved):
    decision = get("max_one_btc")(_ctx(amount))
    assert decision.approved is approved
    assert decision.reason  # always carries a user-facing reason


def test_approval_registry():
    assert "max_one_btc" in names()
    assert is_registered("max_one_btc")
    assert not is_registered("does_not_exist")
    with pytest.raises(KeyError):
        get("does_not_exist")


# --- sat conversion ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.5", 50_000_000),
        ("1", 100_000_000),
        ("0.00000001", 1),
        ("0.999999999", None),  # sub-satoshi precision -> rejected
        ("0", None),
        ("-1", None),
        ("abc", None),
        ("nan", None),
    ],
)
def test_btc_to_sats(raw, expected):
    assert _btc_to_sats(raw) == expected


def test_sats_to_btc():
    assert _sats_to_btc(50_000_000) == "0.50000000"
    assert _sats_to_btc(None) is None


# --- address validation -----------------------------------------------------


def test_address_valid_per_family():
    assert is_valid_address(ADDR_REGTEST_P2WPKH, "regtest")
    for chain in ("test", "testnet4", "signet"):
        assert is_valid_address(ADDR_TB_P2WPKH, chain)
        assert is_valid_address(ADDR_TB_P2TR, chain)
        assert is_valid_address(ADDR_TESTNET_P2PKH, chain)
        assert is_valid_address(ADDR_TESTNET_P2SH, chain)


def test_address_rejects_wrong_family_and_garbage():
    # regtest and testnet-class bech32 HRPs don't cross.
    assert not is_valid_address(ADDR_TB_P2WPKH, "regtest")
    assert not is_valid_address(ADDR_REGTEST_P2WPKH, "signet")
    # mainnet addresses are never valid here.
    assert not is_valid_address(ADDR_MAINNET_BECH32, "signet")
    assert not is_valid_address(ADDR_MAINNET_P2PKH, "test")
    # junk.
    for junk in ("", "   ", "not an address", "tb1qbadchecksum", "bcrt1!"):
        assert not is_valid_address(junk, "regtest")


# --- config -----------------------------------------------------------------


def test_faucet_config_defaults():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}}))
    net = cfg.networks["regtest"]
    assert net.faucet.enabled is True
    assert net.faucet.approval_function is None
    assert net.faucet.fee_sat_per_vbyte == 2
    assert cfg.global_.faucet_default_approval == "max_one_btc"
    # Resolution falls back to the global default.
    assert cfg.faucet_approval_name("regtest") == "max_one_btc"
    assert [k for k, _ in cfg.faucet_networks()] == ["regtest"]


def test_faucet_networks_excludes_disabled():
    cfg = validated(
        make(
            {
                "regtest": {"enabled": True, "bitcart": {"enabled": False}},
                "signet": {
                    "enabled": True, "bitcart": {"enabled": False},
                    "faucet": {"enabled": False},
                },
                "testnet4": {"enabled": False, "bitcart": {"enabled": False}},
            }
        )
    )
    # Only enabled networks with the faucet on.
    assert [k for k, _ in cfg.faucet_networks()] == ["regtest"]


def test_faucet_rejects_unknown_approval(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False},
                             "faucet": {"approval_function": "nope"}}})
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises((ConfigError, ValueError)):
        load_config(p)


def test_global_faucet_default_must_be_known(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}})
    data["global"]["faucet_default_approval"] = "bogus"
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    with pytest.raises((ConfigError, ValueError)):
        load_config(p)


def test_per_network_approval_override():
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": {"enabled": False},
                          "faucet": {"approval_function": "max_one_btc"}}})
    )
    assert cfg.faucet_approval_name("regtest") == "max_one_btc"


# --- generated artefacts ----------------------------------------------------


def _web_compose(tmp_path, data):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_path)
    web_dir = generate_web(cfg, tmp_path / "g", cfg_path)
    return yaml.safe_load((web_dir / "docker-compose.yml").read_text())


def test_web_compose_has_faucet_service(tmp_path):
    data = make(
        {
            "regtest": {"enabled": True, "bitcart": {"enabled": False}},
            "custom-signet-short": {"enabled": True, "bitcart": {"enabled": False}},
        }
    )
    compose = _web_compose(tmp_path, data)
    faucet = compose["services"]["faucet"]
    assert faucet["container_name"] == "argus-faucet"
    assert "argus.faucet.wsgi:app" in faucet["command"]
    # Reads each faucet network's LND volume read-only for the admin macaroon.
    assert "argus-regtest_lnd_data:/lnd/regtest:ro" in faucet["volumes"]
    assert "argus-custom-signet-short_lnd_data:/lnd/custom-signet-short:ro" in faucet["volumes"]
    # External volumes are declared so they resolve to the per-net stacks.
    assert compose["volumes"]["argus-regtest_lnd_data"]["external"] is True
    assert "faucet_data" in compose["volumes"]
    # Joins each faucet network plus web (for the socket-proxy donation read).
    assert set(faucet["networks"]) == {"web", "net-regtest", "net-custom-signet-short"}


def test_web_compose_no_faucet_when_all_disabled(tmp_path):
    data = make(
        {"regtest": {"enabled": True, "bitcart": {"enabled": False},
                     "faucet": {"enabled": False}}}
    )
    data["web"] = {"lnurl": {"enabled": False}}
    compose = _web_compose(tmp_path, data)
    assert "faucet" not in compose["services"]
    assert "faucet_data" not in compose.get("volumes", {})


def test_caddy_faucet_route(tmp_path):
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}}, hostname="x.com")
    )
    out = render_caddyfile(cfg, allocate(cfg))
    assert "@faucet path /regtest/faucet /regtest/faucet/*" in out
    assert "handle @faucet {" in out
    assert "reverse_proxy 127.0.0.1:29081" in out  # FAUCET_BACKEND_PORT
    assert "reverse_proxy 127.0.0.1:29080" in out  # dashboard fallback


def test_caddy_no_faucet_route_when_disabled(tmp_path):
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": {"enabled": False},
                          "faucet": {"enabled": False}}}, hostname="x.com")
    )
    out = render_caddyfile(cfg, allocate(cfg))
    assert "@faucet" not in out
    assert "reverse_proxy 127.0.0.1:29080" in out


def test_reset_sh_has_faucet_hook(tmp_path):
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}})
    )
    generate_reset(cfg, tmp_path)
    script = (tmp_path / "regtest" / "reset.sh").read_text()
    assert "argus.faucet.reset" in script
    assert 'docker exec argus-faucet' in script


def test_reset_sh_no_faucet_hook_when_disabled(tmp_path):
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": {"enabled": False},
                          "faucet": {"enabled": False}}})
    )
    generate_reset(cfg, tmp_path)
    script = (tmp_path / "regtest" / "reset.sh").read_text()
    assert "argus.faucet.reset" not in script


# --- store ------------------------------------------------------------------


def test_store_record_recent_purge(tmp_path):
    db = tmp_path / "faucet.db"
    store.init_db(str(db))
    store.purge("regtest")  # clean slate (global singleton across tests)
    store.purge("signet")
    store.record("regtest", "tx1", "0.10000000", "addrA", ts=1.0)
    store.record("regtest", "tx2", "0.20000000", "addrB", ts=2.0)
    store.record("signet", "tx3", "0.30000000", "addrC", ts=3.0)
    recent = store.recent("regtest", 10)
    assert [p.txid for p in recent] == ["tx2", "tx1"]  # newest first
    assert len(store.recent("regtest", 1)) == 1
    # Purging one network leaves the others untouched.
    assert store.purge("regtest") == 2
    assert store.recent("regtest", 10) == []
    assert [p.txid for p in store.recent("signet", 10)] == ["tx3"]


# --- Flask app end-to-end (stubbed LND) -------------------------------------


class _FakeLnd:
    """Stand-in for argus.faucet.lnd.FaucetLnd in app tests."""

    sent: list[tuple] = []

    def __init__(self, net_key, chain):
        self.net_key = net_key

    def balance_sat(self):
        return 500_000_000  # 5 BTC

    def send(self, address, amount_sat, sat_per_vbyte):
        _FakeLnd.sent.append((self.net_key, address, amount_sat, sat_per_vbyte))
        return "abc123txid"


@pytest.fixture
def faucet_client(tmp_path, monkeypatch):
    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}},
                hostname="x.com")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    _FakeLnd.sent = []
    monkeypatch.setattr("argus.faucet.app.FaucetLnd", _FakeLnd)
    app = create_app(str(cfg_path), db_path=str(tmp_path / "faucet.db"))
    store.purge("regtest")
    app.config.update(TESTING=True)
    return app.test_client()


def test_get_faucet_page(faucet_client):
    resp = faucet_client.get("/regtest/faucet")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "Faucet" in body
    assert "5.00000000" in body  # balance shown in BTC
    assert "less than 1 BTC" in body  # the approval explanation


def test_unknown_network_404(faucet_client):
    assert faucet_client.get("/testnet4/faucet").status_code == 404


def test_post_success_dispenses_and_records(faucet_client):
    # 0.001 BTC is within the fresh-faucet daily cap (5 BTC / 3650 ≈ 0.00137 BTC).
    resp = faucet_client.post(
        "/regtest/faucet",
        data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.001"},
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "abc123txid" in body
    assert _FakeLnd.sent == [("regtest", ADDR_REGTEST_P2WPKH, 100_000, 2)]
    # Recorded and shown in the recent table.
    assert "0.00100000" in body


def test_post_invalid_address(faucet_client):
    resp = faucet_client.post(
        "/regtest/faucet", data={"address": ADDR_TB_P2WPKH, "amount": "0.25"}
    )
    body = resp.get_data(as_text=True)
    assert "not a valid address" in body
    assert _FakeLnd.sent == []  # nothing sent


def test_post_disapproved_over_one_btc(faucet_client):
    resp = faucet_client.post(
        "/regtest/faucet", data={"address": ADDR_REGTEST_P2WPKH, "amount": "2"}
    )
    body = resp.get_data(as_text=True)
    assert "less than 1 BTC" in body
    assert _FakeLnd.sent == []


def test_post_subsatoshi_rejected(faucet_client):
    resp = faucet_client.post(
        "/regtest/faucet",
        data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.999999999"},
    )
    body = resp.get_data(as_text=True)
    assert "8 decimal places" in body
    assert _FakeLnd.sent == []


# --- speed-limit store helpers ----------------------------------------------


def _fresh_store(tmp_path):
    store.init_db(str(tmp_path / "faucet.db"))
    # The peewee DB is a process-global singleton; wipe the rule tables so a test
    # that reuses a prior test's binding still starts clean.
    store.IpClaim.delete().execute()
    store.DailyUsage.delete().execute()
    return store


NOW = 1_000_000_000.0


def test_ip_claim_record_last_and_purge(tmp_path):
    s = _fresh_store(tmp_path)
    assert s.last_ip_claim("regtest", "h1") is None
    s.record_ip_claim("regtest", "h1", ts=NOW)
    assert s.last_ip_claim("regtest", "h1") == NOW
    # Upsert keeps a single row and advances the timestamp.
    s.record_ip_claim("regtest", "h1", ts=NOW + 100)
    assert s.last_ip_claim("regtest", "h1") == NOW + 100
    assert s.IpClaim.select().where(s.IpClaim.ip_hash == "h1").count() == 1
    # Scoped per network.
    assert s.last_ip_claim("signet", "h1") is None
    # Purge drops rows older than 24h.
    s.record_ip_claim("regtest", "old", ts=NOW - 86_400 - 1)
    removed = s.purge_ip_claims(now=NOW + 100)
    assert removed == 1
    assert s.last_ip_claim("regtest", "old") is None
    assert s.last_ip_claim("regtest", "h1") == NOW + 100


def test_usage_stats_formula(tmp_path):
    s = _fresh_store(tmp_path)
    # Fresh: every day is a missing day filled with the floor of 10 -> 365*10.
    assert s.usage_stats("regtest", now=NOW) == (3650, 0)
    # One withdrawal today: today contributes its actual 1, the other 364 the floor.
    s.increment_usage("regtest", ts=NOW)
    assert s.usage_stats("regtest", now=NOW) == (1 + 364 * 10, 1)
    # Twelve today: floor rises to max(busiest day, 10) = 12, so 12 every day.
    for _ in range(11):
        s.increment_usage("regtest", ts=NOW)
    assert s.usage_stats("regtest", now=NOW) == (12 * 365, 12)
    # Usage is per network.
    assert s.usage_stats("signet", now=NOW) == (3650, 0)


def test_purge_usage_drops_old_days(tmp_path):
    s = _fresh_store(tmp_path)
    s.increment_usage("regtest", ts=NOW)  # today
    s.increment_usage("regtest", ts=NOW - 400 * 86_400)  # >1 year ago
    assert s.DailyUsage.select().count() == 2
    removed = s.purge_usage(now=NOW)
    assert removed == 1
    assert s.DailyUsage.select().count() == 1


def test_claim_maintenance_run_is_once_per_day(tmp_path):
    s = _fresh_store(tmp_path)
    assert s.claim_maintenance_run(now=NOW) is True
    # A second claim the same day loses.
    assert s.claim_maintenance_run(now=NOW + 10) is False
    # A day later it wins again.
    assert s.claim_maintenance_run(now=NOW + 86_400) is True


# --- IP hashing -------------------------------------------------------------


def test_hash_ip_deterministic_and_salted():
    from argus.faucet.ip import canonical_ip, hash_ip

    a = hash_ip("1.2.3.4", "salt")
    assert a == hash_ip("1.2.3.4", "salt")  # deterministic
    assert a != hash_ip("1.2.3.5", "salt")  # different IP
    assert a != hash_ip("1.2.3.4", "other")  # different salt
    # IPv6 is canonicalised so equivalent forms hash alike.
    assert hash_ip("::1", "s") == hash_ip("0:0:0:0:0:0:0:1", "s")
    assert canonical_ip("nonsense") is None
    # No IP or no salt => no hash (the per-IP rule then fails open).
    assert hash_ip(None, "salt") is None
    assert hash_ip("1.2.3.4", None) is None


# --- rule evaluation --------------------------------------------------------


def _faucet_cfg(**overrides):
    from argus.config import FaucetCfg

    return FaucetCfg(**overrides)


def _limits(cfg, balance):
    from argus.faucet import rules

    return rules.compute_limits(cfg, "regtest", balance, NOW)


def _rule_ctx(cfg, balance, requested_sat, ip_hash="h"):
    from argus.faucet import rules

    return rules.RuleContext(
        net_key="regtest",
        ip_hash=ip_hash,
        requested_sat=requested_sat,
        balance_sat=balance,
        now=NOW,
        limits=_limits(cfg, balance),
    )


def test_compute_limits_fresh_faucet(tmp_path):
    _fresh_store(tmp_path)
    cfg = _faucet_cfg()
    lim = _limits(cfg, 500_000_000)  # 5 BTC
    assert lim.daily_cap_sat == 500_000_000 // 3650  # 136986
    assert lim.balance_cap_sat == 50_000_000  # 10%
    assert lim.min_claim_sat == 5000
    assert lim.max_request_sat == min(136_986, 50_000_000)


def test_compute_limits_balance_unknown(tmp_path):
    _fresh_store(tmp_path)
    cfg = _faucet_cfg()
    lim = _limits(cfg, None)
    # Balance-derived caps drop out; min-claim still stands.
    assert lim.daily_cap_sat is None
    assert lim.balance_cap_sat is None
    assert lim.max_request_sat is None
    assert lim.min_claim_sat == 5000


def test_rule_min_claim(tmp_path):
    from argus.faucet import rules

    _fresh_store(tmp_path)
    cfg = _faucet_cfg()
    fails = rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 4999))
    assert [f.label for f in fails] == ["Minimum claim"]
    assert rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 5000)) == []


def test_rule_balance_cap(tmp_path):
    from argus.faucet import rules

    _fresh_store(tmp_path)
    cfg = _faucet_cfg(max_amount_per_day=False)  # isolate the balance cap
    # 60M > 10% of 500M (=50M).
    fails = rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 60_000_000))
    assert [f.label for f in fails] == ["Per-request balance cap"]


def test_rule_one_per_ip_per_day_and_retry(tmp_path):
    from argus.faucet import rules

    s = _fresh_store(tmp_path)
    cfg = _faucet_cfg()
    s.record_ip_claim("regtest", "h", ts=NOW - 3600)  # claimed an hour ago
    fails = rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 100_000))
    assert [f.label for f in fails] == ["One claim per 24 hours"]
    assert fails[0].retry_after == NOW - 3600 + 86_400
    # A different IP is unaffected.
    assert rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 100_000, ip_hash="other")) == []
    # Unknown IP (no salt) fails open.
    assert rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 100_000, ip_hash=None)) == []


def test_rules_aggregate_all_failures(tmp_path):
    from argus.faucet import rules

    _fresh_store(tmp_path)
    cfg = _faucet_cfg()
    # 5 BTC: over the daily cap AND over the per-request balance cap at once.
    fails = rules.evaluate(cfg, _rule_ctx(cfg, 500_000_000, 500_000_000))
    labels = {f.label for f in fails}
    assert "Daily maximum" in labels
    assert "Per-request balance cap" in labels


# --- self-mine notice -------------------------------------------------------


def test_faucet_mine_help_per_chain():
    from argus.web.content import faucet_mine_help

    ports = {"bitcoind_p2p": 18444}
    regtest = faucet_mine_help("regtest", "x.com", ports)
    assert regtest is not None and "generatetoaddress" in regtest.command
    assert "-regtest" in regtest.command
    t3 = faucet_mine_help("testnet3", "x.com", {"bitcoind_p2p": 18333})
    assert t3 is not None and "while" in t3.command and "-chain=test" in t3.command
    # Signets need the operator's signing key — no self-mine notice.
    assert faucet_mine_help("signet", "x.com", {"bitcoind_p2p": 38333}) is None
    # No public P2P port => can't peer => no notice.
    assert faucet_mine_help("regtest", "x.com", ports, p2p_public=False) is None


# --- maintenance ------------------------------------------------------------


def test_run_maintenance_purges(tmp_path):
    from argus.faucet import maintenance

    s = _fresh_store(tmp_path)
    s.record_ip_claim("regtest", "old", ts=NOW - 200_000)
    s.increment_usage("regtest", ts=NOW - 400 * 86_400)
    ip_removed, usage_removed = maintenance.run_maintenance(now=NOW)
    assert ip_removed == 1
    assert usage_removed == 1


# --- Flask app: speed limits end-to-end -------------------------------------


def test_post_over_daily_cap_lists_all_unmet_rules(faucet_client):
    # 5 BTC trips the amount policy, the daily maximum, and the balance cap.
    resp = faucet_client.post(
        "/regtest/faucet", data={"address": ADDR_REGTEST_P2WPKH, "amount": "5"}
    )
    body = resp.get_data(as_text=True)
    assert "Amount policy" in body
    assert "Daily maximum" in body
    assert "Per-request balance cap" in body
    assert _FakeLnd.sent == []


def test_post_below_min_claim(faucet_client):
    resp = faucet_client.post(
        "/regtest/faucet",
        data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.00000001"},  # 1 sat
    )
    body = resp.get_data(as_text=True)
    assert "Minimum claim" in body
    assert _FakeLnd.sent == []


def test_one_per_ip_blocks_second_claim(tmp_path, monkeypatch):
    monkeypatch.setattr("argus.faucet.app.FaucetLnd", _FakeLnd)
    monkeypatch.setattr("argus.faucet.app._IP_SALT", "test-salt")
    _FakeLnd.sent = []
    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}},
                hostname="x.com")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    app = create_app(str(cfg_path), db_path=str(tmp_path / "faucet.db"),
                     start_maintenance=False)
    app.config.update(TESTING=True)
    client = app.test_client()

    hdr = {"X-Forwarded-For": "203.0.113.7"}
    ok = client.post("/regtest/faucet",
                     data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.001"},
                     headers=hdr)
    assert "abc123txid" in ok.get_data(as_text=True)
    # Second claim from the same IP is blocked with a retry hint.
    again = client.post("/regtest/faucet",
                        data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.001"},
                        headers=hdr)
    body = again.get_data(as_text=True)
    assert "One claim per 24 hours" in body
    assert "try again in" in body
    # A different IP can still claim.
    other = client.post("/regtest/faucet",
                        data={"address": ADDR_REGTEST_P2WPKH, "amount": "0.001"},
                        headers={"X-Forwarded-For": "203.0.113.8"})
    assert "abc123txid" in other.get_data(as_text=True)


def test_faucet_page_shows_limits_and_mine_notice(faucet_client):
    body = faucet_client.get("/regtest/faucet").get_data(as_text=True)
    # Limits panel: the fresh-faucet daily cap in BTC and sats.
    assert "Request limits" in body
    assert "0.00136986" in body  # 5 BTC / 3650, in BTC
    assert "136,986" in body  # …and in sats
    assert "5,000" in body  # minimum claim in sats
    # Regtest is self-mineable, so the notice and its recipe appear.
    assert "Mine your own" in body
    assert "generatetoaddress" in body


def test_web_compose_injects_ip_salt(tmp_path):
    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}})
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_path)
    web_dir = generate_web(cfg, tmp_path / "g", cfg_path, None, "deadbeefsalt")
    compose = yaml.safe_load((web_dir / "docker-compose.yml").read_text())
    assert compose["services"]["faucet"]["environment"]["FAUCET_IP_SALT"] == "deadbeefsalt"
