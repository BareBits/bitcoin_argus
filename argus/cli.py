"""Command-line interface for Bitcoin Argus.

Commands:
  validate     parse + fully validate the config
  ports        show the allocated host ports per network
  generate     render compose projects for enabled networks
  list         list known networks and their enabled/disabled state
  credentials  show admin login credentials (Bitcart admin)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .constants import NETWORK_ORDER, NETWORK_SPECS
from .credentials import build_credentials, format_credentials
from .generate import generate
from .ports import PortAllocationError, allocate
from .secrets import read_onion_hostname

_DEFAULT_CONFIG = "config.yaml"


def _load(args: argparse.Namespace):
    return load_config(args.config)


def cmd_validate(args: argparse.Namespace) -> int:
    cfg = _load(args)
    enabled = [k for k, _ in cfg.enabled_networks()]
    allocate(cfg)  # exercise the allocator so collisions surface here too
    print(f"OK: config is valid. Enabled networks: {', '.join(enabled) or '(none)'}")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    cfg = _load(args)
    for key in NETWORK_ORDER:
        net = cfg.networks.get(key)
        if net is None:
            state = "absent"
        else:
            state = "enabled" if net.enabled else "disabled"
        print(f"  {key:<14} {state}")
    return 0


def cmd_ports(args: argparse.Namespace) -> int:
    cfg = _load(args)
    port_map = allocate(cfg)
    for net_key, ports in port_map.items():
        print(f"[{net_key}]")
        for name, port in sorted(ports.items(), key=lambda kv: kv[1]):
            print(f"  {port:>6}  {name}")
    if not port_map:
        print("(no enabled networks)")
    return 0


def cmd_generate(args: argparse.Namespace) -> int:
    out_dirs = generate(
        config_path=args.config,
        output_dir=args.output_dir,
        secrets_dir=args.secrets_dir,
        only=args.network,
    )
    if not out_dirs:
        print("Nothing generated (no enabled networks).")
        return 0
    print("Generated:")
    for d in out_dirs:
        print(f"  {d}/docker-compose.yml")
    return 0


def cmd_credentials(args: argparse.Namespace) -> int:
    cfg = _load(args)
    if args.network is not None and args.network not in cfg.networks:
        print(f"error: unknown network {args.network!r}", file=sys.stderr)
        return 1
    port_map = allocate(cfg)
    secrets_dir = Path(args.secrets_dir)
    # Read-only: only surface the onion URL if the seed already exists; never
    # create it (a "show credentials" command must not mutate the secret store).
    onion_hostname = (
        read_onion_hostname(secrets_dir) if cfg.global_.tor.enabled else None
    )
    creds = build_credentials(
        cfg, port_map, secrets_dir, only=args.network, onion_hostname=onion_hostname
    )
    print(format_credentials(creds), end="")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="argus", description="Bitcoin Argus CLI")
    parser.add_argument(
        "-c", "--config", default=_DEFAULT_CONFIG, help="path to config.yaml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("validate", help="validate the config").set_defaults(
        func=cmd_validate
    )
    sub.add_parser("list", help="list networks").set_defaults(func=cmd_list)
    sub.add_parser("ports", help="show allocated ports").set_defaults(func=cmd_ports)

    g = sub.add_parser("generate", help="render compose projects")
    g.add_argument("network", nargs="?", help="only this network (default: all enabled)")
    g.add_argument("--output-dir", default="generated", type=Path)
    g.add_argument("--secrets-dir", default="secrets", type=Path)
    g.set_defaults(func=cmd_generate)

    cr = sub.add_parser("credentials", help="show admin login credentials")
    cr.add_argument(
        "network", nargs="?", help="only this network (default: all enabled)"
    )
    cr.add_argument("--secrets-dir", default="secrets", type=Path)
    cr.set_defaults(func=cmd_credentials)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (ConfigError, PortAllocationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
