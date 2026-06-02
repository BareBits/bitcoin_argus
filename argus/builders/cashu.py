"""Builder for the Cashu (nutshell) ecash mint.

Runs in the same per-network project as the standalone LND and talks to it over
LND's REST API. The LND data volume is mounted read-only so the mint can read
the TLS cert and admin macaroon. Auth is left disabled (the nutshell default),
i.e. an open mint. The HTTP port is published on 127.0.0.1; the shared Caddy
layer fronts it publicly with TLS.
"""

from __future__ import annotations

from ..constants import LND_INTERNAL_PORTS, LND_NETWORK_KEY
from ..context import BuildContext, Fragment

_MINT_INTERNAL_PORT = 3338


def build_cashu(ctx: BuildContext) -> Fragment:
    lnd_net = LND_NETWORK_KEY[ctx.spec.chain]
    rest_port = LND_INTERNAL_PORTS["rest"]
    macaroon = f"/lnd/data/chain/bitcoin/{lnd_net}/admin.macaroon"

    environment = {
        "MINT_LISTEN_HOST": "0.0.0.0",
        "MINT_LISTEN_PORT": str(_MINT_INTERNAL_PORT),
        "MINT_BACKEND_BOLT11_SAT": "LndRestWallet",
        # LND's TLS cert carries a SAN for "lnd" (set via tlsextradomain), so
        # certificate verification succeeds against this service-name endpoint.
        "MINT_LND_REST_ENDPOINT": f"https://lnd:{rest_port}",
        "MINT_LND_REST_CERT": "/lnd/tls.cert",
        "MINT_LND_REST_MACAROON": macaroon,
        "MINT_LND_REST_CERT_VERIFY": "true",
        "MINT_PRIVATE_KEY": "${MINT_PRIVATE_KEY}",
        "MINT_DATABASE": "/data",
    }
    # Operator overrides win.
    environment.update(ctx.net.cashu.extra_env)

    service = {
        "image": "${CASHU_IMAGE}",
        "container_name": f"{ctx.project}-cashu",
        "restart": "unless-stopped",
        "depends_on": {"lnd": {"condition": "service_healthy"}},
        "command": ["poetry", "run", "mint"],
        "environment": environment,
        "volumes": [
            "cashu_data:/data",
            "lnd_data:/lnd:ro",
        ],
        # Closed to the internet; the shared Caddy fronts it.
        "ports": [f"127.0.0.1:{ctx.ports['cashu_backend']}:{_MINT_INTERNAL_PORT}"],
        "networks": [ctx.network_name],
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                f"curl -fsS http://localhost:{_MINT_INTERNAL_PORT}/v1/info "
                ">/dev/null 2>&1 || exit 1",
            ],
            "interval": "20s",
            "timeout": "10s",
            "retries": 15,
            "start_period": "30s",
        },
    }

    return Fragment(
        services={"cashu": service},
        volumes={"cashu_data": {}},
        env={
            "CASHU_IMAGE": ctx.cfg.global_.cashu_image,
            "MINT_PRIVATE_KEY": ctx.secrets["MINT_PRIVATE_KEY"],
        },
    )
