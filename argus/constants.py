"""Static specifications: the known networks and the per-network port layout.

These values are intentionally data-only so that adding a network or a sub-tool is
a localized change. Nothing here depends on user configuration.
"""

from __future__ import annotations

from dataclasses import dataclass


# --- Networks ---------------------------------------------------------------

# The public Mutinynet signet challenge (30s blocks). Sourced from the Mutiny
# Wallet team's published bitcoin.conf. Requires a Bitcoin Knots / patched build.
MUTINYNET_CHALLENGE = (
    "512102f7561d208dd9ae99bf497273e16f389bdbd6c4742ddb8e6b216e64fa2928ad8f51ae"
)


@dataclass(frozen=True)
class NetworkSpec:
    """Immutable facts about a supported network (independent of user config)."""

    key: str
    chain: str  # bitcoind ``chain=`` selector: regtest|test|testnet4|signet
    is_signet: bool
    requires_challenge: bool  # True => user MUST supply a signet challenge
    default_signet_challenge: str | None  # used when the user supplies none
    default_addnodes: tuple[str, ...]
    signetblocktime: int | None  # mutinynet=30; needs Bitcoin Knots
    needs_knots: bool  # network requires the Knots build (signetblocktime support)
    default_mempool: bool  # is the self-hosted explorer on by default?
    supports_miner: bool  # can Argus drive block production for this net?


# Fixed ordering — drives the port-block assignment, so it must remain stable.
NETWORK_ORDER: tuple[str, ...] = (
    "regtest",
    "testnet3",
    "testnet4",
    "signet",
    "mutinynet",
    "custom-signet",
)

NETWORK_SPECS: dict[str, NetworkSpec] = {
    "regtest": NetworkSpec(
        "regtest", "regtest", False, False, None, (), None, False, True, True
    ),
    "testnet3": NetworkSpec(
        "testnet3", "test", False, False, None, (), None, False, False, False
    ),
    "testnet4": NetworkSpec(
        "testnet4", "testnet4", False, False, None, (), None, False, False, False
    ),
    # Default public signet: is_signet but needs no custom challenge.
    "signet": NetworkSpec(
        "signet", "signet", True, False, None, (), None, False, False, False
    ),
    # Mutinynet: custom signet, 30s blocks, ships a default challenge + seed node.
    "mutinynet": NetworkSpec(
        "mutinynet",
        "signet",
        True,
        False,
        MUTINYNET_CHALLENGE,
        ("45.79.52.207:38333",),
        30,
        True,
        True,
        False,
    ),
    # Operator-defined signet: challenge is mandatory; we can mine it ourselves.
    "custom-signet": NetworkSpec(
        "custom-signet", "signet", True, True, None, (), None, True, True, True
    ),
}

# Conventional *internal* (in-container) ports per chain. They can be identical
# across networks because each network runs in its own isolated compose project;
# the host-facing ports come from the allocator below.
CHAIN_INTERNAL_PORTS: dict[str, dict[str, int]] = {
    "regtest": {"rpc": 18443, "p2p": 18444},
    "test": {"rpc": 18332, "p2p": 18333},
    "testnet4": {"rpc": 48332, "p2p": 48333},
    "signet": {"rpc": 38332, "p2p": 38333},
}
ZMQ_BLOCK_INTERNAL = 28332
ZMQ_TX_INTERNAL = 28333

# LND's in-container listen ports (identical across isolated networks).
LND_INTERNAL_PORTS: dict[str, int] = {"p2p": 9735, "grpc": 10009, "rest": 8080}

# Fulcrum's in-container listen ports.
FULCRUM_INTERNAL_PORTS: dict[str, int] = {"tcp": 50001, "ssl": 50002, "admin": 8000}

# Map each chain to LND's bitcoin.<network> config key.
LND_NETWORK_KEY: dict[str, str] = {
    "regtest": "regtest",
    "test": "testnet",
    "testnet4": "testnet4",
    "signet": "signet",
}


# --- Port layout ------------------------------------------------------------

NETWORK_BLOCK_BASE = 30000  # regtest=30000, testnet3=31000, ...
NETWORK_BLOCK_SIZE = 1000  # large block per network to allow expansion

# Offsets within a network's 1000-port block. Fulcrum instances are computed
# separately (they are a variable-length list).
PORT_OFFSETS: dict[str, int] = {
    "bitcoind_p2p": 0,
    "bitcoind_rpc": 1,  # host-published on 127.0.0.1 only (closed to internet)
    # zmq is container-internal only and not host-published.
    "lnd_p2p": 10,  # PUBLIC
    "lnd_rest": 11,  # 127.0.0.1
    "lnd_grpc": 12,  # 127.0.0.1 (closed to internet)
    # Bitcart's btclnd uses contiguous p2p/gRPC pools (see *_OFFSET below),
    # not single ports.
    # Caddy public listeners for HTTP services:
    "cashu_public": 100,
    "bitcart_store_public": 200,
    "bitcart_admin_public": 201,
    "bitcart_api_public": 202,
    "mempool_public": 300,
    # Backend loopback ports (Caddy proxies to these):
    "cashu_backend": 110,
    "bitcart_store": 210,
    "bitcart_admin": 211,
    "bitcart_api": 212,
    "bitcart_daemon": 213,
    "mempool_web": 301,  # frontend loopback (Caddy proxies here)
    "mempool_api": 310,  # backend API loopback (debug)
    "mempool_db": 311,  # reserved; DB is internal-only (not published)
}

# mempool has no "regtest" network, so we run it in mainnet mode against the
# regtest node (data is correct; the UI label is cosmetic). Others map directly.
MEMPOOL_NETWORK_MAP: dict[str, str] = {
    "regtest": "mainnet",
    "test": "testnet",
    "testnet4": "testnet4",
    "signet": "signet",
}

# Bitcart's btclnd uses contiguous port pools (one LND wallet per port) within
# the network block's expansion area: p2p at base+400.., gRPC at base+450..
BITCART_BTCLND_P2P_OFFSET = 400
BITCART_BTCLND_GRPC_OFFSET = 450

# Fulcrum instances occupy 40 + i*stride within the block.
FULCRUM_BASE = 40
FULCRUM_STRIDE = 4  # electrum_tcp, electrum_ssl, admin, (spare)
FULCRUM_MAX_INSTANCES = (PORT_OFFSETS["cashu_public"] - FULCRUM_BASE) // FULCRUM_STRIDE
