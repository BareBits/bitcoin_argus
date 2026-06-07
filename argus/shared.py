"""The host-level shared layer: a single Caddy that fronts every network's HTTP
services.

All networks share one hostname and differ by port, so one Caddy (running with
host networking) listens per-service on each public port, terminates TLS with a
single certificate for the hostname, and reverse-proxies to the backend's
loopback port. With ``ssl_enabled: false`` it serves plain HTTP (no ACME), which
is what we use for local/test runs.

This is generated from the full config (all enabled networks at once), unlike the
per-network builders.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ArgusConfig
from .constants import (
    FAUCET_BACKEND_PORT,
    NETWORK_SPECS,
    ONION_WEB_BACKEND_PORT,
    WEB_BACKEND_PORT,
)
from .resources import global_log
from .tor import onion_web_path_routed


@dataclass(frozen=True)
class _HttpSite:
    """One reverse-proxied site: a public port -> a loopback backend port."""

    public_port: int
    backend_port: int
    ssl: bool


def _http_sites(cfg: ArgusConfig, port_map: dict[str, dict[str, int]]) -> list[_HttpSite]:
    """Collect every HTTP service that Caddy should front, across all networks."""
    sites: list[_HttpSite] = []
    for net_key, net in cfg.enabled_networks():
        ports = port_map[net_key]
        if net.cashu.enabled:
            sites.append(
                _HttpSite(
                    public_port=ports["cashu_public"],
                    backend_port=ports["cashu_backend"],
                    ssl=cfg.global_.ssl_enabled and net.cashu.ssl,
                )
            )
            if net.cashu.wallet:
                # The co-located cashu.me web wallet, on its own public port so it
                # is a distinct browser origin per network. It reuses the mint's
                # SSL setting so the wallet and the mint it talks to share a scheme
                # (an HTTPS wallet can't fetch an HTTP mint — mixed content).
                sites.append(
                    _HttpSite(
                        public_port=ports["cashu_wallet_public"],
                        backend_port=ports["cashu_wallet_backend"],
                        ssl=cfg.global_.ssl_enabled and net.cashu.ssl,
                    )
                )
        if net.fedimint_enabled(NETWORK_SPECS[net_key]):
            # Each guardian's client API (its URL is in the invite code, so a Fedi
            # wallet must reach it) and each gateway's API (Lightning deposits/
            # withdrawals) get fronted publicly. Both are WebSocket/HTTP services —
            # Caddy's reverse_proxy upgrades WebSockets automatically. Fedimint has
            # no per-service SSL flag, so it follows the global switch.
            n = net.fedimint_guardian_count(NETWORK_SPECS[net_key])
            for i in range(n):
                sites.append(
                    _HttpSite(
                        public_port=ports[f"fedimintd_{i}_api_public"],
                        backend_port=ports[f"fedimintd_{i}_api"],
                        ssl=cfg.global_.ssl_enabled,
                    )
                )
                sites.append(
                    _HttpSite(
                        public_port=ports[f"gatewayd_{i}_api_public"],
                        backend_port=ports[f"gatewayd_{i}_api"],
                        ssl=cfg.global_.ssl_enabled,
                    )
                )
        if net.mempool_enabled(NETWORK_SPECS[net_key]):
            sites.append(
                _HttpSite(
                    public_port=ports["mempool_public"],
                    backend_port=ports["mempool_web"],
                    ssl=cfg.global_.ssl_enabled and net.mempool.ssl,
                )
            )
        if net.bitcart.enabled:
            for pub, back in (
                ("bitcart_store_public", "bitcart_store"),
                ("bitcart_admin_public", "bitcart_admin"),
                ("bitcart_api_public", "bitcart_api"),
            ):
                sites.append(
                    _HttpSite(
                        public_port=ports[pub],
                        backend_port=ports[back],
                        ssl=cfg.global_.ssl_enabled and net.bitcart.ssl,
                    )
                )
        if net.cashupayserver.enabled:
            sites.append(
                _HttpSite(
                    public_port=ports["cashupayserver_public"],
                    backend_port=ports["cashupayserver_backend"],
                    ssl=cfg.global_.ssl_enabled and net.cashupayserver.ssl,
                )
            )
        if net.woocommerce.enabled:
            sites.append(
                _HttpSite(
                    public_port=ports["woocommerce_public"],
                    backend_port=ports["woocommerce_backend"],
                    ssl=cfg.global_.ssl_enabled and net.woocommerce.ssl,
                )
            )
    return sites


def web_public_port(cfg: ArgusConfig) -> int:
    """The dashboard's public port: explicit override, else 443 (ssl) / 80."""
    if cfg.web.port is not None:
        return cfg.web.port
    web_ssl = cfg.global_.ssl_enabled and cfg.web.ssl
    return 443 if web_ssl else 80


