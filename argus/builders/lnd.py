"""Builder for the standalone LND node(s).

The primary node (``lnd``) is used by Cashu and general dev work; it connects to
bitcoind over the internal compose network (RPC + ZMQ). By default Argus also
deploys a **second** (``lnd2``) and **third** (``lnd3``) node and wires all three
into a **liquidity ring** (``lnd``->``lnd2``->``lnd3``->``lnd``): each opens one
single-funded channel to the next hop, an initial circular self-payment brings
every channel to ~50/50, and a long-running rebalancer keeps them there. A
triangle is the smallest graph in which a node can restore its own inbound/
outbound balance purely **off-chain** (circular rebalancing) — no Loop/Boltz, so
it works on every testnet. The nodes are funded either by mining (``auto``, on
networks Argus mines) or by externally-sent coins (``external``); see the
``lnd-setup`` funding sidecar, the ``lnd-channels`` ring opener, and the
``lnd-rebalancer``.

Wallets auto-initialise (``noseedbackup``) so the stack comes up unattended —
acceptable for testnets, never for mainnet. P2P is published publicly; gRPC and
REST are bound to 127.0.0.1 (closed to the internet).

Bitcart runs its *own* LND (a separate phase); these are separate nodes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..constants import (
    CHAIN_INTERNAL_PORTS,
    LND_CHANNEL_CORE_RESERVE_BTC,
    LND_INTERNAL_PORTS,
    LND_NETWORK_KEY,
    LND_STATUS_INTERVAL_SECONDS,
    TOR_SOCKS_HOST_ALIAS,
    TOR_SOCKS_PORT,
    ZMQ_BLOCK_INTERNAL,
    ZMQ_TX_INTERNAL,
)
from ..context import BuildContext, Fragment


@dataclass(frozen=True)
class _Node:
    """One LND node's identity within a network's compose project."""

    service: str  # compose service / DNS name ("lnd" or "lnd2")
    alias: str  # gossip alias (<=32 bytes)
    color: str  # gossip color (#rrggbb)
    confdir: str  # output subdir holding lnd.conf ("lnd" / "lnd2")
    volume: str  # data volume name ("lnd_data" / "lnd2_data")
    p2p_key: str  # ctx.ports key for the public P2P host port
    rest_key: str
    grpc_key: str


def _sanitize_arg(arg: str) -> str:
    """Reject newlines in operator-supplied LND args (defense against injection)."""
    if "\n" in arg or "\r" in arg:
        raise ValueError(f"lnd extra_arg contains a newline: {arg!r}")
    return arg.strip()


def _lnd_advertise_onion(ctx: BuildContext) -> bool:
    """Whether the LND node(s) advertise the installation's onion in gossip.

    Requires the shared onion (Tor enabled) and LND P2P exposed on it. BOTH nodes
    advertise it via ``externalip=<onion>:<port>``, so either is reachable
    *inbound* over Tor — the shared hidden service forwards onion traffic to the
    node's P2P listener. This is independent of whether the node dials *out* over
    Tor (see :func:`_lnd_dials_over_tor`)."""
    tor = ctx.cfg.global_.tor
    return bool(tor.enabled and tor.expose_lnd_p2p and ctx.onion_hostname)


def _lnd_dials_over_tor(ctx: BuildContext, node: _Node) -> bool:
    """Whether THIS node runs in Tor mode (``tor.active`` — can dial .onion peers).

    Only the secondary node (``lnd2``) does. The primary (``lnd``) stays
    clearnet-only outbound so its dials never route through Tor — reliable, and it
    can reach private Docker hostnames (which Tor cannot resolve). Both nodes stay
    reachable *inbound* over Tor via the advertised onion; the split only affects
    which node can *initiate* connections to .onion peers. See the README's Tor
    section."""
    return _lnd_advertise_onion(ctx) and node.service == "lnd2"


def _nodes(ctx: BuildContext) -> list[_Node]:
    """The LND node(s) for this network (one, or two when secondary is on)."""
    net, spec = ctx.net, ctx.spec
    alias1 = net.lnd.name or (
        "argus1" if spec.supports_miner else f"argus-{ctx.net_key}"
    )
    nodes = [
        _Node(
            service="lnd",
            alias=alias1,
            color=net.lnd.color,
            confdir="lnd",
            volume="lnd_data",
            p2p_key="lnd_p2p",
            rest_key="lnd_rest",
            grpc_key="lnd_grpc",
        )
    ]
    if net.lnd_secondary_enabled(spec):
        nodes.append(
            _Node(
                service="lnd2",
                alias=net.lnd.secondary.name,
                color=net.lnd.secondary.color,
                confdir="lnd2",
                volume="lnd2_data",
                p2p_key="lnd2_p2p",
                rest_key="lnd2_rest",
                grpc_key="lnd2_grpc",
            )
        )
    if net.lnd_tertiary_enabled(spec):
        nodes.append(
            _Node(
                service="lnd3",
                alias=net.lnd.tertiary.name,
                color=net.lnd.tertiary.color,
                confdir="lnd3",
                volume="lnd3_data",
                p2p_key="lnd3_p2p",
                rest_key="lnd3_rest",
                grpc_key="lnd3_grpc",
            )
        )
    return nodes


def _render_conf(ctx: BuildContext, node: _Node) -> str:
    spec = ctx.spec
    net = ctx.net
    chain = spec.chain
    if chain not in LND_NETWORK_KEY:
        raise NotImplementedError(f"LND network mapping missing for chain {chain!r}")

    rpc_internal = CHAIN_INTERNAL_PORTS[chain]["rpc"]
    challenge = net.signet_challenge or spec.default_signet_challenge
    p = LND_INTERNAL_PORTS

    lines: list[str] = [
        "# Generated by Bitcoin Argus — do not edit by hand.",
        "[Application Options]",
        f"alias={node.alias}",
        f"color={node.color}",
        "debuglevel=info",
        # Unattended wallet init/unlock — fine for testnets only.
        "noseedbackup=true",
        f"listen=0.0.0.0:{p['p2p']}",
        f"rpclisten=0.0.0.0:{p['grpc']}",
        f"restlisten=0.0.0.0:{p['rest']}",
        # Make the TLS cert valid for in-container/loopback clients (Cashu, the
        # node-info + channel sidecars) that reach the node by its service name,
        # plus the unique container name (argus-<net>-lnd) the dashboard uses for
        # LNURL invoice minting — the bare `lnd` alias collides across the per-net
        # networks the dashboard joins, so it must dial the container name and
        # verify TLS against it. tlsautorefresh regenerates the cert in place when
        # this domain set changes, so existing deployments pick up the new SAN.
        f"tlsextradomain={node.service}",
        f"tlsextradomain={ctx.project}-{node.service}",
        "tlsautorefresh=true",
        # Channel-friendliness: accept spontaneous payments and (optionally) make
        # the node discoverable + open to large/small channels from peers.
        "accept-keysend=true",
        "accept-amp=true",
    ]
    if net.lnd.advertise_external_ip:
        # Advertise a reachable address in gossip so peers can auto-connect and
        # open channels to us, instead of needing the URI hand-fed.
        lines.append(f"externalip={ctx.cfg.global_.hostname}:{ctx.ports[node.p2p_key]}")
    if _lnd_advertise_onion(ctx):
        # Also advertise the installation's onion (same node, its P2P port) so
        # peers can open channels to us over Tor. BOTH nodes advertise it (both are
        # reachable inbound over Tor); it lands in the gossip announcement and
        # propagates onto the local mempool node page.
        lines.append(f"externalip={ctx.onion_hostname}:{ctx.ports[node.p2p_key]}")
    if net.lnd.min_chan_size is not None:
        lines.append(f"minchansize={net.lnd.min_chan_size}")
    if net.lnd.auto_compact:
        # Disk hygiene: compact channel.db on startup + drop canceled invoices.
        lines += [
            "gc-canceled-invoices-on-startup=true",
            "gc-canceled-invoices-on-the-fly=true",
            "db.bolt.auto-compact=true",
            "db.bolt.auto-compact-min-age=168h0m0s",
        ]
    if net.lnd_wumbo_enabled(spec):
        # Namespaced option: its own [protocol] section, and the key keeps the
        # full dotted name (same convention as bitcoin.* under [Bitcoin] below).
        lines += ["", "[protocol]", "protocol.wumbo-channels=true"]
    lines += [
        "",
        "[Bitcoin]",
        "bitcoin.active=true",
        f"bitcoin.{LND_NETWORK_KEY[chain]}=true",
        "bitcoin.node=bitcoind",
    ]
    if spec.is_signet and challenge:
        lines.append(f"bitcoin.signetchallenge={challenge}")
        for n in (*spec.default_addnodes, *net.addnode):
            lines.append(f"bitcoin.signetseednode={n}")

    lines += [
        "",
        "[Bitcoind]",
        f"bitcoind.rpchost=bitcoind:{rpc_internal}",
        f"bitcoind.rpcuser={ctx.secrets['RPC_USER']}",
        f"bitcoind.rpcpass={ctx.secrets['RPC_PASSWORD']}",
        f"bitcoind.zmqpubrawblock=tcp://bitcoind:{ZMQ_BLOCK_INTERNAL}",
        f"bitcoind.zmqpubrawtx=tcp://bitcoind:{ZMQ_TX_INTERNAL}",
    ]
    if _lnd_dials_over_tor(ctx, node):
        # Secondary node only: run in Tor mode so it can DIAL .onion peers. Reach
        # clearnet/private targets directly and use Tor only for .onion — but note
        # LND still routes bare *hostnames* through the proxy, so the channel
        # sidecar dials this node's siblings by IP (see channels.sh). It does NOT
        # mint its own onion (no tor.v3): it advertises our shared one.
        lines += [
            "",
            "[Tor]",
            "tor.active=true",
            # The host-networked shared tor container's SOCKS proxy, reached via an
            # /etc/hosts alias pointed at the host gateway (see the service block).
            f"tor.socks={TOR_SOCKS_HOST_ALIAS}:{TOR_SOCKS_PORT}",
            "tor.skip-proxy-for-clearnet-targets=true",
        ]
    return "\n".join(lines) + "\n"


# Wait for the shared Tor SOCKS proxy before starting a Tor-mode node. LND in Tor
# mode validates its onion ``externalip`` through the proxy at startup, so if the
# per-network stack is brought up before the shared-tor stack, LND fails config
# validation and crash-loops — taking its dependents (the channel sidecar) down
# with it. This init sidecar gates the node's start on the proxy being reachable,
# baking the "Tor first" ordering into compose. Deliberately NO ``$`` so Compose's
# variable interpolation leaves it untouched; times out after 5m so a misconfigured
# deploy doesn't hang ``up`` forever (the node then falls back to restart-retry).
_TOR_WAIT_CMD = (
    "timeout 300 sh -c 'until nc -z -w2 {alias} {port}; do "
    "echo \"[tor-wait] waiting for the Tor SOCKS proxy at {alias}:{port} "
    "(is the shared-tor stack up?)\"; sleep 2; done' "
    "&& echo '[tor-wait] Tor SOCKS proxy reachable' "
    "|| echo '[tor-wait] timed out after 5m; starting the node anyway'"
).format(alias=TOR_SOCKS_HOST_ALIAS, port=TOR_SOCKS_PORT)


def _tor_wait_service(ctx: BuildContext, node: _Node) -> dict:
    """One-shot sidecar gating a Tor-mode node on the SOCKS proxy (see above).

    Runs in the bitcoind image (its busybox has ``nc``; already pulled, so no new
    image) and reaches the host-networked proxy via the same host-gateway alias the
    node uses."""
    return {
        "image": "${BITCOIND_IMAGE}",
        "container_name": f"{ctx.project}-{node.service}-tor-wait",
        "restart": "no",
        "entrypoint": ["/bin/sh", "-c", _TOR_WAIT_CMD],
        "extra_hosts": [f"{TOR_SOCKS_HOST_ALIAS}:host-gateway"],
        "networks": [ctx.network_name],
    }


def _node_service(ctx: BuildContext, node: _Node) -> dict:
    p = LND_INTERNAL_PORTS
    lnd_net = LND_NETWORK_KEY[ctx.spec.chain]
    lnddir = "/home/lnd/.lnd"
    command = [f"--configfile=/etc/lnd/{node.confdir}.conf"] + [
        _sanitize_arg(a) for a in ctx.net.lnd.extra_args
    ]
    service = {
        "image": "${LND_IMAGE}",
        "container_name": f"{ctx.project}-{node.service}",
        "restart": "unless-stopped",
        "depends_on": {"bitcoind": {"condition": "service_healthy"}},
        "command": command,
        "volumes": [
            f"{node.volume}:{lnddir}",
            f"./{node.confdir}/lnd.conf:/etc/lnd/{node.confdir}.conf:ro",
        ],
        "ports": [
            # P2P is PUBLIC; gRPC + REST are closed (loopback only).
            f"{ctx.ports[node.p2p_key]}:{p['p2p']}",
            f"127.0.0.1:{ctx.ports[node.grpc_key]}:{p['grpc']}",
            f"127.0.0.1:{ctx.ports[node.rest_key]}:{p['rest']}",
        ],
        "networks": [ctx.network_name],
        "healthcheck": {
            "test": [
                "CMD-SHELL",
                f"lncli --lnddir={lnddir} --network={lnd_net} getinfo "
                ">/dev/null 2>&1 || exit 1",
            ],
            "interval": "20s",
            "timeout": "10s",
            "retries": 15,
            "start_period": "30s",
        },
    }
    if ctx.net.lnd.extra_env:
        service["environment"] = dict(ctx.net.lnd.extra_env)
    if _lnd_dials_over_tor(ctx, node):
        # Secondary node only: reach the host-networked tor SOCKS proxy from this
        # bridged container via the host gateway. SOCKS itself is firewalled off
        # the public internet and further restricted by tor's SocksPolicy (private
        # ranges only). Gate startup on the proxy being reachable (see
        # _tor_wait_service) so the node doesn't crash-loop when the shared-tor
        # stack isn't up yet.
        service["extra_hosts"] = [f"{TOR_SOCKS_HOST_ALIAS}:host-gateway"]
        service["depends_on"][f"{node.service}-tor-wait"] = {
            "condition": "service_completed_successfully"
        }
    return service


# Run from a mounted file (not an inline entrypoint) so Docker Compose's variable
# interpolation never mangles the shell ``$i`` / ``$A`` / ``$(...)`` references.
# Periodic reporter: writes the identity pubkey (argus_nodeinfo.json), keeps a
# stable on-chain deposit address (argus_addr.txt), and a live liquidity snapshot
# (argus_liquidity.json) the operator dashboard reads via the docker socket proxy.
_NODEINFO_SH = """\
#!/bin/sh
# Generated by Bitcoin Argus — LND identity, deposit address, liquidity snapshot.
set -u
OUT="${LNDDIR}/argus_nodeinfo.json"
ADDR="${LNDDIR}/argus_addr.txt"
LIQ="${LNDDIR}/argus_liquidity.json"
L="lncli --lnddir=${LNDDIR} --network=${NET} --rpcserver=${RPCSERVER}"

