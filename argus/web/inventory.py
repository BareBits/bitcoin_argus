"""Turn the validated config + allocated ports + live metrics into the view model
the templates render: one section per configured network, each listing its
services with their ports, access links, and resource usage.

This is the single place that knows *which* services a network runs and how to
reach them — it mirrors the generators (builders + bitcart + shared Caddy).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ArgusConfig
from ..constants import NETWORK_SPECS
from ..tor import onion_routes
from .content import (
    MEMPOOL_SPACE_LN_NODE,
    VARIANT_ORDER,
    VARIANTS,
    AttachCommand,
    Variant,
    attach_commands,
)
from .metrics import bucket_for


@dataclass
class PortRef:
    label: str
    port: int
    public: bool


@dataclass
class LinkRef:
    label: str
    url: str


@dataclass
class OnionPortRef:
    """One sub-tool reachable over the installation's onion at this port."""

    service: str
    port: int


@dataclass
class ServiceRow:
    name: str
    bucket: str
    ports: list[PortRef] = field(default_factory=list)
    links: list[LinkRef] = field(default_factory=list)
    ram: int | None = None
    disk: int | None = None

    @property
    def audience(self) -> str:
        """Who can reach this service: a visitor (any public port/link) or only
        the operator (everything bound to the server's localhost)."""
        if self.links or any(p.public for p in self.ports):
            return "Visitor"
        return "Operator only"


@dataclass
class DonationRow:
    """One row in the "donate / recycle your coins" table: the public donation
    address for a network plus its lifetime received and current wallet balance.
    Figures are BTC strings as bitcoind reports them; None => not available yet."""

    key: str
    title: str
    address: str | None = None
    total_received: str | None = None
    balance: str | None = None


@dataclass
class NetworkSection:
    key: str
    title: str
    variant: Variant
    enabled: bool
    services: list[ServiceRow] = field(default_factory=list)
    attach: list[AttachCommand] = field(default_factory=list)
    onion: list[OnionPortRef] = field(default_factory=list)
    ram_total: int = 0
    disk_total: int = 0


def _url(cfg: ArgusConfig, service_ssl: bool, port: int) -> str:
    scheme = "https" if (cfg.global_.ssl_enabled and service_ssl) else "http"
    return f"{scheme}://{cfg.global_.hostname}:{port}/"


def _usage(metrics: dict, net_key: str, bucket: str) -> tuple[int | None, int | None]:
    entry = (metrics.get("usage") or {}).get(net_key, {}).get(bucket)
    if not entry:
        return None, None
    return entry.get("ram"), entry.get("disk")


def _service_rows(
    cfg: ArgusConfig, net_key: str, ports: dict[str, int], metrics: dict
) -> list[ServiceRow]:
    net = cfg.networks[net_key]
    spec = NETWORK_SPECS[net_key]
    rows: list[ServiceRow] = []

    def row(name: str, bucket: str, **kw) -> ServiceRow:
        ram, disk = _usage(metrics, net_key, bucket)
        return ServiceRow(name=name, bucket=bucket, ram=ram, disk=disk, **kw)

    # Bitcoin Core node (always present).
    rows.append(
        row(
            "Bitcoin Core node",
            "bitcoind",
            ports=[
                PortRef("P2P", ports["bitcoind_p2p"], public=net.bitcoind.p2p_public),
                PortRef("RPC", ports["bitcoind_rpc"], public=False),
            ],
        )
    )

    # Block producer (regtest or a self-mined custom signet).
    if net.miner.enabled and spec.supports_miner:
        rows.append(row("Signet miner" if spec.is_signet else "Regtest miner", "miner"))

    # Standalone LND node(s). On mined networks a second node ("argus2") is added
    # and the two are auto-wired with channels. When we know a node's pubkey, link
    # its row to its Lightning node page: our own mempool if this network runs one,
    # otherwise the public mempool.space explorer for networks it covers.
    def lnd_row(bucket: str, label: str, p2p: str, rest: str, grpc: str, pk) -> None:
        links: list[LinkRef] = []
        if pk and net.mempool_enabled(spec):
            base = _url(cfg, net.mempool.ssl, ports["mempool_public"])
            links.append(LinkRef("Node on mempool", f"{base}lightning/node/{pk}"))
        elif pk and net_key in MEMPOOL_SPACE_LN_NODE:
            links.append(
                LinkRef("Node on mempool.space", f"{MEMPOOL_SPACE_LN_NODE[net_key]}{pk}")
            )
        rows.append(
            row(
                label,
                bucket,
                ports=[
                    PortRef("P2P", ports[p2p], public=True),
                    PortRef("REST", ports[rest], public=False),
                    PortRef("gRPC", ports[grpc], public=False),
                ],
                links=links,
            )
        )

    name1 = net.lnd.name or ("argus1" if spec.supports_miner else f"argus-{net_key}")
    lnd_row(
        "lnd", f"LND ({name1})", "lnd_p2p", "lnd_rest", "lnd_grpc",
        (metrics.get("lnd") or {}).get(net_key),
    )
    if net.lnd_secondary_enabled(spec):
        lnd_row(
            "lnd2", f"LND ({net.lnd.secondary.name})",
            "lnd2_p2p", "lnd2_rest", "lnd2_grpc",
            (metrics.get("lnd2") or {}).get(net_key),
        )

    # Fulcrum indexers (Electrum servers).
    for i, ix in enumerate(net.enabled_indexers()):
        rows.append(
            row(
                f"Fulcrum ({ix.name})",
                bucket_for(ix.name),
                ports=[
                    PortRef("Electrum TCP", ports[f"fulcrum_{i}_electrum_tcp"], public=True),
                    PortRef("admin", ports[f"fulcrum_{i}_admin"], public=False),
                ],
            )
        )

    # Cashu mint.
    if net.cashu.enabled:
        rows.append(
            row(
                "Cashu mint",
                "cashu",
                ports=[PortRef("HTTP", ports["cashu_public"], public=True)],
                links=[LinkRef("Mint", _url(cfg, net.cashu.ssl, ports["cashu_public"]))],
            )
        )

    # mempool explorer.
    if net.mempool_enabled(spec):
        rows.append(
            row(
                "mempool explorer",
                "mempool",
                ports=[PortRef("Web", ports["mempool_public"], public=True)],
                links=[LinkRef("Explorer", _url(cfg, net.mempool.ssl, ports["mempool_public"]))],
            )
        )

    # Bitcart (deployed by the BareBits installer; multiple containers).
    if net.bitcart.enabled:
        rows.append(
            row(
                "Bitcart",
                "bitcart",
                ports=[
                    PortRef("Store", ports["bitcart_store_public"], public=True),
                    PortRef("Admin", ports["bitcart_admin_public"], public=True),
                    PortRef("API", ports["bitcart_api_public"], public=True),
                ],
                links=[
                    LinkRef("Store", _url(cfg, net.bitcart.ssl, ports["bitcart_store_public"])),
                    LinkRef("Admin", _url(cfg, net.bitcart.ssl, ports["bitcart_admin_public"])),
                    LinkRef("API", _url(cfg, net.bitcart.ssl, ports["bitcart_api_public"])),
                ],
            )
        )

    return rows


