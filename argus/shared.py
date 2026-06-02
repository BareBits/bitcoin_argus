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
from .constants import NETWORK_SPECS


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
        if net.mempool_enabled(NETWORK_SPECS[net_key]):
            sites.append(
                _HttpSite(
                    public_port=ports["mempool_public"],
                    backend_port=ports["mempool_web"],
                    ssl=cfg.global_.ssl_enabled and net.mempool.ssl,
                )
            )
        # Phase 6 adds bitcart sites here.
    return sites


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
    services are enabled."""
    if not _http_sites(cfg, port_map):
        return None

    out_dir = output_dir / "shared"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(_compose(), sort_keys=False, default_flow_style=False)
    )
    (out_dir / "Caddyfile").write_text(render_caddyfile(cfg, port_map))
    (out_dir / ".env").write_text(f"CADDY_IMAGE={cfg.global_.caddy_image}\n")
    return out_dir
