"""Config schema + semantic validation."""

from __future__ import annotations

import pytest

from argus.config import ConfigError, load_config
from helpers import BITCART_OFF, BITCART_OK, make, validated


def test_minimal_valid():
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    assert [k for k, _ in cfg.enabled_networks()] == ["regtest"]


def test_unknown_field_rejected():
    # extra="forbid" catches typos like a misspelled sub-tool key.
    with pytest.raises(Exception):
        validated(make({"regtest": {"enabled": True, "mempoool": {}}}))


def test_unknown_network_rejected():
    with pytest.raises(ConfigError, match="unknown network"):
        validated(make({"mainnet": {"enabled": True}}))


def test_invalid_hostname_rejected():
    with pytest.raises(Exception):
        validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
                       hostname="bad host!"))


@pytest.mark.parametrize("extra", [
    {"indexers": [{"name": "fulcrum-1"}]},
    {"mempool": {"enabled": True}},
    {"bitcart": BITCART_OK},
])
def test_prune_conflicts_with_indexes(extra):
    net = {"enabled": True, "prune": 550, "bitcart": BITCART_OFF}
    net.update(extra)
    with pytest.raises(ConfigError, match="prune"):
        validated(make({"regtest": net}))


def test_prune_ok_without_indexes():
    net = {"enabled": True, "prune": 550, "bitcart": BITCART_OFF,
           "indexers": [], "mempool": {"enabled": False}}
    validated(make({"regtest": net}))  # no raise


def test_custom_signet_valid_without_explicit_challenge():
    # Argus auto-generates a challenge + signing key for a custom signet, so
    # enabling it without an explicit challenge is valid.
    cfg = validated(make({"custom-signet": {
        "enabled": True, "miner": {"enabled": False}, "bitcart": BITCART_OFF}}))
    assert "custom-signet" in [k for k, _ in cfg.enabled_networks()]


def test_mutinynet_requires_knots_image():
    with pytest.raises(ConfigError, match="signetblocktime"):
        validated(make({"mutinynet": {"enabled": True, "bitcart": BITCART_OK}}))


def test_mutinynet_ok_with_knots_image():
    validated(make({"mutinynet": {"enabled": True, "bitcart": BITCART_OK}},
                   bitcoind_knots_image="reg/btc:1"))


def test_bitcart_requires_admin_email():
    with pytest.raises(ConfigError, match="admin_email"):
        validated(make({"regtest": {"enabled": True, "bitcart": {
            "liquidity": {"cashout_lightning_address": "p@x.com"}}}}))


def test_bitcart_liquidity_requires_cashout():
    with pytest.raises(ConfigError, match="cashout_lightning_address"):
        validated(make({"regtest": {"enabled": True,
                                    "bitcart": {"admin_email": "a@x.com"}}}))


def test_bitcart_cashout_not_required_when_liquidity_disabled():
    validated(make({"regtest": {"enabled": True, "bitcart": {
        "admin_email": "a@x.com", "liquidity": {"disabled": True}}}}))


def test_acme_email_required_for_public_ssl_service():
    # ssl on + a public (non-regtest) ssl service => acme_email needed.
    with pytest.raises(ConfigError, match="acme_email"):
        validated(make({"signet": {"enabled": True, "bitcart": BITCART_OFF}},
                       ssl_enabled=True))


def test_acme_email_not_required_for_regtest_only():
    # regtest is excluded from the public-ssl check (no public DNS).
    validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}},
                   ssl_enabled=True))


@pytest.mark.parametrize("bad", ["xyz", "abc"])  # non-hex / odd-length
def test_signet_challenge_must_be_hex(bad):
    with pytest.raises(Exception):
        validated(make({"custom-signet": {
            "enabled": True, "signet_challenge": bad,
            "miner": {"enabled": False}, "bitcart": BITCART_OFF}}))


def test_empty_signet_challenge_is_none():
    # An empty placeholder on a disabled network must not error.
    cfg = validated(make({"custom-signet": {
        "enabled": False, "signet_challenge": ""}}))
    assert cfg.networks["custom-signet"].signet_challenge is None


