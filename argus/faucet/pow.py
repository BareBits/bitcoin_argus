"""Proof-of-work for the faucet: difficulty math, the hash primitives, and the
signed, request-bound challenge protocol.

A visitor who has already used their one free claim for the day can *earn* more
by solving a challenge: find a ``solution`` so that
``H(challenge_token || solution) < target``. The browser does the searching (a
WASM/JS solver, see ``argus/web/static/faucet.js``); the server issues the
challenge and re-verifies the single winning hash here.

Security model — the challenge is a signed token (HMAC over a per-install secret)
that **binds the request**: network, destination address, requested amount, the
hash algorithm and the difficulty target, an issue time, and a server-chosen
random nonce. So a solved challenge cannot be (a) replayed — the nonce is
single-use, tracked in :class:`argus.faucet.store.RedeemedNonce`; (b) reused for
a bigger or different request — the amount/address are signed in; (c) pre-farmed
— the nonce is server-random and the token has a TTL; or (d) used on another
network. Verification is one hash, so it cannot be a DoS vector.

Difficulty is expressed as a wall-clock **target in seconds on a reference
machine** (:func:`compute_target_seconds`) and converted to a 256-bit hash
threshold via the configured reference hashrate (:func:`seconds_to_target`), so
yespower and sha256d are calibrated on the same wall-clock footing rather than by
raw (incomparable) hash counts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import threading
from dataclasses import dataclass
from typing import Callable

from ..constants import VALUE_NETWORKS

# 256-bit space; a target T means an attempt succeeds with probability T / 2**256,
# so the expected number of attempts is 2**256 / T.
_TWO_256 = 1 << 256
_HASH_LEN = 32
# A solution is an opaque short counter/string the client appends; bound the
# length so a hostile client can't force huge allocations during verification.
_MAX_SOLUTION_LEN = 80
# Token schema version, so the format can evolve without silently mis-verifying.
_TOKEN_VERSION = 1


class PowError(Exception):
    """A challenge could not be verified (tampered, expired, wrong request, or a
    bad solution). Carries a short, user-facing message."""


class PowUnavailable(Exception):
    """The configured hash primitive cannot be loaded (e.g. the yespower WASM or
    its runtime is missing). The caller disables PoW rather than failing a
    request mid-flight."""


# -- clamps ------------------------------------------------------------------


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


# -- hash primitives ---------------------------------------------------------


def _sha256d(data: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


# yespower runs the *same* CI-built WASM module the browser uses, loaded here via
# wasmtime so client and server provably agree. Lazily constructed and cached;
# both the runtime and the artefact are optional, so import failures surface as
# PowUnavailable (PoW is then disabled for yespower networks).
_YESPOWER_WASM = os.environ.get("FAUCET_POW_WASM") or str(
    # default to the static asset bundled into the web image
    os.path.join(os.path.dirname(__file__), "..", "web", "static", "yespower.wasm")
)
_yespower_lock = threading.Lock()
_yespower_fn: Callable[[bytes], bytes] | None = None


def _load_yespower() -> Callable[[bytes], bytes]:
    """Build (once) a ``bytes -> 32-byte digest`` callable backed by the yespower
    WASM module. Raises :class:`PowUnavailable` if wasmtime or the .wasm is
    missing. Thread-safe; a single store/engine is shared, calls are serialised
    (verification is one hash, so contention is irrelevant)."""
    global _yespower_fn
    with _yespower_lock:
        if _yespower_fn is not None:
            return _yespower_fn
        try:
            import wasmtime  # type: ignore
        except Exception as exc:  # pragma: no cover - exercised on the VPS
            raise PowUnavailable(f"wasmtime runtime not available: {exc}")
        if not os.path.exists(_YESPOWER_WASM):  # pragma: no cover - VPS artefact
            raise PowUnavailable(f"yespower wasm not found at {_YESPOWER_WASM}")
        try:  # pragma: no cover - exercised on the VPS where the wasm exists
            engine = wasmtime.Engine()
            module = wasmtime.Module.from_file(engine, _YESPOWER_WASM)
            store_ = wasmtime.Store(engine)
            instance = wasmtime.Instance(store_, module, [])
            exports = instance.exports(store_)
            memory = exports["memory"]
            alloc = exports["alloc"]
            digest = exports["yespower_hash"]  # (in_ptr, in_len, out_ptr) -> i32
            lock = threading.Lock()

            def _fn(data: bytes) -> bytes:
                with lock:
                    in_ptr = alloc(store_, len(data))
                    out_ptr = alloc(store_, _HASH_LEN)
                    base = memory.data_ptr(store_)
                    for i, b in enumerate(data):
                        base[in_ptr + i] = b
                    digest(store_, in_ptr, len(data), out_ptr)
                    base = memory.data_ptr(store_)
                    return bytes(base[out_ptr + i] for i in range(_HASH_LEN))

            _yespower_fn = _fn
            return _fn
        except Exception as exc:  # pragma: no cover - VPS artefact
            raise PowUnavailable(f"yespower wasm failed to load: {exc}")


def get_hasher(algorithm: str) -> Callable[[bytes], bytes]:
    """A ``bytes -> 32-byte digest`` function for ``algorithm``.

    Raises :class:`PowUnavailable` if the primitive can't be loaded and
    :class:`PowError` for an unknown name."""
    if algorithm == "sha256d":
        return _sha256d
    if algorithm == "yespower":
        return _load_yespower()
    raise PowError(f"unknown pow algorithm {algorithm!r}")


def algorithm_available(algorithm: str) -> bool:
    """Whether ``algorithm`` can actually run in this process (used to disable PoW
    cleanly when the yespower artefact is absent)."""
    try:
        get_hasher(algorithm)
        return True
    except (PowError, PowUnavailable):
        return False


# -- difficulty regime selection --------------------------------------------


def is_value_pegged(pow_cfg, net_key: str) -> bool:
    """Whether ``net_key`` uses the value-pegged regime. ``None`` config => auto:
    on for real-value testnets (testnet3), off otherwise."""
    if pow_cfg.value_pegged is None:
        return net_key in VALUE_NETWORKS
    return pow_cfg.value_pegged


def max_per_day(pow_cfg, net_key: str) -> int:
    """How many PoW-earned claims one IP may make per UTC day on ``net_key``.
    ``None`` config => auto: 1 for value-pegged nets, 0 (unlimited) otherwise."""
    if pow_cfg.max_per_day is None:
        return 1 if is_value_pegged(pow_cfg, net_key) else 0
    return pow_cfg.max_per_day


def reference_hps(pow_cfg, algorithm: str) -> float:
    return (
        pow_cfg.reference_yespower_hps
        if algorithm == "yespower"
        else pow_cfg.reference_sha256d_hps
    )


def compute_target_seconds(
    pow_cfg,
    net_key: str,
    amount_sat: int,
    balance_sat: int | None,
    claims_today: int,
    subsidy_sat: int | None,
) -> float | None:
    """The PoW target for one request, in wall-clock seconds on the reference
    machine. Returns ``None`` when a value-pegged network has no subsidy data
    (the caller then disables PoW for that request).

    * value-pegged: ``safety x amount x (2**32 / subsidy) / sha256d_hps`` —
      2x the cheapest cost to actually mine ``amount`` (a testnet3 difficulty-1
      block is ``2**32`` sha256d hashes for one ``subsidy``), capped at
      ``value_cap_seconds``;
    * otherwise: the flat ``seconds_per_100k`` base, optionally multiplied by the
      balance anchor (harder as the faucet drains) and the demand retarget
      (harder under spam).
    """
    if is_value_pegged(pow_cfg, net_key):
        if not subsidy_sat or subsidy_sat <= 0:
            return None
        sha_hashes = amount_sat * (2**32) / subsidy_sat
        seconds = (
            pow_cfg.value_safety_factor * sha_hashes / pow_cfg.reference_sha256d_hps
        )
        seconds = min(seconds, pow_cfg.value_cap_seconds)
        return _clamp(seconds, pow_cfg.min_seconds, pow_cfg.value_cap_seconds)

    seconds = pow_cfg.seconds_per_100k * amount_sat / 100_000
    if pow_cfg.balance_anchor and balance_sat is not None:
        frac = _clamp(balance_sat / pow_cfg.balance_full_sat, 0.0, 1.0)
        seconds *= 1 + (pow_cfg.balance_max_mult - 1) * (1 - frac)
    if pow_cfg.demand_retarget:
        dem = _clamp(
            claims_today / pow_cfg.demand_target_per_day, 1.0, pow_cfg.demand_max_mult
        )
        seconds *= dem
    return _clamp(seconds, pow_cfg.min_seconds, pow_cfg.max_seconds)


def seconds_to_target(seconds: float, hps: float) -> tuple[int, float]:
    """Convert a seconds-of-work target into ``(threshold_int, expected_hashes)``.

    ``expected_hashes = seconds x hps`` (>= 1); the 256-bit threshold is
    ``2**256 / expected_hashes`` so the expected number of attempts to beat it
    equals ``expected_hashes``."""
    expected = max(1.0, seconds * hps)
    target = min(int(_TWO_256 / expected), _TWO_256 - 1)
    return target, expected


def effective_ttl(target_seconds: float, ttl_seconds: int) -> int:
    """How long a challenge stays valid: at least ``ttl_seconds``, but always at
    least twice the estimated solve time so a hard challenge can be finished."""
    return int(max(ttl_seconds, math.ceil(2 * target_seconds)))


# -- the signed challenge token ----------------------------------------------


@dataclass(frozen=True)
class Challenge:
    """A verified challenge's bound fields (returned by :func:`verify`)."""

    net: str
    address: str
    amount_sat: int
    algorithm: str
    target: int  # 256-bit threshold
    issued_at: int
    ttl: int
    nonce: str  # server-random; the single-use anti-replay key


