"""The Flask application factory for the Argus dashboard."""

from __future__ import annotations

import os

from flask import Flask, g, jsonify, render_template, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

from ..config import ArgusConfig, load_config
from ..constants import NETWORK_ORDER
from ..ports import allocate
from . import cache, history, metrics
from .content import (
    ATTACH_DEFAULT_OS,
    ATTACH_OS_LABELS,
    ATTACH_OS_ORDER,
    VARIANTS,
    when_to_use_columns,
)
from .inventory import build_donations, build_sections
from .lnurl import LnurlError, LnurlService

_DEFAULT_CONFIG = os.environ.get("CONFIG_PATH", "config.yaml")
# Set by the generated dashboard compose when Tor is enabled (see web_gen.py).
_ONION_HOSTNAME = os.environ.get("ONION_HOSTNAME") or None

# Bold, unmissable warning shown on every page.
WARNING_HTML = (
    "These are <strong>TESTNET</strong> installations. The coins here have "
    "<strong>NO VALUE</strong>. Using these services may result in the "
    "<strong>loss of any coins</strong> you put on them. We make "
    "<strong>no guarantees</strong> about uptime, coin safety, or anything else — "
    "you use these services <strong>entirely at your own risk</strong>."
)


def _human_bytes(value) -> str:
    """Render a byte count as a compact human string (or 'n/a')."""
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(num) < 1024.0 or unit == "PB":
            if unit == "B":
                return f"{int(num)} {unit}"
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def _human_cores(value) -> str:
    """Render CPU usage in *cores* (e.g. 0.42), or 'n/a'."""
    if value is None:
        return "n/a"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "n/a"
    if num < 0.005:
        return "~0"
    return f"{num:.2f}"


# Accepted look-back windows for the /stats graphs: label -> seconds. Bounded by
# the retention tiers (raw 24h, hourly 3d, daily 365d).
_RANGES: dict[str, int] = {
    "1h": 3600,
    "6h": 6 * 3600,
    "24h": 86400,
    "3d": 3 * 86400,
    "30d": 30 * 86400,
    "365d": 365 * 86400,
}
_DEFAULT_RANGE = "24h"


def create_app(config_path: str | None = None, cache_db: str | None = None) -> Flask:
    app = Flask(__name__)
    # Honour the shared Caddy's X-Forwarded-Proto/Host so externally-built URLs
    # (the LNURL callback) carry the public https scheme + hostname. Over the
    # onion, tor forwards straight to gunicorn with no proxy headers, so the
    # request's own http scheme + onion host are used instead — both correct.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
    cfg: ArgusConfig = load_config(config_path or _DEFAULT_CONFIG)
    port_map = allocate(cfg)
    cache.init_db(cache_db)

    lnurl = LnurlService(cfg)

    app.jinja_env.filters["humanbytes"] = _human_bytes
    app.jinja_env.filters["humancores"] = _human_cores

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
            response.set_cookie(
                "theme", chosen, max_age=31_536_000, samesite="Lax"
            )
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
            "metrics_history_enabled": cfg.web.metrics_history.enabled,
        }

    net_keys = [k for k, _ in cfg.enabled_networks()]

    def _load_metrics(force: bool = False):
        return cache.get_or_refresh(
            lambda: metrics.collect(net_keys).as_dict(), force=force
        )

    @app.route("/")
    def index():
        payload, age = _load_metrics()
        sections = build_sections(cfg, port_map, payload, _ONION_HOSTNAME)
        donations = build_donations(cfg, payload, _ONION_HOSTNAME)
        # Default-selected network tab: the first enabled network, falling back to
        # the first network present (so a tab is always open).
        default_tab = next(
            (s.key for s in sections if s.enabled),
            sections[0].key if sections else None,
        )
        return render_template(
            "index.html",
            sections=sections,
            donations=donations,
            donations_by_key={d.key: d for d in donations},
            default_tab=default_tab,
            when_columns=when_to_use_columns(net_keys),
            attach_os_order=ATTACH_OS_ORDER,
            attach_os_labels=ATTACH_OS_LABELS,
            attach_default_os=ATTACH_DEFAULT_OS,
            host=payload.get("host", {}),
            metrics_errors=payload.get("errors", []),
            cache_age=int(age),
        )

    # ------------------------------------------------------------------
    # Resource history: the /stats graphs page + the JSON it fetches. Both are
    # no-ops (the page explains, the API returns an empty payload) when history
    # collection is disabled, so links never dead-end.
    # ------------------------------------------------------------------
    _net_titles = {
        k: (VARIANTS[k].title if k in VARIANTS else k)
        for k in NETWORK_ORDER
        if k in cfg.networks
    }

    @app.route("/stats")
    def stats():
        return render_template(
            "stats.html",
            history_enabled=cfg.web.metrics_history.enabled,
            net_titles=_net_titles,
            ranges=list(_RANGES.keys()),
            default_range=_DEFAULT_RANGE,
            host_key=history.HOST_KEY,
        )

    @app.route("/api/metrics/history")
    def metrics_history_api():
        if not cfg.web.metrics_history.enabled:
            return jsonify(enabled=False, series={}), 200
        rng = request.args.get("range", _DEFAULT_RANGE)
        range_seconds = _RANGES.get(rng)
        if range_seconds is None:
            return jsonify(status="ERROR", reason="invalid range"), 400
        try:
            payload = history.load_series(
                range_seconds,
                cfg.web.metrics_history.raw_retention_hours,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            return jsonify(enabled=True, series={}, error=str(exc)), 200
        payload["enabled"] = True
        payload["range"] = rng
        return jsonify(payload)

    @app.route("/tos")
    def tos():
        return render_template("tos.html")

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.route("/contact")
    def contact():
        return render_template("contact.html")

    # ------------------------------------------------------------------
    # LNURL-pay / Lightning Address (LUD-06 + LUD-16). Served from the site
    # root so fees@/cashout@/donate@/referral@<hostname> resolve here, and over
    # the onion for free (same app). See argus/web/lnurl.py.
    # ------------------------------------------------------------------
    @app.route("/.well-known/lnurlp/<name>")
    def lnurlp(name: str):
        if not lnurl.enabled:
            return jsonify(status="ERROR", reason="LNURL is not enabled"), 404
        callback = url_for("lnurlp_callback", name=name, _external=True)
        try:
            return jsonify(lnurl.pay_request(name, request.host, callback))
        except LnurlError as exc:
            return jsonify(status="ERROR", reason=str(exc)), 404

    @app.route("/lnurlp/<name>/callback")
    def lnurlp_callback(name: str):
        if not lnurl.enabled:
            return jsonify(status="ERROR", reason="LNURL is not enabled"), 404
        amount = request.args.get("amount", type=int)
        if amount is None:
            return jsonify(status="ERROR", reason="missing or invalid amount"), 400
        comment = request.args.get("comment")
        try:
            pr = lnurl.invoice(name, request.host, amount, comment)
        except LnurlError as exc:
            # LUD-06 errors are reported as 200 + {status: ERROR} so wallets read
            # the reason; the name itself was syntactically valid to route here.
            return jsonify(status="ERROR", reason=str(exc))
        return jsonify(pr=pr, routes=[])

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    return app
