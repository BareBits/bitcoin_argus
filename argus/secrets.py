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

from .onionkey import OnionKey
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
    # CashuPayServer admin password + WooCommerce/WordPress admin and DB
    # passwords. Hex keeps them free of shell/quote metacharacters, since they
    # flow through generated env files and provisioning commands.
    "CASHUPAYSERVER_ADMIN_PASSWORD": lambda net: _secrets.token_hex(16),
    "WORDPRESS_ADMIN_PASSWORD": lambda net: _secrets.token_hex(16),
    "WORDPRESS_DB_PASSWORD": lambda net: _secrets.token_hex(16),
    "WORDPRESS_DB_ROOT_PASSWORD": lambda net: _secrets.token_hex(16),
    # Fedimint: the guardian setup/admin API password (the DKG ceremony's shared
    # auth) and the gateway-cli RPC password. Hex keeps them free of shell/quote
    # metacharacters, since both flow through generated env files and the setup
    # sidecar's fedimint-cli/gateway-cli commands.
    "FEDIMINT_GUARDIAN_PASSWORD": lambda net: _secrets.token_hex(16),
    "FEDIMINT_GATEWAY_PASSWORD": lambda net: _secrets.token_hex(16),
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


def read_secrets(net_key: str, secrets_root: Path) -> dict[str, str]:
    """Return a network's persisted secrets WITHOUT creating or rotating any.

    Read-only counterpart to :func:`load_or_create`: returns an empty dict when
    the network has no ``secrets.env`` yet. Credential surfacing reads through
    this so it can never generate a value — that would desync what is shown from
    what is actually deployed.
    """
    env_path = secrets_root / net_key / "secrets.env"
    if not env_path.is_file():
        return {}
    return _parse_env(env_path.read_text())


def read_onion_hostname(secrets_root: Path) -> str | None:
    """Return the install's onion hostname if its seed already exists, else None.

    Read-only sibling of :func:`load_or_create_onion_key`, for callers (e.g. the
    ``credentials`` CLI) that must not create the seed as a side effect.
    """
    seed_path = secrets_root / "tor" / "onion_seed.hex"
    if not seed_path.is_file():
        return None
    return OnionKey(bytes.fromhex(seed_path.read_text().strip())).hostname


def load_or_create_faucet_salt(secrets_root: Path) -> str:
    """Return the install-wide faucet IP-hashing salt, creating it once.

    Like the onion identity, this is install-wide: the faucet keys its per-IP
    rows by network, but a single salt across the install is sufficient. Persisted
    under ``secrets/faucet/`` so the salt — and therefore the stored IP hashes —
    stay stable across regenerations. Rotating it would silently reset everyone's
    per-IP daily limit, so it is created once and then left alone.
    """
    faucet_dir = secrets_root / "faucet"
    salt_path = faucet_dir / "ip_salt.hex"

    if salt_path.is_file():
        return salt_path.read_text().strip()

    salt = _secrets.token_hex(32)
    faucet_dir.mkdir(parents=True, exist_ok=True)
    salt_path.write_text(salt + "\n")
    salt_path.chmod(0o600)
    return salt


def load_or_create_onion_key(secrets_root: Path) -> OnionKey:
    """Return the installation's single Tor v3 onion key, creating it once.

    Unlike the per-network secrets, the onion identity is install-wide: one
    address fronts every sub-tool (routing is by port). The 32-byte ed25519 seed
    is persisted under ``secrets/tor/`` so the ``.onion`` address is stable across
    regenerations; the key files Tor reads are derived from it deterministically.
    """
    tor_dir = secrets_root / "tor"
    seed_path = tor_dir / "onion_seed.hex"

    if seed_path.is_file():
        seed = bytes.fromhex(seed_path.read_text().strip())
    else:
        seed = _secrets.token_bytes(32)
        tor_dir.mkdir(parents=True, exist_ok=True)
        seed_path.write_text(seed.hex() + "\n")
        seed_path.chmod(0o600)

    return OnionKey(seed)
