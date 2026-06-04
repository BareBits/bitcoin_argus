"""Deterministic Tor v3 (ed25519) onion-service key generation.

Tor stores a v3 onion service key as two files in the service's ``HiddenServiceDir``:

* ``hs_ed25519_secret_key`` — a 32-byte tag followed by the 64-byte *expanded*
  ed25519 secret key (the clamped scalar + the SHA-512 prefix), and
* ``hs_ed25519_public_key`` — a 32-byte tag followed by the 32-byte public key.

The ``.onion`` hostname is a base32 encoding of ``pubkey || checksum || version``.

We derive all three from a single persisted 32-byte seed so one onion address is
stable across regenerations (the same idempotent-secret pattern as the rest of
Argus). Pre-generating here — rather than letting the tor daemon mint a key on
first boot — is what lets the onion be baked into LND's config and the dashboard
without a two-phase deploy.

ed25519 point math is implemented in pure Python so the generator keeps its tiny
dependency surface (pydantic + PyYAML), mirroring :mod:`argus.signetkey`. It runs
once per deploy, so speed is irrelevant.
"""

from __future__ import annotations

import base64
import hashlib

# --- ed25519 (RFC 8032) reference arithmetic -------------------------------

_Q = 2**255 - 19  # field prime
_L = 2**252 + 27742317777372353535851937790883648493  # group order (unused here)


def _inv(x: int) -> int:
    return pow(x, _Q - 2, _Q)


_D = (-121665 * _inv(121666)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x % 2 != 0:
        x = _Q - x
    return x


_BY = (4 * _inv(5)) % _Q
_BX = _xrecover(_BY)
_B = (_BX % _Q, _BY % _Q)  # base point


def _edwards_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    denom = _D * x1 * x2 * y1 * y2
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + denom) % _Q
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - denom) % _Q
    return (x3 % _Q, y3 % _Q)


def _scalarmult(p: tuple[int, int], e: int) -> tuple[int, int]:
    """Double-and-add scalar multiplication (iterative; one-shot use)."""
    result = (0, 1)  # neutral element
    addend = p
    while e > 0:
        if e & 1:
            result = _edwards_add(result, addend)
        addend = _edwards_add(addend, addend)
        e >>= 1
    return result


def _encode_point(p: tuple[int, int]) -> bytes:
    """Encode an ed25519 point as 32 little-endian bytes (y with x's sign bit)."""
    x, y = p
    val = y | ((x & 1) << 255)
    return val.to_bytes(32, "little")


def _public_key(seed: bytes) -> bytes:
    """Derive the 32-byte ed25519 public key for a 32-byte seed (RFC 8032)."""
    h = hashlib.sha512(seed).digest()
    a = _clamp_scalar(h[:32])
    return _encode_point(_scalarmult(_B, a))


def _clamp_scalar(half: bytes) -> int:
    """Apply ed25519 clamping to the low 32 bytes of the SHA-512 expansion."""
    a = bytearray(half)
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return int.from_bytes(a, "little")


# --- Tor v3 onion encoding --------------------------------------------------

_SECRET_TAG = b"== ed25519v1-secret: type0 ==\x00\x00\x00"  # 32 bytes
_PUBLIC_TAG = b"== ed25519v1-public: type0 ==\x00\x00\x00"  # 32 bytes
_ONION_VERSION = b"\x03"
_CHECKSUM_PREFIX = b".onion checksum"


def _expanded_secret(seed: bytes) -> bytes:
    """Tor's 64-byte expanded secret key: clamped scalar || SHA-512 prefix."""
    h = hashlib.sha512(seed).digest()
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    return bytes(a) + h[32:]


def onion_hostname(pubkey: bytes) -> str:
    """Compute the ``.onion`` hostname for a 32-byte ed25519 public key."""
    checksum = hashlib.sha3_256(
        _CHECKSUM_PREFIX + pubkey + _ONION_VERSION
    ).digest()[:2]
    blob = pubkey + checksum + _ONION_VERSION
    return base64.b32encode(blob).decode("ascii").lower() + ".onion"


class OnionKey:
    """A Tor v3 onion-service key derived deterministically from a seed.

    Exposes the two on-disk key files Tor expects (``secret_key_file`` /
    ``public_key_file``) and the ``.onion`` ``hostname``.
    """

    def __init__(self, seed: bytes) -> None:
        if len(seed) != 32:
            raise ValueError("onion seed must be exactly 32 bytes")
        self.seed = seed
        self.pubkey = _public_key(seed)
        self.hostname = onion_hostname(self.pubkey)

    @property
    def secret_key_file(self) -> bytes:
        """Bytes of Tor's ``hs_ed25519_secret_key`` file."""
        return _SECRET_TAG + _expanded_secret(self.seed)

    @property
    def public_key_file(self) -> bytes:
        """Bytes of Tor's ``hs_ed25519_public_key`` file."""
        return _PUBLIC_TAG + self.pubkey
