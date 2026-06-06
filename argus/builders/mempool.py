"""Builder for the self-hosted mempool explorer (frontend + backend + MariaDB).

The backend talks to bitcoind (RPC) and to a Fulcrum indexer (Electrum backend);
the frontend (nginx) is fronted by the shared Caddy. DB and backend are bound to
127.0.0.1; the DB is internal-only. Default-on for regtest, the custom signets,
and mutinynet (mempool.space covers the rest).
"""

from __future__ import annotations

from ..constants import (
    CHAIN_INTERNAL_PORTS,
    FULCRUM_INTERNAL_PORTS,
    LND_INTERNAL_PORTS,
    LND_NETWORK_KEY,
    MEMPOOL_NETWORK_MAP,
)
from ..context import BuildContext, Fragment

# Startup wrapper for the frontend when it runs in mempool's mainnet slot (which
# has no built-in test-coin warning). The image entrypoint generates the nginx
# config and then execs the container command; this prepends an nginx sub_filter
# that injects a warning banner into every served HTML page, then runs nginx. The
# banner HTML uses single-quoted attributes so it nests inside the sub_filter's
# double-quoted replacement string; "__NET__" is substituted with the net key.
_BANNER_SCRIPT = """#!/bin/sh
set -e
conf=/etc/nginx/conf.d/nginx-mempool.conf
banner="<div style='background:#b71c1c;color:#fff;text-align:center;padding:6px 10px;font-family:sans-serif;font-size:13px'>&#9888; Argus __NET__ &mdash; a local test network. These coins have no real value.</div>"
{
  printf 'sub_filter "<body>" "<body>%s";\\n' "$banner"
  printf 'sub_filter_once on;\\n'
  cat "$conf"
} > "$conf.tmp" && mv "$conf.tmp" "$conf"
exec nginx -g 'daemon off;'
"""


