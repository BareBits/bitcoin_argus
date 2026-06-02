"""Render validated config into per-network Docker Compose projects."""

from __future__ import annotations

from pathlib import Path

import yaml

from .bitcart import generate_bitcart
from .builders import REGISTRY
from .config import ArgusConfig, ConfigError, load_config
from .constants import NETWORK_SPECS
from .context import BuildContext, Fragment
from .firewall import generate_firewall
from .ports import allocate
from .secrets import load_or_create
from .shared import generate_shared


def _project_name(net_key: str) -> str:
    return f"argus-{net_key}"


def _base_compose(project: str, network_name: str) -> dict:
    return {
        "name": project,
        "services": {},
        "networks": {network_name: {"name": f"{project}-net"}},
        "volumes": {},
    }


def _write_env(path: Path, env: dict[str, str]) -> None:
    body = "".join(f"{k}={v}\n" for k, v in sorted(env.items()))
    path.write_text(body)
    path.chmod(0o600)  # contains RPC credentials


def generate_network(
    cfg: ArgusConfig,
    net_key: str,
    ports: dict[str, int],
    output_dir: Path,
    secrets_dir: Path,
) -> Path:
    """Generate one network's compose project. Returns its output directory."""
    net = cfg.networks[net_key]
    spec = NETWORK_SPECS[net_key]
    project = _project_name(net_key)
    out_dir = output_dir / net_key
    out_dir.mkdir(parents=True, exist_ok=True)

    secret_values = load_or_create(net_key, secrets_dir)

    ctx = BuildContext(
        cfg=cfg,
        net_key=net_key,
        net=net,
        spec=spec,
        ports=ports,
        secrets=secret_values,
        out_dir=out_dir,
        project=project,
    )

    compose = _base_compose(project, ctx.network_name)
    # Seed env with credentials needed by multiple services.
    env: dict[str, str] = {
        "RPC_USER": secret_values["RPC_USER"],
        "RPC_PASSWORD": secret_values["RPC_PASSWORD"],
    }

    for tool in REGISTRY:
        if not tool.include(ctx):
            continue
        fragment: Fragment = tool.builder(ctx)
        compose["services"].update(fragment.services)
        compose["volumes"].update(fragment.volumes)
        env.update(fragment.env)

    if not compose["volumes"]:
        del compose["volumes"]

    (out_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
    )
    _write_env(out_dir / ".env", env)

    # Bitcart is deployed by the BareBits installer, not our compose project,
    # so it is generated as a separate env + wrapper alongside the stack.
    generate_bitcart(cfg, net_key, ports, secret_values, output_dir)
    return out_dir


def generate(
    config_path: str | Path,
    output_dir: str | Path = "generated",
    secrets_dir: str | Path = "secrets",
    only: str | None = None,
) -> list[Path]:
    """Generate all enabled networks (or just ``only``). Returns output dirs."""
    cfg = load_config(config_path)
    port_map = allocate(cfg)
    output_dir = Path(output_dir)
    secrets_dir = Path(secrets_dir)

    enabled = {k for k, _ in cfg.enabled_networks()}
    if only is not None:
        if only not in cfg.networks:
            raise ConfigError(f"unknown network {only!r}")
        if only not in enabled:
            raise ConfigError(f"network {only!r} is not enabled")
        targets = [only]
    else:
        targets = [k for k, _ in cfg.enabled_networks()]

    dirs = [
        generate_network(cfg, k, port_map[k], output_dir, secrets_dir)
        for k in targets
    ]

    # The shared Caddy layer always reflects the full set of enabled networks.
    shared_dir = generate_shared(cfg, port_map, output_dir)
    if shared_dir is not None:
        dirs.append(shared_dir)

    # Firewall script opens the public ports across all enabled networks.
    generate_firewall(cfg, port_map, Path(output_dir))

    return dirs
