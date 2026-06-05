"""Validate that a user-supplied address is well-formed for a given chain.

The faucet must never hand LND a transaction to an address that isn't even valid
on the target chain, so it checks structure (and the address *family*) before
dispensing. This is deliberately self-contained — it pulls in no signing/crypto
code — implementing BIP173/BIP350 bech32(m) plus base58check decoding directly.

Caveat the caller should know: signet, testnet3 and testnet4 share the *same*
address encodings (bech32 HRP ``tb``; base58 versions 0x6f/0xc4), so an address
can be validated for the testnet *family* but those three are mutually
indistinguishable by the address alone — only the node can reject a truly
wrong-chain send. Regtest is distinct (bech32 HRP ``bcrt``).
"""

from __future__ import annotations

import hashlib

_BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

_BECH32_CONST = 1
_BECH32M_CONST = 0x2BC830A3

# bech32 human-readable part per bitcoind chain selector (regtest differs; the
# testnet-class chains all share ``tb``).
_BECH32_HRP: dict[str, str] = {
    "regtest": "bcrt",
    "test": "tb",
    "testnet4": "tb",
    "signet": "tb",
}

# base58 version bytes accepted: 0x6f P2PKH, 0xc4 P2SH. regtest shares the
# testnet values, so these apply to every chain the faucet supports.
_BASE58_VERSIONS = frozenset({0x6F, 0xC4})


def _bech32_polymod(values: list[int]) -> int:
    generator = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= generator[i] if ((top >> i) & 1) else 0
    return chk


def _bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(x) >> 5 for x in hrp] + [0] + [ord(x) & 31 for x in hrp]


def _bech32_decode(addr: str):
    """Return ``(hrp, data, const)`` for a valid bech32(m) string (``const`` is
    1 for bech32, 0x2bc830a3 for bech32m), or ``(None, None, None)``."""
    if any(ord(x) < 33 or ord(x) > 126 for x in addr):
        return None, None, None
    if addr.lower() != addr and addr.upper() != addr:
        return None, None, None  # mixed case is invalid per BIP173
    addr = addr.lower()
    pos = addr.rfind("1")
    if pos < 1 or pos + 7 > len(addr) or len(addr) > 90:
        return None, None, None
    hrp = addr[:pos]
    data: list[int] = []
    for c in addr[pos + 1:]:
        d = _BECH32_CHARSET.find(c)
        if d == -1:
            return None, None, None
        data.append(d)
    const = _bech32_polymod(_bech32_hrp_expand(hrp) + data)
    if const not in (_BECH32_CONST, _BECH32M_CONST):
        return None, None, None
    return hrp, data[:-6], const


def _convertbits(data: list[int], frombits: int, tobits: int) -> list[int] | None:
    acc = 0
    bits = 0
    ret: list[int] = []
    maxv = (1 << tobits) - 1
    for value in data:
        if value < 0 or (value >> frombits):
            return None
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            ret.append((acc >> bits) & maxv)
    if bits >= frombits or ((acc << (tobits - bits)) & maxv):
        return None
    return ret


def _is_valid_segwit(addr: str, expected_hrp: str) -> bool:
    hrp, data, const = _bech32_decode(addr)
    if hrp is None or hrp != expected_hrp or not data:
        return False
    witver = data[0]
    if witver > 16:
        return False
    prog = _convertbits(data[1:], 5, 8)
    if prog is None or len(prog) < 2 or len(prog) > 40:
        return False
    if witver == 0 and len(prog) not in (20, 32):
        return False
    # v0 uses bech32; v1+ (e.g. taproot) uses bech32m.
    if witver == 0 and const != _BECH32_CONST:
        return False
    if witver != 0 and const != _BECH32M_CONST:
        return False
    return True


def _base58check_version(addr: str) -> int | None:
    """The version byte of a valid base58check P2PKH/P2SH address, or None."""
    num = 0
    for ch in addr:
        idx = _B58_ALPHABET.find(ch)
        if idx == -1:
            return None
        num = num * 58 + idx
    raw = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    n_pad = len(addr) - len(addr.lstrip("1"))
    combined = b"\x00" * n_pad + raw
    if len(combined) != 25:  # 1 version + 20 hash + 4 checksum
        return None
    payload, checksum = combined[:-4], combined[-4:]
    digest = hashlib.sha256(hashlib.sha256(payload).digest()).digest()
    if digest[:4] != checksum:
        return None
    return payload[0]


def is_valid_address(address: str, chain: str) -> bool:
    """Whether ``address`` is a structurally valid address for ``chain`` (the
    bitcoind chain selector: regtest/test/testnet4/signet)."""
    if not address or any(c.isspace() for c in address):
        return False
    expected_hrp = _BECH32_HRP.get(chain)
    if expected_hrp is None:
        return False
    if _is_valid_segwit(address, expected_hrp):
        return True
    return _base58check_version(address) in _BASE58_VERSIONS
