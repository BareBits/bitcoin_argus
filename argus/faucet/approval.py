"""Faucet approval policy — the single, named Python function a faucet uses to
decide whether to dispense funds.

Each function receives the parsed request as a :class:`FaucetContext` and returns
a :class:`FaucetDecision` (approve/deny + a user-facing reason). Every function
also carries an ``explanation`` string shown on the faucet page, so a visitor
understands the policy (how much they may request) before asking.

Which function a network uses is configured in YAML
(``networks.<net>.faucet.approval_function``), falling back to
``global.faucet_default_approval`` — which defaults to ``max_one_btc``. Add a new
policy by writing a function and decorating it with :func:`register`; the config
layer validates the configured names against this registry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

# One BTC, in satoshis — the default policy's ceiling.
ONE_BTC = 1.0


@dataclass(frozen=True)
class FaucetContext:
    """Everything an approval function may consider about a single request.

    ``amount_raw`` is exactly what the user submitted (un-parsed) so a policy can
    decide what counts as a valid number. ``balance_sat`` is the connected node's
    confirmed on-chain balance, or ``None`` if it could not be read — future
    policies may use it (e.g. cap at a fraction of the balance)."""

    net_key: str
    chain: str
    address: str
    amount_raw: str
    balance_sat: int | None


@dataclass(frozen=True)
class FaucetDecision:
    """The outcome of an approval function: approved or not, plus a reason shown
    to the user."""

    approved: bool
    reason: str


@dataclass(frozen=True)
class ApprovalFunction:
    """A named policy: its user-facing ``explanation`` and the callable itself."""

    name: str
    explanation: str
    func: Callable[[FaucetContext], FaucetDecision]

    def __call__(self, ctx: FaucetContext) -> FaucetDecision:
        return self.func(ctx)


_REGISTRY: dict[str, ApprovalFunction] = {}


def register(name: str, explanation: str):
    """Decorator registering an approval function under ``name``."""

    def deco(fn: Callable[[FaucetContext], FaucetDecision]):
        if name in _REGISTRY:
            raise ValueError(f"duplicate faucet approval function {name!r}")
        _REGISTRY[name] = ApprovalFunction(name, explanation, fn)
        return fn

    return deco


def get(name: str) -> ApprovalFunction:
    """The registered function for ``name`` (raises ``KeyError`` if unknown)."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown faucet approval function {name!r}")


def names() -> list[str]:
    """All registered function names, sorted (used in config error messages)."""
    return sorted(_REGISTRY)


def is_registered(name: str) -> bool:
    return name in _REGISTRY


# --------------------------------------------------------------------------
# Built-in policies.
# --------------------------------------------------------------------------

@register(
    "max_one_btc",
    "You may request any amount that is a valid number and less than 1 BTC.",
)
def _max_one_btc(ctx: FaucetContext) -> FaucetDecision:
    """The default rule: the requested amount must parse as a (finite, positive)
    float and be strictly less than 1 BTC."""
    try:
        amount = float(ctx.amount_raw)
    except (TypeError, ValueError):
        return FaucetDecision(False, "The requested amount must be a number.")
    if not math.isfinite(amount):
        return FaucetDecision(False, "The requested amount must be a finite number.")
    if amount <= 0:
        return FaucetDecision(False, "The requested amount must be greater than zero.")
    if amount >= ONE_BTC:
        return FaucetDecision(False, "You may only request less than 1 BTC at a time.")
    return FaucetDecision(True, "Approved.")
