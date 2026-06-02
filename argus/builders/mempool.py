"""Builder for the self-hosted mempool explorer (frontend + backend + MariaDB).

The backend talks to bitcoind (RPC) and to a Fulcrum indexer (Electrum backend);
the frontend (nginx) is fronted by the shared Caddy. DB and backend are bound to
127.0.0.1; the DB is internal-only. Default-on for regtest, custom-signet, and
mutinynet (mempool.space covers the rest).
"""

from __future__ import annotations

from ..constants import CHAIN_INTERNAL_PORTS, FULCRUM_INTERNAL_PORTS, MEMPOOL_NETWORK_MAP
from ..context import BuildContext, Fragment


def build_mempool(ctx: BuildContext) -> Fragment:
    rpc_internal = CHAIN_INTERNAL_PORTS[ctx.spec.chain]["rpc"]
    mempool_net = MEMPOOL_NETWORK_MAP[ctx.spec.chain]
    backend_host_var = f"BACKEND_{mempool_net.upper()}_HTTP_HOST"

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
        "STATISTICS_ENABLED": "true",
        **electrum_env,
    }

    api_depends = {
        "mempool-db": {"condition": "service_healthy"},
        "bitcoind": {"condition": "service_healthy"},
    }
    if indexers:
        api_depends[indexers[0].name] = {"condition": "service_healthy"}

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

    web = {
        "image": "${MEMPOOL_FRONTEND_IMAGE}",
        "container_name": f"{ctx.project}-mempool-web",
        "restart": "unless-stopped",
        "depends_on": {"mempool-api": {"condition": "service_started"}},
        "environment": {
            "FRONTEND_HTTP_PORT": "8080",
            backend_host_var: "mempool-api",
        },
        # Frontend loopback; the shared Caddy fronts it publicly.
        "ports": [f"127.0.0.1:{ctx.ports['mempool_web']}:8080"],
        "networks": [ctx.network_name],
    }

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
