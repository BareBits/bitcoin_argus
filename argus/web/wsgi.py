"""WSGI entry point for the dashboard (``gunicorn argus.web.wsgi:app``)."""

from __future__ import annotations

from .app import create_app

app = create_app()
