"""Daily faucet housekeeping, run by an in-process background thread.

The faucet keeps two tables that accrue rows over time: short-lived per-IP claim
rows and per-day usage counts. Once a day we purge IP rows past the 24h window
and usage rows past the retention year, so the database doesn't grow without
bound. Rather than a separate cron job or sidecar container, a daemon thread
inside the faucet process wakes hourly and runs the purge when a day has elapsed
— coordinated through :func:`store.claim_maintenance_run` so it fires exactly
once across gunicorn's worker processes, and re-arms naturally across restarts.

Run the same purge by hand with::

    python -m argus.faucet.maintenance
"""

from __future__ import annotations

import threading
import time

from . import store

# How often the thread wakes to check whether a day has elapsed.
_TICK_SECONDS = 3600


def run_maintenance(now: float | None = None) -> tuple[int, int]:
    """Purge expired rows. Idempotent. Returns ``(ip_claims, usage_rows)`` for the
    two long-window tables; the short-lived PoW tables (spent nonces and per-day
    PoW-claim counts) are purged too but not counted in the return."""
    store.purge_redeemed_nonces(now)
    store.purge_pow_claims(now)
    return store.purge_ip_claims(now), store.purge_usage(now)


def _loop(tick: int) -> None:
    while True:
        try:
            if store.claim_maintenance_run():
                run_maintenance()
        except Exception:
            # Never let a maintenance hiccup take down the worker; try again next tick.
            pass
        time.sleep(tick)


def start_maintenance_thread(tick: int = _TICK_SECONDS) -> threading.Thread:
    """Start the daemon maintenance loop. Safe to call once per worker process —
    the atomic daily claim means only one worker actually runs each purge."""
    thread = threading.Thread(
        target=_loop, args=(tick,), name="faucet-maintenance", daemon=True
    )
    thread.start()
    return thread


def main() -> int:
    store.init_db()
    ip_removed, usage_removed = run_maintenance()
    print(
        f"[faucet] maintenance: purged {ip_removed} IP-claim row(s), "
        f"{usage_removed} usage row(s)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
