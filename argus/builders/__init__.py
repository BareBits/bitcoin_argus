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
from .cashu import build_cashu
from .donations import build_donations
from .fulcrum import build_fulcrum
from .lnd import build_lnd
from .mempool import build_mempool
from .miner import build_miner


@dataclass(frozen=True)
class SubTool:
    name: str
    builder: Callable[[BuildContext], Fragment]
    include: Callable[[BuildContext], bool]


# Chain + LND + Fulcrum + Cashu + regtest miner. Later phases append bitcart,
# mempool. The shared Caddy layer is generated separately (see argus.shared).
REGISTRY: list[SubTool] = [
    SubTool("bitcoind", build_bitcoind, lambda c: True),
    SubTool("lnd", build_lnd, lambda c: True),
    SubTool("fulcrum", build_fulcrum, lambda c: bool(c.net.enabled_indexers())),
    SubTool("cashu", build_cashu, lambda c: c.net.cashu.enabled),
    SubTool("mempool", build_mempool, lambda c: c.net.mempool_enabled(c.spec)),
    SubTool(
        "miner",
        build_miner,
        lambda c: c.spec.supports_miner and c.net.miner.enabled,
    ),
    # Donation address + figures writer. Always on: it works off bitcoind (always
    # present), reusing the miner's wallet where there is one.
    SubTool("donations", build_donations, lambda c: True),
]
