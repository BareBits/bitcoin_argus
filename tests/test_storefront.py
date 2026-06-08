"""CashuPayServer + WooCommerce storefront: config, ports, compose, wiring."""

from __future__ import annotations

import json

import pytest
import yaml
from pydantic import ValidationError

from argus.bitcart_cards import product_manifest
from argus.config import ConfigError
from argus.credentials import build_credentials
from argus.firewall import render_firewall
from argus.generate import generate
from argus.ports import allocate
from argus.shared import render_caddyfile
from argus.tor import onion_routes
from helpers import BITCART_OFF, BITCART_OK, make, validated

# A network with both storefront services on, kept otherwise minimal (its own
# admin emails so Bitcart can stay off).
STORE_NET = {
    "enabled": True,
    "bitcart": BITCART_OFF,
    "cashupayserver": {"enabled": True, "admin_email": "shop@example.com"},
    "woocommerce": {"enabled": True, "admin_email": "shop@example.com"},
}


def _gen(tmp_path, data):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(data))
    out, sec = tmp_path / "gen", tmp_path / "sec"
    generate(str(cfgp), output_dir=out, secrets_dir=sec)
    return out, sec


def _secret(sec, net, key):
    for line in (sec / net / "secrets.env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not persisted for {net}")


# --- validation -------------------------------------------------------------

def test_woocommerce_requires_cashupayserver():
    data = make({"regtest": {
        "enabled": True, "bitcart": BITCART_OK,
        "cashupayserver": {"enabled": False},
        "woocommerce": {"enabled": True},
    }})
    with pytest.raises(ConfigError, match="woocommerce.enabled requires cashupayserver"):
        validated(data)


def test_cashupayserver_requires_cashu_mint():
    data = make({"regtest": {
        "enabled": True, "bitcart": BITCART_OK,
        "cashu": {"enabled": False},
        "cashupayserver": {"enabled": True},
        "woocommerce": {"enabled": False},
    }})
    with pytest.raises(ConfigError, match="requires the network's Cashu mint"):
        validated(data)


def test_admin_email_required_when_no_fallback():
    # Bitcart off and no per-service email -> both services error.
    data = make({"regtest": {
        "enabled": True, "bitcart": BITCART_OFF,
        "cashupayserver": {"enabled": True},
        "woocommerce": {"enabled": True},
    }})
    with pytest.raises(ConfigError, match="admin_email is required"):
        validated(data)


def test_admin_email_falls_back_to_bitcart():
    # Bitcart provides the shared admin email; the services need none of their own.
    data = make({"regtest": {
        "enabled": True, "bitcart": BITCART_OK,
        "cashupayserver": {"enabled": True},
        "woocommerce": {"enabled": True},
    }})
    cfg = validated(data)  # must not raise
    net = cfg.networks["regtest"]
    assert net.cashupayserver_admin_email() == "admin@example.com"
    assert net.woocommerce_admin_email() == "admin@example.com"


def test_invalid_admin_user_rejected():
    data = make({"regtest": {
        "enabled": True, "bitcart": BITCART_OK,
        "cashupayserver": {"enabled": True},
        "woocommerce": {"enabled": True, "admin_user": "Bad User"},
    }})
    with pytest.raises(ValidationError):
        validated(data)


