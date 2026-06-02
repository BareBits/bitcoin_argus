"""Command-line interface for Bitcoin Argus.

Commands:
  validate   parse + fully validate the config
  ports      show the allocated host ports per network
  generate   render compose projects for enabled networks
  list       list known networks and their enabled/disabled state
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import ConfigError, load_config
from .constants import NETWORK_ORDER, NETWORK_SPECS
from .generate import generate
from .ports import PortAllocationError, allocate

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
