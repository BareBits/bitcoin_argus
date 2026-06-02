"""Sub-tool registry.

Adding a sub-tool = write a builder module that returns a :class:`Fragment`, then
register it here. ``include`` decides, per network, whether the sub-tool is part
of that network's stack. Order is the order services appear in the compose file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..context import BuildContext, Fragment
from .bitcoind import build_bitcoind
from .lnd import build_lnd
from .miner import build_miner


@dataclass(frozen=True)
class SubTool:
    name: str
    builder: Callable[[BuildContext], Fragment]
    include: Callable[[BuildContext], bool]


# Chain + LND + regtest miner. Later phases append fulcrum, cashu, bitcart,
# mempool, and the shared caddy layer.
REGISTRY: list[SubTool] = [
    SubTool("bitcoind", build_bitcoind, lambda c: True),
    SubTool("lnd", build_lnd, lambda c: True),
    SubTool(
        "miner",
        build_miner,
        lambda c: c.spec.supports_miner and c.net.miner.enabled,
    ),
]
