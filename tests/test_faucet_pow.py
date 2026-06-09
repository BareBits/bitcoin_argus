"""Proof-of-work faucet: unit tests for the difficulty math and the signed
challenge protocol, plus end-to-end "earn an extra claim" flows that claim from
every faucet through the HTTP endpoints using an arbitrarily low PoW difficulty.

The hermetic tests use the ``sha256d`` primitive (pure Python/JS, no toolchain);
the production ``yespower`` primitive runs the same code in the browser (WASM)
and the server (wasmtime) and is exercised on the test VPS where the artefact is
built. Difficulty is independent of the primitive — it's a 256-bit target — so a
trivially low target lets these tests solve a real challenge in one hash."""

from __future__ import annotations

import time

import pytest
import yaml

from argus.config import FaucetCfg, PowCfg
from argus.faucet import difficulty as difficulty_mod
from argus.faucet import pow as pow_mod
from argus.faucet import rules as rules_mod
from argus.faucet import store
from argus.faucet.app import create_app
from helpers import make

# Valid example addresses by chain family (from test_faucet.py).
ADDR_REGTEST = "bcrt1qqqqsyqcyq5rqwzqfpg9scrgwpugpzysnard0ew"
ADDR_TB = "tb1qqqqsyqcyq5rqwzqfpg9scrgwpugpzysnl25zw8"

# Every chain a default install can run a faucet on, with a valid address each,
# and whether that chain is value-pegged (testnet3 only).
CHAINS = {
    "regtest": ADDR_REGTEST,
    "testnet3": ADDR_TB,
    "testnet4": ADDR_TB,
    "signet": ADDR_TB,
    "custom-signet-short": ADDR_TB,
}

NOW = 1_000_000_000.0


# --------------------------------------------------------------------------
# Difficulty regime selection + target math
# --------------------------------------------------------------------------


def _pow(**over) -> PowCfg:
    return PowCfg(**over)


def test_value_pegged_resolution():
    p = _pow()  # value_pegged=None => auto
    assert pow_mod.is_value_pegged(p, "testnet3") is True
    assert pow_mod.is_value_pegged(p, "signet") is False
    assert pow_mod.is_value_pegged(p, "regtest") is False
    # Explicit override wins.
    assert pow_mod.is_value_pegged(_pow(value_pegged=True), "signet") is True
    assert pow_mod.is_value_pegged(_pow(value_pegged=False), "testnet3") is False


def test_max_per_day_resolution():
    p = _pow()  # auto
    assert pow_mod.max_per_day(p, "testnet3") == 1  # value-pegged => one a day
    assert pow_mod.max_per_day(p, "signet") == 0  # unlimited otherwise
    assert pow_mod.max_per_day(_pow(max_per_day=5), "signet") == 5


def test_flat_target_scales_with_amount():
    p = _pow(balance_anchor=False, demand_retarget=False, seconds_per_100k=600)
    s1 = pow_mod.compute_target_seconds(p, "signet", 100_000, None, 0, None)
    s2 = pow_mod.compute_target_seconds(p, "signet", 200_000, None, 0, None)
    assert s1 == pytest.approx(600)
    assert s2 == pytest.approx(1200)


def test_balance_anchor_raises_difficulty_as_faucet_drains():
    p = _pow(demand_retarget=False, balance_full_sat=100_000_000, balance_max_mult=8,
             max_seconds=1e9)
    full = pow_mod.compute_target_seconds(p, "signet", 100_000, 100_000_000, 0, None)
    empty = pow_mod.compute_target_seconds(p, "signet", 100_000, 0, 0, None)
    half = pow_mod.compute_target_seconds(p, "signet", 100_000, 50_000_000, 0, None)
    assert full == pytest.approx(600)  # at/above "full" => no extra
    assert empty == pytest.approx(600 * 8)  # drained => max multiplier
    assert half == pytest.approx(600 * 4.5)  # linear midpoint (1 + 7*0.5)


def test_demand_retarget_raises_difficulty_under_spam():
    p = _pow(balance_anchor=False, demand_target_per_day=50, demand_max_mult=16,
             max_seconds=1e9)
    calm = pow_mod.compute_target_seconds(p, "signet", 100_000, None, 0, None)
    busy = pow_mod.compute_target_seconds(p, "signet", 100_000, None, 25, None)
    swarm = pow_mod.compute_target_seconds(p, "signet", 100_000, None, 5000, None)
    assert calm == pytest.approx(600)  # no demand => factor 1
    assert busy == pytest.approx(600 * 1)  # below target it never drops below 1
    assert swarm == pytest.approx(600 * 16)  # clamped at the max multiplier


