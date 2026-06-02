"""The build context passed to every sub-tool builder."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import ArgusConfig, NetworkCfg
from .constants import NetworkSpec
from .resources import ResolvedResources


@dataclass
class Fragment:
    """What a sub-tool builder contributes to a network's compose project."""

    services: dict[str, dict] = field(default_factory=dict)
    volumes: dict[str, dict] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)  # merged into the project .env


@dataclass
class BuildContext:
    cfg: ArgusConfig
    net_key: str
    net: NetworkCfg
    spec: NetworkSpec
    ports: dict[str, int]
    secrets: dict[str, str]
    out_dir: Path  # generated/<net_key>; builders may write aux files here
    project: str  # docker compose project name, e.g. "argus-regtest"
    resources: ResolvedResources  # effective disk/RAM settings for this network

    @property
    def network_name(self) -> str:
        """The compose network every service attaches to."""
        return "argus"

    def ssl_on(self, service_ssl: bool) -> bool:
        """Effective SSL state = global master switch AND the service's flag."""
        return self.cfg.global_.ssl_enabled and service_ssl
