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
    # Per-network default auto-reset cap (GiB) when the operator doesn't pin one.
    # None => fall back to DEFAULT_RESET_MAX_SIZE_GB. The two custom signets differ
    # only here: the short-lived one keeps the standard cap, the long-lived one
    # defaults much higher so it survives far longer between resets.
    default_reset_max_size_gb: float | None = None


# Fixed ordering — drives the port-block assignment, so it must remain stable.
# The two custom signets are appended last (short keeps the original 35000 block;
# long gets 36000) so adding the long-lived one shifts no existing network's ports.
NETWORK_ORDER: tuple[str, ...] = (
    "regtest",
    "testnet3",
    "testnet4",
    "signet",
    "mutinynet",
    "custom-signet-short",
    "custom-signet-long",
)

# Default auto-reset caps (GiB). The standard cap (DEFAULT_RESET_MAX_SIZE_GB,
# defined with the rest of the reset knobs below) applies to regtest and the
# short-lived signet; the long-lived signet defaults to this larger cap so it
# persists far longer between resets. Both stay operator-overridable per network.
DEFAULT_LONG_RESET_MAX_SIZE_GB = 300.0

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
    # Operator-defined signets: the challenge is auto-generated and we mine it
    # ourselves with the signet miner. Stock bitcoind suffices (no Knots needed).
    # Two ship by default and are identical apart from their default reset cap:
    #   * short-lived — the standard cap (DEFAULT_RESET_MAX_SIZE_GB); resets often,
    #     like regtest, so it stays small and disposable.
    #   * long-lived — a much larger default cap, for testing conditions that need
    #     the chain to persist for longer stretches.
    # Each gets its OWN auto-generated challenge + signing key (secrets/<key>/), so
    # they are two genuinely independent signets, not two views of one chain.
    "custom-signet-short": NetworkSpec(
        "custom-signet-short", "signet", True, True, None, (), None, False, True, True
    ),
    "custom-signet-long": NetworkSpec(
        "custom-signet-long", "signet", True, True, None, (), None, False, True, True,
        default_reset_max_size_gb=DEFAULT_LONG_RESET_MAX_SIZE_GB,
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

# Auto-channel funding: BTC kept in the core/funding wallet after funding the ring
# nodes (the "25 BTC stays in core" goal; the steady miner grows it further).
LND_CHANNEL_CORE_RESERVE_BTC = 25.0
# How often each node's status sidecar refreshes its liquidity snapshot (the JSON
# the operator dashboard reads: deposit address, on-chain + channel balances).
LND_STATUS_INTERVAL_SECONDS = 30
# Early block subsidy on regtest/signet (BTC); used to size the initial maturity
# mining so the funding wallet has enough spendable coinbase.
EARLY_BLOCK_SUBSIDY_BTC = 50.0
COINBASE_MATURITY = 100

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

# The dashboard (gunicorn) listens here on the host loopback; the shared Caddy
# reverse-proxies to it. Deliberately below NETWORK_BLOCK_BASE so it never
# collides with a per-network port block.
WEB_BACKEND_PORT = 29080

# The faucet (a separate gunicorn from the dashboard, so a faucet bug can't crash
# the main page) listens here on the host loopback; the shared Caddy path-routes
# ``/<net>/faucet`` to it. Adjacent to WEB_BACKEND_PORT, below NETWORK_BLOCK_BASE.
FAUCET_BACKEND_PORT = 29081

# The onion-facing dashboard+faucet site: the shared Caddy serves a plain-HTTP,
# path-routed copy of the site root here (loopback) so the Tor onion's port 80 can
# reach BOTH the dashboard and the faucet (the onion forwards to a single port and
# can't path-route itself). Only used when Tor exposes the web and a faucet exists;
# otherwise the onion routes straight to WEB_BACKEND_PORT. See argus/tor.py.
ONION_WEB_BACKEND_PORT = 29082

# Shared Tor layer. The host-networked tor container exposes its SOCKS proxy here
# so the per-network LND containers can dial onion peers / advertise their onion.
# They reach it through TOR_SOCKS_HOST_ALIAS, an /etc/hosts entry pointed at the
# host gateway (Compose ``extra_hosts: host-gateway``).
TOR_SOCKS_PORT = 9050
TOR_SOCKS_HOST_ALIAS = "argus-tor-host"

# Offsets within a network's 1000-port block. Fulcrum instances are computed
# separately (they are a variable-length list).
PORT_OFFSETS: dict[str, int] = {
    "bitcoind_p2p": 0,
    "bitcoind_rpc": 1,  # host-published on 127.0.0.1 only (closed to internet)
    # zmq is container-internal only and not host-published.
    "lnd_p2p": 10,  # PUBLIC
    "lnd_rest": 11,  # 127.0.0.1
    "lnd_grpc": 12,  # 127.0.0.1 (closed to internet)
    # Second + third LND nodes (the liquidity ring's other hops; allocated for
    # collision checks, published/firewalled only when lnd.secondary/tertiary on).
    "lnd2_p2p": 13,  # PUBLIC
    "lnd2_rest": 14,  # 127.0.0.1
    "lnd2_grpc": 15,  # 127.0.0.1 (closed to internet)
    "lnd3_p2p": 16,  # PUBLIC
    "lnd3_rest": 17,  # 127.0.0.1
    "lnd3_grpc": 18,  # 127.0.0.1 (closed to internet)
    # Bitcart's btclnd uses contiguous p2p/gRPC pools (see *_OFFSET below),
    # not single ports.
    # Caddy public listeners for HTTP services:
    "cashu_public": 100,
    "cashu_wallet_public": 101,  # co-located cashu.me web wallet (per network)
    "cashupayserver_public": 120,  # CashuPayServer (BTCPay-compatible gateway)
    "bitcart_store_public": 200,
    "bitcart_admin_public": 201,
    "bitcart_api_public": 202,
    "woocommerce_public": 220,  # WordPress/WooCommerce storefront
    "mempool_public": 300,
    # Backend loopback ports (Caddy proxies to these):
    "cashu_backend": 110,
    "cashu_wallet_backend": 111,  # the per-network cashu.me nginx loopback port
    "cashupayserver_backend": 130,  # CashuPayServer Apache loopback
    "bitcart_store": 210,
    "bitcart_admin": 211,
    "bitcart_api": 212,
    "bitcart_daemon": 213,
    "woocommerce_backend": 230,  # WordPress/WooCommerce Apache loopback
    "woocommerce_db": 231,  # reserved; WordPress DB is internal-only (not published)
    "mempool_web": 301,  # frontend loopback (Caddy proxies here)
    "mempool_api": 310,  # backend API loopback (debug)
    "mempool_db": 311,  # reserved; DB is internal-only (not published)
}

# In-container listen ports for the storefront services (identical across the
# isolated per-network projects; host-facing ports come from the allocator).
CASHUPAYSERVER_INTERNAL_PORT = 80  # php:apache
WORDPRESS_INTERNAL_PORT = 80  # wordpress:apache
WORDPRESS_DB_INTERNAL_PORT = 3306  # MariaDB (internal-only)

# --- Resource profiles ------------------------------------------------------

# Tunable numeric knobs and their value per profile. The resolver applies these
# as the baseline, overridable by explicit per-knob settings (see argus.resources).
DEFAULT_RESOURCE_PROFILE = "medium"
RESOURCE_PROFILES: dict[str, dict[str, int]] = {
    "low": {
        "bitcoind_dbcache": 100,
        "bitcoind_maxmempool": 50,
        "fulcrum_db_mem": 400,
        "fulcrum_db_max_open_files": 100,
        "mempool_mariadb_buffer_mb": 64,
    },
    "medium": {
        "bitcoind_dbcache": 300,
        "bitcoind_maxmempool": 100,
        "fulcrum_db_mem": 600,
        "fulcrum_db_max_open_files": 200,
        "mempool_mariadb_buffer_mb": 128,
    },
    "high": {
        "bitcoind_dbcache": 1000,
        "bitcoind_maxmempool": 300,
        "fulcrum_db_mem": 2048,
        "fulcrum_db_max_open_files": 1000,
        "mempool_mariadb_buffer_mb": 512,
    },
}
RESOURCE_KNOBS = tuple(RESOURCE_PROFILES["medium"].keys())

# Default Docker json-file log rotation (caps unbounded log disk growth).
DEFAULT_LOG_MAX_SIZE = "10m"
DEFAULT_LOG_MAX_FILE = 3


# --- Auto-reset (chain size cap) --------------------------------------------

# Networks Argus mines, and so can tear down and re-deploy to base config when
# their chain grows past a configured cap. Resetting a chain we don't control
# (the real testnets / public signet) would be meaningless, so reset is only
# offered here. Mirrors config._MINEABLE_NETWORKS.
RESET_NETWORKS: frozenset[str] = frozenset(
    {"regtest", "custom-signet-short", "custom-signet-long"}
)

# Standard cap on a mined network's on-disk chain size (GiB) before it is reset.
# Applies to regtest and the short-lived signet; the long-lived signet defaults to
# DEFAULT_LONG_RESET_MAX_SIZE_GB (see NETWORK_SPECS above).
DEFAULT_RESET_MAX_SIZE_GB = 30.0
# How often the reset controller polls each network's size_on_disk (seconds).
DEFAULT_RESET_CHECK_INTERVAL = 300

# "Maximum use of all blocks" assumption for the time-to-reset estimate: every
# block mined at the consensus maximum serialized size (~4 MB at full weight).
# Growth/day = (86400 / block_interval_seconds) * MAX_BLOCK_BYTES; the estimate
# is deliberately conservative (fastest plausible growth -> soonest reset).
MAX_BLOCK_BYTES = 4_000_000

# The reset controller's container name + where it writes the per-network
# size/cap JSON the dashboard reads (via the read-only docker-socket-proxy
# get_archive, like donations/LND info).
RESET_CONTROLLER_CONTAINER = "argus-reset-controller"
RESET_STATE_FILE = "/state/reset_state.json"


# Argus chain -> mempool's network slot. The real testnets use their native slot
# so the explorer labels them correctly and shows mempool's built-in "test coins
# have no value" warning (the custom signets + mutinynet share chain="signet" and
# so the signet slot — fine, each runs its own instance). regtest is the exception:
# mempool's frontend hardcodes regtest out of BOTH its testnet-warning list AND
# its Lightning-supported network list, so we run it in the "mainnet" slot
# instead (network="") — that keeps the Lightning Explorer nav enabled — and add
# our own warning banner via an nginx sub_filter (see build_mempool).
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