def test_value_pegged_needs_subsidy_and_is_capped():
    p = _pow(value_safety_factor=2.0, value_cap_seconds=1800,
             reference_sha256d_hps=15_000_000, min_seconds=0)
    # No subsidy data => no target (caller disables PoW for the request).
    assert pow_mod.compute_target_seconds(p, "testnet3", 100_000, None, 0, None) is None
    # With a subsidy: 2 * amount * (2**32/subsidy) / sha_hps, capped at 1800.
    raw = pow_mod.compute_target_seconds(p, "testnet3", 1_000, None, 0, 596)
    expected = 2.0 * 1_000 * (2**32) / 596 / 15_000_000
    assert raw == pytest.approx(min(expected, 1800))
    # A large amount saturates the cap.
    big = pow_mod.compute_target_seconds(p, "testnet3", 100_000, None, 0, 596)
    assert big == pytest.approx(1800)


def test_seconds_to_target_is_monotonic_and_bounded():
    hps = 1000.0
    t_easy, h_easy = pow_mod.seconds_to_target(1, hps)
    t_hard, h_hard = pow_mod.seconds_to_target(10, hps)
    assert h_easy == pytest.approx(1000) and h_hard == pytest.approx(10000)
    assert t_hard < t_easy  # more work => a smaller (harder) threshold
    assert t_easy < (1 << 256)


def test_effective_ttl_covers_solve_time():
    assert pow_mod.effective_ttl(10, 1800) == 1800  # floor wins for easy work
    assert pow_mod.effective_ttl(2000, 1800) == 4000  # >= 2x the solve estimate


# --------------------------------------------------------------------------
# The signed challenge token
# --------------------------------------------------------------------------

SECRET = "test-secret"


def _issue(**over):
    kw = dict(
        secret=SECRET, net="signet", address=ADDR_TB, amount_sat=100_000,
        algorithm="sha256d", target=(1 << 256) - 1, ttl=300,
        nonce="abc", now=NOW,
    )
    kw.update(over)
    return pow_mod.issue(**kw)


def _verify(token, **over):
    kw = dict(
        secret=SECRET, net="signet", address=ADDR_TB, amount_sat=100_000, now=NOW
    )
    kw.update(over)
    return pow_mod.verify(token, **kw)


def test_challenge_roundtrip():
    ch = _verify(_issue())
    assert ch.net == "signet" and ch.address == ADDR_TB and ch.amount_sat == 100_000
    assert ch.algorithm == "sha256d" and ch.nonce == "abc"
    assert ch.target == (1 << 256) - 1


def test_challenge_rejects_tampering():
    token = _issue()
    # Flip a character in the payload => signature mismatch.
    bad = ("A" if token[0] != "A" else "B") + token[1:]
    with pytest.raises(pow_mod.PowError):
        _verify(bad)
    # Wrong secret.
    with pytest.raises(pow_mod.PowError):
        _verify(token, secret="other")


def test_challenge_binds_request():
    token = _issue()
    with pytest.raises(pow_mod.PowError):
        _verify(token, address=ADDR_REGTEST)  # different address
    with pytest.raises(pow_mod.PowError):
        _verify(token, amount_sat=200_000)  # different amount
    with pytest.raises(pow_mod.PowError):
        _verify(token, net="testnet4")  # different network


def test_challenge_expires():
    token = _issue(ttl=300, now=NOW)
    _verify(token, now=NOW + 299)  # still valid
    with pytest.raises(pow_mod.PowError):
        _verify(token, now=NOW + 301)  # past TTL


def test_check_solution_against_target():
    # Trivial target: every hash is below it.
    easy = _issue(target=(1 << 256) - 1)
    ch = _verify(easy)
    assert pow_mod.check_solution(easy, "0", ch) is True
    # Impossible target: no hash is below zero.
    hard = _issue(target=0)
    ch2 = _verify(hard)
    assert pow_mod.check_solution(hard, "0", ch2) is False
    # Malformed solutions are rejected.
    for bad in ("", "x" * 200):
        with pytest.raises(pow_mod.PowError):
            pow_mod.check_solution(easy, bad, ch)


