#!/usr/bin/env python3
"""Bitcoin Argus — testnet3 / testnet4 min-difficulty block claimer.

The public testnets carry the "20-minute rule": when no block has been found
for twice the 10-minute target spacing, the next block may be mined at the
minimum difficulty (difficulty 1). The rarer full difficulty reset leaves the
whole chain at ~difficulty 1 for a stretch. Either way ``getblocktemplate``
reports an easy ``bits`` target. This sidecar watches for that and, whenever the
next-block difficulty is at or below ``MAX_DIFFICULTY``, grinds a coinbase-only
block (paying a dedicated ``claimer`` wallet) and submits it — capturing the
subsidy.

Block assembly + serialization reuse Bitcoin Core's vendored ``test_framework``
(the same copy the signet miner uses); the proof-of-work itself is found by
shelling out to ``bitcoin-util grind`` over the 80-byte header, exactly as the
signet miner does. The script also writes a small status JSON the dashboard
reads, and (optionally) forwards matured coins to the faucet's LND wallet.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, _HERE)  # so ``test_framework`` (a sibling dir) imports

from test_framework.authproxy import AuthServiceProxy, JSONRPCException  # noqa: E402
from test_framework.blocktools import (  # noqa: E402
    get_witness_script,
    script_BIP34_coinbase_height,
)
from test_framework.messages import (  # noqa: E402
    CBlock,
    CBlockHeader,
    COutPoint,
    CTransaction,
    CTxIn,
    CTxInWitness,
    CTxOut,
    from_hex,
    ser_uint256,
)

# --- configuration (from the environment, wired by the builder) -------------

RPC_CONNECT = os.environ.get("RPC_CONNECT", "bitcoind")
RPC_PORT = int(os.environ.get("RPC_PORT", "0"))
RPC_USER = os.environ.get("RPC_USER", "")
RPC_PASSWORD = os.environ.get("RPC_PASSWORD", "")
WALLET = os.environ.get("WALLET", "claimer")
MAX_DIFFICULTY = float(os.environ.get("MAX_DIFFICULTY", "1.0"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))
STATUS_INTERVAL = int(os.environ.get("STATUS_INTERVAL", "30"))
MAX_BLOCKS_PER_RUN = int(os.environ.get("MAX_BLOCKS_PER_RUN", "100"))
STATE_FILE = os.environ.get("STATE_FILE", "/state/claimer.json")
ADDRESS_FILE = os.environ.get("ADDRESS_FILE", "/state/claimer_address")
GRIND_CMD = os.environ.get("GRIND_CMD", "bitcoin-util grind").split()

# Forwarding (only wired when the faucet is enabled for this network).
FORWARD = os.environ.get("FORWARD", "0") == "1"
# The LND nodeinfo sidecar keeps a stable p2wkh deposit address here, in node
# #1's data volume (mounted read-only). Sending there refills the faucet.
LND_ADDR_FILE = os.environ.get("LND_ADDR_FILE", "/lnd/argus_addr.txt")
FORWARD_THRESHOLD_SAT = int(os.environ.get("FORWARD_THRESHOLD_SAT", "100000"))

# Bumping nTime gives the grinder a fresh 2**32 nonce search; at difficulty 1 a
# single nonce sweep only succeeds ~63% of the time, so retry with a later time.
_GRIND_TIME_BUMPS = 120
# Difficulty-1 target (nBits 0x1d00ffff): 0xffff << (8 * (0x1d - 3)).
_DIFF1_TARGET = 0xFFFF << (8 * (0x1D - 3))


def log(msg: str) -> None:
    print(f"[claimer] {msg}", flush=True)


# --- difficulty helpers ------------------------------------------------------


def nbits_to_target(nbits: int) -> int:
    """Decode compact ``nBits`` into the 256-bit PoW target (mirrors Core)."""
    shift = (nbits >> 24) & 0xFF
    return (nbits & 0x007FFFFF) * 2 ** (8 * (shift - 3))


def target_to_difficulty(target: int) -> float:
    """Difficulty relative to difficulty 1 (larger target => easier)."""
    if target <= 0:
        return float("inf")
    return _DIFF1_TARGET / target


# --- RPC ---------------------------------------------------------------------


def _proxy(path: str = "") -> AuthServiceProxy:
    url = f"http://{RPC_USER}:{RPC_PASSWORD}@{RPC_CONNECT}:{RPC_PORT}{path}"
    return AuthServiceProxy(url, timeout=120)


def wait_for_synced_node() -> AuthServiceProxy:
    """Block until bitcoind answers RPC and has finished initial sync."""
    node = _proxy()
    log("waiting for bitcoind RPC + initial sync...")
    while True:
        try:
            info = node.getblockchaininfo()
            if not info.get("initialblockdownload", True):
                log(f"node synced at height {info['blocks']}")
                return node
        except (JSONRPCException, OSError, ValueError):
            pass
        time.sleep(5)


def ensure_wallet() -> AuthServiceProxy:
    """Create/load the dedicated ``claimer`` wallet; return a wallet-scoped RPC."""
    node = _proxy()
    try:
        # (wallet_name, disable_private_keys, blank, passphrase, avoid_reuse,
        #  descriptors, load_on_startup): a descriptor wallet (v28 default; legacy
        # creation is unavailable) that auto-loads on bitcoind restart.
        node.createwallet(WALLET, False, False, "", False, True, True)
    except JSONRPCException:
        try:
            node.loadwallet(WALLET)
        except JSONRPCException:
            pass  # already loaded
    return _proxy(f"/wallet/{WALLET}")


def claim_address(wallet: AuthServiceProxy) -> str:
    """A stable receive address, reused across restarts so coinbase outputs
    consolidate in one place. Re-derived only if the persisted one is gone."""
    addr = ""
    if os.path.exists(ADDRESS_FILE):
        with open(ADDRESS_FILE) as fh:
            addr = fh.read().strip()
        try:
            if not wallet.getaddressinfo(addr).get("ismine", False):
                addr = ""
        except JSONRPCException:
            addr = ""
    if not addr:
        addr = wallet.getnewaddress("claimer", "bech32")
        os.makedirs(os.path.dirname(ADDRESS_FILE), exist_ok=True)
        with open(ADDRESS_FILE, "w") as fh:
            fh.write(addr)
        log(f"claiming rewards to {addr}")
    return addr


# --- block assembly + grind --------------------------------------------------


def _build_block(tmpl: dict, reward_spk: bytes) -> CBlock:
    """A coinbase-only block paying ``reward_spk``, with a witness commitment.

    No mempool transactions are included — an empty block is the simplest valid
    block and avoids fee/commitment edge cases; on testnet we only want the
    subsidy anyway."""
    cb = CTransaction()
    cb.vin = [
        CTxIn(
            COutPoint(0, 0xFFFFFFFF),
            script_BIP34_coinbase_height(tmpl["height"]),
            0xFFFFFFFF,
        )
    ]
    cb.vout = [CTxOut(tmpl["coinbasevalue"], reward_spk)]

    block = CBlock()
    block.nVersion = tmpl["version"]
    block.hashPrevBlock = int(tmpl["previousblockhash"], 16)
    block.nTime = max(tmpl["curtime"], tmpl["mintime"])
    block.nBits = int(tmpl["bits"], 16)
    block.nNonce = 0
    block.vtx = [cb]

    # BIP141 witness commitment over the (coinbase-only) witness merkle root.
    witnonce = 0
    witroot = block.calc_witness_merkle_root()
    cbwit = CTxInWitness()
    cbwit.scriptWitness.stack = [ser_uint256(witnonce)]
    block.vtx[0].wit.vtxinwit = [cbwit]
    block.vtx[0].vout.append(CTxOut(0, bytes(get_witness_script(witroot, witnonce))))
    block.vtx[0].rehash()
    block.hashMerkleRoot = block.calc_merkle_root()
    return block


def _grind(block: CBlock, target: int) -> bool:
    """Find a nonce for ``block`` via ``bitcoin-util grind``.

    A single nonce sweep at difficulty 1 only succeeds ~63% of the time, so on a
    miss we bump nTime (a fresh search space) and retry. Returns whether a valid
    proof was found within the retry budget."""
    base_time = block.nTime
    for bump in range(_GRIND_TIME_BUMPS):
        block.nTime = base_time + bump
        headhex = CBlockHeader.serialize(block).hex()
        out = subprocess.run(
            GRIND_CMD + [headhex], stdout=subprocess.PIPE, input=b"", check=True
        ).stdout.strip()
        solved = from_hex(CBlockHeader(), out.decode())
        block.nNonce = solved.nNonce
        block.rehash()
        if block.sha256 <= target:
            return True
    return False


def try_claim_one(node: AuthServiceProxy, reward_spk: bytes) -> tuple[bool, float]:
    """Attempt to claim the next block.

    Returns ``(claimed, difficulty)``. ``claimed`` is False (without mining) when
    the next-block difficulty exceeds ``MAX_DIFFICULTY`` — the normal case
    outside a min-difficulty window."""
    tmpl = node.getblocktemplate({"rules": ["segwit"]})
    target = nbits_to_target(int(tmpl["bits"], 16))
    difficulty = target_to_difficulty(target)
    if difficulty > MAX_DIFFICULTY:
        return False, difficulty

    block = _build_block(tmpl, reward_spk)
    if not _grind(block, target):
        log(f"grind found no nonce for height {tmpl['height']} (will retry)")
        return False, difficulty
    reject = node.submitblock(block.serialize().hex())
    if reject:
        log(f"submitblock rejected height {tmpl['height']}: {reject}")
        return False, difficulty
    log(
        f"claimed block {block.hash} at height {tmpl['height']} "
        f"(difficulty {difficulty:.4f}, reward {tmpl['coinbasevalue'] / 1e8:.8f})"
    )
    return True, difficulty


# --- forwarding to the faucet ------------------------------------------------


def maybe_forward(wallet: AuthServiceProxy) -> str | None:
    """If forwarding is on and the matured balance clears the threshold, sweep
    the wallet to the faucet's LND deposit address. Returns the txid or None."""
    if not FORWARD or not os.path.exists(LND_ADDR_FILE):
        return None
    with open(LND_ADDR_FILE) as fh:
        dest = fh.read().strip()
    if not dest:
        return None
    balance_sat = round(float(wallet.getbalance()) * 1e8)
    if balance_sat < FORWARD_THRESHOLD_SAT:
        return None
    try:
        result = wallet.sendall([dest])
    except JSONRPCException as exc:
        log(f"forward to faucet failed: {exc}")
        return None
    txid = result.get("txid") if isinstance(result, dict) else None
    if txid:
        log(f"forwarded {balance_sat / 1e8:.8f} to faucet ({dest}) tx {txid}")
    return txid


