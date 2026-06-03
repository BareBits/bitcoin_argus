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

from .signetkey import generate_signet_key

# name -> generator(net_key). token_hex(32) yields 64 shell-safe hex chars.
_REQUIRED: dict[str, Callable[[str], str]] = {
    "RPC_USER": lambda net: f"argus_{net.replace('-', '_')}",
    "RPC_PASSWORD": lambda net: _secrets.token_hex(32),
    "MINT_PRIVATE_KEY": lambda net: _secrets.token_hex(32),
    "MEMPOOL_DB_PASSWORD": lambda net: _secrets.token_hex(16),
    "MEMPOOL_DB_ROOT_PASSWORD": lambda net: _secrets.token_hex(16),
    # Bitcart admin password (hex => no quote chars, which the installer forbids).
    "BITCART_ADMIN_PASSWORD": lambda net: _secrets.token_hex(16),
}

# Operator-supplied secrets (NOT auto-generated): if present in secrets.env they
# are used, otherwise the related feature is skipped. e.g. BITCART_SMTP_PASSWORD.


def _parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        out[key.strip()] = val.strip()
    return out


def load_or_create(
    net_key: str, secrets_root: Path, *, signet_key: bool = False
) -> dict[str, str]:
    """Return this network's secrets, creating/persisting any missing keys.

    When ``signet_key`` is set (a self-mined custom signet with no operator-supplied
    challenge), a matched signet challenge + block-signing WIF are generated once
    and persisted alongside the other secrets.
    """
    net_dir = secrets_root / net_key
    env_path = net_dir / "secrets.env"

    values = _parse_env(env_path.read_text()) if env_path.is_file() else {}

    changed = False
    for key, gen in _REQUIRED.items():
        if not values.get(key):
            values[key] = gen(net_key)
            changed = True

    # Signet challenge + signing key are generated as a pair, so only create them
    # together when neither is present yet.
    if signet_key and not (values.get("SIGNET_CHALLENGE") and values.get("SIGNET_MINER_WIF")):
        wif, _pubkey, challenge = generate_signet_key()
        values["SIGNET_CHALLENGE"] = challenge
        values["SIGNET_MINER_WIF"] = wif
        changed = True

    if changed:
        net_dir.mkdir(parents=True, exist_ok=True)
        body = "".join(f"{k}={v}\n" for k, v in sorted(values.items()))
        env_path.write_text(body)
        env_path.chmod(0o600)

    return values