def _b64u_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()


def issue(
    *,
    secret: str,
    net: str,
    address: str,
    amount_sat: int,
    algorithm: str,
    target: int,
    ttl: int,
    nonce: str,
    now: float,
) -> str:
    """Build a signed challenge token string for the given bound request."""
    payload = {
        "v": _TOKEN_VERSION,
        "net": net,
        "addr": address,
        "amt": int(amount_sat),
        "alg": algorithm,
        "tgt": format(int(target), "x"),
        "iat": int(now),
        "ttl": int(ttl),
        "non": nonce,
    }
    payload_b64 = _b64u_encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    )
    return f"{payload_b64}.{_sign(payload_b64, secret)}"


def verify(
    token: str,
    *,
    secret: str,
    net: str,
    address: str,
    amount_sat: int,
    now: float,
) -> Challenge:
    """Authenticate ``token`` and check it binds exactly this request and is
    unexpired. Raises :class:`PowError` on any mismatch. Does NOT check the
    solution (see :func:`check_solution`) or single-use (the caller does, via the
    returned ``nonce``)."""
    try:
        payload_b64, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise PowError("malformed challenge")
    expected_sig = _sign(payload_b64, secret)
    if not hmac.compare_digest(sig, expected_sig):
        raise PowError("challenge signature is invalid")
    try:
        payload = json.loads(_b64u_decode(payload_b64))
    except Exception:
        raise PowError("malformed challenge payload")
    if payload.get("v") != _TOKEN_VERSION:
        raise PowError("unsupported challenge version")
    if payload.get("net") != net:
        raise PowError("challenge is for a different network")
    if payload.get("addr") != address:
        raise PowError("challenge does not match this address")
    if payload.get("amt") != int(amount_sat):
        raise PowError("challenge does not match this amount")
    issued_at = int(payload.get("iat", 0))
    ttl = int(payload.get("ttl", 0))
    if now > issued_at + ttl:
        raise PowError("challenge has expired — request a new one")
    try:
        target = int(payload.get("tgt", ""), 16)
    except (TypeError, ValueError):
        raise PowError("malformed challenge target")
    return Challenge(
        net=net,
        address=address,
        amount_sat=int(amount_sat),
        algorithm=str(payload.get("alg")),
        target=target,
        issued_at=issued_at,
        ttl=ttl,
        nonce=str(payload.get("non")),
    )


def check_solution(token: str, solution: str, challenge: Challenge) -> bool:
    """Whether ``solution`` solves ``token`` for ``challenge``'s algorithm/target:
    ``H(token || solution) < target``. Raises :class:`PowUnavailable` if the
    algorithm can't run, :class:`PowError` for a malformed solution."""
    if not isinstance(solution, str) or not solution or len(solution) > _MAX_SOLUTION_LEN:
        raise PowError("missing or malformed proof-of-work solution")
    hasher = get_hasher(challenge.algorithm)
    digest = hasher(token.encode("ascii") + solution.encode("ascii"))
    return int.from_bytes(digest, "big") < challenge.target