# --- status JSON -------------------------------------------------------------


def write_status(node: AuthServiceProxy, wallet: AuthServiceProxy, state: dict) -> None:
    """Atomically write the dashboard status snapshot. Skips on RPC hiccups so
    the last good file survives."""
    try:
        info = node.getblockchaininfo()
        tip = node.getblockheader(info["bestblockhash"])
        balance = float(wallet.getbalance())
    except (JSONRPCException, OSError, ValueError):
        return
    secs_since = max(0, int(time.time()) - int(tip["time"]))
    difficulty = float(info["difficulty"])
    payload = {
        "tip_height": info["blocks"],
        "difficulty": difficulty,
        "secs_since_last_block": secs_since,
        "window_open": secs_since >= int(os.environ.get("WINDOW_SECONDS", "1200")),
        "reset_detected": difficulty <= MAX_DIFFICULTY,
        "blocks_claimed_total": state["claimed"],
        "last_claim_at": state["last_claim_at"],
        "wallet_balance": f"{balance:.8f}",
        "forwarded_total": state["forwarded"],
        "forwarding": FORWARD,
        "updated_at": int(time.time()),
    }
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(payload, fh)
    os.replace(tmp, STATE_FILE)


# --- main loop ---------------------------------------------------------------


def main() -> None:
    wait_for_synced_node()
    wallet = ensure_wallet()
    addr = claim_address(wallet)
    node = _proxy()
    reward_spk = bytes.fromhex(wallet.getaddressinfo(addr)["scriptPubKey"])

    state = {"claimed": 0, "last_claim_at": None, "forwarded": 0}
    last_status = 0.0
    log(
        f"watching for difficulty <= {MAX_DIFFICULTY} "
        f"(forwarding {'on' if FORWARD else 'off'})"
    )
    while True:
        # Mine while the window/reset keeps the next block easy, bounded so the
        # monitor/forward steps still run during a long reset.
        for _ in range(MAX_BLOCKS_PER_RUN):
            try:
                claimed, _difficulty = try_claim_one(node, reward_spk)
            except (JSONRPCException, OSError, ValueError, subprocess.SubprocessError) as exc:
                log(f"claim attempt errored (transient): {exc}")
                break
            if not claimed:
                break
            state["claimed"] += 1
            state["last_claim_at"] = int(time.time())

        if maybe_forward(wallet):
            state["forwarded"] += 1

        now = time.time()
        if now - last_status >= STATUS_INTERVAL:
            write_status(node, wallet, state)
            last_status = now
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
