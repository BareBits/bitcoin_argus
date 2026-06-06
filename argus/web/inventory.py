"""Turn the validated config + allocated ports + live metrics into the view model
the templates render: one section per configured network, each listing its
services with their ports, access links, and resource usage.

This is the single place that knows *which* services a network runs and how to
reach them — it mirrors the generators (builders + bitcart + shared Caddy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from ..config import ArgusConfig
from ..constants import NETWORK_SPECS
from ..reset import format_reset_eta, seconds_until_reset
from ..tor import onion_routes, onion_web_path_routed
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
    # The same endpoint over Tor (http://<onion>:<port>/<path>), when this
    # service is exposed on the installation's onion. None => clearnet only.
    onion_url: str | None = None


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
    # The network's donate LNURL / Lightning Address, when web.lnurl is on.
    # ``lightning_address`` is the clearnet form (shown only with ssl_enabled,
    # since wallets need https); ``lightning_onion`` is the .onion form (http).
    lightning_address: str | None = None
    lightning_onion: str | None = None


@dataclass
class LiquidityNode:
    """One LND node's row in the operator "add liquidity" panel: where to deposit
    on-chain coins, the current on-chain balance, and the channel liquidity it
    holds in each direction (outbound = can send, inbound = can receive). All BTC
    figures are pre-formatted strings; ``address`` is "" until the node reports."""

    label: str
    address: str
    onchain_btc: str
    onchain_pending_btc: str
    outbound_btc: str
    inbound_btc: str
    num_channels: int
    num_active_channels: int


@dataclass
class ResetInfo:
    """The auto-reset countdown for one network's section.

    ``eta_text`` is the human countdown ("X days, Y hours", or "imminent"), or
    None while the controller hasn't reported a chain size yet. ``max_size_gb``
    drives the tooltip copy ("automatically resets every N GB")."""

    max_size_gb: float
    eta_text: str | None = None

    @property
    def known(self) -> bool:
        return self.eta_text is not None

    @property
    def cap_text(self) -> str:
        """The cap as a compact string (30 not 30.0; 0.05 stays 0.05)."""
        return f"{self.max_size_gb:g}"


@dataclass
class NetworkSection:
    key: str
    title: str
    variant: Variant
    enabled: bool
    services: list[ServiceRow] = field(default_factory=list)
    attach: list[AttachCommand] = field(default_factory=list)
    ram_total: int = 0
    disk_total: int = 0
    reset: ResetInfo | None = None
    # Per-node liquidity rows for the operator "add liquidity" panel (empty until
    # the nodes report a snapshot) + network totals (pre-formatted BTC strings).
    liquidity: list[LiquidityNode] = field(default_factory=list)
    liquidity_outbound_btc: str = "0"
    liquidity_inbound_btc: str = "0"


def _url(cfg: ArgusConfig, service_ssl: bool, port: int) -> str:
    scheme = "https" if (cfg.global_.ssl_enabled and service_ssl) else "http"
    return f"{scheme}://{cfg.global_.hostname}:{port}/"


def _sat_to_btc(sat: int) -> str:
    """Format integer satoshis as a compact BTC string (trailing zeros trimmed)."""
    s = f"{sat / 1e8:.8f}".rstrip("0").rstrip(".")
    return s or "0"


def _as_int(value) -> int:
    """Coerce a JSON scalar (the sidecar writes numbers; be defensive) to int."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _liquidity_nodes(
    cfg: ArgusConfig, net_key: str, metrics: dict, labels: list[tuple[str, str]]
) -> tuple[list[LiquidityNode], int, int]:
    """Build the operator liquidity rows for a network from the metrics snapshot.

    ``labels`` maps each present compose service ("lnd"/"lnd2"/"lnd3") to its
    display alias. Returns the rows plus total outbound/inbound satoshis."""
    snaps = (metrics.get("liquidity") or {}).get(net_key) or {}
    nodes: list[LiquidityNode] = []
    tot_out = tot_in = 0
    for service, label in labels:
        snap = snaps.get(service) or {}
        out_sat = _as_int(snap.get("channel_outbound_sat"))
        in_sat = _as_int(snap.get("channel_inbound_sat"))
        tot_out += out_sat
        tot_in += in_sat
        nodes.append(
            LiquidityNode(
                label=label,
                address=str(snap.get("address") or ""),
                onchain_btc=_sat_to_btc(_as_int(snap.get("onchain_confirmed"))),
                onchain_pending_btc=_sat_to_btc(
                    _as_int(snap.get("onchain_unconfirmed"))
                ),
                outbound_btc=_sat_to_btc(out_sat),
                inbound_btc=_sat_to_btc(in_sat),
                num_channels=_as_int(snap.get("num_channels")),
                num_active_channels=_as_int(snap.get("num_active_channels")),
            )
        )
    return nodes, tot_out, tot_in


def _usage(metrics: dict, net_key: str, bucket: str) -> tuple[int | None, int | None]:
    entry = (metrics.get("usage") or {}).get(net_key, {}).get(bucket)
    if not entry:
        return None, None
    return entry.get("ram"), entry.get("disk")


