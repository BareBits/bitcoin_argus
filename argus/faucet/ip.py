"""Salted, one-way hashing of a faucet visitor's IP address.

The per-IP daily limit (:mod:`argus.faucet.rules`) must remember who has recently
withdrawn without storing raw IPs. We keep only ``HMAC-SHA256(salt, ip)`` where
``salt`` is a per-install secret (``FAUCET_IP_SALT``). Salting stops the stored
hashes from being reversed by a casual look at the database; note that an IPv4
address has only ~4 billion possible values, so a determined holder of both the
DB and the salt could still enumerate them — this is pseudonymisation, not strong
anonymity.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress


def canonical_ip(raw: str | None) -> str | None:
    """Normalise a client IP string (so e.g. ``::1`` and its long form hash
    alike), or ``None`` if it isn't a parseable address."""
    if not raw:
        return None
    try:
        return ipaddress.ip_address(raw.strip()).compressed
    except ValueError:
        return None


def hash_ip(raw: str | None, salt: str | None) -> str | None:
    """``HMAC-SHA256(salt, canonical_ip)`` as hex, or ``None`` when the IP can't
    be parsed or no salt is configured (in which case the per-IP rule fails open)."""
    ip = canonical_ip(raw)
    if ip is None or not salt:
        return None
    return hmac.new(salt.encode(), ip.encode(), hashlib.sha256).hexdigest()
