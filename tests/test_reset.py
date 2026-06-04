"""Auto-reset: config schema/validation, the size→ETA estimate, and the
generated reset.sh + controller project."""

from __future__ import annotations

import pytest
import yaml

from argus.constants import MAX_BLOCK_BYTES
from argus.generate import generate
from argus.reset import (
    BYTES_PER_GB,
    format_reset_eta,
    limit_bytes,
    reset_networks,
    seconds_until_reset,
)
from helpers import BITCART_OFF, BITCART_OK, make, validated


# --- config schema / validation ---------------------------------------------


def test_reset_defaults_on_for_mined_networks():
    cfg = validated(
        make(
            {
                "regtest": {"enabled": True, "bitcart": BITCART_OFF},
                "custom-signet": {"enabled": True, "bitcart": BITCART_OFF},
            }
        )
    )
    for key in ("regtest", "custom-signet"):
        net = cfg.networks[key]
        assert net.reset_enabled(key) is True
        assert net.reset.max_size_gb == 30.0
        assert net.reset.check_interval_seconds == 300


def test_reset_off_for_non_mined_networks():
    cfg = validated(make({"signet": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert cfg.networks["signet"].reset_enabled("signet") is False


def test_reset_explicit_enable_rejected_on_non_mined():
    data = make({"testnet4": {"enabled": True, "reset": {"enabled": True}}})
    with pytest.raises(Exception) as exc:
        validated(data)
    assert "reset" in str(exc.value)


def test_reset_explicit_disable_allowed():
    cfg = validated(
        make({"regtest": {"enabled": True, "bitcart": BITCART_OFF,
                          "reset": {"enabled": False}}})
    )
    assert cfg.networks["regtest"].reset_enabled("regtest") is False
    assert reset_networks(cfg) == []


def test_reset_max_size_must_be_positive():
    data = make({"regtest": {"enabled": True, "bitcart": BITCART_OFF,
                            "reset": {"max_size_gb": 0}}})
    with pytest.raises(Exception):
        validated(data)


# --- the time-to-reset estimate ----------------------------------------------


def test_limit_bytes():
    assert limit_bytes(30) == 30 * BYTES_PER_GB
    assert limit_bytes(0.5) == int(0.5 * BYTES_PER_GB)


def test_seconds_until_reset_max_block_growth():
    # 60s blocks, 1 GiB to go: rate is MAX_BLOCK_BYTES per 60s.
    limit = limit_bytes(2)
    size = limit_bytes(1)
    secs = seconds_until_reset(size, limit, 60)
    expected = (limit - size) / (MAX_BLOCK_BYTES / 60)
    assert secs == pytest.approx(expected)


def test_seconds_until_reset_already_over_is_imminent():
    assert seconds_until_reset(limit_bytes(31), limit_bytes(30), 60) == 0.0


def test_seconds_until_reset_unknown_inputs():
    assert seconds_until_reset(None, limit_bytes(30), 60) is None
    assert seconds_until_reset(1, 0, 60) is None  # no cap
    assert seconds_until_reset(1, limit_bytes(30), 0) is None  # no growth


def test_format_reset_eta():
    assert format_reset_eta(None) is None
    assert format_reset_eta(0) == "imminent"
    assert format_reset_eta(-5) == "imminent"
    # 2 days 3 hours
    assert format_reset_eta(2 * 86400 + 3 * 3600 + 59) == "2 days, 3 hours"
    # under a day -> hours, minutes
    assert format_reset_eta(5 * 3600 + 12 * 60) == "5 hours, 12 minutes"
    # under an hour -> minutes
    assert format_reset_eta(7 * 60 + 30) == "7 minutes"
    # singular
    assert format_reset_eta(86400 + 3600) == "1 day, 1 hour"


# --- generated artefacts -----------------------------------------------------


def _gen(tmp_path, data):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out = tmp_path / "gen"
    generate(str(cfgp), output_dir=out, secrets_dir=tmp_path / "sec")
    return out


def test_reset_artefacts_generated(tmp_path):
    out = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    # Controller project
    assert (out / "reset" / "docker-compose.yml").is_file()
    assert (out / "reset" / "watch.sh").is_file()
    assert (out / "reset" / "nets.tsv").is_file()
    assert (out / "reset" / "Dockerfile").is_file()
    # Per-network reset script
    reset_sh = out / "regtest" / "reset.sh"
    assert reset_sh.is_file()
    body = reset_sh.read_text()
    assert "docker compose -p \"$PROJECT\" -f \"$COMPOSE\" down -v" in body
    assert "up -d --build" in body


def test_reset_not_generated_when_disabled(tmp_path):
    out = _gen(
        tmp_path,
        make({"regtest": {"enabled": True, "bitcart": BITCART_OFF,
                          "reset": {"enabled": False}}}),
    )
    assert not (out / "reset").exists()
    assert not (out / "regtest" / "reset.sh").exists()


def test_nets_tsv_contents(tmp_path):
    out = _gen(
        tmp_path,
        make(
            {
                "regtest": {
                    "enabled": True,
                    "bitcart": BITCART_OFF,
                    "reset": {"max_size_gb": 10},
                    "miner": {"block_interval_seconds": 30},
                }
            }
        ),
    )
    rows = [
        line.split("\t")
        for line in (out / "reset" / "nets.tsv").read_text().splitlines()
    ]
    assert len(rows) == 1
    net, chain, rpcport, container, interval, limit, gb, netdir = rows[0]
    assert net == "regtest"
    assert chain == "regtest"
    assert container == "argus-regtest-bitcoind"
    assert interval == "30"
    assert limit == str(10 * BYTES_PER_GB)
    assert gb == "10"
    assert netdir == "regtest"


def test_controller_opt_mount_only_with_bitcart(tmp_path):
    # Bitcart on -> /opt is mounted so the controller can recreate it.
    out = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OK}}))
    compose = yaml.safe_load((out / "reset" / "docker-compose.yml").read_text())
    vols = compose["services"]["reset-controller"]["volumes"]
    assert "/opt:/opt" in vols
    assert "/var/run/docker.sock:/var/run/docker.sock" in vols
    assert compose["services"]["reset-controller"]["network_mode"] == "none"

    # Bitcart off everywhere -> no /opt mount.
    out2 = _gen(tmp_path / "b", make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose2 = yaml.safe_load((out2 / "reset" / "docker-compose.yml").read_text())
    assert "/opt:/opt" not in compose2["services"]["reset-controller"]["volumes"]
