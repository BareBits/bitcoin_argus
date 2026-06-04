"""Deterministic Tor v3 onion-key derivation."""

from __future__ import annotations

import base64
import hashlib

from argus.onionkey import OnionKey, onion_hostname

# A fixed seed (00 01 02 ... 1f) and the .onion it must always produce. The
# derivation was cross-checked against the reference ed25519 (PyNaCl) during
# development; this vector pins it so the address can never silently drift.
_FIXED_SEED = bytes(range(32))
_EXPECTED = "aoqqpp7tzyil4hlq3umoos6atft6jvrqtosq2xy53sdgiesvgg4bqead.onion"


def test_fixed_seed_vector():
    assert OnionKey(_FIXED_SEED).hostname == _EXPECTED


def test_determinism():
    assert OnionKey(_FIXED_SEED).hostname == OnionKey(_FIXED_SEED).hostname


def test_hostname_format():
    host = OnionKey(_FIXED_SEED).hostname
    assert host.endswith(".onion")
    # base32 of (32-byte pubkey + 2-byte checksum + 1 version byte) = 56 chars.
    assert len(host) == 56 + len(".onion")
    assert host[:-len(".onion")].islower()


def test_tor_file_framing():
    ok = OnionKey(_FIXED_SEED)
    assert ok.secret_key_file[:32] == b"== ed25519v1-secret: type0 ==\x00\x00\x00"
    assert len(ok.secret_key_file) == 32 + 64  # tag + expanded secret
    assert ok.public_key_file[:32] == b"== ed25519v1-public: type0 ==\x00\x00\x00"
    assert len(ok.public_key_file) == 32 + 32  # tag + public key
    # The public-key file body is exactly the key the hostname encodes.
    assert ok.public_key_file[32:] == ok.pubkey


def test_hostname_checksum_independent_recompute():
    """Recompute the hostname from the pubkey by the spec, independent of the
    OnionKey path, to guard the checksum/version encoding."""
    ok = OnionKey(_FIXED_SEED)
    checksum = hashlib.sha3_256(b".onion checksum" + ok.pubkey + b"\x03").digest()[:2]
    blob = ok.pubkey + checksum + b"\x03"
    expected = base64.b32encode(blob).decode().lower() + ".onion"
    assert onion_hostname(ok.pubkey) == expected == ok.hostname


def test_rejects_bad_seed_length():
    import pytest

    with pytest.raises(ValueError):
        OnionKey(b"too short")