def build_mempool(ctx: BuildContext) -> Fragment:
    rpc_internal = CHAIN_INTERNAL_PORTS[ctx.spec.chain]["rpc"]
    mempool_net = MEMPOOL_NETWORK_MAP[ctx.spec.chain]

    # Use the first enabled Fulcrum as the Electrum backend; fall back to "none"
    # (Core RPC only, address lookups disabled) if no indexer is enabled.
    indexers = ctx.net.enabled_indexers()
    if indexers:
        backend_mode = "electrum"
        electrum_env = {
            "ELECTRUM_HOST": indexers[0].name,
            "ELECTRUM_PORT": str(FULCRUM_INTERNAL_PORTS["tcp"]),
            "ELECTRUM_TLS_ENABLED": "false",
        }
    else:
        backend_mode = "none"
        electrum_env = {}

    db = {
        "image": "${MARIADB_IMAGE}",
        "container_name": f"{ctx.project}-mempool-db",
        "restart": "unless-stopped",
        # Cap InnoDB's RAM use (resource profile/override).
        "command": [
            f"--innodb-buffer-pool-size={ctx.resources.mempool_mariadb_buffer_mb}M"
        ],
        "environment": {
            "MYSQL_DATABASE": "mempool",
            "MYSQL_USER": "mempool",
            "MYSQL_PASSWORD": "${MEMPOOL_DB_PASSWORD}",
            "MYSQL_ROOT_PASSWORD": "${MEMPOOL_DB_ROOT_PASSWORD}",
            "MARIADB_AUTO_UPGRADE": "1",
        },
        "volumes": ["mempool_db_data:/var/lib/mysql"],
        "networks": [ctx.network_name],
        "healthcheck": {
            "test": ["CMD", "healthcheck.sh", "--connect", "--innodb_initialized"],
            "interval": "20s",
            "timeout": "10s",
            "retries": 15,
            "start_period": "30s",
        },
    }

    api_env = {
        "MEMPOOL_BACKEND": backend_mode,
        "MEMPOOL_NETWORK": mempool_net,
        "CORE_RPC_HOST": "bitcoind",
        "CORE_RPC_PORT": str(rpc_internal),
        "CORE_RPC_USERNAME": ctx.secrets["RPC_USER"],
        "CORE_RPC_PASSWORD": ctx.secrets["RPC_PASSWORD"],
        "DATABASE_ENABLED": "true",
        "DATABASE_HOST": "mempool-db",
        "DATABASE_DATABASE": "mempool",
        "DATABASE_USERNAME": "mempool",
        "DATABASE_PASSWORD": "${MEMPOOL_DB_PASSWORD}",
        # Historical fee/mempool stats are the biggest DB grower — off by default.
        "STATISTICS_ENABLED": "true" if ctx.net.mempool.statistics else "false",
        **electrum_env,
    }

    api_depends = {
        "mempool-db": {"condition": "service_healthy"},
        "bitcoind": {"condition": "service_healthy"},
    }
    if indexers:
        api_depends[indexers[0].name] = {"condition": "service_healthy"}

    # Lightning explorer: point mempool's LN indexer at the primary local LND
    # node. The indexer pulls the network graph (describegraph) over LND's REST
    # API, so the /lightning pages populate. One node is enough — the graph is
    # global, so both argus1/argus2 show up once their channel confirms (a node
    # only appears once it has >=1 channel). We mount LND's data volume read-only
    # for its TLS cert + readonly macaroon, and run mempool-api as root so it can
    # read the macaroon (LND writes it 0600 owned by the lnd uid). The TLS cert
    # already covers the `lnd` hostname (tlsextradomain=lnd in lnd.conf).
    api_volumes: list[str] = []
    api_user: str | None = None
    if ctx.net.mempool.lightning:
        lnd_net = LND_NETWORK_KEY[ctx.spec.chain]
        api_env.update(
            {
                "LIGHTNING_ENABLED": "true",
                "LIGHTNING_BACKEND": "lnd",
                "LND_REST_API_URL": f"https://lnd:{LND_INTERNAL_PORTS['rest']}",
                "LND_TLS_CERT_PATH": "/lnd-data/tls.cert",
                "LND_MACAROON_PATH": (
                    f"/lnd-data/data/chain/bitcoin/{lnd_net}/readonly.macaroon"
                ),
            }
        )
        api_volumes.append("lnd_data:/lnd-data:ro")
        api_depends["lnd"] = {"condition": "service_healthy"}
        api_user = "0:0"

    api = {
        "image": "${MEMPOOL_BACKEND_IMAGE}",
        "container_name": f"{ctx.project}-mempool-api",
        "restart": "unless-stopped",
        "depends_on": api_depends,
        "environment": api_env,
        # API loopback for debugging; the frontend reaches it over the network.
        "ports": [f"127.0.0.1:{ctx.ports['mempool_api']}:8999"],
        "networks": [ctx.network_name],
    }
    if api_volumes:
        api["volumes"] = api_volumes
    if api_user:
        api["user"] = api_user

    # The frontend image's nginx hardwires the root /api proxy to the "mainnet"
    # backend slot (BACKEND_MAINNET_HTTP_HOST) — it has no per-network /<net>/api
    # blocks — so that var is always the proxy target regardless of the chain we
    # actually serve. For a non-mainnet slot we serve that single network at the
    # root path via ROOT_NETWORK and disable mainnet, so the selector lists only
    # this network (and the frontend shows mempool's built-in test-coin warning +
    # Lightning nav, which it supports for testnet/testnet4/signet). regtest maps
    # to the mainnet slot (network="") on purpose — see MEMPOOL_NETWORK_MAP.
    web_env = {
        "FRONTEND_HTTP_PORT": "8080",
        "BACKEND_MAINNET_HTTP_HOST": "mempool-api",
    }
    if mempool_net != "mainnet":
        web_env["ROOT_NETWORK"] = mempool_net
        web_env[f"{mempool_net.upper()}_ENABLED"] = "true"
        web_env["MAINNET_ENABLED"] = "false"  # selector lists only this network
    # Show the Lightning Explorer (the /lightning dashboard + nav) when the
    # backend indexer is on. This is a *separate* flag from the backend's
    # LIGHTNING_ENABLED; without it the frontend hides the whole LN section.
    if ctx.net.mempool.lightning:
        web_env["LIGHTNING"] = "true"

    web = {
        "image": "${MEMPOOL_FRONTEND_IMAGE}",
        "container_name": f"{ctx.project}-mempool-web",
        "restart": "unless-stopped",
        # restart:true makes Compose recreate the frontend whenever it recreates
        # the API. The frontend's nginx resolves the `mempool-api` hostname once at
        # startup and caches the IP for the worker's life (its proxy_pass uses a
        # literal host, not a runtime resolver). When `docker compose up` recreates
        # the API after a config change it gets a fresh container IP, so without
        # this the still-running frontend keeps proxying to the dead old IP and
        # every /api call (the explorer + /lightning data) 502s until manually
        # restarted. (Crash-restarts keep the same container/IP, so they're fine.)
        "depends_on": {
            "mempool-api": {"condition": "service_started", "restart": True}
        },
        "environment": web_env,
        # Frontend loopback; the shared Caddy fronts it publicly.
        "ports": [f"127.0.0.1:{ctx.ports['mempool_web']}:8080"],
        "networks": [ctx.network_name],
    }

    # mempool's frontend has no built-in test-coin warning for the mainnet slot
    # (which we use to host regtest, since regtest is hardcoded out of mempool's
    # Lightning-network list and using mainnet keeps the Lightning Explorer nav).
    # Inject our own warning banner via an nginx sub_filter: a tiny startup script
    # prepends the directive to the generated nginx config, then runs nginx. The
    # image's entrypoint generates the config and then execs the command, so this
    # runs at exactly the right moment.
    if mempool_net == "mainnet":
        web_dir = ctx.out_dir / "mempool"
        web_dir.mkdir(parents=True, exist_ok=True)
        (web_dir / "web-banner.sh").write_text(
            _BANNER_SCRIPT.replace("__NET__", ctx.net_key)
        )
        web["volumes"] = ["./mempool/web-banner.sh:/web-banner.sh:ro"]
        web["command"] = ["/bin/sh", "/web-banner.sh"]

    return Fragment(
        services={
            "mempool-db": db,
            "mempool-api": api,
            "mempool-web": web,
        },
        volumes={"mempool_db_data": {}},
        env={
            "MARIADB_IMAGE": ctx.cfg.global_.mariadb_image,
            "MEMPOOL_BACKEND_IMAGE": ctx.cfg.global_.mempool_backend_image,
            "MEMPOOL_FRONTEND_IMAGE": ctx.cfg.global_.mempool_frontend_image,
            "MEMPOOL_DB_PASSWORD": ctx.secrets["MEMPOOL_DB_PASSWORD"],
            "MEMPOOL_DB_ROOT_PASSWORD": ctx.secrets["MEMPOOL_DB_ROOT_PASSWORD"],
        },
    )
