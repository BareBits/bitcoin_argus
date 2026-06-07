"""Render validated config into per-network Docker Compose projects."""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from .bitcart import generate_bitcart
from .builders import REGISTRY
from .cashu_wallet import generate_cashu_wallet
from .cashupayserver import generate_cashupayserver
from .config import ArgusConfig, ConfigError, load_config
from .constants import NETWORK_SPECS
from .credentials import generate_credentials
from .context import BuildContext, Fragment
from .firewall import generate_firewall
from .onionkey import OnionKey
from .ports import allocate
from .reset import generate_reset
from .resources import log_options, resolve
from .secrets import load_or_create, load_or_create_onion_key
from .shared import generate_shared
from .tor import generate_tor
from .web_gen import generate_web


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
    onion_hostname: str | None = None,
) -> Path:
    """Generate one network's compose project. Returns its output directory."""
    net = cfg.networks[net_key]
    spec = NETWORK_SPECS[net_key]
    project = _project_name(net_key)
    out_dir = output_dir / net_key
    out_dir.mkdir(parents=True, exist_ok=True)

    # A self-mined custom signet (challenge required, none supplied) needs an
    # auto-generated challenge + block-signing key persisted in the secret store.
    needs_signet_key = spec.requires_challenge and not net.signet_challenge
    secret_values = load_or_create(
        net_key, secrets_dir, signet_key=needs_signet_key
    )

    ctx = BuildContext(
        cfg=cfg,
        net_key=net_key,
        net=net,
        spec=spec,
        ports=ports,
        secrets=secret_values,
        out_dir=out_dir,
        project=project,
        resources=resolve(cfg, net_key),
        onion_hostname=onion_hostname,
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

    # Cap Docker json-file log growth on every generated service.
    if ctx.resources.log_rotation:
        block = log_options(ctx.resources)
        for svc in compose["services"].values():
            svc.setdefault("logging", block)

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

    # Capability guard: warn (don't fail) where Fedimint was requested on a chain
    # it can't run, and skip it there. No current chain trips this; the warning
    # exists so a future/unsupported network degrades loudly instead of silently.
    for k, net in cfg.enabled_networks():
        if net.fedimint.enabled and not net.fedimint_supported(NETWORK_SPECS[k]):
            print(
                f"warning: [{k}] Fedimint is enabled but unsupported on this "
                f"network's chain ({NETWORK_SPECS[k].chain}); skipping it here.",
                file=sys.stderr,
            )

    # The installation's single onion identity (pre-generated, persisted, stable).
    # Derived even when Tor is off only if needed; gate on enablement to avoid
    # creating a seed for installs that never use it.
    onion: OnionKey | None = (
        load_or_create_onion_key(secrets_dir) if cfg.global_.tor.enabled else None
    )
    onion_hostname = onion.hostname if onion else None

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
        generate_network(cfg, k, port_map[k], output_dir, secrets_dir, onion_hostname)
        for k in targets
    ]

    # The shared cashu.me web-wallet build context (one image, reused by every
    # network's per-net wallet container). Built from source; spans all networks.
    wallet_dir = generate_cashu_wallet(cfg, output_dir)
    if wallet_dir is not None:
        dirs.append(wallet_dir)

    # The shared CashuPayServer build context (one image, reused by every network's
    # per-net cashupayserver containers). Built from source; spans all networks.
    cps_dir = generate_cashupayserver(cfg, output_dir)
    if cps_dir is not None:
        dirs.append(cps_dir)

    # The shared Caddy layer always reflects the full set of enabled networks.
    shared_dir = generate_shared(cfg, port_map, output_dir)
    if shared_dir is not None:
        dirs.append(shared_dir)

    # The shared Tor layer (one onion fronting every sub-tool), when enabled.
    if onion is not None:
        tor_dir = generate_tor(cfg, port_map, onion, output_dir)
        if tor_dir is not None:
            dirs.append(tor_dir)

    # The dashboard spans all networks too; generated alongside the shared layer.
    web_dir = generate_web(cfg, output_dir, config_path, onion_hostname)
    if web_dir is not None:
        dirs.append(web_dir)

    # The auto-reset controller + per-network reset scripts (mined networks with
    # reset enabled). Spans all enabled networks, so generated from full config.
    reset_dir = generate_reset(cfg, Path(output_dir))
    if reset_dir is not None:
        dirs.append(reset_dir)

    # Firewall script opens the public ports across all enabled networks.
    generate_firewall(cfg, port_map, Path(output_dir))

    # Operator-facing admin credentials summary (Bitcart admin today). Read-only
    # over secrets/, so it is stable across rebuilds and storage-cap resets.
    generate_credentials(cfg, port_map, secrets_dir, output_dir, onion_hostname)

    return dirs
