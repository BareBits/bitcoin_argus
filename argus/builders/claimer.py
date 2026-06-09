"""Builder for the testnet3 / testnet4 min-difficulty block claimer.

A built-image sidecar (stock bitcoind image + python3 + the vendored
``test_framework``) that watches ``getblocktemplate`` on a real testnet and,
whenever the 20-minute rule (or a full difficulty reset) drops the next-block
target to within reach, grinds a coinbase-only block to a dedicated ``claimer``
wallet. It writes a status JSON the dashboard reads and — when the network's
faucet is enabled — auto-forwards matured coins to the faucet's LND wallet by
sending to the stable deposit address the LND nodeinfo sidecar maintains in
node #1's data volume (mounted read-only here).

Restricted by config validation to ``constants.CLAIM_NETWORKS``, so an
unsupported network never reaches this builder.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..constants import (
    CHAIN_INTERNAL_PORTS,
    CLAIMER_MAX_BLOCKS_PER_RUN,
    CLAIMER_STATE_FILE,
    MIN_DIFFICULTY_WINDOW_SECONDS,
)
from ..context import BuildContext, Fragment

_CLAIMER_SRC = Path(__file__).resolve().parent.parent / "claimer_src"
_TEST_FRAMEWORK_SRC = (
    Path(__file__).resolve().parent.parent / "signet_miner" / "test_framework"
)


def build_claimer(ctx: BuildContext) -> Fragment:
    chain = ctx.spec.chain
    rpc_internal = CHAIN_INTERNAL_PORTS[chain]["rpc"]

    # Assemble the build context: the claimer scripts + the vendored
    # test_framework (shared source with the signet miner).
    claimer_dir = ctx.out_dir / "claimer"
    if claimer_dir.exists():
        shutil.rmtree(claimer_dir)
    shutil.copytree(
        _CLAIMER_SRC,
        claimer_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    shutil.copytree(
        _TEST_FRAMEWORK_SRC,
        claimer_dir / "test_framework",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    forwards = ctx.net.claimer_forwards()
    forward_threshold_sat = round(ctx.net.claimer.forward_threshold_btc * 1e8)

    volumes = [
        "claimer_state:/state",
    ]
    if forwards:
        # Read-only access to node #1's data volume, where the LND nodeinfo
        # sidecar writes the stable deposit address (argus_addr.txt). Sending
        # there refills the faucet's on-chain wallet.
        volumes.append("lnd_data:/lnd:ro")

    service = {
        "build": {
            "context": "./claimer",
            "args": {"BITCOIND_IMAGE": "${BITCOIND_IMAGE}"},
        },
        "image": f"{ctx.project}-claimer:local",
        "container_name": f"{ctx.project}-claimer",
        "restart": "unless-stopped",
        # Root so it can write the state volume and read node #1's data volume
        # (both created root-owned), mirroring the donations / lnd-setup sidecars.
        "user": "0:0",
        "depends_on": {"bitcoind": {"condition": "service_healthy"}},
        "volumes": volumes,
        "environment": {
            "RPC_CONNECT": "bitcoind",
            "RPC_PORT": str(rpc_internal),
            "RPC_USER": "${RPC_USER}",
            "RPC_PASSWORD": "${RPC_PASSWORD}",
            "WALLET": "claimer",
            "MAX_DIFFICULTY": str(ctx.net.claimer.max_difficulty),
            "POLL_INTERVAL": str(ctx.net.claimer.poll_interval_seconds),
            "STATUS_INTERVAL": str(ctx.net.claimer.status_interval_seconds),
            "MAX_BLOCKS_PER_RUN": str(CLAIMER_MAX_BLOCKS_PER_RUN),
            "WINDOW_SECONDS": str(MIN_DIFFICULTY_WINDOW_SECONDS),
            "STATE_FILE": CLAIMER_STATE_FILE,
            "FORWARD": "1" if forwards else "0",
            "FORWARD_THRESHOLD_SAT": str(forward_threshold_sat),
        },
        "networks": [ctx.network_name],
    }

    return Fragment(
        services={"claimer": service},
        volumes={"claimer_state": {}},
    )
