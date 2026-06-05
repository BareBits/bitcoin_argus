"""Persistent record of faucet payouts (one SQLite table, via peewee).

Rows are keyed by network so a single faucet process serves every network and a
network reset can purge just that network's rows (see :mod:`argus.faucet.reset`,
invoked from the per-network ``reset.sh``). Stored on the faucet's own data
volume — the dashboard's metrics cache is separate.
"""

from __future__ import annotations

import os
import time

from peewee import CharField, FloatField, Model, SqliteDatabase

_DB_PATH = os.environ.get("FAUCET_DB", "/faucet/faucet.db")
database = SqliteDatabase(None)


class Payout(Model):
    """One dispensed payout."""

    net = CharField(index=True)
    txid = CharField()
    amount_btc = CharField()  # normalized BTC string (8 dp)
    address = CharField()
    created_at = FloatField()

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
    database.create_tables([Payout])


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
    """Delete every payout row for ``net``. Returns the number removed."""
    return Payout.delete().where(Payout.net == net).execute()
