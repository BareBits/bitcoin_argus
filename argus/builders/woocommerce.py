"""Builder for the WooCommerce storefront that sells the demo trading cards.

Three services per network (when enabled):

* ``woocommerce-db`` — a small, memory-tuned MariaDB (internal-only).
* ``woocommerce`` — the official WordPress image (Apache); bound to 127.0.0.1 and
  fronted by the shared Caddy. wp-cron is disabled and file editing locked down to
  keep the footprint small (the BTCPay webhook, not cron, drives order updates).
* ``woocommerce-init`` — a one-shot ``wordpress:cli`` container that provisions
  everything idempotently: installs WordPress + WooCommerce + the
  BTCPay-for-WooCommerce plugin, points the plugin at this network's CashuPayServer
  (reading the API key + store id from the shared pairing volume), enables guest
  checkout, disables user registration, imports the cards (reused from
  :mod:`argus.bitcart_cards`, priced in BTC), strips unused themes/plugins/features,
  and installs a small hardening mu-plugin.

The provisioning files are written per network into ``generated/<net>/woocommerce/``
and bind-mounted into the init container at ``/provision``.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..bitcart_cards import png_paths, product_manifest
from ..constants import WORDPRESS_INTERNAL_PORT
from ..context import BuildContext, Fragment
from .cashupayserver import PAIRING_VOLUME

# Memory cap for the WordPress MariaDB. Deliberately tiny — a demo storefront has
# a handful of rows, and keeping RAM low is an explicit goal of this feature.
_DB_BUFFER_POOL = "64M"

# Extra wp-config.php directives baked by the WordPress image: kill the loopback
# cron (saves a PHP spawn per request), cap memory, and lock down self-updates /
# file editing. Order updates come from the BTCPay webhook, not cron.
_WP_CONFIG_EXTRA = (
    "define('DISABLE_WP_CRON', true);\n"
    "define('WP_MEMORY_LIMIT', '128M');\n"
    "define('DISALLOW_FILE_EDIT', true);\n"
    "define('AUTOMATIC_UPDATER_DISABLED', true);\n"
)


def build_woocommerce(ctx: BuildContext) -> Fragment:
    g = ctx.cfg.global_
    woo = ctx.net.woocommerce
    cps = ctx.net.cashupayserver

    woo_scheme = "https" if ctx.ssl_on(woo.ssl) else "http"
    woo_url = f"{woo_scheme}://{g.hostname}:{ctx.ports['woocommerce_public']}"
    cps_scheme = "https" if ctx.ssl_on(cps.ssl) else "http"
    cps_url = f"{cps_scheme}://{g.hostname}:{ctx.ports['cashupayserver_public']}"
    admin_email = ctx.net.woocommerce_admin_email() or ""

    _write_provision_files(ctx.out_dir)

    db = {
        "image": "${MARIADB_IMAGE}",
        "container_name": f"{ctx.project}-woocommerce-db",
        "restart": "unless-stopped",
        # Keep InnoDB's RAM use tiny; skip DNS reverse lookups for faster connects.
        "command": [
            f"--innodb-buffer-pool-size={_DB_BUFFER_POOL}",
            "--skip-name-resolve",
        ],
        "environment": {
            "MARIADB_DATABASE": "wordpress",
            "MARIADB_USER": "wordpress",
            "MARIADB_PASSWORD": "${WORDPRESS_DB_PASSWORD}",
            "MARIADB_ROOT_PASSWORD": "${WORDPRESS_DB_ROOT_PASSWORD}",
            "MARIADB_AUTO_UPGRADE": "1",
        },
        "volumes": ["woocommerce_db_data:/var/lib/mysql"],
        "networks": [ctx.network_name],
        "healthcheck": {
            "test": ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
            "interval": "20s",
            "timeout": "10s",
            "retries": 15,
            "start_period": "30s",
        },
    }

    wordpress = {
        "image": "${WORDPRESS_IMAGE}",
        "container_name": f"{ctx.project}-woocommerce",
        "restart": "unless-stopped",
        "depends_on": {"woocommerce-db": {"condition": "service_healthy"}},
        "environment": {
            "WORDPRESS_DB_HOST": "woocommerce-db:3306",
            "WORDPRESS_DB_NAME": "wordpress",
            "WORDPRESS_DB_USER": "wordpress",
            "WORDPRESS_DB_PASSWORD": "${WORDPRESS_DB_PASSWORD}",
            "WORDPRESS_CONFIG_EXTRA": _WP_CONFIG_EXTRA,
        },
        "volumes": ["wordpress_data:/var/www/html"],
        # Closed to the internet; the shared Caddy fronts it.
        "ports": [
            f"127.0.0.1:{ctx.ports['woocommerce_backend']}:{WORDPRESS_INTERNAL_PORT}"
        ],
        "networks": [ctx.network_name],
    }

    # One-shot provisioning via WP-CLI. Waits for the DB + the WordPress core copy
    # (the wordpress service populates the shared volume on first start) and for
    # CashuPayServer's pairing file to exist.
    init = {
        "image": "${WORDPRESS_CLI_IMAGE}",
        "container_name": f"{ctx.project}-woocommerce-init",
        "restart": "no",
        "depends_on": {
            "woocommerce-db": {"condition": "service_healthy"},
            "woocommerce": {"condition": "service_started"},
            "cashupayserver-init": {"condition": "service_completed_successfully"},
        },
        "user": "www-data",
        "entrypoint": ["/bin/sh", "/provision/provision.sh"],
        "environment": {
            "WP_URL": woo_url,
            "WP_TITLE": woo.store_name,
            "WP_ADMIN_USER": woo.admin_user,
            "WP_ADMIN_PASSWORD": "${WORDPRESS_ADMIN_PASSWORD}",
            "WP_ADMIN_EMAIL": admin_email,
            "WC_VERSION": g.woocommerce_version,
            "BTCPAY_VERSION": g.btcpay_woocommerce_version,
            "CASHUPAY_URL": cps_url,
        },
        "volumes": [
            "wordpress_data:/var/www/html",
            "./woocommerce:/provision:ro",
            f"{PAIRING_VOLUME}:/pairing:ro",
        ],
        "networks": [ctx.network_name],
    }

    return Fragment(
        services={
            "woocommerce-db": db,
            "woocommerce": wordpress,
            "woocommerce-init": init,
        },
        volumes={"woocommerce_db_data": {}, "wordpress_data": {}},
        env={
            "MARIADB_IMAGE": g.mariadb_image,
            "WORDPRESS_IMAGE": g.wordpress_image,
            "WORDPRESS_CLI_IMAGE": g.wordpress_cli_image,
            "WORDPRESS_DB_PASSWORD": ctx.secrets["WORDPRESS_DB_PASSWORD"],
            "WORDPRESS_DB_ROOT_PASSWORD": ctx.secrets["WORDPRESS_DB_ROOT_PASSWORD"],
            "WORDPRESS_ADMIN_PASSWORD": ctx.secrets["WORDPRESS_ADMIN_PASSWORD"],
        },
    )


def _write_provision_files(out_dir: Path) -> Path:
    """Stage the WP-CLI provisioning script, the product-import PHP, the hardening
    mu-plugin, the card manifest, and the card PNGs into ``<net>/woocommerce/``."""
    prov_dir = out_dir / "woocommerce"
    prov_dir.mkdir(parents=True, exist_ok=True)

    (prov_dir / "provision.sh").write_text(_PROVISION_SH)
    (prov_dir / "import-products.php").write_text(_IMPORT_PRODUCTS_PHP)
    (prov_dir / "argus-hardening.php").write_text(_HARDENING_MU_PLUGIN)

    # The cards: reuse the exact same manifest + rendered PNGs as the Bitcart store.
    (prov_dir / "manifest.json").write_text(
        json.dumps(product_manifest(), indent=2) + "\n"
    )
    for png in png_paths():
        shutil.copy2(png, prov_dir / png.name)
    return prov_dir


# WP-CLI provisioning. Idempotent; runs as www-data against the shared volume.
_PROVISION_SH = r"""#!/bin/sh
# Generated by Bitcoin Argus — provision the WooCommerce storefront (idempotent).
set -eu
WP="wp --path=/var/www/html"
log() { echo "[provision] $*"; }