def _service_rows(
    cfg: ArgusConfig,
    net_key: str,
    ports: dict[str, int],
    metrics: dict,
    onion_hostname: str | None = None,
    onion_ports: frozenset[int] = frozenset(),
    onion_faucet: bool = False,
) -> list[ServiceRow]:
    net = cfg.networks[net_key]
    spec = NETWORK_SPECS[net_key]
    rows: list[ServiceRow] = []

    def row(name: str, bucket: str, **kw) -> ServiceRow:
        ram, disk = _usage(metrics, net_key, bucket)
        return ServiceRow(name=name, bucket=bucket, ram=ram, disk=disk, **kw)

    def link(label: str, ssl: bool, port: int, path: str = "") -> LinkRef:
        """A service link plus its onion equivalent (same port, routed by Tor).
        The onion URL is set only when this port is actually exposed on the
        installation's onion; ``_url`` already ends with a trailing slash."""
        clear = _url(cfg, ssl, port) + path
        onion = (
            f"http://{onion_hostname}:{port}/{path}"
            if onion_hostname and port in onion_ports
            else None
        )
        return LinkRef(label, clear, onion)

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
            links.append(
                link("Node on mempool", net.mempool.ssl, ports["mempool_public"],
                     f"lightning/node/{pk}")
            )
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
    if net.lnd_tertiary_enabled(spec):
        lnd_row(
            "lnd3", f"LND ({net.lnd.tertiary.name})",
            "lnd3_p2p", "lnd3_rest", "lnd3_grpc",
            (metrics.get("lnd3") or {}).get(net_key),
        )

    # The faucet (a separate container, path-routed at /<net>/faucet on the site
    # root). The clearnet link is same-origin (no port). Over Tor it lives on the
    # onion's port 80 (the dashboard's port) since the onion routes that port
    # through Caddy's path routing — see argus/tor.py — so the onion variant is the
    # bare onion host with the same path. Usage shows n/a: the faucet container
    # (argus-faucet) isn't attributed to any one network.
    if net.faucet.enabled:
        faucet_onion = (
            f"http://{onion_hostname}/{net_key}/faucet" if onion_faucet else None
        )
        rows.append(
            row(
                "Faucet and lightning functions",
                "faucet",
                links=[LinkRef("Open faucet", f"/{net_key}/faucet", faucet_onion)],
            )
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

    # Cashu mint. The mint is an API, not a web page — its root 404s — so the
    # link points at /v1/info (the NUT-06 mint-info endpoint), which returns valid
    # JSON and confirms the mint is live. The cashu.me row below is the way to
    # actually use the mint.
    if net.cashu.enabled:
        rows.append(
            row(
                "Cashu mint",
                "cashu",
                ports=[PortRef("HTTP", ports["cashu_public"], public=True)],
                links=[link("Mint info", net.cashu.ssl, ports["cashu_public"], "v1/info")],
            )
        )
        # Co-located cashu.me web wallet, pre-pointed at this network's mint via
        # the ?mint= deep-link (opens cashu.me's add-mint dialog pre-filled). The
        # onion variant embeds the *onion* mint URL so it resolves over Tor, and
        # is offered only when both the wallet and the mint are on the onion.
        if net.cashu.wallet:
            mint_clear = _url(cfg, net.cashu.ssl, ports["cashu_public"])
            wallet_clear = _url(cfg, net.cashu.ssl, ports["cashu_wallet_public"])
            clear = f"{wallet_clear}?mint={quote(mint_clear, safe='')}"
            wp, mp = ports["cashu_wallet_public"], ports["cashu_public"]
            onion = None
            if onion_hostname and wp in onion_ports and mp in onion_ports:
                mint_onion = f"http://{onion_hostname}:{mp}/"
                onion = f"http://{onion_hostname}:{wp}/?mint={quote(mint_onion, safe='')}"
            rows.append(
                row(
                    "cashu.me (web wallet)",
                    "cashu-wallet",
                    ports=[PortRef("HTTP", ports["cashu_wallet_public"], public=True)],
                    links=[LinkRef("Open wallet", clear, onion)],
                )
            )

    # mempool explorer.
    if net.mempool_enabled(spec):
        rows.append(
            row(
                "mempool explorer",
                "mempool",
                ports=[PortRef("Web", ports["mempool_public"], public=True)],
                links=[link("Explorer", net.mempool.ssl, ports["mempool_public"])],
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
                    link("Store", net.bitcart.ssl, ports["bitcart_store_public"]),
                    link("Admin", net.bitcart.ssl, ports["bitcart_admin_public"]),
                    link("API", net.bitcart.ssl, ports["bitcart_api_public"]),
                ],
            )
        )

    return rows


