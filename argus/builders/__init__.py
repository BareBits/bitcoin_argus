"""Sub-tool registry.

Adding a sub-tool = write a builder module that returns a :class:`Fragment`, then
register it here. ``include`` decides, per network, whether the sub-tool is part
of that network's stack. Order is the order services appear in the compose file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..constants import CLAIM_NETWORKS
from ..context import BuildContext, Fragment
from .ark import build_ark
from .bitcoind import build_bitcoind
from .cashu import build_cashu
from .claimer import build_claimer
from .cashu_wallet import build_cashu_wallet
from .cashupayserver import build_cashupayserver
from .donations import build_donations
from .fedimint import build_fedimint
from .fulcrum import build_fulcrum
from .lnd import build_lnd
from .mempool import build_mempool
from .miner import build_miner
from .woocommerce import build_woocommerce


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
    SubTool(
        "cashu-wallet",
        build_cashu_wallet,
        lambda c: c.net.cashu.enabled and c.net.cashu.wallet,
    ),
    # Fedimint federation + per-ring-node Lightning gateways (alongside Cashu).
    # Auto-disabled on any chain Fedimint can't run (none today; see config).
    SubTool("fedimint", build_fedimint, lambda c: c.net.fedimint_enabled(c.spec)),
    # Ark ASP (captaind + a Core Lightning bridge that opens one channel into the
    # ring). Auto-disabled on any chain Ark can't run (none today; see config).
    SubTool("ark", build_ark, lambda c: c.net.ark_enabled(c.spec)),
    # CashuPayServer (BTCPay-compatible gateway) and the WooCommerce storefront
    # that points at it. CashuPayServer is listed first so its pairing volume is
    # declared before WooCommerce references it (order is cosmetic for volumes,
    # but it keeps the compose file readable).
    SubTool(
        "cashupayserver",
        build_cashupayserver,
        lambda c: c.net.cashupayserver.enabled,
    ),
    SubTool("woocommerce", build_woocommerce, lambda c: c.net.woocommerce.enabled),
    SubTool("mempool", build_mempool, lambda c: c.net.mempool_enabled(c.spec)),
    SubTool(
        "miner",
        build_miner,
        lambda c: c.spec.supports_miner and c.net.miner.enabled,
    ),
    # Opportunistic min-difficulty block claimer for the real testnets
    # (testnet3/testnet4). Config validation restricts it to CLAIM_NETWORKS.
    SubTool(
        "claimer",
        build_claimer,
        lambda c: c.net_key in CLAIM_NETWORKS and c.net.claimer.enabled,
    ),
    # Donation address + figures writer. Always on: it works off bitcoind (always
    # present), reusing the miner's wallet where there is one.
    SubTool("donations", build_donations, lambda c: True),
]