# 1. Wait for WordPress core (copied by the wordpress service) + the database.
i=0
while [ "$i" -lt 60 ]; do
    if $WP core is-installed >/dev/null 2>&1 || $WP db check >/dev/null 2>&1; then
        break
    fi
    [ "$i" = 0 ] && log "waiting for WordPress files + database..."
    i=$((i + 1)); sleep 5
done

# 2. Install WordPress core (no-op if already installed).
if ! $WP core is-installed >/dev/null 2>&1; then
    log "installing WordPress core..."
    $WP core install \
        --url="$WP_URL" --title="$WP_TITLE" \
        --admin_user="$WP_ADMIN_USER" --admin_password="$WP_ADMIN_PASSWORD" \
        --admin_email="$WP_ADMIN_EMAIL" --skip-email
fi
$WP option update siteurl "$WP_URL"
$WP option update home "$WP_URL"
$WP option update blogname "$WP_TITLE"

# 3. Lock the site down: no public registration, no comments/pings, no indexing.
$WP option update users_can_register 0
$WP option update default_comment_status closed
$WP option update default_ping_status closed
$WP option update blog_public 0

# 4. WooCommerce.
$WP plugin is-installed woocommerce \
    || $WP plugin install woocommerce ${WC_VERSION:+--version="$WC_VERSION"}
