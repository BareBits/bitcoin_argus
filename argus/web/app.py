"""The Flask application factory for the Argus dashboard."""

from __future__ import annotations

import os

from flask import Flask, g, render_template, request, url_for

from ..config import ArgusConfig, load_config
from ..ports import allocate
from . import cache, metrics
from .content import when_to_use_columns
from .inventory import build_sections

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


def create_app(config_path: str | None = None, cache_db: str | None = None) -> Flask:
    app = Flask(__name__)
    cfg: ArgusConfig = load_config(config_path or _DEFAULT_CONFIG)
    port_map = allocate(cfg)
    cache.init_db(cache_db)

    app.jinja_env.filters["humanbytes"] = _human_bytes

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
        return render_template(
            "index.html",
            sections=sections,
            when_columns=when_to_use_columns(net_keys),
            host=payload.get("host", {}),
            metrics_errors=payload.get("errors", []),
            cache_age=int(age),
        )

    @app.route("/tos")
    def tos():
        return render_template("tos.html")

    @app.route("/privacy")
    def privacy():
        return render_template("privacy.html")

    @app.route("/healthz")
    def healthz():
        return "ok", 200

    return app
