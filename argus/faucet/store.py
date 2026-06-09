"""Persistent record of faucet payouts (a small set of SQLite tables, via peewee).

Rows are keyed by network so a single faucet process serves every network and a
network reset can purge just that network's rows (see :mod:`argus.faucet.reset`,
invoked from the per-network ``reset.sh``). Stored on the faucet's own data
volume — the dashboard's metrics cache is separate.

Beyond the payout log, three tables back the speed-limit rules
(:mod:`argus.faucet.rules`):

* :class:`IpClaim` — one row per ``(net, salted-IP-hash)`` holding the time of
  that visitor's most recent successful withdrawal; backs the one-per-IP-per-day
  rule. Short-lived: rows older than 24h are purged daily.
* :class:`DailyUsage` — a compact per-``(net, UTC-day)`` count of successful
  withdrawals; backs the max-amount-per-day cap. Retained for a year (and NOT
  wiped on a network reset, since it models visitor demand, not chain state).
* :class:`Maintenance` — a single row whose ``last_run`` timestamp lets the
  in-process maintenance thread claim the once-a-day purge exactly once across
  gunicorn's worker processes (see :mod:`argus.faucet.maintenance`).
"""

from __future__ import annotations

import os
import time

from peewee import (
    CharField,
    FloatField,
    IntegerField,
    IntegrityError,
    Model,
    SqliteDatabase,
    fn,
)

_DB_PATH = os.environ.get("FAUCET_DB", "/faucet/faucet.db")
database = SqliteDatabase(None)

# Rolling window for the per-IP daily limit and Ip-claim retention (seconds).
DAY_SECONDS = 86_400
# How long per-day usage counts are kept (the trailing-year cap window).
USAGE_RETENTION_DAYS = 365