$WP plugin activate woocommerce
$WP option update woocommerce_currency BTC
$WP option update woocommerce_price_num_decimals 8
$WP option update woocommerce_enable_guest_checkout yes
$WP option update woocommerce_enable_signup_and_login_from_checkout no
$WP option update woocommerce_enable_myaccount_registration no
$WP option update woocommerce_registration_generate_username yes
# Skip the setup wizard + marketing/analytics/tracking bloat.
$WP option update woocommerce_onboarding_profile --format=json '{"skipped":true,"completed":true}'
$WP option update woocommerce_task_list_hidden yes
$WP option update woocommerce_extended_task_list_hidden yes
$WP option update woocommerce_allow_tracking no
$WP option update woocommerce_show_marketplace_suggestions no
$WP plugin is-active woocommerce-admin && $WP plugin deactivate woocommerce-admin || true

# 5. BTCPay-for-WooCommerce, pointed at this network's CashuPayServer.
$WP plugin is-installed btcpay-greenfield-for-woocommerce \
    || $WP plugin install btcpay-greenfield-for-woocommerce ${BTCPAY_VERSION:+--version="$BTCPAY_VERSION"}
$WP plugin activate btcpay-greenfield-for-woocommerce
API_KEY=$(php -r '$d=@json_decode(@file_get_contents("/pairing/pairing.json"),true); echo is_array($d)?($d["api_key"]??""):"";')
STORE_ID=$(php -r '$d=@json_decode(@file_get_contents("/pairing/pairing.json"),true); echo is_array($d)?($d["store_id"]??""):"";')
if [ -n "$API_KEY" ] && [ -n "$STORE_ID" ]; then
    $WP option update btcpay_gf_url "$CASHUPAY_URL"
    $WP option update btcpay_gf_api_key "$API_KEY"
    $WP option update btcpay_gf_store_id "$STORE_ID"
    $WP option update woocommerce_btcpaygf_default_settings --format=json \
        '{"enabled":"yes","title":"Bitcoin (Lightning / ecash)","description":"Pay with Bitcoin via Lightning or Cashu ecash."}'
    # Register the webhook on CashuPayServer (best-effort; the WooCommerce BTCPay
    # settings page can re-run this on save if the server was not yet ready).
    $WP eval 'try { \BTCPayServer\WC\Helper\GreenfieldApiWebhook::registerWebhook(get_option("btcpay_gf_url"), get_option("btcpay_gf_api_key"), get_option("btcpay_gf_store_id")); echo "webhook-ok"; } catch (\Throwable $e) { echo "webhook-deferred"; }' \
        && log "BTCPay webhook registration attempted." \
        || log "BTCPay webhook registration deferred (finish on the settings page)."
    log "BTCPay plugin wired to CashuPayServer at $CASHUPAY_URL (store $STORE_ID)."
else
    log "WARNING: no CashuPayServer pairing found; BTCPay plugin left unconfigured."
fi

# 6. Lightweight storefront theme; drop the default themes + bundled plugins.
$WP theme is-installed storefront || $WP theme install storefront
$WP theme activate storefront
for t in twentytwentytwo twentytwentythree twentytwentyfour twentytwentyfive twentytwentyone; do
    $WP theme is-installed "$t" && $WP theme delete "$t" || true
done
for p in akismet hello; do
    $WP plugin is-installed "$p" && $WP plugin delete "$p" || true
done

# 7. Hardening mu-plugin (xmlrpc/feeds/emoji/embeds/comments off — saves weight).
mkdir -p /var/www/html/wp-content/mu-plugins
cp /provision/argus-hardening.php /var/www/html/wp-content/mu-plugins/argus-hardening.php

# 8. Pretty permalinks (WooCommerce + the BTCPay webhook endpoint need them).
$WP rewrite structure '/%postname%/' || true
$WP rewrite flush --hard || $WP rewrite flush || true

# 9. Import the trading-card products (idempotent by SKU).
$WP eval-file /provision/import-products.php