def test_miner_unsupported_net_is_noop():
    # signet doesn't support mining; miner.enabled default true must not error.
    validated(make({"signet": {"enabled": True, "bitcart": BITCART_OFF}},
                   ssl_enabled=False))


def test_miner_on_custom_signet_allowed():
    # Argus can sign + mine a custom signet, so the miner is permitted there.
    cfg = validated(make({"custom-signet": {
        "enabled": True,
        "miner": {"enabled": True},
        "bitcart": BITCART_OFF}}))
    assert cfg.networks["custom-signet"].miner.enabled


def test_miner_on_non_mineable_network_is_noop():
    # A network whose blocks Argus can't drive (supports_miner is False) simply
    # ignores the miner flag — it's valid, the registry just won't include it.
    cfg = validated(make({"mutinynet": {
        "enabled": True, "miner": {"enabled": True}, "bitcart": BITCART_OFF}},
        bitcoind_knots_image="x/y:1"))
    assert cfg.networks["mutinynet"].miner.enabled


def test_secondary_and_channels_default_on_for_mined_nets():
    from argus.constants import NETWORK_SPECS

    cfg = validated(make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "custom-signet": {"enabled": True, "bitcart": BITCART_OFF},
        "signet": {"enabled": True, "bitcart": BITCART_OFF},
    }))
    for key in ("regtest", "custom-signet"):
        net, spec = cfg.networks[key], NETWORK_SPECS[key]
        assert net.lnd_secondary_enabled(spec)
        assert net.lnd_channels_enabled(spec)
        assert net.lnd_wumbo_enabled(spec)  # 10 BTC channels force wumbo on
    # Public signet: not a network Argus mines => feature off.
    net, spec = cfg.networks["signet"], NETWORK_SPECS["signet"]
    assert not net.lnd_secondary_enabled(spec)
    assert not net.lnd_channels_enabled(spec)


def test_disabling_miner_opts_out_without_error():
    from argus.constants import NETWORK_SPECS

    # No miner => the auto feature defaults off (no validation error).
    cfg = validated(make({"regtest": {
        "enabled": True, "miner": {"enabled": False}, "bitcart": BITCART_OFF}}))
    net, spec = cfg.networks["regtest"], NETWORK_SPECS["regtest"]
    assert not net.lnd_secondary_enabled(spec)
    assert not net.lnd_channels_enabled(spec)


def test_secondary_rejected_on_non_mined_net():
    with pytest.raises(ConfigError, match="only supported on networks Argus mines"):
        validated(make({"signet": {
            "enabled": True, "bitcart": BITCART_OFF,
            "lnd": {"secondary": {"enabled": True}}}}))


def test_channels_require_secondary():
    with pytest.raises(ConfigError, match="requires lnd.secondary"):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF,
            "lnd": {"secondary": {"enabled": False}, "channels": {"enabled": True}}}}))


def test_explicit_channels_require_miner():
    with pytest.raises(ConfigError, match="requires the miner"):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF, "miner": {"enabled": False},
            "lnd": {"channels": {"enabled": True}}}}))


def test_channel_btc_cannot_exceed_fund_btc():
    with pytest.raises(ConfigError, match="channel_btc"):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF,
            "lnd": {"channels": {"fund_btc": 5, "channel_btc": 10}}}}))


@pytest.mark.parametrize("bad", ["blue", "#ggg", "#12345", "123456"])
def test_bad_lnd_color_rejected(bad):
    with pytest.raises(Exception):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF, "lnd": {"color": bad}}}))


def test_lnd_alias_over_32_bytes_rejected():
    with pytest.raises(Exception):
        validated(make({"regtest": {
            "enabled": True, "bitcart": BITCART_OFF, "lnd": {"name": "x" * 33}}}))


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_load_config_bad_yaml(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("global: {hostname: x\n  bad")
    with pytest.raises(ConfigError):
        load_config(p)
