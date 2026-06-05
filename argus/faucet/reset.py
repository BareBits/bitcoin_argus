"""Purge one network's faucet payout history.

Run from a network's generated ``reset.sh`` when that network is reset, so the
faucet's recorded payouts are wiped along with the chain they belong to (the
faucet is a separate long-lived container, so the core ``down -v`` never touches
its data volume):

    python -m argus.faucet.reset <net>
"""

from __future__ import annotations

import sys

from . import store


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m argus.faucet.reset <net>", file=sys.stderr)
        return 2
    net = argv[0]
    store.init_db()
    removed = store.purge(net)
    print(f"[faucet] purged {removed} payout(s) for {net}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