# --------------------------------------------------------------------------
# store: single-use nonces + per-day PoW counts
# --------------------------------------------------------------------------


def _fresh_store(tmp_path):
    store.init_db(str(tmp_path / "faucet.db"))
    store.RedeemedNonce.delete().execute()
    store.PowDailyClaim.delete().execute()
    store.IpClaim.delete().execute()
    store.DailyUsage.delete().execute()
    return store


def test_redeem_nonce_is_single_use(tmp_path):
    s = _fresh_store(tmp_path)
    assert s.redeem_nonce("signet", "n1", NOW + 300) is True
    assert s.redeem_nonce("signet", "n1", NOW + 300) is False  # replay rejected
    assert s.redeem_nonce("testnet4", "n1", NOW + 300) is True  # scoped per net
    # Purge drops expired rows.
    assert s.redeem_nonce("signet", "old", NOW - 10) is True
    assert s.purge_redeemed_nonces(now=NOW) == 1
    assert s.redeem_nonce("signet", "old", NOW + 300) is True  # reusable after purge


def test_pow_daily_claim_counts(tmp_path):
    s = _fresh_store(tmp_path)
    assert s.pow_claims_today("testnet3", "h", NOW) == 0
    s.record_pow_claim("testnet3", "h", NOW)
    s.record_pow_claim("testnet3", "h", NOW)
    assert s.pow_claims_today("testnet3", "h", NOW) == 2
    # Per (net, ip, day).
    assert s.pow_claims_today("testnet3", "other", NOW) == 0
    assert s.pow_claims_today("signet", "h", NOW) == 0
    assert s.pow_claims_today("testnet3", "h", NOW + 86_400) == 0  # next day resets


# --------------------------------------------------------------------------
# rules: a valid PoW overrides the daily limit but the PoW cap still applies
# --------------------------------------------------------------------------


def _ctx(**over):
    cfg = FaucetCfg()
    base = dict(
        net_key="signet", ip_hash="h", requested_sat=10_000, balance_sat=500_000_000,
        now=NOW, limits=rules_mod.compute_limits(cfg, "signet", 500_000_000, NOW),
    )
    base.update(over)
    return cfg, rules_mod.RuleContext(**base)


def test_pow_overrides_one_per_day(tmp_path):
    s = _fresh_store(tmp_path)
    s.record_ip_claim("signet", "h", ts=NOW - 3600)  # already claimed within 24h
    cfg, ctx = _ctx(pow_verified=False)
    assert any(o.label == "One claim per 24 hours" for o in rules_mod.evaluate(cfg, ctx))
    # A valid proof clears that limit.
    cfg, ctx = _ctx(pow_verified=True)
    assert not any(
        o.label == "One claim per 24 hours" for o in rules_mod.evaluate(cfg, ctx)
    )


def test_pow_daily_cap_rule(tmp_path):
    _fresh_store(tmp_path)
    # testnet3-style: one PoW claim a day; the second is blocked.
    cfg, ctx = _ctx(net_key="testnet3", pow_verified=True, pow_max_per_day=1,
                    pow_claims_today=1)
    fails = rules_mod.evaluate(cfg, ctx)
    assert [o.label for o in fails] == ["Daily proof-of-work limit"]
    assert fails[0].retry_after is not None
    # Unlimited (cap 0) never blocks.
    cfg, ctx = _ctx(pow_verified=True, pow_max_per_day=0, pow_claims_today=99)
    assert not any(o.label == "Daily proof-of-work limit" for o in rules_mod.evaluate(cfg, ctx))


# --------------------------------------------------------------------------
# End-to-end: claim from every faucet, free then PoW-earned
# --------------------------------------------------------------------------


class _FakeLnd:
    """Stand-in node: ample balance, a unique txid per send."""

    sent: list = []
    _n = 0

    def __init__(self, net_key, chain):
        self.net_key = net_key

    def balance_sat(self):
        return 500_000_000  # 5 BTC

    def send(self, address, amount_sat, sat_per_vbyte):
        _FakeLnd._n += 1
        txid = f"txid{_FakeLnd._n}"
        _FakeLnd.sent.append((self.net_key, address, amount_sat, txid))
        return txid


