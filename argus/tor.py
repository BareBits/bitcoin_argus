"""The host-level Tor layer: a single v3 onion service that fronts every sub-tool.

One onion address serves the whole installation; which sub-tool you reach is
decided purely by **port** — the onion reuses the same port numbers as clearnet.
That keeps the model simple (one address, no per-service onions) and is why the
LND P2P onion address is just ``<onion>:<lnd_p2p_port>``.

HTTP services are routed straight to their plain-HTTP backend loopback port
(bypassing the shared Caddy): the onion layer already encrypts the transport, so
there is no TLS/Host-header to reconcile. TCP services (Electrum, LND/bitcoind
P2P) are routed to their published loopback port. Because every enabled network
owns a disjoint 1000-port block, all ``HiddenServicePort`` virtual ports are
unique within the one hidden service.

Like the shared Caddy layer, this is generated from the full config (all enabled
networks at once). The onion key is pre-generated (see :mod:`argus.onionkey`) so
the address is known here — baked into LND's gossip advertisement and shown on the
dashboard — without a two-phase deploy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .config import ArgusConfig
from .constants import NETWORK_SPECS, TOR_SOCKS_PORT, WEB_BACKEND_PORT
from .onionkey import OnionKey
from .resources import global_log

# The dashboard is the installation root, so it answers on the onion's port 80.
DASHBOARD_ONION_PORT = 80

# tor reaches every backend on the host loopback (it runs with host networking).
_TARGET_HOST = "127.0.0.1"


@dataclass(frozen=True)
class OnionRoute:
    """One ``<onion>:<virtual_port>`` mapping to a local backend.

    ``net_key`` is None for the installation-wide dashboard route; ``kind`` is
    ``"http"`` or ``"tcp"`` (informational — both route the same way).
    """

    net_key: str | None
    service: str  # short label, e.g. "mempool explorer", "LND (argus1) P2P"
    virtual_port: int  # the onion-side port (matches the clearnet public port)
    target_port: int  # the local loopback port tor forwards to
    kind: str


def onion_routes(cfg: ArgusConfig, port_map: dict[str, dict[str, int]]) -> list[OnionRoute]:
    """Every onion route for the enabled config, honouring the expose toggles.

    Operator-only ports (Core RPC, LND gRPC/REST, Fulcrum admin, the DBs) are
    deliberately never included — they stay bound to the server's localhost.
    """
    tor = cfg.global_.tor
    routes: list[OnionRoute] = []

    for net_key, net in cfg.enabled_networks():
        ports = port_map[net_key]
        spec = NETWORK_SPECS[net_key]

        if tor.expose_web:
            if net.cashu.enabled:
                routes.append(OnionRoute(
                    net_key, "Cashu mint",
                    ports["cashu_public"], ports["cashu_backend"], "http",
                ))
                if net.cashu.wallet:
                    routes.append(OnionRoute(
                        net_key, "cashu.me (web wallet)",
                        ports["cashu_wallet_public"], ports["cashu_wallet_backend"],
                        "http",
                    ))
            if net.mempool_enabled(spec):
                routes.append(OnionRoute(
                    net_key, "mempool explorer",
                    ports["mempool_public"], ports["mempool_web"], "http",
                ))
            if net.bitcart.enabled:
                for label, pub, back in (
                    ("Bitcart store", "bitcart_store_public", "bitcart_store"),
                    ("Bitcart admin", "bitcart_admin_public", "bitcart_admin"),
                    ("Bitcart API", "bitcart_api_public", "bitcart_api"),
                ):
                    routes.append(OnionRoute(
                        net_key, label, ports[pub], ports[back], "http",
                    ))

        if tor.expose_electrum:
            for i, ix in enumerate(net.enabled_indexers()):
                key = f"fulcrum_{i}_electrum_tcp"
                routes.append(OnionRoute(
                    net_key, f"Fulcrum ({ix.name}) Electrum",
                    ports[key], ports[key], "tcp",
                ))

        if tor.expose_lnd_p2p:
            name1 = net.lnd.name or (
                "argus1" if spec.supports_miner else f"argus-{net_key}"
            )
            routes.append(OnionRoute(
                net_key, f"LND ({name1}) P2P",
                ports["lnd_p2p"], ports["lnd_p2p"], "tcp",
            ))
            if net.lnd_secondary_enabled(spec):
                routes.append(OnionRoute(
                    net_key, f"LND ({net.lnd.secondary.name}) P2P",
                    ports["lnd2_p2p"], ports["lnd2_p2p"], "tcp",
                ))
            if net.lnd_tertiary_enabled(spec):
                routes.append(OnionRoute(
                    net_key, f"LND ({net.lnd.tertiary.name}) P2P",
                    ports["lnd3_p2p"], ports["lnd3_p2p"], "tcp",
                ))

        # Only expose bitcoind P2P over the onion where it is already public on
        # clearnet — if the operator closed it, the onion must not reopen it.
        if tor.expose_bitcoind_p2p and net.bitcoind.p2p_public:
            routes.append(OnionRoute(
                net_key, "Bitcoin Core P2P",
                ports["bitcoind_p2p"], ports["bitcoind_p2p"], "tcp",
            ))

    if tor.expose_web and cfg.web.enabled:
        routes.append(OnionRoute(
            None, "Dashboard", DASHBOARD_ONION_PORT, WEB_BACKEND_PORT, "http",
        ))

    return routes


def socks_open_to_containers(cfg: ArgusConfig) -> bool:
    """Whether tor's SOCKS must be reachable from the per-network LND containers.

    Only the SECONDARY LND node (``lnd2``) runs in Tor mode and dials peers
    through this proxy; the primary node is clearnet-only outbound. So SOCKS is
    opened to containers iff Tor + LND-P2P-on-onion are on AND some enabled
    network actually has a secondary node — otherwise it stays loopback-only.
    """
    tor = cfg.global_.tor
    if not (tor.enabled and tor.expose_lnd_p2p):
        return False
    return any(
        net.lnd_secondary_enabled(NETWORK_SPECS[key])
        for key, net in cfg.enabled_networks()
    )


def render_torrc(cfg: ArgusConfig, port_map: dict[str, dict[str, int]]) -> str:
    routes = onion_routes(cfg, port_map)
    lines = ["# Generated by Bitcoin Argus — do not edit by hand.", "Log notice stdout"]

    if socks_open_to_containers(cfg):
        # Bound to all host interfaces so the per-network LND containers can reach
        # it via the host gateway; SocksPolicy still restricts use to loopback +
        # the private (Docker/LAN) ranges, and the firewall blocks it publicly.
        lines += [
            f"SocksPort 0.0.0.0:{TOR_SOCKS_PORT}",
            "SocksPolicy accept 127.0.0.0/8",
            "SocksPolicy accept 10.0.0.0/8",
            "SocksPolicy accept 172.16.0.0/12",
            "SocksPolicy accept 192.168.0.0/16",
            "SocksPolicy reject *",
        ]
    else:
        lines.append(f"SocksPort {_TARGET_HOST}:{TOR_SOCKS_PORT}")

    lines += [
        "DataDirectory /var/lib/tor",
        "",
        "HiddenServiceDir /var/lib/tor/argus-onion",
        "HiddenServiceVersion 3",
    ]
    for r in routes:
        lines.append(f"HiddenServicePort {r.virtual_port} {_TARGET_HOST}:{r.target_port}")
    return "\n".join(lines) + "\n"


# Stages the pre-generated key into the HiddenServiceDir with the permissions tor
# demands (it refuses a group/other-readable key dir), then runs tor. The keys are
# mounted read-only, so they are copied to a writable, tor-private location first.
# Runs as root (network_mode host); tor warns about root but is otherwise happy,
# and this avoids assuming any particular non-root user exists in the image. Run
# from a file so Compose variable interpolation never touches it.
_ENTRYPOINT_SH = """\
#!/bin/sh
# Generated by Bitcoin Argus — stage the onion key, then run tor.
set -e
HSDIR=/var/lib/tor/argus-onion
mkdir -p "$HSDIR"
cp /run/onion-keys/hs_ed25519_secret_key "$HSDIR/hs_ed25519_secret_key"
cp /run/onion-keys/hs_ed25519_public_key "$HSDIR/hs_ed25519_public_key"
chmod 700 "$HSDIR"
chmod 600 "$HSDIR/hs_ed25519_secret_key" "$HSDIR/hs_ed25519_public_key"
exec tor -f /etc/tor/torrc
"""


def _compose() -> dict:
    return {
        "name": "argus-shared-tor",
        "services": {
            "tor": {
                "image": "${TOR_IMAGE}",
                "container_name": "argus-tor",
                "restart": "unless-stopped",
                # Host networking so the one onion can reach every backend on the
                # host loopback (HTTP backends are bound to 127.0.0.1).
                "network_mode": "host",
                # Root only to stage the key dir; torrc's `User tor` then drops it.
                "user": "0:0",
                "entrypoint": ["/bin/sh", "/entrypoint.sh"],
                "volumes": [
                    "./torrc:/etc/tor/torrc:ro",
                    "./entrypoint.sh:/entrypoint.sh:ro",
                    "./keys/hs_ed25519_secret_key:/run/onion-keys/hs_ed25519_secret_key:ro",
                    "./keys/hs_ed25519_public_key:/run/onion-keys/hs_ed25519_public_key:ro",
                    "tor_data:/var/lib/tor",
                ],
            }
        },
        "volumes": {"tor_data": {}},
    }


def generate_tor(
    cfg: ArgusConfig,
    port_map: dict[str, dict[str, int]],
    onion: OnionKey,
    output_dir: Path,
) -> Path | None:
    """Generate the shared Tor project. Returns its dir, or None when Tor is off."""
    if not cfg.global_.tor.enabled:
        return None

    out_dir = output_dir / "shared-tor"
    keys_dir = out_dir / "keys"
    keys_dir.mkdir(parents=True, exist_ok=True)

    compose = _compose()
    rotation, log_block = global_log(cfg)
    if rotation:
        compose["services"]["tor"].setdefault("logging", log_block)

    (out_dir / "docker-compose.yml").write_text(
        yaml.safe_dump(compose, sort_keys=False, default_flow_style=False)
    )
    (out_dir / "torrc").write_text(render_torrc(cfg, port_map))
    (out_dir / "entrypoint.sh").write_text(_ENTRYPOINT_SH)
    (out_dir / ".env").write_text(f"TOR_IMAGE={cfg.global_.tor.image}\n")

    # The onion identity files. The secret key is sensitive (like the project
    # .env credentials), so it is written 0600.
    sk = keys_dir / "hs_ed25519_secret_key"
    sk.write_bytes(onion.secret_key_file)
    sk.chmod(0o600)
    (keys_dir / "hs_ed25519_public_key").write_bytes(onion.public_key_file)
    (keys_dir / "hostname").write_text(onion.hostname + "\n")

    return out_dir
