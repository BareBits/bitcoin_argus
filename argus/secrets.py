"""Per-network secret generation and persistence.

Secrets (currently the bitcoind RPC credentials) are generated once and stored
under ``secrets/<net>/`` so that regenerating the compose files does not rotate
credentials out from under a running stack. This directory is gitignored.
"""

from __future__ import annotations

import secrets as _secrets
from pathlib import Path


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
    """Return this network's secrets, creating + persisting them on first use."""
    net_dir = secrets_root / net_key
    env_path = net_dir / "secrets.env"

    if env_path.is_file():
        existing = _parse_env(env_path.read_text())
        if "RPC_USER" in existing and "RPC_PASSWORD" in existing:
            return existing

    values = {
        "RPC_USER": f"argus_{net_key.replace('-', '_')}",
        # token_hex(32) => 64 hex chars; no shell-special characters.
        "RPC_PASSWORD": _secrets.token_hex(32),
    }
    net_dir.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    env_path.write_text(body)
    env_path.chmod(0o600)
    return values
