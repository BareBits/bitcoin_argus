"""Per-network secret generation and persistence.

Secrets are generated once and stored under ``secrets/<net>/`` so that
regenerating the compose files does not rotate credentials out from under a
running stack. This directory is gitignored. New required keys are added on
demand without disturbing existing ones.
"""

from __future__ import annotations

import secrets as _secrets
from pathlib import Path
from typing import Callable

# name -> generator(net_key). token_hex(32) yields 64 shell-safe hex chars.
_REQUIRED: dict[str, Callable[[str], str]] = {
    "RPC_USER": lambda net: f"argus_{net.replace('-', '_')}",
    "RPC_PASSWORD": lambda net: _secrets.token_hex(32),
    "MINT_PRIVATE_KEY": lambda net: _secrets.token_hex(32),
    "MEMPOOL_DB_PASSWORD": lambda net: _secrets.token_hex(16),
    "MEMPOOL_DB_ROOT_PASSWORD": lambda net: _secrets.token_hex(16),
}


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def load_or_create(net_key: str, secrets_root: Path) -> dict[str, str]:
    """Return this network's secrets, creating/persisting any missing keys."""
    net_dir = secrets_root / net_key
    env_path = net_dir / "secrets.env"

    values = _parse_env(env_path.read_text()) if env_path.is_file() else {}

    changed = False
    for key, gen in _REQUIRED.items():
        if not values.get(key):
            values[key] = gen(net_key)
            changed = True

    if changed:
        net_dir.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
        env_path.write_text(body)
        env_path.chmod(0o600)

    return values