log "WooCommerce provisioning complete."
"""


# Creates the demo products via the WooCommerce API (loaded by `wp eval-file`).
# Idempotent: a card whose SKU already exists is skipped. Prices are the cards'
# sats value converted to BTC (the store currency), shown at 8 decimals.
_IMPORT_PRODUCTS_PHP = r"""<?php
/* Generated by Bitcoin Argus — seed the trading-card products into WooCommerce. */
if (!function_exists('wc_get_product_id_by_sku')) {
    fwrite(STDERR, "[import] WooCommerce not loaded; aborting.\n");
    return;
}

function argus_attach_image(string $path, int $post_id): int {
    if (!is_file($path)) { return 0; }
    require_once ABSPATH . 'wp-admin/includes/image.php';
    $name = basename($path);
    $bits = wp_upload_bits($name, null, file_get_contents($path));
    if (!empty($bits['error'])) { return 0; }
    $type = wp_check_filetype($name, null)['type'] ?? 'image/png';
    $attach_id = wp_insert_attachment([
        'post_mime_type' => $type,
        'post_title'     => sanitize_file_name($name),
        'post_content'   => '',
        'post_status'    => 'inherit',
    ], $bits['file'], $post_id);
    if (is_wp_error($attach_id) || !$attach_id) { return 0; }
    wp_update_attachment_metadata(
        $attach_id, wp_generate_attachment_metadata($attach_id, $bits['file'])
    );
    return (int) $attach_id;
}

$manifest = json_decode((string) file_get_contents('/provision/manifest.json'), true);
if (!is_array($manifest)) {
    fwrite(STDERR, "[import] manifest.json missing/invalid; aborting.\n");
    return;
}

$created = 0; $skipped = 0;
foreach ($manifest as $item) {
    $sku = (string) ($item['slug'] ?? '');
    if ($sku === '') { continue; }
    if (wc_get_product_id_by_sku($sku)) {
        $skipped++;
        echo "[import] '{$item['name']}' already present; skipping.\n";
        continue;
    }
    // sats -> BTC (store currency), formatted to 8 decimals without thousands sep.
    $btc = number_format(((int) ($item['price_sats'] ?? 0)) / 1e8, 8, '.', '');

    $product = new WC_Product_Simple();
    $product->set_name((string) ($item['name'] ?? $sku));
    $product->set_sku($sku);
    $product->set_regular_price($btc);
    $product->set_description((string) ($item['description'] ?? ''));
    $product->set_catalog_visibility('visible');
    $product->set_status('publish');
    // Collectible demo cards: mark virtual so guest checkout needs no shipping.
    $product->set_virtual(true);
    $product_id = $product->save();

    $img = '/provision/' . basename((string) ($item['image'] ?? ''));
    $att = argus_attach_image($img, (int) $product_id);
    if ($att) { set_post_thumbnail($product_id, $att); }

    $created++;
    echo "[import] created '{$item['name']}' ({$btc} BTC).\n";
}
echo "[import] done: {$created} created, {$skipped} already present.\n";
"""


# A must-use plugin: trims weight/attack surface we do not need for a storefront.
# Kept conservative so guest checkout (classic cart/checkout) keeps working.
_HARDENING_MU_PLUGIN = r"""<?php
/**
 * Plugin Name: Argus Hardening
 * Description: Generated by Bitcoin Argus — disables unused WordPress features to
 *   shrink the footprint and attack surface of the demo storefront.
 */
if (!defined('ABSPATH')) { exit; }

// Disable XML-RPC entirely.
add_filter('xmlrpc_enabled', '__return_false');

// Drop RSS/Atom feed links, the emoji loader, and oEmbed discovery.
add_action('init', function () {
    remove_action('wp_head', 'feed_links', 2);
    remove_action('wp_head', 'feed_links_extra', 3);
    remove_action('wp_head', 'rsd_link');
    remove_action('wp_head', 'wlwmanifest_link');
    remove_action('wp_head', 'wp_generator');
    remove_action('wp_head', 'print_emoji_detection_script', 7);
    remove_action('wp_print_styles', 'print_emoji_styles');
    remove_action('wp_head', 'wp_oembed_add_discovery_links');
    remove_action('wp_head', 'wp_oembed_add_host_js');
});
add_filter('embed_oembed_discover', '__return_false');

// Turn off comments and pingbacks site-wide.
add_filter('comments_open', '__return_false', 20);
add_filter('pings_open', '__return_false', 20);
add_filter('comments_array', '__return_empty_array', 10);
add_action('admin_menu', function () { remove_menu_page('edit-comments.php'); });
add_action('init', function () {
    foreach (get_post_types() as $pt) {
        if (post_type_supports($pt, 'comments')) {
            remove_post_type_support($pt, 'comments');
            remove_post_type_support($pt, 'trackbacks');
        }
    }
});
"""