def build_donations(cfg: ArgusConfig, metrics: dict) -> list[DonationRow]:
    """One donation row per *enabled* network, in the recommended order.

    Each network runs a single bitcoind wallet (the miner's where there is one);
    the sidecar publishes that wallet's donation address, its lifetime received,
    and its current balance (see :mod:`argus.builders.donations`). Rows render
    even before the sidecar has reported — the figures just show as pending.
    """
    data = metrics.get("donations") or {}
    rows: list[DonationRow] = []
    for net_key in VARIANT_ORDER:
        net = cfg.networks.get(net_key)
        if net is None or not net.enabled:
            continue
        info = data.get(net_key) or {}
        rows.append(
            DonationRow(
                key=net_key,
                title=VARIANTS[net_key].title,
                address=info.get("address"),
                total_received=info.get("total_received"),
                balance=info.get("balance"),
            )
        )
    return rows


def build_sections(
    cfg: ArgusConfig,
    port_map: dict[str, dict[str, int]],
    metrics: dict,
    onion_hostname: str | None = None,
) -> list[NetworkSection]:
    """Build one section per configured network, in the recommended order.

    Disabled networks still get a section (with no service table) so visitors see
    the full menu of variants and their status. When ``onion_hostname`` is set
    (Tor enabled), each section also lists how its sub-tools map onto the single
    onion address (by port).
    """
    # Onion routes for the whole install, grouped by network (computed once).
    onion_by_net: dict[str | None, list] = {}
    if onion_hostname:
        for r in onion_routes(cfg, port_map):
            onion_by_net.setdefault(r.net_key, []).append(r)

    sections: list[NetworkSection] = []
    for net_key in VARIANT_ORDER:
        net = cfg.networks.get(net_key)
        if net is None:
            continue  # not configured at all on this host
        variant = VARIANTS[net_key]
        section = NetworkSection(
            key=net_key,
            title=variant.title,
            variant=variant,
            enabled=net.enabled,
        )
        if net.enabled and net_key in port_map:
            ports = port_map[net_key]
            section.services = _service_rows(cfg, net_key, ports, metrics)
            section.ram_total = sum(s.ram or 0 for s in section.services)
            section.disk_total = sum(s.disk or 0 for s in section.services)
            section.attach = attach_commands(
                net_key, cfg.global_.hostname, ports, net.bitcoind.p2p_public
            )
            # Prepend the LND connection URI(s) when the node pubkey(s) are known.
            # Insert node2 first then node1 so argus1 ends up at the very top.
            spec = NETWORK_SPECS[net_key]
            uris: list[tuple[str, str, int]] = []
            pk1 = (metrics.get("lnd") or {}).get(net_key)
            if pk1:
                name1 = net.lnd.name or (
                    "argus1" if spec.supports_miner else f"argus-{net_key}"
                )
                uris.append((name1, pk1, ports["lnd_p2p"]))
            if net.lnd_secondary_enabled(spec):
                pk2 = (metrics.get("lnd2") or {}).get(net_key)
                if pk2:
                    uris.append((net.lnd.secondary.name, pk2, ports["lnd2_p2p"]))
            for label, pk, p2p in reversed(uris):
                uri = f"{pk}@{cfg.global_.hostname}:{p2p}"
                # Onion connect line first (when Tor is on) so it sits above the
                # clearnet one: the node advertises this onion in gossip, so peers
                # can open channels to it over Tor.
                if onion_hostname:
                    onion_uri = f"{pk}@{onion_hostname}:{p2p}"
                    section.attach.insert(0, AttachCommand(
                        label=f"Lightning node {label} (LND) — connect over Tor",
                        command=f"lncli connect {onion_uri}",
                        note="The node advertises this onion URI in gossip; "
                             "torify/launch lncli through Tor to reach it.",
                        audience="visitor",
                    ))
                section.attach.insert(0, AttachCommand(
                    label=f"Lightning node {label} (LND) — connect / open a channel",
                    command=f"lncli connect {uri}",
                    note="The node's public connection URI is pubkey@host:port.",
                    audience="visitor",
                ))

            # Per-network onion port map (for the "Tor accessibility" section).
            section.onion = [
                OnionPortRef(r.service, r.virtual_port)
                for r in onion_by_net.get(net_key, [])
            ]
        sections.append(section)
    return sections