def build_donations(
    cfg: ArgusConfig, metrics: dict, onion_hostname: str | None = None
) -> list[DonationRow]:
    """One donation row per *enabled* network, in the recommended order.

    Each network runs a single bitcoind wallet (the miner's where there is one);
    the sidecar publishes that wallet's donation address, its lifetime received,
    and its current balance (see :mod:`argus.builders.donations`). Rows render
    even before the sidecar has reported — the figures just show as pending.

    When ``web.lnurl`` is on, each row also carries the network's ``donate``
    Lightning Address (clearnet and/or onion), so coins can be returned over
    Lightning too.
    """
    data = metrics.get("donations") or {}
    lnurl = cfg.web.lnurl
    lnurl_on = cfg.web.enabled and lnurl.enabled
    enabled_keys = [k for k, _ in cfg.enabled_networks()]
    default_net = lnurl.default_network or (enabled_keys[0] if enabled_keys else None)

    rows: list[DonationRow] = []
    for net_key in VARIANT_ORDER:
        net = cfg.networks.get(net_key)
        if net is None or not net.enabled:
            continue
        info = data.get(net_key) or {}
        ln_addr = ln_onion = None
        if lnurl_on:
            # Bare ``donate@`` for the default network, ``donate-<net>@`` elsewhere.
            local = "donate" if net_key == default_net else f"donate-{net_key}"
            if cfg.global_.ssl_enabled:
                ln_addr = f"{local}@{cfg.global_.hostname}"
            if onion_hostname:
                ln_onion = f"{local}@{onion_hostname}"
        rows.append(
            DonationRow(
                key=net_key,
                title=VARIANTS[net_key].title,
                address=info.get("address"),
                total_received=info.get("total_received"),
                balance=info.get("balance"),
                lightning_address=ln_addr,
                lightning_onion=ln_onion,
            )
        )
    return rows


def _reset_info(cfg: ArgusConfig, net_key: str, metrics: dict) -> ResetInfo | None:
    """Build the reset countdown for a network, or None if it isn't auto-reset.

    The cap (``max_size_gb``) comes from config so the section can always explain
    the policy; the live size (and thus the countdown) comes from the controller's
    published state, which may not be there yet (then ``eta_text`` is None)."""
    net = cfg.networks[net_key]
    if not net.reset_enabled(net_key):
        return None
    info = ResetInfo(max_size_gb=net.reset_max_size_gb(NETWORK_SPECS[net_key]))
    state = (metrics.get("reset") or {}).get(net_key) or {}
    size = state.get("size_on_disk")
    if size is not None:
        eta = seconds_until_reset(
            int(size),
            int(state.get("limit_bytes", 0)),
            int(state.get("block_interval_seconds", net.miner.block_interval_seconds)),
        )
        info.eta_text = format_reset_eta(eta)
    return info


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
    # The faucet rides the onion's port-80 path routing (Caddy), not its own onion
    # port, so it has an onion link exactly when that path routing is active.
    onion_faucet = onion_hostname is not None and onion_web_path_routed(cfg)

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
        if net.enabled:
            section.reset = _reset_info(cfg, net_key, metrics)
        if net.enabled and net_key in port_map:
            ports = port_map[net_key]
            onion_ports = frozenset(
                r.virtual_port for r in onion_by_net.get(net_key, [])
            )
            section.services = _service_rows(
                cfg, net_key, ports, metrics, onion_hostname, onion_ports,
                onion_faucet,
            )
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
            if net.lnd_tertiary_enabled(spec):
                pk3 = (metrics.get("lnd3") or {}).get(net_key)
                if pk3:
                    uris.append((net.lnd.tertiary.name, pk3, ports["lnd3_p2p"]))
            for label, pk, p2p in reversed(uris):
                uri = f"{pk}@{cfg.global_.hostname}:{p2p}"
                # Clearnet connect line, with the onion connect (when Tor is on)
                # shown right below it in the SAME code box. The node advertises
                # this onion in gossip, so peers can open channels over Tor too.
                onion_cmd = (
                    f"lncli connect {pk}@{onion_hostname}:{p2p}"
                    if onion_hostname else ""
                )
                section.attach.insert(0, AttachCommand(
                    label=f"Lightning node {label} (LND) — connect / open a channel",
                    command=f"lncli connect {uri}",
                    note="The node's public connection URI is pubkey@host:port.",
                    audience="visitor",
                    command_onion=onion_cmd,
                ))

            # Operator "add liquidity" panel: deposit addresses + balances per node.
            labels: list[tuple[str, str]] = [
                ("lnd", net.lnd.name or (
                    "argus1" if spec.supports_miner else f"argus-{net_key}"
                ))
            ]
            if net.lnd_secondary_enabled(spec):
                labels.append(("lnd2", net.lnd.secondary.name))
            if net.lnd_tertiary_enabled(spec):
                labels.append(("lnd3", net.lnd.tertiary.name))
            nodes, tot_out, tot_in = _liquidity_nodes(cfg, net_key, metrics, labels)
            section.liquidity = nodes
            section.liquidity_outbound_btc = _sat_to_btc(tot_out)
            section.liquidity_inbound_btc = _sat_to_btc(tot_in)
        sections.append(section)
    return sections
