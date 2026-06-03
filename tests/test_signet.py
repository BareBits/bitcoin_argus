"""Custom-signet: challenge/key generation, secrets, and miner wiring."""

from __future__ import annotations

import yaml

from argus.generate import generate
from argus.secrets import load_or_create
from argus.signetkey import _compressed_pubkey, generate_signet_key
from helpers import BITCART_OFF, make


# --- key generation ---------------------------------------------------------


def test_compressed_pubkey_known_vector():
    # Matches Bitcoin Core's ECKey for this private key.
    priv = int("f0ca309d86296c0c0b4624ed004e7d23392b3126ec1117724dab74833de5dc96", 16)
    assert (
        _compressed_pubkey(priv).hex()
        == "020d15d868637c619b71fc49d0ce26f8622cf2189b1a6862d642529e3fa063297c"
    )


def test_generate_signet_key_shape():
    wif, pubkey, challenge = generate_signet_key()
    assert len(pubkey) == 66 and pubkey[:2] in ("02", "03")
    assert challenge == f"5121{pubkey}51ae" and len(challenge) == 74
    assert wif[0] in ("c", "9")  # compressed/uncompressed testnet WIF prefix
    # Fresh each call.
    assert generate_signet_key()[0] != wif


# --- secrets ----------------------------------------------------------------


def test_secrets_signet_pair_created_and_stable(tmp_path):
    v1 = load_or_create("custom-signet", tmp_path, signet_key=True)
    assert v1["SIGNET_CHALLENGE"].startswith("5121")
    assert v1["SIGNET_MINER_WIF"]
    # Idempotent: a second load returns the same pair (no rotation).
    v2 = load_or_create("custom-signet", tmp_path, signet_key=True)
    assert v2["SIGNET_CHALLENGE"] == v1["SIGNET_CHALLENGE"]
    assert v2["SIGNET_MINER_WIF"] == v1["SIGNET_MINER_WIF"]


def test_secrets_no_signet_key_when_not_requested(tmp_path):
    v = load_or_create("regtest", tmp_path, signet_key=False)
    assert "SIGNET_CHALLENGE" not in v and "SIGNET_MINER_WIF" not in v


# --- generation -------------------------------------------------------------


def _gen_custom_signet(tmp_path):
    data = make({"custom-signet": {
        "enabled": True,
        "miner": {"enabled": True, "block_interval_seconds": 30},
        "bitcart": BITCART_OFF,
    }})
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    return out, sec


def test_generate_custom_signet_challenge_matches_secret(tmp_path):
    out, sec = _gen_custom_signet(tmp_path)
    secrets = dict(
        line.split("=", 1)
        for line in (sec / "custom-signet" / "secrets.env").read_text().splitlines()
        if "=" in line
    )
    conf = (out / "custom-signet" / "bitcoin" / "bitcoin.conf").read_text()
    assert f"signetchallenge={secrets['SIGNET_CHALLENGE']}" in conf
    assert "dnsseed=0" in conf  # isolated custom signet


def test_generate_custom_signet_miner(tmp_path):
    out, _ = _gen_custom_signet(tmp_path)
    csdir = out / "custom-signet"
    # Self-contained build context for the signet miner image.
    for f in ("Dockerfile", "miner", "mine-signet.sh"):
        assert (csdir / "miner" / f).is_file()
    assert (csdir / "miner" / "test_framework" / "messages.py").is_file()

    compose = yaml.safe_load((csdir / "docker-compose.yml").read_text())
    miner = compose["services"]["miner"]
    assert miner["build"]["context"] == "./miner"
    assert miner["environment"]["SIGNET_MINER_WIF"] == "${SIGNET_MINER_WIF}"
    assert miner["environment"]["RPC_PORT"] == "38332"

    env = (csdir / ".env").read_text()
    assert "SIGNET_MINER_WIF=" in env  # secret threaded into the project env
