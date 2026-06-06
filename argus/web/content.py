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


# Recommended order: regtest -> custom signets (short then long) -> mutinynet ->
# signet -> testnet4 -> testnet3. Developers should pick the most contained variant
# that still exercises what they need.
VARIANT_ORDER: tuple[str, ...] = (
    "regtest",
    "custom-signet-short",
    "custom-signet-long",
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
    "custom-signet-short": Variant(
        key="custom-signet-short",
        title="Custom Signet (short-lived)",
        blurb=(
            "A signet whose blocks are produced with a private signing key held by "
            "the site operator. You get signet's realistic rules; the operator "
            "decides when blocks are produced. This one is reset often (on a small "
            "size cap), so it stays compact and disposable — reach for it first."
        ),
        pros=(
            "Realistic signet rules on a stable, reproducible chain",
            "Shared with you without being exposed to the whole world",
            "Connect wallets to the public indexer like any other network",
            "Reset frequently, so it stays small and predictable",
        ),
        cons=(
            "Block production is operator-controlled (the operator holds the signer)",
            "Resets relatively often — not for tests that must run for weeks",
            "Smaller peer set than the public signet",
        ),
    ),
    "custom-signet-long": Variant(
        key="custom-signet-long",
        title="Custom Signet (long-lived)",
        blurb=(
            "The same operator-signed custom signet, but with a much larger size "
            "cap so it persists for long stretches before resetting. Use it when "
            "you need a stable signet that survives for weeks — for soak tests, "
            "long-running channels, and aged-chain conditions."
        ),
        pros=(
            "Realistic signet rules on a long-lived, reproducible chain",
            "Survives far longer between resets than the short-lived signet",
            "Ideal for soak tests, long-running Lightning channels, and aged state",
            "Connect wallets to the public indexer like any other network",
        ),
        cons=(
            "Block production is operator-controlled (the operator holds the signer)",
            "Grows much larger on disk before it resets",
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
    "custom-signet-short": (
        "Realistic signet consensus rules without exposing your work publicly",
        "Stable, reproducible chain shared only with people you invite",
        "Operator-held signer means predictable, controlled block production",
        "Reset often, so it stays small — ideal for quick, disposable runs",
        "Connect wallets to the public indexer like any other network",
    ),
    "custom-signet-long": (
        "Realistic signet rules on a chain that persists for weeks, not hours",
        "Best for soak tests and long-running Lightning channels that must age",
        "Operator-held signer means predictable, controlled block production",
        "Shared only with people you invite, like the short-lived signet",
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


# --- sub-tool versions + source repos ----------------------------------------

# GitHub source repositories for each sub-tool, used to link the version shown in
# the services table. Our own components (faucet, regtest/signet miner) point at
# the Argus repo; the Bitcoin Core node links upstream, except mutinynet, whose
# signetblocktime node is a fork. ``electrum`` / ``lnd`` here are the *client*
# tools linked from the "attach your tools" recipes.
SUBTOOL_REPO: dict[str, str] = {
    "bitcoind": "https://github.com/bitcoin/bitcoin",
    "bitcoind_signet_fork": "https://github.com/MutinyWallet/mutiny-net",
    "lnd": "https://github.com/lightningnetwork/lnd",
    "fulcrum": "https://github.com/cculianu/Fulcrum",
    "cashu": "https://github.com/cashubtc/nutshell",
    "cashu_wallet": "https://github.com/cashubtc/cashu.me",
    "mempool": "https://github.com/mempool/mempool",
    "bitcart": "https://github.com/BareBits/bitcart",
    "electrum": "https://github.com/spesmilo/electrum",
    "argus": "https://github.com/BareBits/bitcoin_argus",
}


def image_version(image: str) -> str:
    """The tag of a docker image reference (``repo/name:tag`` -> ``tag``).

    Splits on the final path segment first so a registry ``host:port/...`` prefix
    can't be mistaken for the tag. Returns ``""`` when the reference has no tag."""
    name = image.rsplit("/", 1)[-1]
    return name.split(":", 1)[1] if ":" in name else ""


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
    # Optional Tor variant shown in the SAME code box, on the line below the
    # clearnet command (e.g. the onion connect URI for an LND node).
    command_onion: str = ""


# --- OS-specific "attach your tools" recipes ---------------------------------

# The three operating systems the visitor attach instructions are tabbed by, in
# display order. Linux is the default tab (the typical Bitcoin dev environment).
ATTACH_OS_ORDER: tuple[str, ...] = ("linux", "macos", "windows")
ATTACH_OS_LABELS: dict[str, str] = {
    "linux": "Linux",
    "macos": "macOS",
    "windows": "Windows",
}
ATTACH_DEFAULT_OS: str = "linux"

# Default binary locations per OS, verified against each project's official
# install docs (June 2026). Where a tool ships only as a PATH binary — lncli on
# every OS (LND has no installer), and Electrum/bitcoind on Linux (pip/AppImage,
# tarball) — the bare command name is used. The Electrum Windows installer names
# the exe ``electrum-<version>.exe`` under this folder; ``electrum.exe`` stands in
# for it (see the recipe note).
_ELECTRUM_BIN: dict[str, str] = {
    "linux": "electrum",
    "macos": "/Applications/Electrum.app/Contents/MacOS/run_electrum",
    "windows": r'"C:\Program Files (x86)\Electrum\electrum.exe"',
}
_BITCOIND_BIN: dict[str, str] = {
    "linux": "bitcoind",
    "macos": "/Applications/Bitcoin-Qt.app/Contents/MacOS/bitcoind",
    "windows": r'"C:\Program Files\Bitcoin\daemon\bitcoind.exe"',
}
_LNCLI_BIN: dict[str, str] = {
    "linux": "lncli",
    "macos": "lncli",
    "windows": "lncli.exe",
}


@dataclass(frozen=True)
class AttachVariant:
    """One operating system's form of an attach recipe. The recipe is the same on
    every OS; only the binary's default path/name changes. ``command`` may span
    several lines (e.g. one connect line per LND node, each node's 🧅 onion variant
    on the line below its clearnet line)."""

    os: str  # one of ATTACH_OS_ORDER
    command: str


@dataclass(frozen=True)
class AttachTool:
    """A collapsible tool group in "Attach your tools": one command per OS plus a
    shared note. ``key`` is a stable id used for element ids/CSS."""

    key: str  # "electrum" | "bitcoind" | "lncli"
    title: str
    repo_url: str
    variants: tuple[AttachVariant, ...]
    note: str = ""


def attach_tool_groups(
    net_key: str,
    hostname: str,
    ports: dict[str, int],
    p2p_public: bool,
    signet_challenge: str | None,
    lnd_uris: list[tuple[str, str, int]],
    onion_hostname: str | None = None,
) -> list[AttachTool]:
    """Visitor "attach your tools" recipes, grouped by tool with one command per
    OS (the recipe is identical; only the default binary path changes).

    Up to three groups, each emitted only when it applies to this network:

    * **Electrum** — point a wallet at the public Fulcrum server (when one runs).
    * **Bitcoin Core** — peer your own node over P2P, gated on a public P2P port.
      Custom signets get the ``-signetchallenge`` a vanilla node needs to accept
      their blocks; on regtest the note adds the considerate mining follow-up.
    * **Lightning (lncli)** — connect / open a channel to the public LND node(s),
      one connect line per known node (argus1 first), with onion variants on Tor.

    ``signet_challenge`` is the custom-signet challenge (None for regtest and the
    public networks). ``lnd_uris`` is ``(label, pubkey, p2p_port)`` per node.
    """
    spec = NETWORK_SPECS[net_key]
    chain = spec.chain
    tools: list[AttachTool] = []

    # --- Electrum -------------------------------------------------------------
    electrum_port = ports.get("fulcrum_0_electrum_tcp")
    if electrum_port is not None:
        flag = _ELECTRUM_FLAG.get(chain)
        flag_part = f" {flag}" if flag else ""
        note = "':t' selects plaintext TCP (a TLS port lands later)."
        if not flag:
            note = (
                "Electrum has no dedicated testnet4 flag yet; use a testnet4-aware "
                "build. " + note
            )
        note += (
            " On Windows the installer names the exe electrum-<version>.exe in that "
            "folder."
        )
        variants = tuple(
            AttachVariant(
                os=os_key,
                command=(
                    f"{_ELECTRUM_BIN[os_key]}{flag_part} --oneserver "
                    f"--server {hostname}:{electrum_port}:t"
                ),
            )
            for os_key in ATTACH_OS_ORDER
        )
        tools.append(
            AttachTool(
                key="electrum",
                title="Electrum",
                repo_url=SUBTOOL_REPO["electrum"],
                variants=variants,
                note=note,
            )
        )

    # --- Bitcoin Core ---------------------------------------------------------
    # Peer your own node with ours over P2P. Only meaningful when the P2P port is
    # public. A custom signet (is_signet but not the public signet) additionally
    # needs its challenge to accept blocks: Mutinynet's is a public constant, but
    # an operator-generated signet's challenge lives in the node's secret store —
    # not in the dashboard's config — so there we emit a placeholder + note.
    if p2p_public and "bitcoind_p2p" in ports:
        p2p = ports["bitcoind_p2p"]
        needs_challenge = spec.is_signet and net_key != "signet"
        placeholder = needs_challenge and not signet_challenge
        challenge_part = ""
        if needs_challenge:
            challenge_part = f" -signetchallenge={signet_challenge or '<signet-challenge>'}"
        variants = tuple(
            AttachVariant(
                os=os_key,
                command=(
                    f"{_BITCOIND_BIN[os_key]} -chain={chain}{challenge_part} "
                    f"-addnode={hostname}:{p2p} -daemon"
                ),
            )
            for os_key in ATTACH_OS_ORDER
        )
        if net_key == "regtest":
            note = (
                "Starts your own regtest node and peers it with ours so it syncs "
                "this chain. Once peered, mine to your own wallet:\n"
                "  bitcoin-cli -regtest generatetoaddress 1 "
                "$(bitcoin-cli -regtest getnewaddress)\n"
                "Please be considerate — this is a shared chain, so mine only the "
                "blocks you need; over-mining or deep reorgs disrupt others (handy "
                "for robustness testing, but don't overdo it). The P2P port opens "
                "automatically once the node's two Lightning channels are set up, "
                "so it may be briefly closed right after a fresh deploy."
            )
        else:
            note = (
                "Starts your own Bitcoin Core node and peers it with ours over P2P "
                "so it syncs this chain."
            )
            if placeholder:
                note += (
                    " This is a custom signet — replace <signet-challenge> with this "
                    "network's signetchallenge (it's in the node's bitcoin.conf; ask "
                    "the operator) so your node accepts its blocks."
                )
            elif needs_challenge:
                note += (
                    " The -signetchallenge is what makes your node accept this "
                    "custom signet's blocks."
                )
        tools.append(
            AttachTool(
                key="bitcoind",
                title="Bitcoin Core",
                repo_url=SUBTOOL_REPO["bitcoind"],
                variants=variants,
                note=note,
            )
        )

    # --- Lightning (lncli) ----------------------------------------------------
    # One connect line per node (argus1 first). When Tor is on, each node's onion
    # connect line follows its clearnet line, tagged with 🧅 — for every node, not
    # just the first, so all of argus1/2/3 carry the icon.
    if lnd_uris:
        multi = len(lnd_uris) > 1
        ln_variants: list[AttachVariant] = []
        for os_key in ATTACH_OS_ORDER:
            lncli = _LNCLI_BIN[os_key]
            lines: list[str] = []
            for label, pk, p2p in lnd_uris:
                if multi:
                    lines.append(f"# {label}")
                lines.append(f"{lncli} connect {pk}@{hostname}:{p2p}")
                if onion_hostname:
                    lines.append(f"🧅 {lncli} connect {pk}@{onion_hostname}:{p2p}")
            ln_variants.append(AttachVariant(os=os_key, command="\n".join(lines)))
        tools.append(
            AttachTool(
                key="lncli",
                title="Lightning (lncli)",
                repo_url=SUBTOOL_REPO["lnd"],
                variants=tuple(ln_variants),
                note=(
                    "The node's public connection URI is pubkey@host:port. LND ships "
                    "as release binaries with no installer — put lncli on your PATH."
                ),
            )
        )

    return tools


def attach_commands(
    net_key: str,
    ports: dict[str, int],
) -> list[AttachCommand]:
    """Operator-only CLI recipes for this network.

    Just the Bitcoin Core RPC recipe: the node's RPC is bound to the server's
    localhost and reachable only with shell access to the host — shown for
    completeness, clearly marked, not something a visitor can use. Visitor recipes
    (Electrum, peer-your-own-node, lncli connect) live in :func:`attach_tool_groups`.
    """
    spec = NETWORK_SPECS[net_key]
    chain = spec.chain
    cmds: list[AttachCommand] = []

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