field() {  # $1 = json text, $2 = key -> first scalar value
  echo "$1" | grep -o "\\"$2\\": *\\"[^\\"]*\\"" | head -1 | cut -d'"' -f4
}

while true; do
  if INFO=$($L getinfo 2>/dev/null) && [ -n "$INFO" ]; then
    printf '%s' "$INFO" > "$OUT"
    PUBKEY=$(field "$INFO" identity_pubkey)

    # Keep a stable deposit address so the operator can fund this node on-chain.
    if [ ! -s "$ADDR" ]; then
      A=$($L newaddress p2wkh 2>/dev/null \
          | grep -o '"address": *"[^"]*"' | cut -d'"' -f4)
      [ -n "$A" ] && printf '%s' "$A" > "$ADDR"
    fi
    DEPOSIT=$(cat "$ADDR" 2>/dev/null || echo "")

    # On-chain wallet balance.
    WB=$($L walletbalance 2>/dev/null || echo "")
    CONF=$(field "$WB" confirmed_balance); CONF=${CONF:-0}
    UNCONF=$(field "$WB" unconfirmed_balance); UNCONF=${UNCONF:-0}

    # Channel liquidity: local = OUTBOUND (we can send), remote = INBOUND (we can
    # receive). Sum across channels straight from listchannels.
    CH=$($L listchannels 2>/dev/null | awk '
      /"local_balance":/  { gsub(/[" ,]/,""); split($0,a,":"); loc+=a[2] }
      /"remote_balance":/ { gsub(/[" ,]/,""); split($0,a,":"); rem+=a[2]; n++ }
      /"active": true/    { act++ }
      END { printf "%d %d %d %d", loc+0, rem+0, n+0, act+0 }')
    OUTB=$(echo "$CH" | awk '{print $1}'); OUTB=${OUTB:-0}
    INB=$(echo "$CH" | awk '{print $2}'); INB=${INB:-0}
    NCH=$(echo "$CH" | awk '{print $3}'); NCH=${NCH:-0}
    NACT=$(echo "$CH" | awk '{print $4}'); NACT=${NACT:-0}

    printf '{"alias":"%s","pubkey":"%s","address":"%s","onchain_confirmed":%s,"onchain_unconfirmed":%s,"channel_outbound_sat":%s,"channel_inbound_sat":%s,"num_channels":%s,"num_active_channels":%s}\\n' \
      "${ALIAS}" "${PUBKEY:-}" "${DEPOSIT}" "${CONF}" "${UNCONF}" "${OUTB}" "${INB}" "${NCH}" "${NACT}" \
      > "${LIQ}.tmp" && mv "${LIQ}.tmp" "${LIQ}"
  fi
  sleep "${INTERVAL}"
done
"""


def _nodeinfo_service(ctx: BuildContext, node: _Node) -> dict:
    """Per-node periodic sidecar: records the identity pubkey, a stable on-chain
    deposit address, and a live liquidity snapshot into the node's data volume.

    The dashboard reads these (read-only, via the docker-socket-proxy) to show the
    node's connection URI and the operator-only "add liquidity" panel; the
    ``lnd-setup`` funding sidecar reads the address to send initial coins.
    """
    p = LND_INTERNAL_PORTS
    lnddir = "/home/lnd/.lnd"
    return {
        "image": "${LND_IMAGE}",
        "container_name": f"{ctx.project}-{node.service}-nodeinfo",
        "restart": "unless-stopped",
        "depends_on": {node.service: {"condition": "service_healthy"}},
        "entrypoint": ["/bin/sh", "/scripts/nodeinfo.sh"],
        "environment": {
            "LNDDIR": lnddir,
            "NET": LND_NETWORK_KEY[ctx.spec.chain],
            "RPCSERVER": f"{node.service}:{p['grpc']}",
            "ALIAS": node.alias,
            "INTERVAL": str(LND_STATUS_INTERVAL_SECONDS),
        },
        "volumes": [
            f"{node.volume}:{lnddir}",
            f"./{node.confdir}/nodeinfo.sh:/scripts/nodeinfo.sh:ro",
        ],
        "networks": [ctx.network_name],
    }


# ---------------------------------------------------------------------------
# Liquidity ring: funding + channel orchestration + off-chain rebalancing.
# ---------------------------------------------------------------------------

# Funds the three LND nodes' on-chain wallets from the miner/signer wallet
# (``funding: auto``; only generated on networks Argus mines). ``external`` skips
# this sidecar entirely — the ring opener waits for coins sent to each node.
_SETUP_SH = """\
#!/bin/sh
# Generated by Bitcoin Argus — fund the three LND nodes' on-chain wallets.
set -eu

CLI="bitcoin-cli ${CHAIN_FLAG} -rpcconnect=${RPC_CONNECT} -rpcport=${RPC_PORT} \
-rpcuser=${RPC_USER} -rpcpassword=${RPC_PASSWORD}"
WCLI="${CLI} -rpcwallet=${FUNDING_WALLET}"
STATE=/state

if [ -f "${STATE}/funded" ]; then
  echo "[lnd-setup] already funded; nothing to do"; exit 0
fi

echo "[lnd-setup] waiting for bitcoind RPC..."
until $CLI getblockchaininfo >/dev/null 2>&1; do sleep 2; done

echo "[lnd-setup] waiting for funding wallet '${FUNDING_WALLET}'..."
until $WCLI getwalletinfo >/dev/null 2>&1; do
  $CLI loadwallet "${FUNDING_WALLET}" >/dev/null 2>&1 || true
  sleep 3
done

echo "[lnd-setup] waiting for all three LND funding addresses..."
until [ -s /lnd1/argus_addr.txt ] && [ -s /lnd2/argus_addr.txt ] \
   && [ -s /lnd3/argus_addr.txt ]; do sleep 3; done
A1=$(cat /lnd1/argus_addr.txt); A2=$(cat /lnd2/argus_addr.txt)
A3=$(cat /lnd3/argus_addr.txt)

NEED=$(awk "BEGIN{print 3*${FUND_BTC}+${RESERVE_BTC}}")
echo "[lnd-setup] need >= ${NEED} BTC in ${FUNDING_WALLET}; addrs ${A1} ${A2} ${A3}"

enough() { awk "BEGIN{exit !($($WCLI getbalance) >= ${NEED})}"; }

if [ "${CAN_MINE}" = "1" ]; then
  MADDR=$($WCLI getnewaddress)
  while ! enough; do
    echo "[lnd-setup] mining to mature funds (balance $($WCLI getbalance))"
    $CLI generatetoaddress 20 "$MADDR" >/dev/null
  done
else
  echo "[lnd-setup] waiting for the signer to accumulate matured coins..."
  while ! enough; do echo "  balance $($WCLI getbalance)"; sleep 10; done
fi

echo "[lnd-setup] funding ${FUND_BTC} BTC to each node"
for A in "$A1" "$A2" "$A3"; do
  $WCLI -named sendtoaddress address="$A" amount=${FUND_BTC} fee_rate=2 >/dev/null
done

if [ "${CAN_MINE}" = "1" ]; then
  echo "[lnd-setup] confirming funding transactions"
  $CLI generatetoaddress 6 "$MADDR" >/dev/null
fi

touch "${STATE}/funded"
echo "[lnd-setup] funding complete (channels confirm via the steady-state miner)"
"""

# Wires the three nodes into a ring (lnd->lnd2->lnd3->lnd), one single-funded
# channel per hop, then a single circular self-payment of half a channel brings
# every channel to ~50/50 so both directions are live from the start.
_CHANNELS_SH = """\
#!/bin/sh
# Generated by Bitcoin Argus — build the three-node LND liquidity ring.
set -eu

GRPC=__GRPC__
P2P=__P2P__
L1="lncli --network=${NET} --rpcserver=lnd:${GRPC} --lnddir=/lnd1"
L2="lncli --network=${NET} --rpcserver=lnd2:${GRPC} --lnddir=/lnd2"
L3="lncli --network=${NET} --rpcserver=lnd3:${GRPC} --lnddir=/lnd3"
STATE=/state

until $L1 getinfo >/dev/null 2>&1; do sleep 3; done
until $L2 getinfo >/dev/null 2>&1; do sleep 3; done
until $L3 getinfo >/dev/null 2>&1; do sleep 3; done

pk()  { eval "\\$$1 getinfo" | grep -o '"identity_pubkey": *"[0-9a-f]*"' \
        | grep -oE '[0-9a-f]{66}'; }
# Resolve a peer service name to its container IP and append the P2P port. We
# connect by IP (not hostname) because the secondary node runs in Tor mode, and a
# Tor-mode LND would route a bare hostname through the proxy (failing on a private
# Docker name); an IP literal is dialed directly (tor.skip-proxy-for-clearnet-
# targets). Harmless for the clearnet-only nodes. Falls back to the name.
addr() { _ip=$(getent hosts "$1" 2>/dev/null | awk '{print $1; exit}'); \
         echo "${_ip:-$1}:${P2P}"; }
conf() { eval "\\$$1 walletbalance" | grep -o '"confirmed_balance": *"[0-9]*"' \
        | grep -oE '[0-9]+' | head -1; }
active() { eval "\\$$1 listchannels" | grep -c '"active": true' || true; }

if [ "${FUNDING}" = "auto" ]; then
  echo "[lnd-ring] waiting for on-chain funding to complete..."
  until [ -f "${STATE}/funded" ]; do sleep 3; done
else
  echo "[lnd-ring] EXTERNAL funding mode — send >= ${CHAN_BTC} BTC on-chain to:"
  for n in 1 2 3; do
    A=$(cat "/lnd${n}/argus_addr.txt" 2>/dev/null || echo "(address pending)")
    echo "[lnd-ring]   node${n}: ${A}"
  done
fi

P1=$(pk L1); P2=$(pk L2); P3=$(pk L3)
echo "[lnd-ring] node1=$P1 node2=$P2 node3=$P3"

echo "[lnd-ring] waiting for confirmed on-chain funds (>= ${CHAN_SAT} sat) on all nodes..."
until [ "$(conf L1)" -ge "${CHAN_SAT}" ] 2>/dev/null; do sleep 10; done
until [ "$(conf L2)" -ge "${CHAN_SAT}" ] 2>/dev/null; do sleep 10; done
until [ "$(conf L3)" -ge "${CHAN_SAT}" ] 2>/dev/null; do sleep 10; done

# Each node opens ONE single-funded channel to the next hop. A per-edge marker
# makes this idempotent (a peer-presence check can't, since both ends then list a
# channel to each other).
open_dir() {  # caller_var marker peer_pubkey peer_host
  if [ -f "${STATE}/$2" ]; then echo "[lnd-ring] $2 already opened"; return 0; fi
  echo "[lnd-ring] $1 connect $3@$4"
  eval "\\$$1 connect $3@$4" >/dev/null 2>&1 || true
  echo "[lnd-ring] $1 openchannel ${CHAN_SAT} sat -> $3"
  eval "\\$$1 openchannel --node_key=$3 --local_amt=${CHAN_SAT}"
  touch "${STATE}/$2"
}

open_dir L1 ring_l1_l2 "$P2" "$(addr lnd2)"
open_dir L2 ring_l2_l3 "$P3" "$(addr lnd3)"
open_dir L3 ring_l3_l1 "$P1" "$(addr lnd)"

# Signal the regtest bitcoind P2P self-gate that ring funding is broadcast.
touch "${STATE}/channels"

if [ -f "${STATE}/rebalanced" ]; then
  echo "[lnd-ring] initial rebalance already done; ring is up"; exit 0
fi

echo "[lnd-ring] waiting for all ring channels to become active..."
until [ "$(active L1)" -ge 2 ] && [ "$(active L2)" -ge 2 ] \
   && [ "$(active L3)" -ge 2 ] 2>/dev/null; do
  echo "[lnd-ring]   active channels: L1=$(active L1) L2=$(active L2) L3=$(active L3)"
  sleep 10
done

# Initial rebalance: a single circular self-payment of half a channel around the
# ring (L1->L2->L3->L1) moves every channel from ~100/0 to ~50/50. At this point
# only the forward direction is routable (the reverse needs outbound the nodes
# don't have yet), so the pathfinder has just one choice — no hints needed.
HALF=$(( CHAN_SAT / 2 ))
echo "[lnd-ring] initial circular rebalance of ${HALF} sat around the ring"
PR=$($L1 addinvoice --amt=${HALF} 2>/dev/null \
     | grep -o '"payment_request": *"[^"]*"' | cut -d'"' -f4)
if [ -n "$PR" ] && $L1 payinvoice --force --allow_self_payment \
     --fee_limit=${MAX_FEE_SAT} "$PR"; then
  touch "${STATE}/rebalanced"
  echo "[lnd-ring] initial rebalance complete — every channel ~50/50"
else
  echo "[lnd-ring] initial rebalance incomplete; the rebalancer will balance the ring"
fi

echo "[lnd-ring] ring setup complete"
"""

# Long-running rebalancer: every INTERVAL it nudges any ring channel that drifts
# out of the [LOW,HIGH]% band back toward 50/50 with an OFF-CHAIN circular
# self-payment (out the over-full channel, back in via the depleted one). A
# triangle has a single rebalancing degree of freedom, so a node whose BOTH
# channels are skewed the same way can't self-correct — the negative-amount guard
# skips that case (another node fixes it from its own side).
_REBALANCER_SH = """\
#!/bin/sh
# Generated by Bitcoin Argus — keep the LND liquidity ring near 50/50 off-chain.
set -u

GRPC=__GRPC__

chans() {  # $1 svc  $2 lnddir -> CSV per channel: active,chan_id,remote_pubkey,local,capacity
  lncli --network=${NET} --rpcserver="$1:${GRPC}" --lnddir="$2" listchannels 2>/dev/null | awk '
    /"active":/        { gsub(/[ ,]/,""); split($0,a,":"); act=a[2] }
    /"remote_pubkey":/ { gsub(/[" ,]/,""); split($0,a,":"); rp=a[2] }
    /"chan_id":/       { gsub(/[" ,]/,""); split($0,a,":"); ci=a[2] }
    /"capacity":/      { gsub(/[" ,]/,""); split($0,a,":"); cap=a[2] }
    /"local_balance":/ { gsub(/[" ,]/,""); split($0,a,":"); lb=a[2];
                         print act","ci","rp","lb","cap }'
}

rebalance_node() {  # $1 svc  $2 lnddir
  SVC="$1"; DIR="$2"
  DATA=$(chans "$SVC" "$DIR")
  [ "$(echo "$DATA" | grep -c true)" -eq 2 ] || return 0
  HI_RATIO=-1; LO_RATIO=101; HI_ID=""; LO_PK=""
  HI_LOCAL=0; HI_CAP=0; LO_LOCAL=0; LO_CAP=0
  for line in $DATA; do
    act=$(echo "$line" | cut -d, -f1); [ "$act" = "true" ] || continue
    ci=$(echo "$line" | cut -d, -f2); rp=$(echo "$line" | cut -d, -f3)
    lb=$(echo "$line" | cut -d, -f4); cap=$(echo "$line" | cut -d, -f5)
    [ "$cap" -gt 0 ] 2>/dev/null || continue
    ratio=$(( lb * 100 / cap ))
    if [ "$ratio" -gt "$HI_RATIO" ]; then HI_RATIO=$ratio; HI_ID=$ci; HI_LOCAL=$lb; HI_CAP=$cap; fi
    if [ "$ratio" -lt "$LO_RATIO" ]; then LO_RATIO=$ratio; LO_PK=$rp; LO_LOCAL=$lb; LO_CAP=$cap; fi
  done
  # In band? (over-full channel not above HIGH and depleted one not below LOW.)
  if [ "$HI_RATIO" -le "$HIGH" ] && [ "$LO_RATIO" -ge "$LOW" ]; then return 0; fi
  # Move the smaller of "what the full side must shed" and "what the empty side
  # can take" to reach 50/50; a same-direction skew yields <=0 here and is skipped.
  amt_hi=$(( HI_LOCAL - HI_CAP / 2 ))
  amt_lo=$(( LO_CAP / 2 - LO_LOCAL ))
  amt=$amt_hi; [ "$amt_lo" -lt "$amt" ] && amt=$amt_lo
  [ "$amt" -ge "${MIN_REBAL_SAT}" ] || return 0
  echo "[rebal] ${SVC}: hi=${HI_RATIO}% lo=${LO_RATIO}% -> move ${amt} sat"
  PR=$(lncli --network=${NET} --rpcserver="${SVC}:${GRPC}" --lnddir="${DIR}" \
       addinvoice --amt=${amt} 2>/dev/null \
       | grep -o '"payment_request": *"[^"]*"' | cut -d'"' -f4)
  [ -n "$PR" ] || return 0
  if lncli --network=${NET} --rpcserver="${SVC}:${GRPC}" --lnddir="${DIR}" \
       payinvoice --force --allow_self_payment --outgoing_chan_id="${HI_ID}" \
       --last_hop="${LO_PK}" --fee_limit="${MAX_FEE_SAT}" "$PR" >/dev/null 2>&1; then
    echo "[rebal] ${SVC}: rebalanced ${amt} sat"
  else
    echo "[rebal] ${SVC}: attempt failed (retry next cycle)"
  fi
}

echo "[rebal] ring rebalancer started (interval=${INTERVAL}s band=${LOW}-${HIGH}%)"
while true; do
  rebalance_node lnd  /lnd1
  rebalance_node lnd2 /lnd2
  rebalance_node lnd3 /lnd3
  sleep "${INTERVAL}"
done
"""


def _setup_services(ctx: BuildContext) -> dict[str, dict]:
    """The ring sidecars: funding (``auto`` only), the ring opener, and (when
    enabled) the rebalancer.

    ``lnd-setup`` runs in the bitcoind image (it needs bitcoin-cli to mine/fund);
    ``lnd-channels`` and ``lnd-rebalancer`` run in the LND image (they need lncli).
    They coordinate through marker files in the shared ``lnd_setup_state`` volume.
    """
    chain = ctx.spec.chain
    net = ctx.net
    p = LND_INTERNAL_PORTS
    rpc_internal = CHAIN_INTERNAL_PORTS[chain]["rpc"]
    funding = net.lnd_funding_mode(ctx.spec)
    can_mine = chain == "regtest"
    funding_wallet = "miner" if chain == "regtest" else "signer"
    chan_sat = round(net.lnd.channels.channel_btc * 1e8)
    reb = net.lnd.channels.rebalancer
    max_fee_sat = reb.max_fee_sat
    min_rebal_sat = max(1000, chan_sat // 1000)

    node_volumes = ["lnd_data:/lnd1", "lnd2_data:/lnd2", "lnd3_data:/lnd3"]

    conf_dir = ctx.out_dir / "lnd_setup"
    conf_dir.mkdir(parents=True, exist_ok=True)
    channels_sh = _CHANNELS_SH.replace("__GRPC__", str(p["grpc"])).replace(
        "__P2P__", str(p["p2p"])
    )
    (conf_dir / "channels.sh").write_text(channels_sh)

    services: dict[str, dict] = {}

    # lnd-channels depends on all three nodes; in auto mode it also waits for the
    # funding sidecar to start (it then blocks on the /state/funded marker).
    channels_deps = {
        "lnd": {"condition": "service_healthy"},
        "lnd2": {"condition": "service_healthy"},
        "lnd3": {"condition": "service_healthy"},
    }

    if funding == "auto":
        (conf_dir / "setup.sh").write_text(_SETUP_SH)
        services["lnd-setup"] = {
            "image": "${BITCOIND_IMAGE}",
            "container_name": f"{ctx.project}-lnd-setup",
            # Root so it can write the shared-state markers (the state volume is
            # root-owned and shared with the LND-image sidecars, whose UID
            # differs). It only mines/funds via RPC + reads the addr files (ro).
            "user": "0:0",
            "restart": "on-failure",
            "depends_on": {
                "bitcoind": {"condition": "service_healthy"},
                "lnd": {"condition": "service_healthy"},
                "lnd2": {"condition": "service_healthy"},
                "lnd3": {"condition": "service_healthy"},
            },
            "entrypoint": ["/bin/sh", "/scripts/setup.sh"],
            "environment": {
                "CHAIN_FLAG": "-regtest" if chain == "regtest" else "-signet",
                "CAN_MINE": "1" if can_mine else "0",
                "FUNDING_WALLET": funding_wallet,
                "RPC_CONNECT": "bitcoind",
                "RPC_PORT": str(rpc_internal),
                "RPC_USER": "${RPC_USER}",
                "RPC_PASSWORD": "${RPC_PASSWORD}",
                "FUND_BTC": str(net.lnd.channels.fund_btc),
                "RESERVE_BTC": str(LND_CHANNEL_CORE_RESERVE_BTC),
            },
            "volumes": [
                "./lnd_setup/setup.sh:/scripts/setup.sh:ro",
                *[f"{v}:ro" for v in node_volumes],
                "lnd_setup_state:/state",
            ],
            "networks": [ctx.network_name],
        }
        channels_deps["lnd-setup"] = {"condition": "service_started"}

    services["lnd-channels"] = {
        "image": "${LND_IMAGE}",
        "container_name": f"{ctx.project}-lnd-channels",
        # Root so it can write the shared-state markers (see lnd-setup). lncli
        # only reads each node's cert/macaroon (mounted read-only).
        "user": "0:0",
        "restart": "on-failure",
        "depends_on": channels_deps,
        "entrypoint": ["/bin/sh", "/scripts/channels.sh"],
        "environment": {
            "NET": LND_NETWORK_KEY[chain],
            "FUNDING": funding,
            "CHAN_SAT": str(chan_sat),
            "CHAN_BTC": str(net.lnd.channels.channel_btc),
            "MAX_FEE_SAT": str(max_fee_sat),
        },
        "volumes": [
            "./lnd_setup/channels.sh:/scripts/channels.sh:ro",
            *[f"{v}:ro" for v in node_volumes],
            "lnd_setup_state:/state",
        ],
        "networks": [ctx.network_name],
    }

    if net.lnd_rebalancer_enabled(ctx.spec):
        rebalancer_sh = _REBALANCER_SH.replace("__GRPC__", str(p["grpc"]))
        (conf_dir / "rebalancer.sh").write_text(rebalancer_sh)
        services["lnd-rebalancer"] = {
            "image": "${LND_IMAGE}",
            "container_name": f"{ctx.project}-lnd-rebalancer",
            "user": "0:0",
            "restart": "unless-stopped",
            "depends_on": {
                "lnd": {"condition": "service_healthy"},
                "lnd2": {"condition": "service_healthy"},
                "lnd3": {"condition": "service_healthy"},
                "lnd-channels": {"condition": "service_started"},
            },
            "entrypoint": ["/bin/sh", "/scripts/rebalancer.sh"],
            "environment": {
                "NET": LND_NETWORK_KEY[chain],
                "INTERVAL": str(reb.interval_seconds),
                "LOW": str(round(reb.low_ratio * 100)),
                "HIGH": str(round(reb.high_ratio * 100)),
                "MAX_FEE_SAT": str(max_fee_sat),
                "MIN_REBAL_SAT": str(min_rebal_sat),
            },
            "volumes": [
                "./lnd_setup/rebalancer.sh:/scripts/rebalancer.sh:ro",
                *[f"{v}:ro" for v in node_volumes],
                "lnd_setup_state:/state",
            ],
            "networks": [ctx.network_name],
        }

    return services


def build_lnd(ctx: BuildContext) -> Fragment:
    nodes = _nodes(ctx)
    channels_on = ctx.net.lnd_channels_enabled(ctx.spec)

    services: dict[str, dict] = {}
    volumes: dict[str, dict] = {}
    for node in nodes:
        conf_dir = ctx.out_dir / node.confdir
        conf_dir.mkdir(parents=True, exist_ok=True)
        (conf_dir / "lnd.conf").write_text(_render_conf(ctx, node))
        (conf_dir / "nodeinfo.sh").write_text(_NODEINFO_SH)
        services[node.service] = _node_service(ctx, node)
        # Tor-mode nodes get a one-shot sidecar that waits for the SOCKS proxy
        # before they start (bakes the "shared-tor first" ordering into compose).
        if _lnd_dials_over_tor(ctx, node):
            services[f"{node.service}-tor-wait"] = _tor_wait_service(ctx, node)
        services[f"{node.service}-nodeinfo"] = _nodeinfo_service(ctx, node)
        volumes[node.volume] = {}

    if channels_on:
        services.update(_setup_services(ctx))
        volumes["lnd_setup_state"] = {}

    return Fragment(
        services=services,
        volumes=volumes,
        env={"LND_IMAGE": ctx.cfg.global_.lnd_image},
    )