def day_ordinal(ts: float) -> int:
    """The UTC day number (days since the Unix epoch) for ``ts`` — the key used
    to bucket usage counts. Timezone-free and stable."""
    return int(ts // DAY_SECONDS)


class Payout(Model):
    """One dispensed payout."""

    net = CharField(index=True)
    txid = CharField()
    amount_btc = CharField()  # normalized BTC string (8 dp)
    address = CharField()
    created_at = FloatField()

    class Meta:
        database = database


class IpClaim(Model):
    """The most recent successful withdrawal time for a salted IP hash on a
    network. One row per ``(net, ip_hash)`` (unique), upserted on each success."""

    net = CharField()
    ip_hash = CharField()
    last_claim_at = FloatField()

    class Meta:
        database = database
        indexes = ((("net", "ip_hash"), True),)  # unique


class DailyUsage(Model):
    """Successful-withdrawal count for a network on one UTC day."""

    net = CharField()
    day = IntegerField()  # day_ordinal()
    count = IntegerField(default=0)

    class Meta:
        database = database
        indexes = ((("net", "day"), True),)  # unique


class RedeemedNonce(Model):
    """A single-use proof-of-work challenge nonce that has been spent, kept until
    the challenge would have expired. Backs PoW replay protection: a solved
    challenge can be redeemed exactly once (see :mod:`argus.faucet.pow`)."""

    net = CharField()
    nonce = CharField()
    expires_at = FloatField()

    class Meta:
        database = database
        indexes = ((("net", "nonce"), True),)  # unique


class PowDailyClaim(Model):
    """Per-``(net, salted-IP-hash, UTC-day)`` count of PoW-earned claims, so a
    network can cap PoW claims per IP per day (testnet3 allows one)."""

    net = CharField()
    ip_hash = CharField()
    day = IntegerField()  # day_ordinal()
    count = IntegerField(default=0)

    class Meta:
        database = database
        indexes = ((("net", "ip_hash", "day"), True),)  # unique


class Maintenance(Model):
    """Singleton row coordinating the daily purge across worker processes."""

    last_run = FloatField(default=0.0)

    class Meta:
        database = database


def init_db(path: str | None = None) -> None:
    """Bind and create the schema. Safe to call more than once."""
    db_path = path or _DB_PATH
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    database.init(db_path)
    if database.is_closed():
        database.connect(reuse_if_open=True)
    database.create_tables(
        [Payout, IpClaim, DailyUsage, RedeemedNonce, PowDailyClaim, Maintenance]
    )
    # Seed the single maintenance row (last_run=0 => the first claim wins).
    if not Maintenance.select().exists():
        Maintenance.create(last_run=0.0)


def record(
    net: str, txid: str, amount_btc: str, address: str, ts: float | None = None
) -> None:
    Payout.create(
        net=net,
        txid=txid,
        amount_btc=amount_btc,
        address=address,
        created_at=time.time() if ts is None else ts,
    )


def recent(net: str, limit: int) -> list["Payout"]:
    """The most recent payouts for ``net``, newest first."""
    return list(
        Payout.select()
        .where(Payout.net == net)
        .order_by(Payout.created_at.desc())
        .limit(limit)
    )


def purge(net: str) -> int:
    """Delete every payout row for ``net``. Returns the number removed.

    Note: this intentionally does NOT touch :class:`DailyUsage` — usage history
    survives a network reset (it models demand, not chain state). The short-lived
    :class:`IpClaim` rows self-expire within a day, so a reset leaves them be too.
    """
    return Payout.delete().where(Payout.net == net).execute()


# -- per-IP daily limit ------------------------------------------------------


def record_ip_claim(net: str, ip_hash: str, ts: float | None = None) -> None:
    """Upsert the last successful-withdrawal time for ``(net, ip_hash)``."""
    when = time.time() if ts is None else ts
    (
        IpClaim.insert(net=net, ip_hash=ip_hash, last_claim_at=when)
        .on_conflict(
            conflict_target=[IpClaim.net, IpClaim.ip_hash],
            update={IpClaim.last_claim_at: when},
        )
        .execute()
    )


def last_ip_claim(net: str, ip_hash: str) -> float | None:
    """The most recent successful-withdrawal time for ``(net, ip_hash)``, or
    ``None`` if this IP has never had a successful withdrawal on ``net``."""
    row = IpClaim.get_or_none((IpClaim.net == net) & (IpClaim.ip_hash == ip_hash))
    return row.last_claim_at if row else None


def purge_ip_claims(now: float | None = None) -> int:
    """Delete IP-claim rows older than the 24h window. Returns the number removed."""
    cutoff = (time.time() if now is None else now) - DAY_SECONDS
    return IpClaim.delete().where(IpClaim.last_claim_at < cutoff).execute()


# -- usage history (per-day counts) ------------------------------------------


def increment_usage(net: str, ts: float | None = None) -> None:
    """Add one to ``net``'s successful-withdrawal count for the current UTC day."""
    day = day_ordinal(time.time() if ts is None else ts)
    updated = (
        DailyUsage.update(count=DailyUsage.count + 1)
        .where((DailyUsage.net == net) & (DailyUsage.day == day))
        .execute()
    )
    if not updated:
        # First withdrawal of the day; on_conflict guards a concurrent insert.
        (
            DailyUsage.insert(net=net, day=day, count=1)
            .on_conflict(
                conflict_target=[DailyUsage.net, DailyUsage.day],
                update={DailyUsage.count: DailyUsage.count + 1},
            )
            .execute()
        )


def usage_today(net: str, now: float | None = None) -> int:
    """Successful-withdrawal count for ``net`` on the current UTC day (drives the
    PoW demand-retarget factor)."""
    day = day_ordinal(time.time() if now is None else now)
    row = DailyUsage.get_or_none((DailyUsage.net == net) & (DailyUsage.day == day))
    return row.count if row else 0


def usage_stats(net: str, now: float | None = None) -> tuple[int, int]:
    """``(expected_365, historical_max)`` for ``net``.

    ``historical_max`` is the busiest single day's count ever recorded.
    ``expected_365`` is the expected number of withdrawals over the next 365 days:
    the trailing-year sum where each of the 365 days contributes its actual count
    if logged, else the missing-day fill value ``max(historical_max, 10)``. This
    equals (trailing-year daily average) x 365, and is always >= 365*10, so the
    caller can divide the balance by it without a zero-division guard.
    """
    today = day_ordinal(time.time() if now is None else now)
    start = today - 364  # inclusive 365-day window
    counts = {
        row.day: row.count
        for row in DailyUsage.select().where(
            (DailyUsage.net == net)
            & (DailyUsage.day >= start)
            & (DailyUsage.day <= today)
        )
    }
    historical_max = (
        DailyUsage.select(fn.MAX(DailyUsage.count))
        .where(DailyUsage.net == net)
        .scalar()
        or 0
    )
    fill = max(historical_max, 10)
    expected = sum(counts.get(day, fill) for day in range(start, today + 1))
    return expected, historical_max


def purge_usage(now: float | None = None) -> int:
    """Delete usage rows older than the retention window. Returns rows removed."""
    cutoff = day_ordinal(time.time() if now is None else now) - USAGE_RETENTION_DAYS
    return DailyUsage.delete().where(DailyUsage.day < cutoff).execute()


# -- proof-of-work: single-use nonces ----------------------------------------


def redeem_nonce(net: str, nonce: str, expires_at: float) -> bool:
    """Atomically mark ``(net, nonce)`` spent. Returns ``True`` if this caller
    won the redemption (the nonce was unseen), ``False`` if it was already spent
    — which is exactly the replay rejection. The ``(net, nonce)`` UNIQUE index
    makes the second insert raise, so concurrent redemptions can't both win."""
    try:
        RedeemedNonce.insert(net=net, nonce=nonce, expires_at=expires_at).execute()
        return True
    except IntegrityError:
        return False


def purge_redeemed_nonces(now: float | None = None) -> int:
    """Delete spent-nonce rows whose challenges have expired. Returns rows removed."""
    cutoff = time.time() if now is None else now
    return RedeemedNonce.delete().where(RedeemedNonce.expires_at < cutoff).execute()


# -- proof-of-work: per-IP daily claim counts --------------------------------


def pow_claims_today(net: str, ip_hash: str, ts: float | None = None) -> int:
    """How many PoW-earned claims ``ip_hash`` has made on ``net`` this UTC day."""
    day = day_ordinal(time.time() if ts is None else ts)
    row = PowDailyClaim.get_or_none(
        (PowDailyClaim.net == net)
        & (PowDailyClaim.ip_hash == ip_hash)
        & (PowDailyClaim.day == day)
    )
    return row.count if row else 0


def record_pow_claim(net: str, ip_hash: str, ts: float | None = None) -> None:
    """Increment ``ip_hash``'s PoW-claim count for ``net`` on the current UTC day."""
    day = day_ordinal(time.time() if ts is None else ts)
    updated = (
        PowDailyClaim.update(count=PowDailyClaim.count + 1)
        .where(
            (PowDailyClaim.net == net)
            & (PowDailyClaim.ip_hash == ip_hash)
            & (PowDailyClaim.day == day)
        )
        .execute()
    )
    if not updated:
        (
            PowDailyClaim.insert(net=net, ip_hash=ip_hash, day=day, count=1)
            .on_conflict(
                conflict_target=[
                    PowDailyClaim.net,
                    PowDailyClaim.ip_hash,
                    PowDailyClaim.day,
                ],
                update={PowDailyClaim.count: PowDailyClaim.count + 1},
            )
            .execute()
        )


def purge_pow_claims(now: float | None = None) -> int:
    """Delete PoW-claim count rows older than a day. Returns rows removed."""
    cutoff = day_ordinal(time.time() if now is None else now) - 1
    return PowDailyClaim.delete().where(PowDailyClaim.day < cutoff).execute()


# -- maintenance coordination ------------------------------------------------


def claim_maintenance_run(now: float | None = None, interval: int = DAY_SECONDS) -> bool:
    """Atomically claim the once-per-``interval`` maintenance run.

    Returns ``True`` to exactly one caller per interval (the one whose UPDATE
    flips ``last_run``), so the in-process thread runs the purge once a day even
    with multiple gunicorn workers racing.
    """
    when = time.time() if now is None else now
    updated = (
        Maintenance.update(last_run=when)
        .where(Maintenance.last_run <= when - interval)
        .execute()
    )
    return updated == 1
