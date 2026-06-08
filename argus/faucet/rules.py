"""Composable faucet speed-limit rules.

The faucet runs a set of INDEPENDENT rules and combines them with AND: a request
is approved only when every enabled rule passes, and the page reports EVERY rule
that failed (not just the first) so the visitor knows exactly what to change —
including, for time-based limits, when they may try again.

A rule is a function ``(faucet_cfg, ctx) -> RuleOutcome | None``. It returns
``None`` when it passes or does not apply (disabled, or missing data it can't
evaluate against — in which case it fails OPEN so the faucet keeps working), and
a failing :class:`RuleOutcome` otherwise. :func:`evaluate` runs them all and
returns the failures. To add a rule, write a function and append it to
:data:`RULES`.

The amount policy (``approval_function``, e.g. ``max_one_btc``) is evaluated
separately in :mod:`argus.faucet.app` and folded into the same failure list, so
the user sees one combined list of unmet requirements.

Two of the rules cap the request amount; their ceilings — and the resulting
"most you can request right now" — are also surfaced ahead of time via
:func:`compute_limits` so the page can show them before the visitor submits.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store

DAY_SECONDS = 86_400
_SATS_PER_BTC = 100_000_000


@dataclass(frozen=True)
class RuleOutcome:
    """A single rule's verdict. ``retry_after`` (epoch seconds) is set by
    time-based rules so the caller can render a 'try again in X' hint."""

    passed: bool
    label: str
    reason: str = ""
    retry_after: float | None = None


@dataclass(frozen=True)
class Limits:
    """The amount ceilings in effect for a faucet right now, in whole sats, for
    display. ``None`` for a field means 'not enforced' or 'currently unknown'."""

    one_per_day: bool
    daily_cap_sat: int | None
    balance_cap_sat: int | None
    min_claim_sat: int | None
    max_request_sat: int | None  # min of the active caps — the headline number


@dataclass(frozen=True)
class RuleContext:
    """Everything the rules need to know about one request."""

    net_key: str
    ip_hash: str | None
    requested_sat: int
    balance_sat: int | None
    now: float
    limits: Limits


def compute_limits(faucet_cfg, net_key: str, balance_sat: int | None, now: float) -> Limits:
    """The amount ceilings currently in effect for ``net_key``.

    Used both for display and by the amount-capping rules, so the page and the
    enforcement always agree. Balance-derived caps are ``None`` when the node
    balance can't be read (the rules then fail open).
    """
    daily_cap: int | None = None
    if faucet_cfg.max_amount_per_day and balance_sat is not None:
        expected, _ = store.usage_stats(net_key, now)
        daily_cap = balance_sat // expected  # expected >= 3650, never zero

    balance_cap: int | None = None
    if faucet_cfg.per_request_balance_cap and balance_sat is not None:
        balance_cap = int(balance_sat * faucet_cfg.balance_cap_fraction)

    min_claim = faucet_cfg.min_claim_sat if faucet_cfg.min_claim_enabled else None

    active = [c for c in (daily_cap, balance_cap) if c is not None]
    return Limits(
        one_per_day=faucet_cfg.one_per_ip_per_day,
        daily_cap_sat=daily_cap,
        balance_cap_sat=balance_cap,
        min_claim_sat=min_claim,
        max_request_sat=min(active) if active else None,
    )


def _fmt(sats: int) -> str:
    return f"{sats / _SATS_PER_BTC:.8f} BTC ({sats:,} sats)"


# -- the rules ---------------------------------------------------------------


def _one_per_ip_per_day(cfg, ctx: RuleContext) -> RuleOutcome | None:
    if not cfg.one_per_ip_per_day or ctx.ip_hash is None:
        return None  # disabled, or client IP unknown => fail open
    last = store.last_ip_claim(ctx.net_key, ctx.ip_hash)
    if last is None or ctx.now - last >= DAY_SECONDS:
        return None
    return RuleOutcome(
        passed=False,
        label="One claim per 24 hours",
        reason="This IP address already received coins from this faucet in the last 24 hours.",
        retry_after=last + DAY_SECONDS,
    )


def _max_amount_per_day(cfg, ctx: RuleContext) -> RuleOutcome | None:
    cap = ctx.limits.daily_cap_sat
    if not cfg.max_amount_per_day or cap is None:
        return None  # disabled, or balance unknown => fail open
    if ctx.requested_sat <= cap:
        return None
    return RuleOutcome(
        passed=False,
        label="Daily maximum",
        reason=f"The most you can request today is {_fmt(cap)}.",
    )


def _per_request_balance_cap(cfg, ctx: RuleContext) -> RuleOutcome | None:
    cap = ctx.limits.balance_cap_sat
    if not cfg.per_request_balance_cap or cap is None:
        return None
    if ctx.requested_sat <= cap:
        return None
    pct = int(round(cfg.balance_cap_fraction * 100))
    return RuleOutcome(
        passed=False,
        label="Per-request balance cap",
        reason=f"A single request may be at most {pct}% of the faucet balance = {_fmt(cap)}.",
    )


def _min_claim(cfg, ctx: RuleContext) -> RuleOutcome | None:
    if not cfg.min_claim_enabled or ctx.requested_sat >= cfg.min_claim_sat:
        return None
    return RuleOutcome(
        passed=False,
        label="Minimum claim",
        reason=f"Requests must be at least {_fmt(cfg.min_claim_sat)}.",
    )


# Order here is the order failures are reported in.
RULES = (
    _one_per_ip_per_day,
    _max_amount_per_day,
    _per_request_balance_cap,
    _min_claim,
)


def evaluate(faucet_cfg, ctx: RuleContext) -> list[RuleOutcome]:
    """Run every rule; return the outcomes that FAILED (empty => all passed)."""
    failures: list[RuleOutcome] = []
    for rule in RULES:
        outcome = rule(faucet_cfg, ctx)
        if outcome is not None and not outcome.passed:
            failures.append(outcome)
    return failures
