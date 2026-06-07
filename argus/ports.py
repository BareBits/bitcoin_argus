"""Deterministic, collision-checked host-port allocation.

Each enabled network gets a fixed 1000-port block (see :mod:`argus.constants`).
Within a block, ports are assigned from fixed offsets plus a computed range for
the variable list of Fulcrum indexers. User overrides are applied last, then the
whole set is checked for duplicates so two services never fight for one host port.
"""

from __future__ import annotations

from .config import ArgusConfig, NetworkCfg
from .constants import (
    BITCART_BTCLND_GRPC_OFFSET,
    BITCART_BTCLND_P2P_OFFSET,
    FEDIMINT_GATEWAY_BASE,
    FEDIMINT_GATEWAY_STRIDE,
    FEDIMINT_GUARDIAN_BASE,
    FEDIMINT_GUARDIAN_STRIDE,
    FULCRUM_BASE,
    FULCRUM_MAX_INSTANCES,
    FULCRUM_STRIDE,
    NETWORK_BLOCK_BASE,
    NETWORK_BLOCK_SIZE,
    NETWORK_ORDER,
    NETWORK_SPECS,
    PORT_OFFSETS,
)


class PortAllocationError(Exception):
    """Raised on a port collision or an out-of-range / unknown override."""


def block_base(net_key: str) -> int:
    """Return the first port of a network's block."""
    return NETWORK_BLOCK_BASE + NETWORK_ORDER.index(net_key) * NETWORK_BLOCK_SIZE


def _network_ports(net_key: str, net: NetworkCfg) -> dict[str, int]:
    """Compute the default port map for one network, then apply overrides."""
    base = block_base(net_key)
    ports: dict[str, int] = {name: base + off for name, off in PORT_OFFSETS.items()}

    enabled_indexers = net.enabled_indexers()
    if len(enabled_indexers) > FULCRUM_MAX_INSTANCES:
        raise PortAllocationError(
            f"[{net_key}] {len(enabled_indexers)} indexers exceeds the per-network "
            f"maximum of {FULCRUM_MAX_INSTANCES}"
        )
    for i, _ix in enumerate(enabled_indexers):
        start = base + FULCRUM_BASE + i * FULCRUM_STRIDE
        ports[f"fulcrum_{i}_electrum_tcp"] = start
        ports[f"fulcrum_{i}_electrum_ssl"] = start + 1
        ports[f"fulcrum_{i}_admin"] = start + 2

    # Fedimint guardians + gateways (variable count, like Fulcrum). Per guardian:
    # api_public (Caddy fronts it; its URL is embedded in the invite code), api
    # (loopback ws backend), ui (loopback admin/setup UI). Per gateway: api
    # (loopback). Guardian P2P is container-internal and not host-published. Only
    # allocated where Fedimint is effectively on (supported chain + enabled).
    guardians = net.fedimint_guardian_count(NETWORK_SPECS[net_key])
    for i in range(guardians):
        g = base + FEDIMINT_GUARDIAN_BASE + i * FEDIMINT_GUARDIAN_STRIDE
        ports[f"fedimintd_{i}_api_public"] = g
        ports[f"fedimintd_{i}_api"] = g + 1
        ports[f"fedimintd_{i}_ui"] = g + 2
    for i in range(guardians):  # one gateway paired with each guardian's ring node
        gw = base + FEDIMINT_GATEWAY_BASE + i * FEDIMINT_GATEWAY_STRIDE
        ports[f"gatewayd_{i}_api"] = gw  # loopback backend (Caddy proxies to it)
        ports[f"gatewayd_{i}_api_public"] = gw + 1  # Caddy public listener (wss)

    # Bitcart's btclnd p2p/gRPC pool bases (only when Bitcart is enabled).
    # The pools occupy dedicated sub-ranges so they never collide
    # with the fixed offsets above; we register the bases for collision checks.
    if net.bitcart.enabled:
        ports["bitcart_btclnd_p2p_base"] = base + BITCART_BTCLND_P2P_OFFSET
        ports["bitcart_btclnd_grpc_base"] = base + BITCART_BTCLND_GRPC_OFFSET

    # Apply explicit Bitcart port overrides (operator-facing convenience).
    bp = net.bitcart.ports
    for field, name in (
        (bp.store, "bitcart_store_public"),
        (bp.admin, "bitcart_admin_public"),
        (bp.api, "bitcart_api_public"),
        (bp.daemon, "bitcart_daemon"),
    ):
        if field is not None:
            ports[name] = field

    # Apply generic overrides last; they win over everything.
    for name, port in net.ports.items():
        if name not in ports:
            raise PortAllocationError(
                f"[{net_key}] override for unknown port {name!r}; "
                f"known: {sorted(ports)}"
            )
        ports[name] = port

    return ports


def allocate(cfg: ArgusConfig) -> dict[str, dict[str, int]]:
    """Allocate host ports for every enabled network.

    Returns ``{net_key: {port_name: port_number}}``. Raises
    :class:`PortAllocationError` if any two assignments collide.
    """
    result: dict[str, dict[str, int]] = {}
    owner: dict[int, str] = {}  # port -> "net_key.port_name" for diagnostics

    for net_key, net in cfg.enabled_networks():
        ports = _network_ports(net_key, net)
        for name, port in ports.items():
            tag = f"{net_key}.{name}"
            if port in owner:
                raise PortAllocationError(
                    f"port {port} is claimed by both {owner[port]} and {tag}; "
                    f"adjust a 'ports' override"
                )
            owner[port] = tag
        result[net_key] = ports

    return result
