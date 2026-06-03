"""The Argus dashboard: a small Flask app that serves the welcome/landing page
and live, per-service resource usage for every enabled network.

Unlike the rest of :mod:`argus` (which only *generates* compose files) this is a
runtime component. It is built into its own image, deployed via the generated
``generated/web/`` project, and fronted by the shared Caddy layer. It reuses
:mod:`argus.config`/:mod:`argus.ports`/:mod:`argus.constants` so it never
duplicates the generator's notion of which services exist or where they listen.
"""

from __future__ import annotations

from .app import create_app

__all__ = ["create_app"]
