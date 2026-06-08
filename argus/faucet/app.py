"""The faucet Flask application factory.

A SEPARATE process from the dashboard (its own gunicorn/container — see
:mod:`argus.web_gen`) so a faucet bug can never take the main page down. The
shared Caddy path-routes ``/<net>/faucet`` here. Templates and themes are shared
with the dashboard via a Jinja ``ChoiceLoader``.

Request flow on POST: validate the address for the chain → run the network's
single configurable approval function → (if approved) convert to whole sats and
ask LND node #1 to send → record the payout. The four user-facing outcomes are
*invalid address*, *disapproved*, *payment failure*, and *success*.
"""

from __future__ import annotations

import os
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from flask import Flask, g, render_template, request, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix

from ..config import ArgusConfig, load_config
from ..constants import NETWORK_SPECS
from ..ports import allocate
from ..web.app import WARNING_HTML, _human_bytes
from ..web.content import VARIANTS, faucet_mine_help
from . import approval as approval_mod
from . import donations as donations_mod
from . import maintenance as maintenance_mod
from . import mempool as mempool_mod
from . import rules as rules_mod
from . import store
from .addresses import is_valid_address
from .ip import hash_ip
from .lnd import FaucetLnd, FaucetLndError

_DEFAULT_CONFIG = os.environ.get("CONFIG_PATH", "config.yaml")
_ONION_HOSTNAME = os.environ.get("ONION_HOSTNAME") or None
# Per-install secret used to hash visitor IPs for the per-IP daily limit. Absent
# in unit tests (the per-IP rule then fails open).
_IP_SALT = os.environ.get("FAUCET_IP_SALT") or None

_FAUCET_DIR = Path(__file__).resolve().parent
_WEB_DIR = _FAUCET_DIR.parent / "web"

_SATS_PER_BTC = Decimal(100_000_000)


def _sats_to_btc(sats: int | None) -> str | None:
    """Format a satoshi amount as a fixed 8-dp BTC string, or None."""
    if sats is None:
        return None
    return f"{Decimal(sats) / _SATS_PER_BTC:.8f}"


def _btc_to_sats(amount_raw: str) -> int | None:
    """Convert a BTC amount string to whole satoshis, or None if it isn't a
    clean, positive value with at most 8 decimal places (sub-satoshi rejected)."""
    try:
        amount = Decimal(amount_raw)
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not amount.is_finite() or amount <= 0:
        return None
    sats = amount * _SATS_PER_BTC
    if sats != sats.to_integral_value():
        return None
    return int(sats)


def _fmt_amt(sats: int | None) -> dict | None:
    """A ``{"btc", "sats"}`` display pair for a sat amount, or None."""
    if sats is None:
        return None
    return {"btc": _sats_to_btc(sats), "sats": f"{sats:,}"}


def _human_delta(seconds: float) -> str:
    """A compact 'Xh Ym' rendering of a positive duration, for 'try again in …'."""
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    if minutes:
        return f"{minutes}m"
    return "less than a minute"


