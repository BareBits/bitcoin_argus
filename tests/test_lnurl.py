"""Tests for LNURL-pay / Lightning Address support: config schema, name
resolution, the LUD-06 documents, invoice minting (with a fake Docker + REST),
the Flask routes, the donations Lightning Addresses, the web compose network
attachment, and the Bitcart liquidity-helper wiring."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import tarfile
import types

import pytest
import yaml

from argus.bitcart import _env_lines
from argus.config import load_config
from argus.ports import allocate
from argus.web.inventory import build_donations
from argus.web.lnurl import LnurlError, LnurlService
from argus.web_gen import generate_web

from helpers import make, validated

# A bitcart block whose liquidity helper relies on LNURL for the cash-out address
# (no explicit cashout_lightning_address). Valid only with web.lnurl on + ssl.
BITCART_LNURL = {"admin_email": "admin@example.com", "liquidity": {}}


def _cfg(ssl=True, lnurl=None, networks=None, **global_over):
    nets = networks or {
        "regtest": {"enabled": True, "bitcart": {"enabled": False}},
        "custom-signet-short": {"enabled": True, "bitcart": {"enabled": False}},
    }
    if ssl:
        global_over.setdefault("acme_email", "ops@example.com")
    data = make(nets, ssl_enabled=ssl, hostname="faucet.example", **global_over)
    if lnurl is not None:
        data["web"] = {"lnurl": lnurl}
    return validated(data)


# --- config schema ----------------------------------------------------------


def test_lnurl_defaults_on():
    cfg = _cfg()
    assert cfg.web.lnurl.enabled is True
    assert cfg.web.lnurl.min_sat == 1
    assert cfg.web.lnurl.max_sat == 5_000_000
    assert cfg.web.lnurl.comment_length == 255
    assert cfg.web.lnurl.default_network is None


@pytest.mark.parametrize("bad", [{"min_sat": 0}, {"max_sat": 0}, {"comment_length": 5000}])
def test_lnurl_field_validation(bad):
    with pytest.raises(Exception):
        _cfg(lnurl=bad)


def test_lnurl_max_below_min_rejected():
    with pytest.raises(Exception):
        _cfg(lnurl={"min_sat": 100, "max_sat": 50})


def test_lnurl_default_network_must_be_enabled():
    # Pinned to a network that exists but is disabled -> rejected.
    nets = {
        "regtest": {"enabled": True, "bitcart": {"enabled": False}},
        "signet": {"enabled": False, "bitcart": {"enabled": False}},
    }
    with pytest.raises(Exception):
        _cfg(lnurl={"default_network": "signet"}, networks=nets)
    # Pinned to an unconfigured network -> rejected.
    with pytest.raises(Exception):
        _cfg(lnurl={"default_network": "mutinynet"}, networks=nets)


# --- name resolution --------------------------------------------------------


def test_parse_name_bare_and_per_net():
    svc = LnurlService(_cfg())
    # Default network is the first enabled (regtest); bare names map to it.
    assert svc.default_network == "regtest"
    for purpose in ("fees", "cashout", "donate", "referral"):
        r = svc.parse_name(purpose)
        assert r is not None and r.purpose == purpose and r.net_key == "regtest"
        # The per-net form for a non-default network resolves to that network.
        r2 = svc.parse_name(f"{purpose}-custom-signet-short")
        assert r2 is not None and r2.net_key == "custom-signet-short"
        assert r2.chain == "signet"


def test_parse_name_hyphenated_network():
    # custom-signet-short contains a hyphen; the split must keep it intact.
    svc = LnurlService(_cfg())
    r = svc.parse_name("referral-custom-signet-short")
    assert r is not None and r.purpose == "referral" and r.net_key == "custom-signet-short"


def test_parse_name_rejects_unknown():
    svc = LnurlService(_cfg())
    assert svc.parse_name("bogus") is None
    assert svc.parse_name("fees-signet") is None  # signet not enabled
    assert svc.parse_name("tips") is None  # not a purpose
    assert svc.parse_name("fees-nope") is None
    assert svc.parse_name("") is None


def test_parse_name_disabled_service():
    svc = LnurlService(_cfg(lnurl={"enabled": False}))
    assert svc.enabled is False
    assert svc.parse_name("donate") is None


def test_address_bare_vs_per_net():
    svc = LnurlService(_cfg())
    assert svc.address("donate", "regtest", "h.com") == "donate@h.com"
    assert svc.address("donate", "custom-signet-short", "h.com") == "donate-custom-signet-short@h.com"


# --- LUD-06 payRequest -------------------------------------------------------


def test_pay_request_document():
    svc = LnurlService(_cfg())
    doc = svc.pay_request("donate", "faucet.example", "https://faucet.example/cb")
    assert doc["tag"] == "payRequest"
    assert doc["callback"] == "https://faucet.example/cb"
    assert doc["minSendable"] == 1000  # 1 sat in msat
    assert doc["maxSendable"] == 5_000_000_000  # 5,000,000 sat in msat
    assert doc["commentAllowed"] == 255
    meta = json.loads(doc["metadata"])
    kinds = {row[0]: row[1] for row in meta}
    assert "text/plain" in kinds
    assert kinds["text/identifier"] == "donate@faucet.example"


def test_pay_request_unknown_raises():
    svc = LnurlService(_cfg())
    with pytest.raises(LnurlError):
        svc.pay_request("nope", "faucet.example", "https://faucet.example/cb")


# --- invoice validation + minting -------------------------------------------


def test_invoice_amount_bounds(monkeypatch):
    svc = LnurlService(_cfg(lnurl={"min_sat": 10, "max_sat": 100}))
    monkeypatch.setattr(svc, "_mint", lambda *a, **k: "lnbc-fake")
    # In range, whole sats -> mints.
    assert svc.invoice("donate", "h", 50_000, None) == "lnbc-fake"
    # Below min / above max -> error.
    with pytest.raises(LnurlError):
        svc.invoice("donate", "h", 9_000, None)
    with pytest.raises(LnurlError):
        svc.invoice("donate", "h", 101_000, None)
    # Sub-sat (not a whole number of sats) -> error.
    with pytest.raises(LnurlError):
        svc.invoice("donate", "h", 50_500, None)


def test_invoice_comment_length(monkeypatch):
    svc = LnurlService(_cfg(lnurl={"comment_length": 8}))
    monkeypatch.setattr(svc, "_mint", lambda *a, **k: "ok")
    assert svc.invoice("donate", "h", 1000, "12345678") == "ok"
    with pytest.raises(LnurlError):
        svc.invoice("donate", "h", 1000, "123456789")


def test_invoice_unknown_name_raises():
    svc = LnurlService(_cfg())
    with pytest.raises(LnurlError):
        svc.invoice("nope", "h", 1000, None)


def _fake_archive(data: bytes, member="f"):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        info = tarfile.TarInfo(member)
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    return [buf.getvalue()], {}


def test_invoice_mints_against_node(monkeypatch):
    """End-to-end mint with a fake docker (get_archive creds) + fake requests."""
    svc = LnurlService(_cfg())

    captured = {}

    class FakeContainer:
        def get_archive(self, path):
            if path.endswith("invoice.macaroon"):
                # Path must target the per-net LND subdir (regtest here).
                assert "/data/chain/bitcoin/regtest/" in path
                return _fake_archive(b"\xde\xad\xbe\xef")
            assert path.endswith("tls.cert")
            return _fake_archive(b"-----CERT-----")

    class FakeClient:
        def __init__(self):
            self.containers = types.SimpleNamespace(get=self._get)

        def _get(self, name):
            assert name == "argus-regtest-lnd"  # unique container name, not `lnd`
            return FakeContainer()

    fake_docker = types.ModuleType("docker")
    fake_docker.from_env = lambda: FakeClient()
    monkeypatch.setitem(sys.modules, "docker", fake_docker)

    class FakeResp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"payment_request": "lnbcrt1invoice"}

    def fake_post(url, json=None, headers=None, verify=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        captured["macaroon"] = headers["Grpc-Metadata-macaroon"]
        captured["verify"] = verify
        return FakeResp()

    fake_requests = types.ModuleType("requests")
    fake_requests.post = fake_post
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    pr = svc.invoice("donate", "faucet.example", 2000, None)
    assert pr == "lnbcrt1invoice"
    # Dialled the node's REST over the per-net container name on the internal port.
    assert captured["url"] == "https://argus-regtest-lnd:8080/v1/invoices"
    assert captured["macaroon"] == "deadbeef"  # hex of the macaroon bytes
    assert captured["body"]["value_msat"] == "2000"
    # description_hash == sha256(metadata) per LUD-06.
    meta = svc._metadata(svc.parse_name("donate"), "donate@faucet.example")
    want = base64.b64encode(hashlib.sha256(meta.encode()).digest()).decode()
    assert captured["body"]["description_hash"] == want


# --- Flask routes -----------------------------------------------------------


@pytest.fixture()
def client(tmp_path):
    from argus.web.app import create_app

    data = make(
        {"regtest": {"enabled": True, "bitcart": {"enabled": False}}},
        ssl_enabled=True,
        hostname="faucet.example",
        acme_email="ops@example.com",
    )
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    app = create_app(config_path=str(cfg_path), cache_db=str(tmp_path / "c.db"))
    return app.test_client()


def test_route_well_known_ok(client):
    r = client.get(
        "/.well-known/lnurlp/donate", base_url="https://faucet.example"
    )
    assert r.status_code == 200
    doc = r.get_json()
    assert doc["tag"] == "payRequest"
    # The callback is absolute and carries the public https scheme + host.
    assert doc["callback"] == "https://faucet.example/lnurlp/donate/callback"


def test_route_well_known_unknown_404(client):
    r = client.get("/.well-known/lnurlp/nope", base_url="https://faucet.example")
    assert r.status_code == 404
    assert r.get_json()["status"] == "ERROR"


def test_route_callback_missing_amount(client):
    r = client.get("/lnurlp/donate/callback", base_url="https://faucet.example")
    assert r.status_code == 400
    assert r.get_json()["status"] == "ERROR"


def test_route_callback_out_of_range_is_error_json(client):
    r = client.get(
        "/lnurlp/donate/callback?amount=999999999999999",
        base_url="https://faucet.example",
    )
    assert r.status_code == 200
    assert r.get_json()["status"] == "ERROR"


def test_routes_404_when_disabled(tmp_path):
    from argus.web.app import create_app

    data = make(
        {"regtest": {"enabled": True, "bitcart": {"enabled": False}}},
        ssl_enabled=True,
        hostname="faucet.example",
        acme_email="ops@example.com",
    )
    data["web"] = {"lnurl": {"enabled": False}}
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    app = create_app(config_path=str(cfg_path), cache_db=str(tmp_path / "c.db"))
    c = app.test_client()
    assert c.get("/.well-known/lnurlp/donate").status_code == 404
    assert c.get("/lnurlp/donate/callback?amount=1000").status_code == 404


# --- donations Lightning Addresses ------------------------------------------


def test_donations_lightning_address_with_ssl():
    cfg = _cfg(ssl=True)
    rows = {r.key: r for r in build_donations(cfg, {})}
    # Bare donate@ for the default network (regtest), per-net for the other.
    assert rows["regtest"].lightning_address == "donate@faucet.example"
    assert rows["custom-signet-short"].lightning_address == "donate-custom-signet-short@faucet.example"
    # No Tor configured -> no onion address.
    assert rows["regtest"].lightning_onion is None


def test_donations_no_clearnet_address_without_ssl():
    cfg = _cfg(ssl=False)
    rows = {r.key: r for r in build_donations(cfg, {})}
    # Clearnet Lightning Address needs https; omitted when ssl is off.
    assert rows["regtest"].lightning_address is None


def test_donations_onion_address_with_tor():
    cfg = _cfg(ssl=True, tor={"enabled": True})
    onion = "abc234def567ghi890jkl123mno456pqr789stu012vwx345yz678abd.onion"
    rows = {r.key: r for r in build_donations(cfg, {}, onion)}
    assert rows["regtest"].lightning_onion == f"donate@{onion}"


def test_donations_omitted_when_lnurl_off():
    cfg = _cfg(ssl=True, lnurl={"enabled": False})
    rows = {r.key: r for r in build_donations(cfg, {})}
    assert rows["regtest"].lightning_address is None


# --- web compose network attachment -----------------------------------------


def _web_compose(tmp_path, data):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(data))
    cfg = load_config(cfg_path)
    web_dir = generate_web(cfg, tmp_path / "g", cfg_path)
    return yaml.safe_load((web_dir / "docker-compose.yml").read_text())


def test_web_joins_per_net_networks_when_lnurl_on(tmp_path):
    data = make(
        {
            "regtest": {"enabled": True, "bitcart": {"enabled": False}},
            "custom-signet-short": {"enabled": True, "bitcart": {"enabled": False}},
        },
        ssl_enabled=True,
        hostname="faucet.example",
        acme_email="ops@example.com",
    )
    compose = _web_compose(tmp_path, data)
    nets = compose["networks"]
    # Each enabled network's compose network is declared external and joined.
    for key in ("regtest", "custom-signet-short"):
        alias = f"net-{key}"
        assert nets[alias] == {"name": f"argus-{key}-net", "external": True}
        assert alias in compose["services"]["web"]["networks"]
    # The socket-proxy stays on the private web net only.
    assert compose["services"]["socket-proxy"]["networks"] == ["web"]


def test_web_isolated_when_lnurl_off(tmp_path):
    data = make(
        {
            "regtest": {
                "enabled": True,
                "bitcart": {"enabled": False},
                # Disable the faucet too, so this tests pure web isolation: with
                # both LNURL and the faucet off, nothing joins the per-net nets.
                "faucet": {"enabled": False},
            }
        },
        ssl_enabled=True,
        hostname="faucet.example",
        acme_email="ops@example.com",
    )
    data["web"] = {"lnurl": {"enabled": False}}
    compose = _web_compose(tmp_path, data)
    assert compose["services"]["web"]["networks"] == ["web"]
    assert "faucet" not in compose["services"]
    assert list(compose["networks"]) == ["web"]


def test_lnd_conf_has_container_name_san(tmp_path):
    # The dashboard verifies LND's TLS by container name, so the cert must list it.
    from argus.builders.lnd import build_lnd
    from argus.context import BuildContext
    from argus.ports import block_base
    from argus.constants import NETWORK_SPECS

    data = make({"regtest": {"enabled": True, "bitcart": {"enabled": False}}},
                ssl_enabled=True, hostname="faucet.example", acme_email="ops@example.com")
    cfg = validated(data)
    out = tmp_path / "regtest"
    out.mkdir()
    ctx = BuildContext(
        cfg=cfg, net_key="regtest", net=cfg.networks["regtest"],
        spec=NETWORK_SPECS["regtest"], ports=allocate(cfg)["regtest"],
        secrets={"RPC_USER": "u", "RPC_PASSWORD": "p"},
        out_dir=out, project="argus-regtest", resources=None,
    )
    build_lnd(ctx)
    conf = (out / "lnd" / "lnd.conf").read_text()
    assert "tlsextradomain=lnd" in conf
    assert "tlsextradomain=argus-regtest-lnd" in conf
    assert "tlsautorefresh=true" in conf


# --- Bitcart liquidity-helper wiring ----------------------------------------


def _liq_env(net_key, **liq):
    nets = {
        "regtest": {
            "enabled": True,
            "bitcart": {"admin_email": "a@b.com", "liquidity": liq},
        },
        "custom-signet-short": {
            "enabled": True,
            "bitcart": {"admin_email": "a@b.com", "liquidity": liq},
        },
    }
    data = make(nets, ssl_enabled=True, hostname="faucet.example",
                acme_email="ops@example.com")
    cfg = validated(data)
    return _env_lines(cfg, net_key, allocate(cfg)[net_key],
                      {"BITCART_ADMIN_PASSWORD": "pw"})


def test_liquidity_wired_to_lnurl_addresses():
    # Default network (regtest) gets bare addresses; others get per-net ones.
    rt = _liq_env("regtest")
    assert rt["CASHOUT_LIGHTNING_ADDRESS"] == "cashout@faucet.example"
    assert rt["LIQUIDITYHELPER_LN_FEE_DEST"] == "fees@faucet.example"
    assert rt["LIQUIDITYHELPER_REFERRAL_FEE_DEST"] == "referral@faucet.example"

    cs = _liq_env("custom-signet-short")
    assert cs["CASHOUT_LIGHTNING_ADDRESS"] == "cashout-custom-signet-short@faucet.example"
    assert cs["LIQUIDITYHELPER_LN_FEE_DEST"] == "fees-custom-signet-short@faucet.example"
    assert cs["LIQUIDITYHELPER_REFERRAL_FEE_DEST"] == "referral-custom-signet-short@faucet.example"


def test_liquidity_operator_cashout_override_wins():
    env = _liq_env("regtest", cashout_lightning_address="pay@operator.example")
    assert env["CASHOUT_LIGHTNING_ADDRESS"] == "pay@operator.example"
    # The fee/referral dests still come from LNURL.
    assert env["LIQUIDITYHELPER_LN_FEE_DEST"] == "fees@faucet.example"


def test_referral_fee_amount_passed_through():
    env = _liq_env("regtest", referral_fee_amount=0.03)
    assert env["LIQUIDITYHELPER_REFERRAL_FEE_AMOUNT"] == "0.03"
    assert env["LIQUIDITYHELPER_REFERRAL_FEE_DEST"] == "referral@faucet.example"


def test_no_fee_dests_without_ssl():
    # Without ssl the LNURL clearnet addresses can't resolve, so the fee/referral
    # dests are omitted (the plugin keeps its own defaults).
    nets = {
        "regtest": {
            "enabled": True,
            "bitcart": {
                "admin_email": "a@b.com",
                # liquidity needs an explicit cashout when LNURL can't supply one.
                "liquidity": {"cashout_lightning_address": "pay@x.com"},
            },
        }
    }
    data = make(nets, ssl_enabled=False, hostname="faucet.example")
    cfg = validated(data)
    env = _env_lines(cfg, "regtest", allocate(cfg)["regtest"],
                     {"BITCART_ADMIN_PASSWORD": "pw"})
    assert "LIQUIDITYHELPER_LN_FEE_DEST" not in env
    assert "LIQUIDITYHELPER_REFERRAL_FEE_DEST" not in env


# --- cross-config validation ------------------------------------------------


def test_cashout_required_relaxed_with_lnurl():
    # Liquidity on, no explicit cashout, but web.lnurl + ssl -> valid (LNURL fills).
    _cfg(ssl=True, networks={"regtest": {"enabled": True, "bitcart": BITCART_LNURL}})


def test_cashout_still_required_without_lnurl():
    nets = {"regtest": {"enabled": True, "bitcart": BITCART_LNURL}}
    data = make(nets, ssl_enabled=True, hostname="faucet.example",
                acme_email="ops@example.com")
    data["web"] = {"lnurl": {"enabled": False}}
    with pytest.raises(Exception):
        validated(data)


def test_referral_fee_needs_lnurl_and_ssl():
    nets = {
        "regtest": {
            "enabled": True,
            "bitcart": {
                "admin_email": "a@b.com",
                "liquidity": {"cashout_lightning_address": "pay@x.com",
                              "referral_fee_amount": 0.02},
            },
        }
    }
    # ssl off -> no referral payout address -> rejected.
    data = make(nets, ssl_enabled=False, hostname="faucet.example")
    with pytest.raises(Exception):
        validated(data)
