"""Builder for the co-located cashu.me web wallet.

A static PWA (built from source — see :mod:`argus.cashu_wallet`) that talks to
this network's mint. It is deployed per network: each runs its own nginx
container on its own public port, so a browser sees a distinct origin per network
and each network's wallet state (mints, proofs) stays isolated in that origin's
storage. The mint itself is not baked in — the dashboard hands the wallet this
network's mint URL via the ``?mint=`` deep-link.

Every network's service tags the same ``argus-cashu-wallet:local`` image and
points its ``build.context`` at the shared ``generated/cashu-wallet`` context, so
the image is built once and reused. The container is bound to 127.0.0.1; the
shared Caddy layer fronts it publicly with TLS.
"""

from __future__ import annotations

from ..cashu_wallet import CASHU_WALLET_IMAGE
from ..context import BuildContext, Fragment

_WALLET_INTERNAL_PORT = 80


def build_cashu_wallet(ctx: BuildContext) -> Fragment:
    service = {
        # Shared build context (one image, reused by every network). The relative
        # path resolves from generated/<net>/ to generated/cashu-wallet/.
        "build": {
            "context": "../cashu-wallet",
            "dockerfile": "Dockerfile",
            "args": {"CASHU_WALLET_REF": ctx.cfg.global_.cashu_wallet_ref},
        },
        "image": CASHU_WALLET_IMAGE,
        "container_name": f"{ctx.project}-cashu-wallet",
        "restart": "unless-stopped",
        # Closed to the internet; the shared Caddy fronts it.
        "ports": [
            f"127.0.0.1:{ctx.ports['cashu_wallet_backend']}:{_WALLET_INTERNAL_PORT}"
        ],
        "networks": [ctx.network_name],
    }
    return Fragment(services={"cashu-wallet": service})