def create_app(
    config_path: str | None = None,
    db_path: str | None = None,
    start_maintenance: bool = True,
) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(_WEB_DIR / "static"),
        static_url_path="/static",
        template_folder=str(_FAUCET_DIR / "templates"),
    )
    # Honour the shared Caddy's forwarded scheme/host AND client IP. x_for=1 is
    # essential for the per-IP daily limit: without it request.remote_addr is
    # Caddy's address, not the visitor's. Caddy's reverse_proxy forwards
    # X-Forwarded-For by default, and it is the single trusted hop in front of us.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)
    # Resolve faucet templates first, then fall back to the dashboard's (base.html,
    # footer.html, themes) so the page shares the dashboard's chrome.
    app.jinja_loader = ChoiceLoader(
        [
            FileSystemLoader(str(_FAUCET_DIR / "templates")),
            FileSystemLoader(str(_WEB_DIR / "templates")),
        ]
    )
    app.jinja_env.filters["humanbytes"] = _human_bytes

    cfg: ArgusConfig = load_config(config_path or _DEFAULT_CONFIG)
    port_map = allocate(cfg)
    store.init_db(db_path)
    # Daily purge of expired per-IP and old usage rows (in-process daemon thread,
    # coordinated through the DB so it runs once a day across gunicorn workers).
    if start_maintenance:
        maintenance_mod.start_maintenance_thread()

    # -- theming (shared cookie with the dashboard) ------------------------

    def _resolve_theme() -> str:
        themes = cfg.web.themes
        requested = request.args.get("theme")
        if requested in themes:
            g.set_theme_cookie = requested
            return requested
        cookie = request.cookies.get("theme")
        if cookie in themes:
            return cookie
        return cfg.web.default_theme

    @app.after_request
    def _persist_theme(response):
        chosen = getattr(g, "set_theme_cookie", None)
        if chosen:
            response.set_cookie("theme", chosen, max_age=31_536_000, samesite="Lax")
        return response

    @app.context_processor
    def _inject_common() -> dict:
        theme = _resolve_theme()
        css = cfg.web.themes[theme]
        theme_links = [
            {
                "name": name,
                "url": f"{request.path}?theme={name}",
                "active": name == theme,
            }
            for name in cfg.web.themes
        ]
        return {
            "web": cfg.web,
            "hostname": cfg.global_.hostname,
            "current_theme": theme,
            "theme_css": url_for("static", filename=css),
            "theme_links": theme_links,
            "warning_html": WARNING_HTML,
            "repo_url": cfg.web.repo_url,
            "onion_hostname": _ONION_HOSTNAME,
        }

    # -- helpers -----------------------------------------------------------

    def _faucet_net(net_key: str):
        """``(net_cfg, spec)`` if the faucet is enabled for ``net_key``, else
        None (→ 404)."""
        net = cfg.networks.get(net_key)
        if net is None or not net.enabled or not net.faucet.enabled:
            return None
        return net, NETWORK_SPECS[net_key]

    def _approval_for(net) -> approval_mod.ApprovalFunction:
        name = net.faucet.approval_function or cfg.global_.faucet_default_approval
        return approval_mod.get(name)

    def _recent_rows(net_key: str, limit: int, ports: dict) -> list[dict]:
        base = mempool_mod.explorer_base(cfg, net_key, ports)
        rows: list[dict] = []
        for p in store.recent(net_key, limit):
            rows.append(
                {
                    "when": time.strftime(
                        "%Y-%m-%d %H:%M UTC", time.gmtime(p.created_at)
                    ),
                    "txid": p.txid,
                    "tx_url": mempool_mod.tx_url(base, p.txid),
                    "amount_btc": p.amount_btc,
                    "address": p.address,
                    "address_url": mempool_mod.address_url(base, p.address),
                }
            )
        return rows

    def _balance(net_key, chain) -> int | None:
        """The faucet node's confirmed on-chain balance for ``net_key``, or None
        if it can't be read (the balance-derived rules then fail open)."""
        try:
            return FaucetLnd(net_key, chain).balance_sat()
        except FaucetLndError:
            return None

    def _process(
        net_key, net, spec, ports, address, amount_raw, client_ip, balance_sat, limits, now
    ) -> dict:
        """Run one dispense request; returns a ``result`` dict for the template.

        Every enabled rule is checked and ALL failures are collected (not just the
        first), so the user sees the full list of unmet requirements at once. The
        amount policy (the configurable approval function) is evaluated alongside
        the speed-limit rules and folded into the same ``failures`` list.
        """
        # 1. The address must be valid for this chain (before anything else).
        if not is_valid_address(address, spec.chain):
            return {
                "status": "invalid_address",
                "message": "That is not a valid address for this network.",
            }

        failures: list[dict] = []

        # 2. The configurable approval function (the amount policy, e.g. < 1 BTC).
        decision = _approval_for(net)(
            approval_mod.FaucetContext(
                net_key=net_key,
                chain=spec.chain,
                address=address,
                amount_raw=amount_raw,
                balance_sat=balance_sat,
            )
        )
        if not decision.approved:
            failures.append({"label": "Amount policy", "reason": decision.reason})

        # 3. Parse to whole sats (positive, ≤ 8 decimal places). When that fails the
        #    amount is fundamentally invalid, so the speed-limit rules can't run; add
        #    a format error only if the policy hasn't already objected to the amount.
        sats = _btc_to_sats(amount_raw)
        if sats is None:
            if decision.approved:
                failures.append(
                    {
                        "label": "Amount format",
                        "reason": "The amount must be a positive number with at "
                        "most 8 decimal places.",
                    }
                )
        else:
            ctx = rules_mod.RuleContext(
                net_key=net_key,
                ip_hash=hash_ip(client_ip, _IP_SALT),
                requested_sat=sats,
                balance_sat=balance_sat,
                now=now,
                limits=limits,
            )
            for outcome in rules_mod.evaluate(net.faucet, ctx):
                entry = {"label": outcome.label, "reason": outcome.reason}
                if outcome.retry_after is not None:
                    entry["retry_in"] = _human_delta(outcome.retry_after - now)
                    entry["retry_at"] = time.strftime(
                        "%H:%M UTC", time.gmtime(outcome.retry_after)
                    )
                failures.append(entry)

        # 4. AND across every rule: any failure blocks the payout.
        if failures:
            return {"status": "disapproved", "failures": failures}

        # 5. Dispense via the node; surface node errors as a payment failure.
        try:
            txid = FaucetLnd(net_key, spec.chain).send(
                address, sats, net.faucet.fee_sat_per_vbyte
            )
        except FaucetLndError as exc:
            return {"status": "payment_failure", "message": str(exc)}

        # 6. Record the payout, the per-IP claim (for the daily limit), and the
        #    day's usage count (for the max-per-day cap) — all on success only.
        amount_btc = _sats_to_btc(sats)
        store.record(net_key, txid, amount_btc, address)
        ip_hash = hash_ip(client_ip, _IP_SALT)
        if ip_hash is not None:
            store.record_ip_claim(net_key, ip_hash, now)
        store.increment_usage(net_key, now)
        base = mempool_mod.explorer_base(cfg, net_key, ports)
        return {
            "status": "success",
            "message": "Funds dispensed.",
            "txid": txid,
            "tx_url": mempool_mod.tx_url(base, txid),
            "amount_btc": amount_btc,
            "address": address,
        }

    # -- routes ------------------------------------------------------------

    @app.route("/<net_key>/faucet", methods=["GET", "POST"])
    def faucet(net_key: str):
        found = _faucet_net(net_key)
        if found is None:
            return render_template("faucet_unavailable.html", net_key=net_key), 404
        net, spec = found
        ports = port_map.get(net_key, {})

        # Read the balance once and derive the current limits — used both for the
        # page's limits panel and (on POST) for evaluating the amount-cap rules.
        balance_sat = _balance(net_key, spec.chain)
        now = time.time()
        limits = rules_mod.compute_limits(net.faucet, net_key, balance_sat, now)

        result = None
        form_address = ""
        form_amount = ""
        if request.method == "POST":
            form_address = (request.form.get("address") or "").strip()
            form_amount = (request.form.get("amount") or "").strip()
            result = _process(
                net_key, net, spec, ports, form_address, form_amount,
                request.remote_addr, balance_sat, limits, now,
            )
            if result["status"] == "success":
                form_address = form_amount = ""  # clear on success

        limits_view = {
            "one_per_day": limits.one_per_day,
            "daily_cap": _fmt_amt(limits.daily_cap_sat),
            "balance_cap": _fmt_amt(limits.balance_cap_sat),
            "balance_cap_pct": int(round(net.faucet.balance_cap_fraction * 100)),
            "min_claim": _fmt_amt(limits.min_claim_sat),
            "max_request": _fmt_amt(limits.max_request_sat),
            "balance_known": balance_sat is not None,
        }

        return render_template(
            "faucet.html",
            net_key=net_key,
            net_title=VARIANTS[net_key].title,
            chain=spec.chain,
            balance_btc=_sats_to_btc(balance_sat),
            balance_known=balance_sat is not None,
            explanation=_approval_for(net).explanation,
            reminder=donations_mod.donation_reminder(cfg, net_key),
            result=result,
            form_address=form_address,
            form_amount=form_amount,
            recent=_recent_rows(net_key, net.faucet.recent_limit, ports),
            limits=limits_view,
            mine_help=faucet_mine_help(
                net_key, cfg.global_.hostname, ports, net.bitcoind.p2p_public
            ),
        )

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    return app
