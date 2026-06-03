"""Generate a signet block-signing key and its matching challenge.

A custom signet's ``signetchallenge`` is a script that miners must satisfy to
produce a valid block. We use the conventional 1-of-1 bare multisig wrapper
(``OP_1 <pubkey> OP_1 OP_CHECKMULTISIG`` => ``5121<pubkey>51ae``), the same form
the Mutinynet challenge uses.

The challenge (public) goes into ``bitcoin.conf``; the WIF private key (secret)
is imported by the signet miner so it can sign blocks. Both are generated as a
matched pair and stored in the secret store.

secp256k1 point math is implemented here in pure Python to avoid adding a crypto
dependency to the generator — it runs once per deploy, so speed is irrelevant.
"""

from __future__ import annotations

import hashlib
import secrets as _secrets

# secp256k1 domain parameters.
_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_GX = 0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798
_GY = 0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _inv(a: int, m: int) -> int:
    return pow(a, m - 2, m)


def _point_add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    (x1, y1), (x2, y2) = p, q
    if x1 == x2 and (y1 + y2) % _P == 0:
        return None
    if p == q:
        m = (3 * x1 * x1) * _inv(2 * y1, _P) % _P
    else:
        m = (y2 - y1) * _inv((x2 - x1) % _P, _P) % _P
    x3 = (m * m - x1 - x2) % _P
    y3 = (m * (x1 - x3) - y1) % _P
    return (x3, y3)


def _point_mul(k: int, point=(_GX, _GY)):
    result = None
    addend = point
    while k:
        if k & 1:
            result = _point_add(result, addend)
        addend = _point_add(addend, addend)
        k >>= 1
    return result


def _compressed_pubkey(priv: int) -> bytes:
    x, y = _point_mul(priv)
    prefix = b"\x03" if (y & 1) else b"\x02"
    return prefix + x.to_bytes(32, "big")


def _b58check(payload: bytes) -> str:
    chk = hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    data = payload + chk
    n = int.from_bytes(data, "big")
    out = ""
    while n > 0:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(data) - len(data.lstrip(b"\x00"))
    return "1" * pad + out


def generate_signet_key() -> tuple[str, str, str]:
    """Return ``(wif, pubkey_hex, challenge_hex)`` for a fresh signing key.

    The WIF uses the testnet/signet prefix (0xEF) and the compressed flag, which
    is what Bitcoin Core expects for signet keys.
    """
    while True:
        priv_bytes = _secrets.token_bytes(32)
        priv = int.from_bytes(priv_bytes, "big")
        if 1 <= priv < _N:
            break
    pubkey = _compressed_pubkey(priv).hex()
    wif = _b58check(b"\xef" + priv_bytes + b"\x01")
    challenge = f"5121{pubkey}51ae"
    return wif, pubkey, challenge
