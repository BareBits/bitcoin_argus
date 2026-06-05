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
from ..web.content import VARIANTS
from . import approval as approval_mod
from . import donations as donations_mod
from . import mempool as mempool_mod
from . import store
from .addresses import is_valid_address
from .lnd import FaucetLnd, FaucetLndError

_DEFAULT_CONFIG = os.environ.get("CONFIG_PATH", "config.yaml")
_ONION_HOSTNAME = os.environ.get("ONION_HOSTNAME") or None

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


def create_app(config_path: str | None = None, db_path: str | None = None) -> Flask:
    app = Flask(
        __name__,
        static_folder=str(_WEB_DIR / "static"),
        static_url_path="/static",
        template_folder=str(_FAUCET_DIR / "templates"),
    )
    # Honour the shared Caddy's forwarded scheme/host (mirrors the dashboard).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
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

    def _process(net_key, net, spec, ports, address, amount_raw) -> dict:
        """Run one dispense request; returns a ``result`` dict for the template."""
        # 1. The address must be valid for this chain (before anything else).
        if not is_valid_address(address, spec.chain):
            return {
                "status": "invalid_address",
                "message": "That is not a valid address for this network.",
            }
        # Read the node balance once, to pass to the policy and (later) the send.
        lnd = FaucetLnd(net_key, spec.chain)
        try:
            balance_sat: int | None = lnd.balance_sat()
        except FaucetLndError:
            balance_sat = None
        # 2. The single configurable approval function decides.
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
            return {"status": "disapproved", "message": decision.reason}
        # 3. Mechanical conversion to whole sats (positive, ≤ 8 decimal places).
        sats = _btc_to_sats(amount_raw)
        if sats is None or sats <= 0:
            return {
                "status": "disapproved",
                "message": "The amount must be a positive number with at most "
                "8 decimal places.",
            }
        # 4. Dispense via the node; surface node errors as a payment failure.
        try:
            txid = lnd.send(address, sats, net.faucet.fee_sat_per_vbyte)
        except FaucetLndError as exc:
            return {"status": "payment_failure", "message": str(exc)}
        amount_btc = _sats_to_btc(sats)
        store.record(net_key, txid, amount_btc, address)
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

        result = None
        form_address = ""
        form_amount = ""
        if request.method == "POST":
            form_address = (request.form.get("address") or "").strip()
            form_amount = (request.form.get("amount") or "").strip()
            result = _process(net_key, net, spec, ports, form_address, form_amount)
            if result["status"] == "success":
                form_address = form_amount = ""  # clear on success

        lnd = FaucetLnd(net_key, spec.chain)
        try:
            balance_sat: int | None = lnd.balance_sat()
        except FaucetLndError:
            balance_sat = None

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
        )

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    return app
