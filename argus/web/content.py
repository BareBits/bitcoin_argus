"""Static, human-facing facts about each network variant and the command-line
recipes for attaching local tools to them.

Kept separate from the live data so the copy is easy to review and edit. The
ordering reflects the recommendation in the page: reach for the most contained,
fastest-feedback option first (regtest) and only move outward as your testing
needs demand it.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import NETWORK_SPECS


@dataclass(frozen=True)
class Variant:
    key: str
    title: str
    blurb: str
    pros: tuple[str, ...]
    cons: tuple[str, ...]


# Recommended order: regtest -> custom signet -> mutinynet -> signet -> testnet4
# -> testnet3. Developers should pick the most contained variant that still
# exercises what they need.
VARIANT_ORDER: tuple[str, ...] = (
    "regtest",
    "custom-signet",
    "mutinynet",
    "signet",
    "testnet4",
    "testnet3",
)

VARIANTS: dict[str, Variant] = {
    "regtest": Variant(
        key="regtest",
        title="Regtest",
        blurb=(
            "A private chain controlled by the site operator. The operator mines "
            "blocks on a fixed schedule (about one a minute), so confirmations are "
            "fast — but block production and resets are the operator's, not yours."
        ),
        pros=(
            "Fast confirmations — the operator mines a block about once a minute",
            "When the P2P port is public, you can attach your own regtest node and "
            "mine blocks yourself (see the mining recipe below)",
            "Connect an Electrum wallet to the public indexer and start testing",
        ),
        cons=(
            "Trivial mining difficulty + a public P2P port means ANYONE can mine, "
            "so expect reorgs — please mine only the blocks you actually need",
            "Operator-controlled resets; coins/wallet admin (Core RPC) stay private",
        ),
    ),
    "custom-signet": Variant(
        key="custom-signet",
        title="Custom Signet",
        blurb=(
            "A signet whose blocks are produced with a private signing key held by "
            "the site operator. You get signet's realistic rules; the operator "
            "decides when blocks are produced."
        ),
        pros=(
            "Realistic signet rules on a stable, reproducible chain",
            "Shared with you without being exposed to the whole world",
            "Connect wallets to the public indexer like any other network",
        ),
        cons=(
            "Block production is operator-controlled (the operator holds the signer)",
            "Smaller peer set than the public signet",
        ),
    ),
    "mutinynet": Variant(
        key="mutinynet",
        title="Mutinynet (30s signet)",
        blurb=(
            "A custom signet tuned for fast 30-second blocks — great for exercising "
            "confirmation flows and Lightning. Blocks are produced by the operator's "
            "signer; you connect to it like any shared network."
        ),
        pros=(
            "30-second blocks: quick confirmations, still a shared network",
            "Excellent for Lightning and time-sensitive flows",
        ),
        cons=(
            "Block production is operator-controlled (signetblocktime signer)",
            "Faster blocks mean faster chain growth (more disk over time)",
        ),
    ),
    "signet": Variant(
        key="signet",
        title="Public Signet",
        blurb=(
            "The default, globally shared signet. Realistic, stable, and reliable, "
            "with coins handed out by community faucets."
        ),
        pros=(
            "Stable and predictable — closest to mainnet behaviour",
            "Widely used; good interoperability testing",
        ),
        cons=(
            "~10-minute blocks; slower feedback than regtest/mutinynet",
            "Shared globally — not for private experiments",
        ),
    ),
    "testnet4": Variant(
        key="testnet4",
        title="Testnet4",
        blurb=(
            "The current public testnet. Proof-of-work like mainnet, with fixes for "
            "testnet3's long-standing difficulty-reset and griefing issues."
        ),
        pros=(
            "Public PoW network closest to mainnet mechanics",
            "Healthier than testnet3 (anti-griefing rules)",
        ),
        cons=(
            "Variable block times; reorgs happen",
            "Coins can be scarce; mining races affect timing",
        ),
    ),
    "testnet3": Variant(
        key="testnet3",
        title="Testnet3",
        blurb=(
            "The long-running legacy testnet. Still around for compatibility, but "
            "prefer testnet4 for new work."
        ),
        pros=(
            "Maximum compatibility with older tooling",
            "Large historical chain available",
        ),
        cons=(
            "Notorious difficulty-reset swings and block-storm griefing",
            "Largest chain to sync; mostly superseded by testnet4",
        ),
    ),
}


# --- "Which test network should I use?" column copy --------------------------

# The synthetic column for plain local regtest (not one of this host's networks).
LOCAL_REGTEST_KEY = "local-regtest"
LOCAL_REGTEST_TITLE = "Regtest on your machine"

# Five "when this makes the most sense" reasons per network mode. Rendered as
# side-by-side columns for the enabled networks plus the local-regtest column.
WHEN_TO_USE: dict[str, tuple[str, ...]] = {
    LOCAL_REGTEST_KEY: (
        "Total control: you mine, you reset, you set the rules — no operator",
        "Instant blocks on demand (generatetoaddress) — never wait for a confirmation",
        "Fully offline and private; nothing ever leaves your machine",
        "Best for unit/integration tests and CI — deterministic and disposable",
        "Safely try advanced tricks like double-spends and hand-crafted reorgs",
    ),
    "regtest": (
        "Fastest feedback of the shared nets — blocks about once a minute",
        "Attach your own node over the public P2P port and mine blocks yourself",
        "No coin scarcity — coins come from mining, with no faucet to chase",
        "Great for smoke-testing wallets and indexers against a clean chain",
        "Lowest resource footprint of the hosted networks",
    ),
    "custom-signet": (
        "Realistic signet consensus rules without exposing your work publicly",
        "Stable, reproducible chain shared only with people you invite",
        "Operator-held signer means predictable, controlled block production",
        "Ideal for private demos and integration tests that need signet semantics",
        "Connect wallets to the public indexer like any other network",
    ),
    "mutinynet": (
        "30-second blocks: exercise confirmation flows fast, still shared",
        "Excellent for Lightning channel open/close and time-sensitive logic",
        "Realistic signet rules at a much higher tempo than public signet",
        "Good for iteration or light load testing where 10-minute blocks drag",
        "Shared network, so several parties can interact on the same chain",
    ),
    "signet": (
        "Closest to mainnet behaviour of the public test networks",
        "Globally shared — ideal for interoperability testing with other software",
        "Stable and predictable, with community faucets handing out coins",
        "Long-lived chain, good when you want to avoid surprise reorgs",
        "No private infrastructure needed; everyone reaches the same network",
    ),
    "testnet4": (
        "Public proof-of-work network closest to mainnet mining mechanics",
        "Healthier than testnet3 thanks to anti-griefing and difficulty fixes",
        "Good for testing real PoW dynamics: variable block times and reorgs",
        "Interoperability testing with other testnet4-aware tooling",
        "Use when you specifically need a public PoW testnet, not a signet",
    ),
    "testnet3": (
        "Maximum compatibility with older tools that only speak testnet3",
        "Large historical chain available for sync and scale testing",
        "Public PoW network for interoperability with legacy software",
        "Handy to reproduce testnet3-specific behaviours or bugs",
        "Still reachable when a dependency hasn't moved to testnet4 yet",
    ),
}


@dataclass(frozen=True)
class WhenColumn:
    key: str
    title: str
    reasons: tuple[str, ...]


def when_to_use_columns(enabled_keys: list[str]) -> list[WhenColumn]:
    """Columns for the picker: local regtest first, then each enabled network
    in the recommended order."""
    cols = [
        WhenColumn(LOCAL_REGTEST_KEY, LOCAL_REGTEST_TITLE, WHEN_TO_USE[LOCAL_REGTEST_KEY])
    ]
    for key in VARIANT_ORDER:
        if key in enabled_keys and key in WHEN_TO_USE:
            cols.append(WhenColumn(key, VARIANTS[key].title, WHEN_TO_USE[key]))
    return cols


# Public mempool.space Lightning node pages, keyed by Argus network *key* (not
# chain): only the truly public networks belong here. The custom signets
# (mutinynet, custom-signet) share chain="signet" but are private, so they must
# never map to mempool.space/signet — keying off the network key avoids that.
# Used as a fallback link when a network has no local mempool of its own.
MEMPOOL_SPACE_LN_NODE: dict[str, str] = {
    "testnet3": "https://mempool.space/testnet/lightning/node/",
    "testnet4": "https://mempool.space/testnet4/lightning/node/",
    "signet": "https://mempool.space/signet/lightning/node/",
}


# Electrum's network selection flag per chain (testnet4 has no dedicated flag yet).
_ELECTRUM_FLAG: dict[str, str | None] = {
    "regtest": "--regtest",
    "test": "--testnet",
    "testnet4": None,
    "signet": "--signet",
}


@dataclass(frozen=True)
class AttachCommand:
    label: str
    command: str
    note: str = ""
    audience: str = "visitor"  # "visitor" (anyone) or "operator" (host access only)


def attach_commands(
    net_key: str,
    hostname: str,
    ports: dict[str, int],
    p2p_public: bool = True,
) -> list[AttachCommand]:
    """Suggested CLI recipes for connecting to this network.

    Visitor recipes use the public ports (e.g. Electrum -> the public Fulcrum
    server). The Bitcoin Core RPC recipe is **operator-only**: the node's RPC is
    bound to the server's localhost and is reachable only by someone with shell
    access to the host — it is shown for completeness, clearly marked, not as
    something a visitor can use.

    On regtest, when the node's P2P port is public, a visitor *can* mine: they run
    their own regtest node peered with ours and call ``generatetoaddress`` locally
    (their blocks propagate over P2P). ``p2p_public`` gates that recipe.
    """
    spec = NETWORK_SPECS[net_key]
    chain = spec.chain
    cmds: list[AttachCommand] = []

    # Visitor mining recipe — only regtest (signet needs the operator's signing
    # key) and only when the P2P port is actually exposed.
    if net_key == "regtest" and p2p_public:
        p2p = ports["bitcoind_p2p"]
        cmds.append(
            AttachCommand(
                label="Mine regtest blocks (attach your own node)",
                command=(
                    f"# Peer your own regtest node with ours, then mine to your wallet:\n"
                    f"bitcoind -regtest -addnode={hostname}:{p2p} -daemon\n"
                    f"bitcoin-cli -regtest generatetoaddress 1 "
                    f"$(bitcoin-cli -regtest getnewaddress)"
                ),
                note=(
                    "Please be considerate: this is a shared chain — mine only the "
                    "blocks you need. Over-mining or deep reorgs disrupt others "
                    "(handy for robustness testing, but don't overdo it). This port "
                    "opens automatically once the node's two Lightning channels are "
                    "set up, so it may be briefly closed right after a fresh deploy."
                ),
                audience="visitor",
            )
        )

    # First Fulcrum instance, if any, exposes a public Electrum TCP port — this
    # is the recipe a visitor actually uses.
    electrum_port = ports.get("fulcrum_0_electrum_tcp")
    if electrum_port is not None:
        flag = _ELECTRUM_FLAG.get(chain)
        flag_part = f" {flag}" if flag else ""
        note = (
            ""
            if flag
            else "Electrum has no dedicated testnet4 flag yet; use a testnet4-aware build."
        )
        cmds.append(
            AttachCommand(
                label="Electrum wallet (via the public Fulcrum server)",
                command=(
                    f"electrum{flag_part} --oneserver "
                    f"--server {hostname}:{electrum_port}:t"
                ),
                note=note or "':t' selects plaintext TCP (TLS port lands later).",
                audience="visitor",
            )
        )

    rpc_port = ports.get("bitcoind_rpc")
    if rpc_port is not None:
        cmds.append(
            AttachCommand(
                label="Bitcoin Core RPC",
                command=(
                    f"# Run on the server (operator shell access required):\n"
                    f"bitcoin-cli -rpcconnect=127.0.0.1 -rpcport={rpc_port} "
                    f"-chain={chain} \\\n"
                    f"  -rpcuser=<rpc-user> -rpcpassword=<rpc-password> getblockchaininfo"
                ),
                note=(
                    "Operator-only: the node's RPC is bound to the server's localhost "
                    "and is not reachable over the internet. Visitors cannot connect to "
                    "it (not even by SSH tunnel — that needs a host login). It drives "
                    "mining, wallet, and admin actions, which is why it stays private."
                ),
                audience="operator",
            )
        )

    return cmds