# Arbitrarily low PoW so any challenge solves in one hash: expected hashes =
# target_seconds x reference_hps. With a tiny reference rate (1e-6 H/s) every
# regime's target_seconds (flat base, or the value-pegged cap) maps to < 1
# expected hash, i.e. a near-2**256 (trivial) threshold.
_TEST_POW = {
    "enabled": True,
    "algorithm": "sha256d",
    "min_seconds": 0.0,
    "seconds_per_100k": 1.0,
    "balance_anchor": False,
    "demand_retarget": False,
    "reference_sha256d_hps": 1e-6,
    "value_cap_seconds": 1800.0,
    "ttl_seconds": 300,
}


@pytest.fixture
def pow_client(tmp_path, monkeypatch):
    networks = {
        net: {
            "enabled": True,
            "bitcart": {"enabled": False},
            "faucet": {"pow": dict(_TEST_POW)},
        }
        for net in CHAINS
    }
    data = make(networks, hostname="x.com")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))

    _FakeLnd.sent = []
    _FakeLnd._n = 0
    monkeypatch.setattr("argus.faucet.app.FaucetLnd", _FakeLnd)
    # Enable PoW (needs a signing secret + the salted per-IP limit to be active).
    monkeypatch.setattr("argus.faucet.app._IP_SALT", "test-salt")
    monkeypatch.setattr("argus.faucet.app._POW_SECRET", "test-pow-secret")
    # testnet3 is value-pegged => give it a block subsidy without touching mempool.
    monkeypatch.setattr(difficulty_mod, "block_subsidy_sat", lambda net, **k: 596)
    difficulty_mod._reset_cache()

    app = create_app(str(cfg_path), db_path=str(tmp_path / "faucet.db"),
                     start_maintenance=False)
    app.config.update(TESTING=True)
    for net in CHAINS:
        store.purge(net)
    store.RedeemedNonce.delete().execute()
    store.PowDailyClaim.delete().execute()
    store.IpClaim.delete().execute()
    store.DailyUsage.delete().execute()
    return app.test_client()


def _solve(token: str, target_hex: str) -> str:
    """Find a solution for ``token`` against ``target`` using sha256d (the same
    primitive the server verifies with). Trivial difficulty => first try."""
    target = int(target_hex, 16)
    hasher = pow_mod.get_hasher("sha256d")
    for i in range(2_000_000):
        sol = str(i)
        if int.from_bytes(hasher(token.encode() + sol.encode()), "big") < target:
            return sol
    raise AssertionError("no PoW solution found at the test difficulty")


def _earn_claim(client, net, address, amount, ip):
    """Run the browser PoW flow over HTTP: fetch a challenge, solve it, POST it.
    Returns the POST response."""
    hdr = {"X-Forwarded-For": ip}
    ch = client.get(
        f"/{net}/faucet/challenge",
        query_string={"address": address, "amount": amount},
        headers=hdr,
    ).get_json()
    assert ch["available"] is True, f"{net}: challenge not available: {ch}"
    solution = _solve(ch["token"], ch["target"])
    return client.post(
        f"/{net}/faucet",
        data={"address": address, "amount": amount,
              "pow_token": ch["token"], "pow_solution": solution},
        headers=hdr,
    )


@pytest.mark.parametrize("net,address", list(CHAINS.items()))
def test_e2e_free_then_pow_claim(pow_client, net, address):
    """Each faucet: the first claim is free; a second without PoW is blocked; a
    second WITH a solved PoW succeeds."""
    amount = "0.0001"  # 10,000 sats — above the 5,000 sat floor, under the caps
    ip = "203.0.113.10"
    hdr = {"X-Forwarded-For": ip}

    # 1. The free daily claim (no PoW) dispenses.
    free = pow_client.post(
        f"/{net}/faucet", data={"address": address, "amount": amount}, headers=hdr
    )
    assert "Funds dispensed" in free.get_data(as_text=True), net
    sent_after_free = len(_FakeLnd.sent)

    # 2. A second claim from the same IP without PoW is blocked.
    blocked = pow_client.post(
        f"/{net}/faucet", data={"address": address, "amount": amount}, headers=hdr
    )
    assert "One claim per 24 hours" in blocked.get_data(as_text=True), net
    assert len(_FakeLnd.sent) == sent_after_free  # nothing dispensed

    # 3. The same second claim WITH a solved proof-of-work succeeds.
    earned = _earn_claim(pow_client, net, address, amount, ip)
    assert "Funds dispensed" in earned.get_data(as_text=True), net
    assert len(_FakeLnd.sent) == sent_after_free + 1


