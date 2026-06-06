"""Builder for CashuPayServer (BareBits Lite) — a BTCPay-compatible payment
gateway backed by this network's Cashu mint.

Two services per network, both built from the shared source image (see
:mod:`argus.cashupayserver`):

* ``cashupayserver-init`` — a one-shot that runs the baked-in seed script to
  provision the server (admin password, a store wired to the in-network mint with
  submarine swaps disabled, and a Greenfield API key for WooCommerce written to a
  shared pairing volume). Idempotent, so it re-runs cleanly on every ``up``.
* ``cashupayserver`` — the long-running PHP/Apache app, gated on the init having
  completed. Bound to 127.0.0.1; the shared Caddy fronts it publicly.

The server talks to the mint over the internal Docker network (server-side calls
only — customers just pay a BOLT11), so the mint URL is the in-network service
name, not the public one.
"""

from __future__ import annotations

from ..cashupayserver import CASHUPAYSERVER_IMAGE, CASHUPAYSERVER_REPO
from ..constants import CASHUPAYSERVER_INTERNAL_PORT
from ..context import BuildContext, Fragment

# The mint's in-container port (see argus.builders.cashu._MINT_INTERNAL_PORT).
_MINT_INTERNAL_PORT = 3338

# Where the init writes the WooCommerce pairing JSON (api key + store id), on a
# volume also mounted by woocommerce-init. See argus.builders.woocommerce.
PAIRING_VOLUME = "cashupayserver_pairing"
PAIRING_PATH = "/pairing/pairing.json"


def build_cashupayserver(ctx: BuildContext) -> Fragment:
    g = ctx.cfg.global_
    cps = ctx.net.cashupayserver

    scheme = "https" if ctx.ssl_on(cps.ssl) else "http"
    base_url = f"{scheme}://{g.hostname}:{ctx.ports['cashupayserver_public']}"
    # Server-side mint endpoint on the per-network Docker network.
    mint_url = f"http://cashu:{_MINT_INTERNAL_PORT}"

    build = {
        "context": "../cashupayserver",
        "dockerfile": "Dockerfile",
        "args": {
            "CASHUPAYSERVER_REF": g.cashupayserver_ref,
            "CASHUPAYSERVER_REPO": CASHUPAYSERVER_REPO,
        },
    }

    # One-shot provisioning. Builds the shared image (so it exists before the
    # web service starts) and writes the pairing file for WooCommerce.
    init = {
        "build": build,
        "image": CASHUPAYSERVER_IMAGE,
        "container_name": f"{ctx.project}-cashupayserver-init",
        "restart": "no",
        "entrypoint": ["php", "/opt/argus/seed-cashupay.php"],
        "environment": {
            "CASHUPAY_ADMIN_PASSWORD": "${CASHUPAYSERVER_ADMIN_PASSWORD}",
            "CASHUPAY_MINT_URL": mint_url,
            "CASHUPAY_MINT_UNIT": "sat",
            "CASHUPAY_STORE_NAME": ctx.net.woocommerce.store_name,
            "CASHUPAY_BASE_URL": base_url,
            "CASHUPAY_SUBMARINE_SWAPS": "1" if cps.submarine_swaps else "0",
            "CASHUPAY_PAIRING_FILE": PAIRING_PATH,
            "CASHUPAY_API_LABEL": "woocommerce",
        },
        "volumes": [
            "cashupayserver_data:/var/www/html/data",
            f"{PAIRING_VOLUME}:/pairing",
        ],
        "networks": [ctx.network_name],
    }

    # The long-running gateway. Reuses the image the init built (image-only, no
    # second build), and waits for provisioning to finish.
    web = {
        "image": CASHUPAYSERVER_IMAGE,
        "container_name": f"{ctx.project}-cashupayserver",
        "restart": "unless-stopped",
        "depends_on": {
            "cashupayserver-init": {"condition": "service_completed_successfully"}
        },
        "environment": {"CASHUPAY_APP_ROOT": "/var/www/html"},
        "volumes": ["cashupayserver_data:/var/www/html/data"],
        # Closed to the internet; the shared Caddy fronts it.
        "ports": [
            f"127.0.0.1:{ctx.ports['cashupayserver_backend']}:{CASHUPAYSERVER_INTERNAL_PORT}"
        ],
        "networks": [ctx.network_name],
    }

    return Fragment(
        services={"cashupayserver-init": init, "cashupayserver": web},
        volumes={"cashupayserver_data": {}, PAIRING_VOLUME: {}},
        env={"CASHUPAYSERVER_ADMIN_PASSWORD": ctx.secrets["CASHUPAYSERVER_ADMIN_PASSWORD"]},
    )