def test_storefront_off_by_default_in_minimal_config():
    # make() defaults the services off, so a bare network has neither.
    cfg = validated(make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    net = cfg.networks["regtest"]
    assert not net.cashupayserver.enabled
    assert not net.woocommerce.enabled


# --- ports ------------------------------------------------------------------

def test_ports_allocated_and_disjoint():
    cfg = validated(make({
        "regtest": dict(STORE_NET),
        "signet": dict(STORE_NET),
    }))
    pm = allocate(cfg)
    r = pm["regtest"]
    assert r["cashupayserver_public"] == 30120
    assert r["cashupayserver_backend"] == 30130
    assert r["woocommerce_public"] == 30220
    assert r["woocommerce_backend"] == 30230
    assert r["woocommerce_db"] == 30231
    # signet is in its own block; no collisions across all ports of both nets.
    all_ports = list(pm["regtest"].values()) + list(pm["signet"].values())
    assert len(all_ports) == len(set(all_ports))


# --- compose generation -----------------------------------------------------

def test_compose_has_storefront_services(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": dict(STORE_NET)}))
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    svcs = compose["services"]
    for name in (
        "cashupayserver-init", "cashupayserver",
        "woocommerce-db", "woocommerce", "woocommerce-init",
    ):
        assert name in svcs, name

    # The pairing volume is shared between the two init steps.
    assert "cashupayserver_pairing" in compose["volumes"]
    assert "wordpress_data" in compose["volumes"]

    # WooCommerce provisioning waits for CashuPayServer's pairing to be written.
    deps = compose["services"]["woocommerce-init"]["depends_on"]
    assert deps["cashupayserver-init"]["condition"] == "service_completed_successfully"

    # CashuPayServer talks to the in-network mint, with submarine swaps disabled.
    init_env = compose["services"]["cashupayserver-init"]["environment"]
    assert init_env["CASHUPAY_MINT_URL"] == "http://cashu:3338"
    assert init_env["CASHUPAY_SUBMARINE_SWAPS"] == "0"

    # The gateway waits for its own provisioning to finish.
    web_dep = compose["services"]["cashupayserver"]["depends_on"]
    assert web_dep["cashupayserver-init"]["condition"] == "service_completed_successfully"


def test_submarine_swaps_toggle_flows_through(tmp_path):
    net = dict(STORE_NET)
    net["cashupayserver"] = {
        "enabled": True, "admin_email": "shop@example.com", "submarine_swaps": True,
    }
    out, _ = _gen(tmp_path, make({"regtest": net}))
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    env = compose["services"]["cashupayserver-init"]["environment"]
    assert env["CASHUPAY_SUBMARINE_SWAPS"] == "1"


def test_compose_omits_storefront_when_disabled(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": {"enabled": True, "bitcart": BITCART_OFF}}))
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    for name in ("cashupayserver", "woocommerce", "woocommerce-db"):
        assert name not in compose["services"]


# --- build contexts + provisioning files ------------------------------------

def test_build_contexts_and_provision_files(tmp_path):
    out, _ = _gen(tmp_path, make({"regtest": dict(STORE_NET)}))

    # Shared CashuPayServer image build context.
    assert (out / "cashupayserver" / "Dockerfile").is_file()
    seed = (out / "cashupayserver" / "seed-cashupay.php").read_text()
    assert "Auth::setAdminPassword" in seed
    assert "createApiKey" in seed

    # Per-network WooCommerce provisioning, with the reused cards.
    woo = out / "regtest" / "woocommerce"
    assert (woo / "provision.sh").is_file()
    assert (woo / "import-products.php").is_file()
    assert (woo / "argus-hardening.php").is_file()
    manifest = json.loads((woo / "manifest.json").read_text())
    assert manifest == product_manifest()
    for item in manifest:
        assert (woo / item["image"]).is_file()

    prov = (woo / "provision.sh").read_text()
    assert "woocommerce_currency BTC" in prov
    assert "woocommerce_enable_guest_checkout yes" in prov
    assert "users_can_register 0" in prov
    assert "btcpay-greenfield-for-woocommerce" in prov
    # Frictionless checkout: classic shortcode pages (so fields are removable) and
    # no login-at-checkout prompt.
    assert "[woocommerce_checkout]" in prov
    assert "[woocommerce_cart]" in prov
    assert "woocommerce_enable_checkout_login_reminder no" in prov
    # Storefront launch + default-content cleanup + front page -> shop.
    assert "woocommerce_coming_soon no" in prov
    assert "page_on_front" in prov
    assert "hello-world" in prov
    # No non-admin content: product reviews off + registration off.
    assert "woocommerce_enable_reviews no" in prov
    assert "woocommerce_enable_myaccount_registration no" in prov
    hardening = (woo / "argus-hardening.php").read_text()
    assert "option_users_can_register" in hardening  # registration locked off
    assert "woocommerce_product_tabs" in hardening    # reviews tab removed
    assert "comments_open" in hardening               # comments off
    # Checkout asks for no information: all field groups stripped, order notes off,
    # a placeholder email injected, and the empty fields column hidden.
    assert "woocommerce_checkout_fields" in hardening
    assert "woocommerce_enable_order_notes_field" in hardening
    assert "woocommerce_checkout_posted_data" in hardening
    assert "#customer_details" in hardening
    # Seed wires auto-withdraw + derives an on-chain xpub from bitcoind.
    assert "auto_melt_address" in seed
    assert "derive_core_xpub" in seed


def test_cashupayserver_init_env_autowithdraw_and_rpc(tmp_path):
    # ssl on so the donate LNURL address resolves; acme email required then.
    data = make(
        {"regtest": dict(STORE_NET)},
        ssl_enabled=True, acme_email="ops@example.com",
    )
    out, _ = _gen(tmp_path, data)
    compose = yaml.safe_load((out / "regtest" / "docker-compose.yml").read_text())
    env = compose["services"]["cashupayserver-init"]["environment"]
    # Default network's bare donate address (regtest is first enabled here).
    assert env["CASHUPAY_AUTOWITHDRAW_LN_ADDRESS"] == "donate@x.com"
    assert env["CASHUPAY_RPC_URL"] == "http://bitcoind:18443"
    assert env["CASHUPAY_ONCHAIN_NETWORK"] == "regtest"
    assert env["CASHUPAY_ONCHAIN_WALLET"] == "argus-cashupay-regtest"


def test_dashboard_lists_storefront_services():
    from argus.web.inventory import build_sections
    cfg = validated(make({"regtest": dict(STORE_NET)}))
    pm = allocate(cfg)
    sections = {s.key: s for s in build_sections(cfg, pm, {})}
    names = {r.name for r in sections["regtest"].services}
    assert "CashuPayServer" in names
    assert "WooCommerce store" in names


# --- shared Caddy + firewall + credentials + tor ----------------------------

def test_caddy_fronts_storefront(tmp_path):
    cfg = validated(make({"regtest": dict(STORE_NET)}, ssl_enabled=False))
    pm = allocate(cfg)
    caddy = render_caddyfile(cfg, pm)
    assert "http://x.com:30120 {" in caddy  # cashupayserver public
    assert "reverse_proxy 127.0.0.1:30130" in caddy
    assert "http://x.com:30220 {" in caddy  # woocommerce public
    assert "reverse_proxy 127.0.0.1:30230" in caddy


def test_firewall_opens_storefront_ports():
    cfg = validated(make({"regtest": dict(STORE_NET)}))
    pm = allocate(cfg)
    fw = render_firewall(cfg, pm)
    assert "ufw allow 30120/tcp" in fw  # cashupayserver
    assert "ufw allow 30220/tcp" in fw  # woocommerce
    # The internal DB port is never opened.
    assert "30231/tcp" not in fw


def test_credentials_include_storefront(tmp_path):
    data = make({"regtest": dict(STORE_NET)})
    _, sec = _gen(tmp_path, data)
    cfg = validated(data)
    pm = allocate(cfg)
    creds = {c.component: c for c in build_credentials(cfg, pm, sec)}

    cps = creds["CashuPayServer admin"]
    assert cps.username == "admin"
    assert cps.username_label == "Username"
    assert cps.password == _secret(sec, "regtest", "CASHUPAYSERVER_ADMIN_PASSWORD")
    assert cps.login_url == "http://x.com:30120/admin.php"  # ssl off in tests

    woo = creds["WooCommerce admin"]
    assert woo.username == "argus-admin"
    assert woo.password == _secret(sec, "regtest", "WORDPRESS_ADMIN_PASSWORD")
    assert woo.login_url == "http://x.com:30220/wp-admin/"


def test_tor_routes_include_storefront():
    cfg = validated(make(
        {"regtest": dict(STORE_NET)},
        tor={"enabled": True},
    ))
    pm = allocate(cfg)
    labels = {r.service for r in onion_routes(cfg, pm)}
    assert "CashuPayServer" in labels
    assert "WooCommerce store" in labels
