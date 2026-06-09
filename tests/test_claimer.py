"""The testnet3/testnet4 min-difficulty block claimer: config validation,
generation wiring, and the block-assembly / difficulty logic in claim.py."""

from __future__ import annotations

import importlib.util
import os

import pytest
import yaml

import argus
from argus.config import ConfigError
from argus.generate import generate
from helpers import make, validated


# --- config validation -------------------------------------------------------


def test_claimer_rejected_off_claim_networks():
    for net in ("regtest", "signet"):
        with pytest.raises(ConfigError, match="claimer"):
            validated(make({net: {"enabled": True, "claimer": {"enabled": True}}}))


def test_claimer_accepted_on_testnets():
    for net in ("testnet3", "testnet4"):
        cfg = validated(
            make({net: {"enabled": True, "bitcart": {"enabled": False},
                        "claimer": {"enabled": True}}})
        )
        assert cfg.networks[net].claimer.enabled


def test_claimer_forward_resolution():
    # None => follow the faucet's enabled state.
    cfg = validated(make({"testnet4": {"enabled": True, "bitcart": {"enabled": False},
                                       "claimer": {"enabled": True},
                                       "faucet": {"enabled": False}}}))
    assert cfg.networks["testnet4"].claimer_forwards() is False

    cfg = validated(make({"testnet4": {"enabled": True, "bitcart": {"enabled": False},
                                       "claimer": {"enabled": True},
                                       "faucet": {"enabled": True}}}))
    assert cfg.networks["testnet4"].claimer_forwards() is True

    # An explicit value always wins over the faucet state.
    cfg = validated(make({"testnet4": {"enabled": True, "bitcart": {"enabled": False},
                                       "claimer": {"enabled": True,
                                                   "forward_to_faucet": False},
                                       "faucet": {"enabled": True}}}))
    assert cfg.networks["testnet4"].claimer_forwards() is False


# --- generation ---------------------------------------------------------------


def _gen(tmp_path, data):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out = tmp_path / "gen"
    generate(str(cfgp), output_dir=out, secrets_dir=tmp_path / "sec")
    return out


def _compose(out, net):
    return yaml.safe_load((out / net / "docker-compose.yml").read_text())


def test_claimer_absent_when_disabled(tmp_path):
    out = _gen(tmp_path, make({"testnet4": {"enabled": True, "bitcart": {"enabled": False}}}))
    assert "claimer" not in _compose(out, "testnet4")["services"]


def test_claimer_service_and_build_context(tmp_path):
    out = _gen(tmp_path, make({"testnet4": {
        "enabled": True, "bitcart": {"enabled": False},
        "claimer": {"enabled": True, "max_difficulty": 1.5}}}))
    svc = _compose(out, "testnet4")["services"]["claimer"]
    assert svc["build"]["context"] == "./claimer"
    assert svc["environment"]["MAX_DIFFICULTY"] == "1.5"
    assert svc["environment"]["WINDOW_SECONDS"] == "1200"
    assert svc["environment"]["STATE_FILE"] == "/state/claimer.json"
    assert "claimer_state:/state" in svc["volumes"]
    # The build context is fully assembled (scripts + vendored test_framework).
    cdir = out / "testnet4" / "claimer"
    for f in ("Dockerfile", "claim.py", "claim.sh"):
        assert (cdir / f).is_file()
    assert (cdir / "test_framework" / "blocktools.py").is_file()


def test_claimer_forwards_when_faucet_on(tmp_path):
    out = _gen(tmp_path, make({"testnet4": {
        "enabled": True, "bitcart": {"enabled": False},
        "claimer": {"enabled": True, "forward_threshold_btc": 0.5},
        "faucet": {"enabled": True}}}))
    svc = _compose(out, "testnet4")["services"]["claimer"]
    assert svc["environment"]["FORWARD"] == "1"
    assert svc["environment"]["FORWARD_THRESHOLD_SAT"] == "50000000"
    assert "lnd_data:/lnd:ro" in svc["volumes"]


def test_claimer_no_forward_when_faucet_off(tmp_path):
    out = _gen(tmp_path, make({"testnet3": {
        "enabled": True, "bitcart": {"enabled": False},
        "claimer": {"enabled": True}, "faucet": {"enabled": False}}}))
    svc = _compose(out, "testnet3")["services"]["claimer"]
    assert svc["environment"]["FORWARD"] == "0"
    assert not any(v.startswith("lnd_data") for v in svc["volumes"])


# --- claim.py block assembly + difficulty -------------------------------------


def _load_claim_module():
    """Import the generated claim.py with the vendored test_framework on path."""
    pkg_dir = os.path.dirname(argus.__file__)
    # test_framework lives with the signet miner; claim.py imports it by name.
    import sys

    sys.path.insert(0, os.path.join(pkg_dir, "signet_miner"))
    path = os.path.join(pkg_dir, "claimer_src", "claim.py")
    spec = importlib.util.spec_from_file_location("argus_claim_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_nbits_difficulty_math():
    claim = _load_claim_module()
    # 0x1d00ffff is exactly difficulty 1.
    diff1_target = claim.nbits_to_target(0x1D00FFFF)
    assert claim.target_to_difficulty(diff1_target) == pytest.approx(1.0)
    # A smaller target (harder) is difficulty > 1.
    harder = claim.nbits_to_target(0x1C00FFFF)
    assert claim.target_to_difficulty(harder) > 1.0


def _fake_template(bits="1d00ffff"):
    return {
        "height": 200,
        "coinbasevalue": 5_000_000_000,
        "previousblockhash": "00" * 32,
        "version": 0x20000000,
        "curtime": 1_700_000_000,
        "mintime": 1_699_999_000,
        "bits": bits,
    }


def test_build_block_is_a_valid_coinbase_only_block():
    claim = _load_claim_module()
    reward_spk = bytes.fromhex("0014" + "11" * 20)  # p2wpkh
    block = claim._build_block(_fake_template(), reward_spk)

    assert len(block.vtx) == 1  # coinbase only
    cb = block.vtx[0]
    # First output pays the requested subsidy to our script.
    assert cb.vout[0].nValue == 5_000_000_000
    assert bytes(cb.vout[0].scriptPubKey) == reward_spk
    # Second output is the BIP141 witness commitment (OP_RETURN aa21a9ed ...).
    assert bytes(cb.vout[1].scriptPubKey).hex().startswith("6a24aa21a9ed")
    assert block.nBits == 0x1D00FFFF
    assert block.nTime == 1_700_000_000  # max(curtime, mintime)
    # It serializes to a non-empty block (header + txns) with witness data.
    assert len(block.serialize()) > 80


def test_try_claim_one_skips_hard_target():
    claim = _load_claim_module()

    class FakeNode:
        def getblocktemplate(self, _rules):
            return _fake_template(bits="1c00ffff")  # difficulty > 1

    claimed, difficulty = claim.try_claim_one(FakeNode(), bytes.fromhex("0014" + "11" * 20))
    assert claimed is False
    assert difficulty > 1.0
