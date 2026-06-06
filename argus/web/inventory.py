"""Turn the validated config + allocated ports + live metrics into the view model
the templates render: one section per configured network, each listing its
services with their ports, access links, and resource usage.

This is the single place that knows *which* services a network runs and how to
reach them — it mirrors the generators (builders + bitcart + shared Caddy).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from .. import __version__ as ARGUS_VERSION
from ..ark_cln import ARK_CLN_VERSION
from ..config import ArgusConfig
from ..constants import NETWORK_SPECS
from ..reset import format_reset_eta, seconds_until_reset
from ..tor import onion_routes, onion_web_path_routed
from .content import (
    MEMPOOL_SPACE_LN_NODE,
    SUBTOOL_REPO,
    VARIANT_ORDER,
    VARIANTS,
    AttachCommand,
    AttachTool,
    Variant,
    attach_commands,
    attach_tool_groups,
    image_version,
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
    cpu: float | None = None
    disk: int | None = None
    # Cumulative network bytes since the container last started (point-in-time, for
    # the live table; the /stats graphs derive speed + totals from interval deltas).
    net_rx: int | None = None
    net_tx: int | None = None
    # The deployed version of this sub-tool (a docker image tag, a git ref, or the
    # Argus version for our own components) and the GitHub repo it links to. Both
    # None => no version cell (shown as "—").
    version: str | None = None
    repo_url: str | None = None
    # Fedimint federation invite code (leader-guardian row only): the string a
    # wallet joins with, plus a scannable QR (inline SVG) when it can be rendered.
    invite_code: str | None = None
    invite_qr: str | None = None

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
    # Visitor "attach your tools" recipes, grouped by tool with per-OS commands.
    attach_tools: list[AttachTool] = field(default_factory=list)
    ram_total: int = 0
    cpu_total: float = 0.0
    disk_total: int = 0
    net_rx_total: int = 0
    net_tx_total: int = 0
    reset: ResetInfo | None = None
    # Per-node liquidity rows for the operator "add liquidity" panel (empty until
    # the nodes report a snapshot) + network totals (pre-formatted BTC strings).
    liquidity: list[LiquidityNode] = field(default_factory=list)
    liquidity_outbound_btc: str = "0"
    liquidity_inbound_btc: str = "0"


def _url(cfg: ArgusConfig, service_ssl: bool, port: int) -> str:
    scheme = "https" if (cfg.global_.ssl_enabled and service_ssl) else "http"
    return f"{scheme}://{cfg.global_.hostname}:{port}/"


def _invite_qr_svg(code: str | None) -> str | None:
    """Render a Fedimint invite code as an inline SVG QR a Fedi wallet can scan.

    Offline (no external service). Returns None when there is no code or the
    optional ``qrcode`` lib isn't installed — the row still shows the copyable
    text, so the dashboard degrades gracefully."""
    if not code:
        return None
    try:
        import io

        import qrcode
        import qrcode.image.svg

        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(code)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(image_factory=qrcode.image.svg.SvgPathImage).save(buf)
        return buf.getvalue().decode()
    except Exception:
        return None


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


def _usage(metrics: dict, net_key: str, bucket: str) -> dict | None:
    """The live usage dict ``{"ram","cpu","disk","net_rx","net_tx"}`` for a bucket,
    or None when nothing has been recorded for it yet."""
    return (metrics.get("usage") or {}).get(net_key, {}).get(bucket) or None


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
    g = cfg.global_
    rows: list[ServiceRow] = []

    def row(name: str, bucket: str, **kw) -> ServiceRow:
        u = _usage(metrics, net_key, bucket)
        if not u:
            return ServiceRow(name=name, bucket=bucket, **kw)
        return ServiceRow(
            name=name,
            bucket=bucket,
            ram=u.get("ram"),
            cpu=u.get("cpu"),
            disk=u.get("disk"),
            net_rx=u.get("net_rx"),
            net_tx=u.get("net_tx"),
            **kw,
        )

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

    # Bitcoin Core node (always present). Mutinynet runs a signetblocktime fork
    # (its own image + repo); everyone else runs upstream Bitcoin Core.
    if spec.needs_knots:
        core_image, core_repo = g.bitcoind_knots_image, SUBTOOL_REPO["bitcoind_signet_fork"]
    else:
        core_image, core_repo = g.bitcoind_image, SUBTOOL_REPO["bitcoind"]
    rows.append(
        row(
            "Bitcoin Core node",
            "bitcoind",
            ports=[
                PortRef("P2P", ports["bitcoind_p2p"], public=net.bitcoind.p2p_public),
                PortRef("RPC", ports["bitcoind_rpc"], public=False),
            ],
            version=image_version(core_image) or None,
            repo_url=core_repo,
        )
    )

    # Block producer (regtest or a self-mined custom signet) — Argus's own miner.
    if net.miner.enabled and spec.supports_miner:
        rows.append(row(
            "Signet miner" if spec.is_signet else "Regtest miner", "miner",
            version=ARGUS_VERSION, repo_url=SUBTOOL_REPO["argus"],
        ))

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
                version=image_version(g.lnd_image) or None,
                repo_url=SUBTOOL_REPO["lnd"],
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
    # (argus-faucet) isn't attributed to any one network. The faucet is Argus's own
    # code and, when enabled, is shown first in the list (rows.insert(0, ...)).
    if net.faucet.enabled:
        faucet_onion = (
            f"http://{onion_hostname}/{net_key}/faucet" if onion_faucet else None
        )
        rows.insert(
            0,
            row(
                "Faucet and lightning functions",
                "faucet",
                links=[LinkRef("Open faucet", f"/{net_key}/faucet", faucet_onion)],
                version=ARGUS_VERSION,
                repo_url=SUBTOOL_REPO["argus"],
            ),
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
                version=image_version(g.fulcrum_image) or None,
                repo_url=SUBTOOL_REPO["fulcrum"],
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
                version=image_version(g.cashu_image) or None,
                repo_url=SUBTOOL_REPO["cashu"],
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
                    version=(g.cashu_wallet_ref[:7] or None),
                    repo_url=SUBTOOL_REPO["cashu_wallet"],
                )
            )

    # Fedimint federation (guardians) + a Lightning gateway per ring node. Each
    # gateway rides the LND node it is paired with, so its row names that node
    # (gateway i -> argus_i) — the federation's Lightning liquidity is that node's.
    if net.fedimint_enabled(spec):
        n = net.fedimint_guardian_count(spec)
        lnd_names = [name1, net.lnd.secondary.name, net.lnd.tertiary.name]
        # The live federation invite code (a wallet joins with it), read by the
        # metrics collector from the leader guardian; shown on the leader's row.
        invite = (metrics.get("fedimint") or {}).get(net_key)
        for i in range(n):
            num = "" if n == 1 else f" {i + 1}"
            rows.append(
                row(
                    f"Fedimint guardian{num}",
                    "fedimintd" if i == 0 else f"fedimintd{i + 1}",
                    ports=[
                        PortRef("API", ports[f"fedimintd_{i}_api_public"], public=True),
                        PortRef("UI", ports[f"fedimintd_{i}_ui"], public=False),
                    ],
                    version=image_version(g.fedimintd_image) or None,
                    repo_url=SUBTOOL_REPO["fedimint"],
                    invite_code=invite if i == 0 else None,
                    invite_qr=_invite_qr_svg(invite) if i == 0 else None,
                )
            )
        for i in range(n):
            rows.append(
                row(
                    f"Fedimint gateway ({lnd_names[i]})",
                    "gatewayd" if i == 0 else f"gatewayd{i + 1}",
                    ports=[PortRef("API", ports[f"gatewayd_{i}_api_public"], public=True)],
                    links=[
                        link("Gateway UI", g.ssl_enabled, ports[f"gatewayd_{i}_api_public"])
                    ],
                    version=image_version(g.gatewayd_image) or None,
                    repo_url=SUBTOOL_REPO["fedimint"],
                )
            )

    # Ark ASP: the captaind server + its Core Lightning bridge node. The bridge
    # opens one channel into the ring (default argus1); both are funded externally
    # via the two on-chain addresses the setup sidecars publish (see the operator
    # docs / `argus credentials`). captaind's Ark gRPC is fronted publicly (h2c) so
    # bark wallets can reach it; the CLN bridge's P2P is public (a reachable node).
    if net.ark_enabled(spec):
        _ark_target, _ark_svc, _ark_vol = net.ark_channel_target(spec)
        rows.append(
            row(
                "Ark server (captaind)",
                "captaind",
                ports=[
                    PortRef("API", ports["ark_captaind_public"], public=True),
                    PortRef("admin", ports["ark_captaind_admin"], public=False),
                ],
                version=image_version(g.ark_captaind_image) or None,
                repo_url=SUBTOOL_REPO["ark"],
            )
        )
        rows.append(
            row(
                f"Ark Lightning bridge (CLN -> {_ark_target})",
                "cln",
                ports=[
                    PortRef("P2P", ports["ark_cln_p2p"], public=True),
                    PortRef("gRPC", ports["ark_cln_grpc"], public=False),
                ],
                version=ARK_CLN_VERSION,
                repo_url=SUBTOOL_REPO["ark_cln"],
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
                version=image_version(g.mempool_frontend_image) or None,
                repo_url=SUBTOOL_REPO["mempool"],
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
                version=(net.bitcart.branch or None),
                repo_url=SUBTOOL_REPO["bitcart"],
            )
        )

    # CashuPayServer (BTCPay-compatible gateway backed by the mint).
    if net.cashupayserver.enabled:
        rows.append(
            row(
                "CashuPayServer",
                "cashupayserver",
                ports=[PortRef("HTTP", ports["cashupayserver_public"], public=True)],
                links=[link("Admin", net.cashupayserver.ssl,
                            ports["cashupayserver_public"], "admin.php")],
                version=(g.cashupayserver_ref[:7] or None),
                repo_url=SUBTOOL_REPO["cashupayserver"],
            )
        )

    # WooCommerce storefront (WordPress + its own MariaDB).
    if net.woocommerce.enabled:
        rows.append(
            row(
                "WooCommerce store",
                "woocommerce",
                ports=[PortRef("HTTP", ports["woocommerce_public"], public=True)],
                links=[link("Shop", net.woocommerce.ssl,
                            ports["woocommerce_public"], "shop/")],
                version=image_version(g.wordpress_image) or None,
                repo_url=SUBTOOL_REPO["woocommerce"],
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
            section.cpu_total = sum(s.cpu or 0.0 for s in section.services)
            section.disk_total = sum(s.disk or 0 for s in section.services)
            section.net_rx_total = sum(s.net_rx or 0 for s in section.services)
            section.net_tx_total = sum(s.net_tx or 0 for s in section.services)
            # Operator recipes (Bitcoin Core RPC). Visitor recipes are grouped,
            # per-OS, in section.attach_tools below.
            section.attach = attach_commands(net_key, ports)

            # The LND connection URI(s) for the per-OS "Lightning (lncli)" group,
            # known only once each node has reported its pubkey. argus1 first.
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

            # A vanilla node needs the challenge to peer a *custom* signet; the
            # public signet and the PoW chains don't (None there). Mirrors the
            # resolution in argus/bitcart.py.
            signet_challenge = None
            if spec.is_signet and net_key != "signet":
                signet_challenge = net.signet_challenge or spec.default_signet_challenge
            section.attach_tools = attach_tool_groups(
                net_key, cfg.global_.hostname, ports, net.bitcoind.p2p_public,
                signet_challenge, uris, onion_hostname,
            )

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
