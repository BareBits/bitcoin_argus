"""Validate generated compose files with `docker compose config`.

Skipped when Docker isn't available (e.g. plain CI runners). When it is, this
catches schema mistakes in the generated docker-compose.yml without pulling
images or starting anything.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import yaml

from argus.generate import generate
from helpers import BITCART_OFF, make

_HAS_DOCKER = shutil.which("docker") is not None and (
    subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0
)

pytestmark = pytest.mark.skipif(not _HAS_DOCKER, reason="docker compose not available")


def test_generated_compose_is_valid(tmp_path):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(yaml.safe_dump(make({
        "regtest": {"enabled": True, "bitcart": BITCART_OFF},
        "signet": {"enabled": True, "bitcart": BITCART_OFF},
    })))
    generate(str(cfgp), output_dir=tmp_path / "gen", secrets_dir=tmp_path / "sec")
    for net in ("regtest", "signet"):
        d = tmp_path / "gen" / net
        r = subprocess.run(
            ["docker", "compose", "-f", str(d / "docker-compose.yml"), "config"],
            capture_output=True, text=True, cwd=d,
        )
        assert r.returncode == 0, r.stderr
