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
            "Isolated, reset-able environment maintained by the operator",
            "Connect an Electrum wallet to the public indexer and start testing",
        ),
        cons=(
            "You can't mine, reset, or get coins on demand here — that is "
            "operator-controlled (those need Bitcoin Core RPC, which is not public)",
            "Not a public network; no realistic peer behaviour",
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


def attach_commands(net_key: str, hostname: str, ports: dict[str, int]) -> list[AttachCommand]:
    """Suggested CLI recipes for connecting to this network.

    Visitor recipes use the public ports (e.g. Electrum -> the public Fulcrum
    server). The Bitcoin Core RPC recipe is **operator-only**: the node's RPC is
    bound to the server's localhost and is reachable only by someone with shell
    access to the host — it is shown for completeness, clearly marked, not as
    something a visitor can use.
    """
    spec = NETWORK_SPECS[net_key]
    chain = spec.chain
    cmds: list[AttachCommand] = []

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
