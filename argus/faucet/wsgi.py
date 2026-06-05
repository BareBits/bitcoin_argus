"""Gunicorn entrypoint for the faucet (``argus.faucet.wsgi:app``)."""

from __future__ import annotations

from .app import create_app

app = create_app()