def _web_site_address(cfg: ArgusConfig) -> str:
    """The Caddy site address for the dashboard.

    Defaults to the bare hostname (the site root) unless a custom port is set.
    """
    g = cfg.global_
    port = web_public_port(cfg)
    web_ssl = g.ssl_enabled and cfg.web.ssl
    host = g.hostname if web_ssl else f"http://{g.hostname}"
    # Omit the port for the implicit scheme defaults so it stays the bare root.
    if (web_ssl and port == 443) or (not web_ssl and port == 80):
        return host
    return f"{host}:{port}"


def _site_root_body(faucet_keys: list[str]) -> list[str]:
    """The inner directives for the dashboard site root.

    When any network has a faucet, the faucet runs as a separate backend, so the
    site path-routes ``/<net>/faucet`` to it and everything else to the dashboard
    (ordered ``handle`` blocks: first match wins). Otherwise it is a plain reverse
    proxy to the dashboard. Shared by the clearnet site root and the onion-facing
    site so the two routings can never drift."""
    if not faucet_keys:
        return [f"    reverse_proxy 127.0.0.1:{WEB_BACKEND_PORT}"]
    paths = " ".join(f"/{k}/faucet /{k}/faucet/*" for k in faucet_keys)
    return [
        f"    @faucet path {paths}",
        "    handle @faucet {",
        f"        reverse_proxy 127.0.0.1:{FAUCET_BACKEND_PORT}",
        "    }",
        "    handle {",
        f"        reverse_proxy 127.0.0.1:{WEB_BACKEND_PORT}",
        "    }",
    ]


def render_caddyfile(cfg: ArgusConfig, port_map: dict[str, dict[str, int]]) -> str:
    """Render the Caddyfile. Each service is a site on hostname:public_port."""
    g = cfg.global_
    sites = _http_sites(cfg, port_map)

    lines: list[str] = []
    global_opts: list[str] = []
    if not g.ssl_enabled:
        global_opts.append("    auto_https off")
    elif g.acme_email:
        global_opts.append(f"    email {g.acme_email}")
    if global_opts:
        lines += ["{", *global_opts, "}", ""]

    for s in sites:
        # host-networked Caddy reaches the backend on the host loopback.
        addr = f"{g.hostname}:{s.public_port}" if s.ssl else f"http://{g.hostname}:{s.public_port}"
        lines += [
            f"{addr} {{",
            f"    reverse_proxy 127.0.0.1:{s.backend_port}",
            "}",
            "",
        ]

    # The dashboard: the site root, fronting the gunicorn loopback port (with the
    # faucet path-routed onto its own backend — see _site_root_body).
    if cfg.web.enabled:
        faucet_keys = [k for k, _ in cfg.faucet_networks()]
        lines.append(f"{_web_site_address(cfg)} {{")
        lines += _site_root_body(faucet_keys)
        lines += ["}", ""]

        # An onion-facing copy of the site root: a plain-HTTP, path-routed site on
        # a loopback port that the Tor onion's port 80 forwards to, so the faucet
        # is reachable over Tor (the onion forwards to a single port and can't
        # path-route itself). Only emitted when Tor exposes the web and a faucet
        # exists; matches any Host (the request carries the .onion) but binds to
        # loopback so only tor can reach it. See argus/tor.py.
        if onion_web_path_routed(cfg):
            lines += [
                f":{ONION_WEB_BACKEND_PORT} {{",
                "    bind 127.0.0.1",
                *_site_root_body(faucet_keys),
                "}",
                "",
            ]

    return "\n".join(lines).rstrip() + "\n"


def _compose() -> dict:
    return {
        "name": "argus-shared",
        "services": {
            "caddy": {
                "image": "${CADDY_IMAGE}",
                "container_name": "argus-caddy",
                "restart": "unless-stopped",
                # Host networking so Caddy binds each service's public port
                # directly and can reach backends on 127.0.0.1.
                "network_mode": "host",
                "volumes": [
                    "./Caddyfile:/etc/caddy/Caddyfile:ro",
                    "caddy_data:/data",
                    "caddy_config:/config",
                ],
            }
        },
        "volumes": {"caddy_data": {}, "caddy_config": {}},
    }


def generate_shared(
    cfg: ArgusConfig,
    port_map: dict[str, dict[str, int]],
    output_dir: Path,
) -> Path | None:
    """Generate the shared Caddy project. Returns its dir, or None if no HTTP
    services (nor the dashboard) are enabled."""
    if not _http_sites(cfg, port_map) and not cfg.web.enabled:
        return None

    out_dir = output_dir / "shared"
    out_dir.mkdir(parents=True, exist_ok=True)
    compose = _compose()
    rotation, log_block = global_log(cfg)
    if rotation:
        compose["services"]["caddy"].setdefault("logging", log_block)
    (out_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
    )
    (out_dir / "Caddyfile").write_text(render_caddyfile(cfg, port_map))
    (out_dir / ".env").write_text(f"CADDY_IMAGE={cfg.global_.caddy_image}\n")
    return out_dir