def test_e2e_testnet3_caps_pow_at_one_per_day(pow_client):
    """testnet3 (value-pegged) allows exactly one PoW-earned claim per day."""
    net, address, amount, ip = "testnet3", ADDR_TB, "0.0001", "203.0.113.20"
    hdr = {"X-Forwarded-For": ip}
    # Use up the free claim, then earn one via PoW (allowed)...
    pow_client.post(f"/{net}/faucet", data={"address": address, "amount": amount},
                    headers=hdr)
    first = _earn_claim(pow_client, net, address, amount, ip)
    assert "Funds dispensed" in first.get_data(as_text=True)
    # ...a second PoW claim the same day is capped.
    second = _earn_claim(pow_client, net, address, amount, ip)
    assert "Daily proof-of-work limit" in second.get_data(as_text=True)


def test_e2e_signet_allows_repeated_pow_claims(pow_client):
    """A non-value net (signet) has no per-day PoW cap: earn repeatedly."""
    net, address, ip = "signet", ADDR_TB, "203.0.113.30"
    pow_client.post(f"/{net}/faucet", data={"address": address, "amount": "0.0001"},
                    headers={"X-Forwarded-For": ip})
    for _ in range(3):
        r = _earn_claim(pow_client, net, address, "0.0001", ip)
        assert "Funds dispensed" in r.get_data(as_text=True)


def test_e2e_challenge_is_single_use(pow_client):
    """A solved challenge can't be replayed for a second payout."""
    net, address, ip = "signet", ADDR_TB, "203.0.113.40"
    hdr = {"X-Forwarded-For": ip}
    pow_client.post(f"/{net}/faucet", data={"address": address, "amount": "0.0001"},
                    headers=hdr)
    ch = pow_client.get(
        f"/{net}/faucet/challenge",
        query_string={"address": address, "amount": "0.0001"}, headers=hdr,
    ).get_json()
    solution = _solve(ch["token"], ch["target"])
    data = {"address": address, "amount": "0.0001",
            "pow_token": ch["token"], "pow_solution": solution}
    first = pow_client.post(f"/{net}/faucet", data=data, headers=hdr)
    assert "Funds dispensed" in first.get_data(as_text=True)
    # Replaying the very same token+solution is rejected.
    replay = pow_client.post(f"/{net}/faucet", data=data, headers=hdr)
    assert "already been used" in replay.get_data(as_text=True)


def test_e2e_bad_solution_is_rejected(pow_client):
    """A challenge submitted with a wrong solution doesn't dispense."""
    net, address, ip = "signet", ADDR_TB, "203.0.113.50"
    hdr = {"X-Forwarded-For": ip}
    pow_client.post(f"/{net}/faucet", data={"address": address, "amount": "0.0001"},
                    headers=hdr)
    ch = pow_client.get(
        f"/{net}/faucet/challenge",
        query_string={"address": address, "amount": "0.0001"}, headers=hdr,
    ).get_json()
    # Force an impossible target check by tampering the amount the solution binds
    # to: submit the token for a different amount than it was issued for.
    resp = pow_client.post(
        f"/{net}/faucet",
        data={"address": address, "amount": "0.0002",
              "pow_token": ch["token"], "pow_solution": "0"},
        headers=hdr,
    )
    body = resp.get_data(as_text=True)
    assert "Proof of work" in body and "Funds dispensed" not in body


def test_e2e_no_pow_when_disabled(tmp_path, monkeypatch):
    """With PoW off, the challenge endpoint declines and the page omits the UI."""
    networks = {
        "signet": {"enabled": True, "bitcart": {"enabled": False},
                   "faucet": {"pow": {"enabled": False}}},
    }
    data = make(networks, hostname="x.com")
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    monkeypatch.setattr("argus.faucet.app.FaucetLnd", _FakeLnd)
    monkeypatch.setattr("argus.faucet.app._POW_SECRET", "s")
    app = create_app(str(cfg_path), db_path=str(tmp_path / "f.db"),
                     start_maintenance=False)
    app.config.update(TESTING=True)
    client = app.test_client()
    ch = client.get("/signet/faucet/challenge",
                    query_string={"address": ADDR_TB, "amount": "0.0001"}).get_json()
    assert ch["available"] is False
    body = client.get("/signet/faucet").get_data(as_text=True)
    assert 'id="faucet-pow"' not in body  # the solver config block is absent
    assert "faucet.js" not in body  # ...and the controller script isn't included
