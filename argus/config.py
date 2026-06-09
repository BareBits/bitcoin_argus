"""Configuration schema, loading, and validation for Bitcoin Argus.

All user input flows through these pydantic models. ``extra="forbid"`` is set on
every model so unknown keys (typos, stale options) are rejected rather than
silently ignored. Cross-field rules (prune vs. txindex, required signet
challenges, etc.) are enforced in :meth:`ArgusConfig._validate_semantics`.
"""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from .constants import (
    ARK_RING_NODES,
    ARK_SUPPORTED_CHAINS,
    DEFAULT_RESET_CHECK_INTERVAL,
    DEFAULT_RESET_MAX_SIZE_GB,
    FEDIMINT_MAX_GUARDIANS,
    FEDIMINT_SUPPORTED_CHAINS,
    NETWORK_SPECS,
    NetworkSpec,
)


class ConfigError(Exception):
    """Raised for any invalid configuration (syntactic or semantic)."""


# Hostname per RFC 1123 (labels of a-z, 0-9, hyphen; not leading/trailing hyphen).
_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# Networks whose block production Argus can drive itself.
_MINEABLE_NETWORKS = {"regtest", "custom-signet-short", "custom-signet-long"}

# Minimum Bitcoin Core major version the Ark server (captaind) needs: it parses
# the `bits` field of getblockchaininfo, which Core added in 29.0.
_ARK_MIN_CORE_MAJOR = 29


def _core_major_from_image(image: str) -> int | None:
    """Best-effort Core major version from a Docker image ref's tag.

    e.g. ``bitcoin/bitcoin:30.0`` -> 30, ``lncm/bitcoind:v28.0`` -> 28. Returns
    None when the tag is absent or has no leading numeric version (a custom build
    whose version can't be inferred — we don't guess, so the Ark guard skips it).
    """
    if ":" not in image:
        return None
    tag = image.rsplit(":", 1)[1].lstrip("vV")
    m = re.match(r"(\d+)", tag)
    return int(m.group(1)) if m else None


def _is_valid_host(value: str) -> bool:
    """Accept a DNS hostname or a bare IP address."""
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        pass
    if len(value) > 253:
        return False
    labels = value.split(".")
    return all(_HOSTNAME_LABEL.match(label) for label in labels)


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ResourcesCfg(_Base):
    """Disk/RAM tuning. A ``profile`` sets a baseline; explicit knobs override it.

    Used at global level and per-network (per-network wins). All fields are
    optional/None so the resolver can tell "unset" (inherit) from an explicit 0.
    """

    profile: Literal["low", "medium", "high"] | None = None
    log_rotation: bool | None = None  # default true; caps Docker json-file logs
    log_max_size: str | None = None  # default "10m"
    log_max_file: int | None = Field(default=None, ge=1)
    # Explicit overrides of the profile's per-knob values:
    bitcoind_dbcache: int | None = Field(default=None, ge=4)  # MiB
    bitcoind_maxmempool: int | None = Field(default=None, ge=5)  # MB
    fulcrum_db_mem: int | None = Field(default=None, ge=1)  # MiB
    fulcrum_db_max_open_files: int | None = Field(default=None, ge=20)
    mempool_mariadb_buffer_mb: int | None = Field(default=None, ge=8)  # MB


class TorCfg(_Base):
    """Tor (v3 onion) accessibility for the whole installation.

    Opt-in. When enabled, Argus stands up a single onion service that fronts
    *every* enabled sub-tool — routing purely by port, so the onion uses the same
    port numbers as clearnet (one address for the entire install). Operator-only
    ports (Core RPC, LND gRPC/REST, Fulcrum admin) are never exposed. The four
    expose toggles let an operator narrow that surface without disabling Tor.
    """

    enabled: bool = False
    image: str = "lncm/tor:0.4.7.13"
    expose_web: bool = True  # mempool / cashu / bitcart frontends + the dashboard
    expose_electrum: bool = True  # public Fulcrum Electrum TCP ports
    expose_lnd_p2p: bool = True  # LND P2P (also drives LND onion advertisement)
    expose_bitcoind_p2p: bool = True  # bitcoind P2P (only where it is already public)

    @field_validator("image")
    @classmethod
    def _check_image(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("global.tor.image must not be empty")
        return v


class GlobalConfig(_Base):
    """Settings shared by every network."""

    hostname: str
    ssl_enabled: bool = True  # master switch; set False for local/test runs
    acme_email: str | None = None
    # Container images (verified against the target host at deploy time).
    # Bitcoin Core >= 29.0 is required by the Ark server (captaind needs the `bits`
    # field added to getblockchaininfo in Core 29.0); the official image is used
    # since lncm/bitcoind currently tops out at v28. Pinned to the same major as
    # Second's Ark reference stack.
    bitcoind_image: str = "bitcoin/bitcoin:30.0"
    # Mutinynet needs a signetblocktime-capable bitcoind; no public image
    # exists, so the operator must supply one (build from MutinyWallet/mutiny-net
    # or Bitcoin Knots). Empty unless mutinynet is enabled.
    bitcoind_knots_image: str = ""
    lnd_image: str = "polarlightning/lnd:0.19.3-beta"
    fulcrum_image: str = "cculianu/fulcrum:v2.1.1"
    cashu_image: str = "cashubtc/nutshell:0.20.1"
    # The cashu.me web wallet has no official published image, so Argus builds it
    # from source (a self-contained build context under generated/cashu-wallet).
    # This pins the cashu.me git ref (commit SHA or tag) the build clones; bump it
    # to update the wallet. Defaults to a known-good commit that supports the
    # ?mint= deep-link the dashboard relies on.
    cashu_wallet_ref: str = "adc4e107e961ad2567bc8cac1b7a987352ffc259"
    # CashuPayServer (BareBits Lite) is a PHP app with no published image, so
    # Argus builds it from source (generated/cashupayserver/). This pins the
    # github.com/BareBits/cashupayserver git ref (commit SHA or tag) cloned; bump
    # it to update the gateway. Defaults to the fork's main branch.
    cashupayserver_ref: str = "main"
    # Fedimint guardian daemon + Lightning gateway images. Bump together: the
    # gateway and guardians should run matching versions, and the bundled
    # fedimint-cli/gateway-cli must match for the DKG/connect-fed automation. Pinned
    # to v0.11.2-alpha.1 — the non-interactive DKG flow Argus generates (incl.
    # `set-local-params --federation-size`) is scripted against this release's CLI.
    fedimintd_image: str = "fedimint/fedimintd:v0.11.2-alpha.1"
    gatewayd_image: str = "fedimint/gatewayd:v0.11.2-alpha.1"
    # The Ark server (Second's captaind). The published image bundles its own
    # PostgreSQL, so no separate DB image is needed. Its Core Lightning bridge
    # node has no published image (the Boltz hold-invoice plugin is built in), so
    # Argus builds it from source into a shared context (see argus.ark_cln);
    # bump global.ark_cln_ref / the build args there to update CLN/the plugin.
    ark_captaind_image: str = "secondark/captaind:latest"
    # The WooCommerce storefront runs on the official WordPress images; the
    # WP-CLI image drives the (idempotent) provisioning.
    # Latest WordPress (>= 6.9 is required by current WooCommerce releases).
    wordpress_image: str = "wordpress:7.0.0-php8.3-apache"
    wordpress_cli_image: str = "wordpress:cli-php8.3"
    # WooCommerce + the BTCPay-for-WooCommerce plugin are installed at provision
    # time via WP-CLI. Empty => the latest from wordpress.org (fine for testnets);
    # pin a version string for reproducible builds.
    woocommerce_version: str = ""
    btcpay_woocommerce_version: str = ""
    caddy_image: str = "caddy:2"
    mempool_backend_image: str = "mempool/backend:v3.3.1"
    mempool_frontend_image: str = "mempool/frontend:v3.3.1"
    mariadb_image: str = "mariadb:10.5.21"
    # The dashboard image is built from this repo; these pin the build base and
    # the read-only Docker-API proxy the dashboard reads live metrics through.
    web_python_image: str = "python:3.12-slim"
    socket_proxy_image: str = "tecnativa/docker-socket-proxy:0.3.0"
    # Image for the auto-reset controller (needs the docker CLI + compose plugin;
    # it drives `docker compose down -v/up -d` on the mined networks via the
    # mounted Docker socket). Only used when a network has reset enabled.
    reset_controller_image: str = "docker:27-cli"
    # Default faucet approval function for networks that don't pin their own
    # (see argus.faucet.approval). Must name a registered function.
    faucet_default_approval: str = "max_one_btc"
    resources: ResourcesCfg = Field(default_factory=ResourcesCfg)
    tor: TorCfg = Field(default_factory=TorCfg)

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        if not _is_valid_host(v):
            raise ValueError(f"invalid hostname or IP: {v!r}")
        return v

    @field_validator("faucet_default_approval")
    @classmethod
    def _check_faucet_default(cls, v: str) -> str:
        from .faucet.approval import is_registered, names

        if not is_registered(v):
            raise ValueError(
                f"global.faucet_default_approval {v!r} is not a known function "
                f"(known: {names()})"
            )
        return v


class BitcoindCfg(_Base):
    extra_args: list[str] = Field(default_factory=list)
    # Publish the node's P2P port to the internet (and open it in the firewall) so
    # peers can connect their own node to this chain. Default on. Set false to bind
    # it to the host loopback only (operator-only) — e.g. to stop strangers from
    # connecting to / reorging a trivial-difficulty regtest chain.
    p2p_public: bool = True


def _check_alias(v: str) -> str:
    """Validate an LND alias: <=32 bytes (BOLT #7 limit), no control chars."""
    if "\n" in v or "\r" in v:
        raise ValueError("lnd alias must not contain newlines")
    if len(v.encode("utf-8")) > 32:
        raise ValueError(f"lnd alias {v!r} exceeds 32 bytes (Lightning gossip limit)")
    return v


_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _check_color(v: str) -> str:
    if not _COLOR_RE.match(v):
        raise ValueError(f"lnd color {v!r} must be a hex color like #rrggbb")
    return v


class LndSecondaryCfg(_Base):
    """The second LND node (``argus2``) — the ring's middle hop.

    ``enabled`` is tri-state: None => on wherever the liquidity ring is on (see
    :meth:`NetworkCfg.lnd_channels_enabled`), off if explicitly disabled.
    """

    enabled: bool | None = None
    name: str = "argus2"  # gossip alias (<=32 bytes)
    color: str = "#ff9900"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _check_alias(v)

    @field_validator("color")
    @classmethod
    def _check_col(cls, v: str) -> str:
        return _check_color(v)


class LndTertiaryCfg(_Base):
    """The third LND node (``argus3``) — the hop that closes the liquidity ring.

    A triangle (argus1->argus2->argus3->argus1) is the smallest graph in which a
    node can restore its own inbound/outbound balance purely off-chain (circular
    rebalancing), with no swap provider — so it works on every testnet. ``enabled``
    is tri-state like the secondary (None => on wherever the ring is on).
    """

    enabled: bool | None = None
    name: str = "argus3"  # gossip alias (<=32 bytes)
    color: str = "#33cc66"

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        return _check_alias(v)

    @field_validator("color")
    @classmethod
    def _check_col(cls, v: str) -> str:
        return _check_color(v)


class LndRebalancerCfg(_Base):
    """Periodic off-chain circular rebalancing to keep ring channels near 50/50.

    A long-running sidecar checks each ring channel every ``interval_seconds``; a
    channel whose local share leaves the ``low_ratio``..``high_ratio`` band is
    nudged back toward 0.5 with a circular self-payment routed around the ring
    (``lncli payinvoice --outgoing_chan_id .. --last_hop ..``), capped at
    ``max_fee_sat``. ``enabled`` is tri-state (None => on wherever the ring is on).
    Only meaningful with the ring (``lnd.channels``).
    """

    enabled: bool | None = None
    interval_seconds: int = Field(default=300, ge=30)
    low_ratio: float = Field(default=0.35, gt=0.0, lt=1.0)
    high_ratio: float = Field(default=0.65, gt=0.0, lt=1.0)
    # Circular-rebalance fees are paid between the operator's OWN ring nodes, so a
    # generous cap just lets rebalances succeed; it isn't a real cost. Sized to
    # clear a half-channel move at LND's default ~1 ppm over two hops.
    max_fee_sat: int = Field(default=5000, ge=0)

    @model_validator(mode="after")
    def _check_band(self) -> "LndRebalancerCfg":
        if self.low_ratio >= self.high_ratio:
            raise ValueError(
                f"lnd.channels.rebalancer.low_ratio ({self.low_ratio}) must be "
                f"< high_ratio ({self.high_ratio})"
            )
        return self


class LndChannelsCfg(_Base):
    """The liquidity ring: fund the three LND nodes and wire them into a channel
    ring at startup (argus1->argus2->argus3->argus1).

    Each node opens one single-funded ``channel_btc`` channel to the next hop, then
    a one-shot circular rebalance brings every channel to ~50/50 so both directions
    are live from the start. ``funding`` selects how the nodes get their on-chain
    coins:

    * ``auto`` — mine + fund from the miner/signer wallet (only on networks Argus
      mines: regtest/custom signets).
    * ``external`` — wait for coins sent to each node's on-chain address (e.g. a
      public faucet); the operator dashboard surfaces the addresses to fund.

    ``funding`` is tri-state: None => ``auto`` on a mineable network with the miner
    on, else ``external``. ``enabled`` is tri-state (None => on for every enabled
    network). Each node is funded with ``fund_btc`` on-chain.
    """

    enabled: bool | None = None
    funding: Literal["auto", "external"] | None = None
    fund_btc: float = Field(default=25.0, gt=0)
    channel_btc: float = Field(default=10.0, gt=0)
    rebalancer: LndRebalancerCfg = Field(default_factory=LndRebalancerCfg)


class LndCfg(_Base):
    # Node naming/branding. ``name`` is the gossip alias; None => "argus1" on
    # mined networks (paired with the secondary "argus2"), else "argus-<net>".
    name: str | None = None
    color: str = "#3399ff"  # LND's default node color
    extra_args: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)
    auto_compact: bool = True  # bbolt auto-compact + canceled-invoice GC (hygiene)
    # Discovery/channel-friendliness knobs (make it easy for peers to open to us):
    advertise_external_ip: bool = True  # set externalip=<hostname>:<lnd_p2p_port>
    wumbo: bool = False  # allow/accept channels > ~0.167 BTC (auto-on if channels need it)
    min_chan_size: int | None = Field(default=None, ge=1)  # sats; lower for tiny channels
    secondary: LndSecondaryCfg = Field(default_factory=LndSecondaryCfg)
    tertiary: LndTertiaryCfg = Field(default_factory=LndTertiaryCfg)
    channels: LndChannelsCfg = Field(default_factory=LndChannelsCfg)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str | None) -> str | None:
        return v if v is None else _check_alias(v)

    @field_validator("color")
    @classmethod
    def _check_col(cls, v: str) -> str:
        return _check_color(v)


class CashuCfg(_Base):
    enabled: bool = True
    ssl: bool = True
    # Deploy a co-located cashu.me web wallet alongside the mint, pointed at it.
    # The wallet is a static PWA (built from source, see global.cashu_wallet_ref)
    # served per network so each network's wallet state stays isolated by origin.
    wallet: bool = True
    extra_env: dict[str, str] = Field(default_factory=dict)


class FedimintGatewayCfg(_Base):
    """The Lightning gateway(s) bridging this federation to the LND ring.

    One ``gatewayd`` is paired with each ring LND node (gateway *i* -> argus*i*),
    so the gateway count equals ``fedimint.guardians`` and the federation's
    Lightning traffic rides the triangle's self-rebalancing channels — no new
    liquidity machinery is needed, which is what makes this work on networks where
    Argus can't mine coins. ``float_btc`` is the ecash balance pegged into the
    federation for each gateway at setup: it is analogous to inbound liquidity (the
    gateway pre-holds ecash so it can credit recipients on incoming Lightning
    payments). It is funded from the same wallet as the ring — mined on
    auto-funded networks, an operator/faucet deposit on external-funded ones.
    """

    float_btc: float = Field(default=0.5, gt=0)
    extra_env: dict[str, str] = Field(default_factory=dict)


class FedimintCfg(_Base):
    """A Fedimint federation (Chaumian ecash with M-of-N guardian custody) plus a
    Lightning gateway per ring node, deployed *alongside* — not instead of — this
    network's Cashu mint.

    Guardians (``fedimintd``) custody on-chain BTC in a threshold multisig and
    issue ecash backed 1:1. They track the chain through this network's bitcoind,
    which all guardians share: bitcoind is only a blockchain view + broadcaster and
    never holds keys (each guardian holds its own key share), so sharing it on a
    single host is safe. The federation is created by an automated, non-interactive
    DKG ceremony on first deploy (see :mod:`argus.builders.fedimint`).

    ``guardians`` is 1..FEDIMINT_MAX_GUARDIANS; one Lightning gateway is paired
    with each of the ring's LND nodes, so it also caps the gateway count at the
    number of ring nodes that are enabled.

    On by default on every supported network. On a network whose chain Fedimint
    cannot run (none today — see ``constants.FEDIMINT_SUPPORTED_CHAINS``) it is
    auto-disabled with a warning rather than generating a stack fedimintd would
    reject at runtime; resolve effective state with
    :meth:`NetworkCfg.fedimint_enabled`, never read ``enabled`` directly.
    """

    enabled: bool = True
    guardians: int = Field(default=1, ge=1, le=FEDIMINT_MAX_GUARDIANS)
    # Federation display name; None => "Argus <net>".
    federation_name: str | None = None
    gateway: FedimintGatewayCfg = Field(default_factory=FedimintGatewayCfg)
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("federation_name")
    @classmethod
    def _check_fed_name(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Passed to fedimint-cli at DKG; reject control chars/quotes that could
        # break the generated setup command, and bound the length.
        if any(c in v for c in "\n\r'\"`$\\") or not v.strip():
            raise ValueError(
                "fedimint.federation_name must be non-empty and free of "
                "quotes/control chars"
            )
        if len(v.encode("utf-8")) > 100:
            raise ValueError("fedimint.federation_name must be <= 100 bytes")
        return v


class ArkChannelCfg(_Base):
    """The single Lightning channel the Ark bridge (CLN) opens into the ring.

    ``target_node`` names which ring LND node the bridge peers with and funds a
    channel to (default ``argus1`` — the node Cashu/Fedimint also use, so Ark
    traffic shares the triangle's self-rebalancing liquidity). ``channel_btc`` is
    the channel size; the operator funds the bridge's on-chain wallet with at
    least this much (plus a little for fees) at the address the setup sidecar
    prints, then the channel opens automatically.
    """

    target_node: Literal["argus1", "argus2", "argus3"] = "argus1"
    channel_btc: float = Field(default=0.1, gt=0)


class ArkCfg(_Base):
    """An Ark ASP (Ark Service Provider): Second's ``captaind`` server plus a Core
    Lightning bridge node, deployed *alongside* this network's LND liquidity ring.

    captaind runs the Ark protocol (cheap, self-custodial off-chain VTXOs) and its
    own on-chain wallet seeds rounds; the bundled PostgreSQL is part of its image.
    The CLN bridge (``cln`` + the Boltz hold-invoice plugin) bridges Ark to
    Lightning and opens ONE channel into the ring (see :class:`ArkChannelCfg`), so
    Ark<->Lightning payments route through the triangle — no separate liquidity
    machinery. Both wallets are funded by the operator (external funding): captaind
    and CLN each print a deposit address at setup. Ark creates no coins, so on a
    network Argus can't mine the operator/faucet seeds those addresses once.

    Off by default (opt in with ``ark.enabled: true``): it is an alpha stack, is
    heavier than the other sub-tools (captaind + Postgres + a CLN node), and
    requires Bitcoin Core >= 29 (see the guard in ``_validate_semantics``). When
    enabled on a network whose chain Ark cannot run (none today — see
    ``constants.ARK_SUPPORTED_CHAINS``) it is auto-disabled with a warning rather
    than generating a stack captaind/CLN would reject; resolve effective state with
    :meth:`NetworkCfg.ark_enabled`, never read ``enabled`` directly.
    """

    enabled: bool = False
    # The CLN bridge node's gossip alias (<=32 bytes, like an LND alias).
    cln_alias: str = "argus-ark"
    channel: ArkChannelCfg = Field(default_factory=ArkChannelCfg)
    # Extra environment for the CLN bridge container (e.g. CLN_LOG_LEVEL).
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("cln_alias")
    @classmethod
    def _check_alias(cls, v: str) -> str:
        return _check_alias(v)


class BitcartPorts(_Base):
    """Optional explicit public ports for Bitcart's frontends/daemon."""

    store: int | None = None
    admin: int | None = None
    api: int | None = None
    daemon: int | None = None


class BitcartLiquidityCfg(_Base):
    """Liquidity-helper plugin options (LIQUIDITYHELPER_* in the installer)."""

    disabled: bool = False  # LIQUIDITYHELPER_LIQUIDITY_DISABLED (False = on)
    automatic_channel_creation: bool = True  # …_AUTOMATIC_CHANNEL_CREATION_ENABLED
    cashout_lightning_address: str | None = None  # CASHOUT_LIGHTNING_ADDRESS (required)
    # Referral/hosting fee, additive on top of the dev fee (0.0 = off). Its
    # destination is auto-supplied as the referral LNURL address when web.lnurl
    # is on (LIQUIDITYHELPER_REFERRAL_FEE_DEST); raise this above 0 to activate.
    referral_fee_amount: float = 0.0  # LIQUIDITYHELPER_REFERRAL_FEE_AMOUNT
    log_level: str = "INFO"  # LIQUIDITYHELPER_LOG_LEVEL

    @field_validator("referral_fee_amount")
    @classmethod
    def _check_referral_fee(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError(
                f"bitcart.liquidity.referral_fee_amount {v} must be in 0.0..1.0"
            )
        return v


class BitcartSmtpCfg(_Base):
    """Installation-wide SMTP (BITCART_SMTP_*); password lives in the secret store."""

    server: str | None = None
    port: int = 587
    tls: bool = True  # STARTTLS
    ssl: bool = False  # implicit SSL/TLS
    username: str | None = None
    from_email: str | None = None


class BitcartCfg(_Base):
    enabled: bool = True
    ssl: bool = True
    branch: str = "main"  # BRANCH (main | testing) — BareBits fork channel
    admin_email: str | None = None  # BITCART_ADMIN_EMAIL (required when enabled)
    btclnd_debug: bool = False  # BTCLND_DEBUG
    btclnd_p2p_pool_size: int = Field(default=5, ge=1, le=50)  # LND wallets per inst.
    liquidity: BitcartLiquidityCfg = Field(default_factory=BitcartLiquidityCfg)
    smtp: BitcartSmtpCfg = Field(default_factory=BitcartSmtpCfg)
    extra_env: dict[str, str] = Field(default_factory=dict)
    ports: BitcartPorts = Field(default_factory=BitcartPorts)


class CashuPayServerCfg(_Base):
    """CashuPayServer (BareBits Lite): a PHP, BTCPay-compatible payment gateway
    backed by this network's Cashu mint.

    No published image exists, so Argus builds it from source (see
    :mod:`argus.cashupayserver`, pinned by ``global.cashupayserver_ref``) and
    provisions it non-interactively with a generated PHP seed script that uses the
    app's own classes (admin password, a store wired to the in-network mint, a
    Greenfield API key for WooCommerce). Submarine swaps are disabled by default:
    a self-contained testnet settles in ecash, not via an on-chain swap provider.
    """

    enabled: bool = True
    ssl: bool = True
    # Admin login email. Falls back to ``bitcart.admin_email`` when unset (see
    # NetworkCfg.cashupayserver_admin_email), so a typical config sets it once.
    admin_email: str | None = None
    # The site-wide submarine-swap master switch. Off by design; flip to enable.
    submarine_swaps: bool = False
    extra_env: dict[str, str] = Field(default_factory=dict)


class WooCommerceCfg(_Base):
    """A minimal WordPress + WooCommerce storefront selling the demo trading
    cards through the BTCPay-for-WooCommerce plugin pointed at this network's
    CashuPayServer.

    Guest checkout is on and user registration is off; unused WordPress/WooCommerce
    features are stripped to keep the footprint small. Requires CashuPayServer on
    the same network (the plugin points at it). Uses a dedicated, memory-tuned
    MariaDB. The store currency is BTC; the cards (reused from
    :mod:`argus.bitcart_cards`) are priced by converting their sats price to BTC.
    """

    enabled: bool = True
    ssl: bool = True
    # Admin login email; falls back to ``bitcart.admin_email`` when unset.
    admin_email: str | None = None
    admin_user: str = "argus-admin"  # WordPress admin username (not "admin")
    store_name: str = "Argus Trading Cards"
    extra_env: dict[str, str] = Field(default_factory=dict)

    @field_validator("admin_user")
    @classmethod
    def _check_admin_user(cls, v: str) -> str:
        # Used as the WP login and passed to WP-CLI; keep it shell/DNS-safe.
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{2,30}", v):
            raise ValueError(
                "woocommerce.admin_user must be 3-31 chars: lowercase "
                "alphanumeric, hyphen or underscore (and not start with -/_)"
            )
        return v

    @field_validator("store_name")
    @classmethod
    def _check_store_name(cls, v: str) -> str:
        # Passed to WP-CLI as the blog title; reject control chars / quotes that
        # could break the generated provisioning command.
        if any(c in v for c in "\n\r'\"`$\\") or not v.strip():
            raise ValueError(
                "woocommerce.store_name must be non-empty and free of "
                "quotes/control chars"
            )
        return v


class IndexerCfg(_Base):
    """A Fulcrum instance. Multiple are allowed per network."""

    name: str = "fulcrum-1"
    enabled: bool = True
    ssl: bool = True

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        # Used to build container names — keep it shell/DNS-safe.
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", v):
            raise ValueError(
                f"indexer name {v!r} must be lowercase alphanumeric/hyphen (<=31 chars)"
            )
        return v


class MempoolCfg(_Base):
    # None => fall back to the network's default (regtest/custom-signet/mutinynet on).
    enabled: bool | None = None
    ssl: bool = True
    # Historical fee/mempool graphs. On by default; it's the biggest MariaDB
    # grower, so set False on disk-constrained hosts.
    statistics: bool = True
    # Run mempool's Lightning indexer against the local primary LND node, so the
    # explorer's /lightning pages (and the dashboard's node links) are populated.
    lightning: bool = True


class MinerCfg(_Base):
    """Regtest / custom-signet block production."""

    enabled: bool = True
    block_interval_seconds: int = Field(default=60, ge=1)
    initial_blocks: int = Field(default=101, ge=0)  # coinbase maturity on regtest


class ResetCfg(_Base):
    """Auto-reset a mined network when its chain outgrows a size cap.

    Only meaningful on the networks Argus mines (regtest and the two custom
    signets) — see ``constants.RESET_NETWORKS``. When the network's bitcoind
    ``size_on_disk`` reaches the effective cap, the whole installation for that
    network is torn down (``docker compose down -v``) and re-deployed to its base
    config — wiping all coins, Lightning channels, transactions,
    mempool/Fulcrum/Cashu state, and Bitcart. ``enabled`` is tri-state: None => on
    for the mined networks, off elsewhere. (A custom signet keeps its
    challenge/signing key, so it resets to genesis as the *same* signet.)

    ``max_size_gb`` is tri-state too: None => the network's default cap (the
    standard ``DEFAULT_RESET_MAX_SIZE_GB`` for regtest/short-lived signet, the
    larger ``DEFAULT_LONG_RESET_MAX_SIZE_GB`` for the long-lived signet); an
    explicit value always wins. Resolve it with
    :meth:`NetworkCfg.reset_max_size_gb`, never read this field directly.
    """

    enabled: bool | None = None
    max_size_gb: float | None = Field(default=None, gt=0)
    check_interval_seconds: int = Field(default=DEFAULT_RESET_CHECK_INTERVAL, ge=1)


class PowCfg(_Base):
    """Proof-of-work that lets a visitor *earn* faucet claims beyond the one free
    per-IP-per-day claim (see :mod:`argus.faucet.pow`).

    The browser solves a server-issued, request-bound challenge — find a nonce so
    that ``H(challenge || nonce) < target`` — and submits the solution; the server
    re-verifies in one hash. A valid proof overrides the one-claim-per-day limit,
    so a determined visitor can keep claiming at the cost of (deliberately) more
    work than minting the coins any other way.

    Difficulty is expressed as a wall-clock **target in seconds on a reference
    machine**, then converted to a 256-bit hash target via the reference
    hashrate. Three regimes (see :func:`argus.faucet.pow.compute_target`):

    * **flat** — ``seconds_per_100k`` of work per 100k sats requested (most nets);
    * **value-pegged** — for real-value testnets (testnet3): pegged to *2x the
      cheapest cost to actually mine the requested amount* (the testnet3 20-minute
      difficulty-1 rule), capped at ``value_cap_seconds`` and limited to
      ``max_per_day`` claims. Needs the network's self-hosted mempool explorer for
      the current block subsidy; if that is unavailable, PoW is disabled there;
    * **adaptive factors** — applied on top of the flat base on non-value nets:
      a balance anchor (harder as the faucet drains) and a demand retarget
      (harder under spam), each bounded, so ASIC/GPU spammers hit diminishing
      returns.
    """

    enabled: bool = True
    # Hash primitive. ``yespower`` (CPU memory-hard, ASIC/GPU-resistant) is the
    # production default, run identically in the browser (WASM) and on the server
    # (via wasmtime). ``sha256d`` is a pure-Python/JS fallback for tests and
    # low-security setups (NOT ASIC-resistant).
    algorithm: Literal["yespower", "sha256d"] = "yespower"

    # -- flat base ---------------------------------------------------------
    # Target wall-clock seconds of PoW per 100k sats requested, on the reference
    # machine. 10 minutes/100k sats by default.
    seconds_per_100k: float = Field(default=600.0, gt=0)
    # Overall clamp on the final per-request target (seconds), after all scaling.
    min_seconds: float = Field(default=1.0, ge=0)
    max_seconds: float = Field(default=3600.0, gt=0)
    # Challenge time-to-live (seconds). The effective TTL is widened to at least
    # twice the target so a client always has time to finish a hard challenge.
    ttl_seconds: int = Field(default=1800, ge=30)

    # -- value-pegged (testnet3-style) -------------------------------------
    # None => auto: True for real-value testnets (testnet3), else False.
    value_pegged: bool | None = None
    value_safety_factor: float = Field(default=2.0, gt=0)
    value_cap_seconds: float = Field(default=1800.0, gt=0)  # ~30 min ceiling
    # Max PoW-earned claims per IP per UTC day. None => auto: 1 for value-pegged
    # nets, 0 (unlimited) otherwise.
    max_per_day: int | None = Field(default=None, ge=0)

    # -- adaptive factors (non-value nets) ---------------------------------
    balance_anchor: bool = True
    # At or above this confirmed balance the anchor adds nothing (factor 1.0);
    # as the balance falls to zero the factor rises linearly to balance_max_mult.
    balance_full_sat: int = Field(default=100_000_000, ge=1)
    balance_max_mult: float = Field(default=8.0, ge=1)
    demand_retarget: bool = True
    # Today's successful-claim count at which the demand factor reaches its max.
    demand_target_per_day: int = Field(default=50, ge=1)
    demand_max_mult: float = Field(default=16.0, ge=1)

    # -- calibration (reference-machine hashrates, H/s) --------------------
    # Assumed throughput of the PoW primitives on a consumer laptop, used to turn
    # a "seconds of work" target into a hash target. reference_yespower_hps must
    # be measured against the CI-built WASM (done at deploy time).
    reference_yespower_hps: float = Field(default=1500.0, gt=0)
    reference_sha256d_hps: float = Field(default=15_000_000.0, gt=0)

    @model_validator(mode="after")
    def _check_clamp(self) -> "PowCfg":
        if self.min_seconds > self.max_seconds:
            raise ValueError(
                "faucet.pow.min_seconds must not exceed max_seconds"
            )
        return self


class FaucetCfg(_Base):
    """Per-network faucet: dispenses on-chain testnet coins from LND node #1.

    Runs as a separate, isolated container from the dashboard (so a faucet bug
    can't take the main page down) and is reachable at ``/<net>/faucet`` on the
    site root. A single named approval function (see
    :mod:`argus.faucet.approval`) decides whether to dispense; ``None`` falls back
    to ``global.faucet_default_approval``. If the network is reset, the faucet's
    recorded payouts for it are wiped too (the per-network ``reset.sh`` calls
    ``argus.faucet.reset``).
    """

    enabled: bool = True
    # Name of the approval function. None => global.faucet_default_approval.
    approval_function: str | None = None
    # Fee rate for dispensing transactions (sat/vByte). Testnet fees are trivial.
    fee_sat_per_vbyte: int = Field(default=2, ge=1)
    # How many recent payouts to list on the faucet page.
    recent_limit: int = Field(default=50, ge=1, le=1000)

    # -- speed-limit rules (see argus.faucet.rules) ------------------------
    # Independent validation rules, combined with AND on every request: a payout
    # needs ALL enabled rules to pass, and the page reports every rule that failed
    # (with a "try again in X" for time-based ones). The amount policy (< 1 BTC by
    # default, see ``approval_function``) always runs in addition to these.

    # One successful withdrawal per client IP per rolling 24h (per network). A
    # salted hash of the IP + the last-withdrawal time is kept in the faucet DB.
    one_per_ip_per_day: bool = True
    # Cap a request at the per-day maximum = faucet balance / expected withdrawals
    # over the next 365 days (the trailing-year daily average; missing days assume
    # max(busiest day ever, 10)). Shown on the page in BTC + sats.
    max_amount_per_day: bool = True
    # Cap any single request at a fraction of the faucet's CURRENT balance, so one
    # request can't drain it even before the yearly-averaged cap bites.
    per_request_balance_cap: bool = True
    balance_cap_fraction: float = Field(default=0.10, gt=0, le=1)
    # Reject dust requests below a floor (anti-spam).
    min_claim_enabled: bool = True
    min_claim_sat: int = Field(default=5000, ge=1)

    # Proof-of-work: earn extra claims beyond the one free per-IP-per-day claim.
    pow: PowCfg = Field(default_factory=PowCfg)

    @field_validator("approval_function")
    @classmethod
    def _check_approval(cls, v: str | None) -> str | None:
        if v is None:
            return v
        # Imported lazily to keep the faucet out of the config import cycle.
        from .faucet.approval import is_registered, names

        if not is_registered(v):
            raise ValueError(
                f"faucet.approval_function {v!r} is not a known function "
                f"(known: {names()})"
            )
        return v


class NetworkCfg(_Base):
    enabled: bool = False
    prune: int = Field(default=0, ge=0)  # MiB; 0 = no pruning
    signet_challenge: str | None = None
    addnode: list[str] = Field(default_factory=list)

    bitcoind: BitcoindCfg = Field(default_factory=BitcoindCfg)
    lnd: LndCfg = Field(default_factory=LndCfg)
    cashu: CashuCfg = Field(default_factory=CashuCfg)
    fedimint: FedimintCfg = Field(default_factory=FedimintCfg)
    ark: ArkCfg = Field(default_factory=ArkCfg)
    bitcart: BitcartCfg = Field(default_factory=BitcartCfg)
    cashupayserver: CashuPayServerCfg = Field(default_factory=CashuPayServerCfg)
    woocommerce: WooCommerceCfg = Field(default_factory=WooCommerceCfg)
    indexers: list[IndexerCfg] = Field(default_factory=lambda: [IndexerCfg()])
    mempool: MempoolCfg = Field(default_factory=MempoolCfg)
    miner: MinerCfg = Field(default_factory=MinerCfg)
    reset: ResetCfg = Field(default_factory=ResetCfg)
    faucet: FaucetCfg = Field(default_factory=FaucetCfg)
    resources: ResourcesCfg = Field(default_factory=ResourcesCfg)

    # Optional host-port overrides keyed by the names in constants.PORT_OFFSETS
    # (or "fulcrum_<i>_electrum_tcp" etc.).
    ports: dict[str, int] = Field(default_factory=dict)

    @field_validator("signet_challenge")
    @classmethod
    def _check_challenge(cls, v: str | None) -> str | None:
        # Treat an empty placeholder as "unset" (validated later only if enabled).
        if v is None or v == "":
            return None
        if not re.fullmatch(r"[0-9a-fA-F]+", v) or len(v) % 2 != 0:
            raise ValueError("signet_challenge must be an even-length hex string")
        return v.lower()

    @field_validator("ports")
    @classmethod
    def _check_ports(cls, v: dict[str, int]) -> dict[str, int]:
        for name, port in v.items():
            if not (1 <= port <= 65535):
                raise ValueError(f"port override {name}={port} out of range 1-65535")
        return v

    def enabled_indexers(self) -> list[IndexerCfg]:
        return [ix for ix in self.indexers if ix.enabled]

    def cashupayserver_admin_email(self) -> str | None:
        """CashuPayServer admin email: its own, else Bitcart's (shared default)."""
        return self.cashupayserver.admin_email or self.bitcart.admin_email

    def woocommerce_admin_email(self) -> str | None:
        """WooCommerce admin email: its own, else Bitcart's (shared default)."""
        return self.woocommerce.admin_email or self.bitcart.admin_email

    def mempool_enabled(self, spec: NetworkSpec) -> bool:
        if self.mempool.enabled is None:
            return spec.default_mempool
        return self.mempool.enabled

    def reset_enabled(self, net_key: str) -> bool:
        """Whether this network is auto-reset on a size cap.

        Tri-state: defaults on for the mined networks (regtest/custom-signet) and
        off everywhere else. An explicit value always wins (and an explicit True
        on a non-mined network is rejected in semantic validation)."""
        v = self.reset.enabled
        return (net_key in _MINEABLE_NETWORKS) if v is None else v

    def reset_max_size_gb(self, spec: NetworkSpec) -> float:
        """Effective auto-reset cap (GiB) for this network.

        Tri-state: an explicit ``reset.max_size_gb`` always wins; otherwise the
        network's own default (``spec.default_reset_max_size_gb`` — larger for the
        long-lived signet), falling back to the standard
        ``DEFAULT_RESET_MAX_SIZE_GB``. Always returns a positive float, so callers
        (reset controller, dashboard countdown) never see ``None``.
        """
        if self.reset.max_size_gb is not None:
            return self.reset.max_size_gb
        if spec.default_reset_max_size_gb is not None:
            return spec.default_reset_max_size_gb
        return DEFAULT_RESET_MAX_SIZE_GB

    def lnd_channels_enabled(self, spec: NetworkSpec) -> bool:
        """Whether the liquidity ring (fund + open the 3-node channel ring) runs.

        Defaults on for every enabled network: on mineable nets it self-funds by
        mining; elsewhere it funds externally (the operator sends coins to each
        node's address). An explicit value always wins (and is validated).
        """
        v = self.lnd.channels.enabled
        return True if v is None else v

    def lnd_ring_enabled(self, spec: NetworkSpec) -> bool:
        """Alias for :meth:`lnd_channels_enabled` — the ring *is* the channels."""
        return self.lnd_channels_enabled(spec)

    def lnd_secondary_enabled(self, spec: NetworkSpec) -> bool:
        """Whether the second LND node (``argus2``) is deployed.

        The ring needs all three nodes, so this defaults to whatever the ring does;
        it can also be enabled standalone (two idle nodes, no auto-channels). An
        explicit value always wins.
        """
        v = self.lnd.secondary.enabled
        return self.lnd_channels_enabled(spec) if v is None else v

    def lnd_tertiary_enabled(self, spec: NetworkSpec) -> bool:
        """Whether the third LND node (``argus3``) is deployed (closes the ring).

        Defaults to whatever the ring does. An explicit value always wins.
        """
        v = self.lnd.tertiary.enabled
        return self.lnd_channels_enabled(spec) if v is None else v

    def lnd_funding_mode(self, spec: NetworkSpec) -> str:
        """How the ring nodes get on-chain coins: ``auto`` (mine) or ``external``.

        Tri-state: None => ``auto`` on a mineable network with the miner on, else
        ``external``. An explicit value always wins (and is validated)."""
        v = self.lnd.channels.funding
        if v is not None:
            return v
        return "auto" if (spec.supports_miner and self.miner.enabled) else "external"

    def lnd_rebalancer_enabled(self, spec: NetworkSpec) -> bool:
        """Whether the periodic circular-rebalancer sidecar runs.

        Only meaningful with the ring; defaults to whatever the ring does. An
        explicit value always wins (and is validated)."""
        v = self.lnd.channels.rebalancer.enabled
        return self.lnd_channels_enabled(spec) if v is None else v

    def fedimint_supported(self, spec: NetworkSpec) -> bool:
        """Whether Fedimint can run on this network's chain.

        fedimintd accepts a fixed set of ``FM_BITCOIN_NETWORK`` values; every
        current Argus chain maps to one (the custom signets/mutinynet all run as
        ``signet``), so this is True for all networks today. It is the capability
        guard for any future chain Fedimint can't express."""
        return spec.chain in FEDIMINT_SUPPORTED_CHAINS

    def fedimint_enabled(self, spec: NetworkSpec) -> bool:
        """Effective Fedimint state: requested AND supported on this chain.

        Auto-disables (rather than erroring) where the chain is unsupported, so a
        broad ``fedimint.enabled: true`` never generates a stack fedimintd would
        reject; the unsupported case is surfaced as a generation-time warning."""
        return self.fedimint.enabled and self.fedimint_supported(spec)

    def fedimint_available_ring_nodes(self, spec: NetworkSpec) -> int:
        """How many ring LND nodes exist to pair gateways with (node1 + 2 + 3).

        Node 1 (``lnd``) is always present; nodes 2/3 follow the secondary/tertiary
        toggles. Caps the number of guardians (each pairs a gateway to one node)."""
        return (
            1
            + int(self.lnd_secondary_enabled(spec))
            + int(self.lnd_tertiary_enabled(spec))
        )

    def fedimint_guardian_count(self, spec: NetworkSpec) -> int:
        """Number of guardians (= gateways) to deploy, or 0 when Fedimint is off."""
        return self.fedimint.guardians if self.fedimint_enabled(spec) else 0

    def fedimint_federation_name(self, net_key: str) -> str:
        """The federation's display name: explicit, else ``Argus <net>``."""
        return self.fedimint.federation_name or f"Argus {net_key}"

    def ark_supported(self, spec: NetworkSpec) -> bool:
        """Whether the Ark ASP can run on this network's chain.

        captaind (a rust-bitcoin ``Network``) and Core Lightning both accept a
        fixed set of network names; every current Argus chain maps to one (the
        custom signets/mutinynet all run as ``signet``), so this is True for all
        networks today. It is the capability guard for any future chain Ark can't
        express."""
        return spec.chain in ARK_SUPPORTED_CHAINS

    def ark_enabled(self, spec: NetworkSpec) -> bool:
        """Effective Ark state: requested AND supported on this chain.

        Auto-disables (rather than erroring) where the chain is unsupported, so a
        broad ``ark.enabled: true`` never generates a stack captaind/CLN would
        reject; the unsupported case is surfaced as a generation-time warning."""
        return self.ark.enabled and self.ark_supported(spec)

    def ark_channel_target(self, spec: NetworkSpec) -> tuple[str, str, str]:
        """Resolve the Ark bridge's channel target to (alias, lnd_service, volume).

        The alias is the configured ``argus1/2/3``; the service/volume are that
        ring node's LND container name and data volume (the channel sidecar reads
        the target's identity pubkey from the volume's argus_liquidity.json)."""
        alias = self.ark.channel.target_node
        service, volume = ARK_RING_NODES[alias]
        return alias, service, volume

    def ark_target_enabled(self, spec: NetworkSpec) -> bool:
        """Whether the Ark bridge's chosen ring node is actually deployed.

        argus1 (``lnd``) is always present; argus2/argus3 follow the
        secondary/tertiary toggles. The channel can't open to a node that isn't
        there, so this gates validation."""
        alias, _service, _volume = self.ark_channel_target(spec)
        if alias == "argus2":
            return self.lnd_secondary_enabled(spec)
        if alias == "argus3":
            return self.lnd_tertiary_enabled(spec)
        return True  # argus1 is always deployed

    def bitcoind_p2p_gated(self, net_key: str, spec: NetworkSpec) -> bool:
        """Whether bitcoind self-gates its P2P listener (regtest auto-channels):
        it keeps inbound P2P closed until LND channel setup completes, then
        restarts with P2P open — so funding can't be reorged during setup.

        Only regtest needs this — on a custom signet outsiders can't produce valid
        blocks without our signing key, so early P2P exposure can't be abused.
        """
        return (
            net_key == "regtest"
            and self.bitcoind.p2p_public
            and self.lnd_channels_enabled(spec)
        )

    def lnd_wumbo_enabled(self, spec: NetworkSpec) -> bool:
        """Effective wumbo: explicit, or forced on when a large auto-channel needs it."""
        if self.lnd.wumbo:
            return True
        if self.lnd_channels_enabled(spec):
            # Max non-wumbo channel is 16,777,215 sat (~0.167 BTC).
            return round(self.lnd.channels.channel_btc * 1e8) > 16_777_215
        return False


class WebLnurlCfg(_Base):
    """LNURL-pay / Lightning Address support served by the dashboard.

    When enabled, the dashboard answers the LUD-06/LUD-16 endpoints under
    ``/.well-known/lnurlp/<name>`` and mints invoices on each network's *primary*
    LND node (node #1). Four purposes are served — ``fees``, ``cashout``,
    ``donate`` and ``referral`` — each as a bare address (backed by
    ``default_network``) and a per-network variant ``<purpose>-<net>@<hostname>``
    so a payer's wallet can target the matching chain. The cashout/fees/referral
    addresses are wired into each network's Bitcart liquidity-helper plugin (see
    :mod:`argus.bitcart`); ``donate`` is the public donation address shown on the
    dashboard. The four names are otherwise identical — they differ only in the
    invoice memo.
    """

    enabled: bool = True
    # Network backing the BARE addresses (fees@/cashout@/donate@/referral@).
    # None => the first enabled network in canonical order.
    default_network: str | None = None
    min_sat: int = 1
    max_sat: int = 5_000_000  # 0.05 BTC — generous for valueless testnet coins.
    # LUD-12 comment length advertised (0 disables comments). The liquidity plugin
    # sends a `storeid:<id>` attribution comment, so keep this > 0 to accept it.
    comment_length: int = 255

    @field_validator("min_sat", "max_sat")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("web.lnurl.min_sat / max_sat must be >= 1")
        return v

    @field_validator("comment_length")
    @classmethod
    def _comment_len(cls, v: int) -> int:
        if not (0 <= v <= 1000):
            raise ValueError("web.lnurl.comment_length must be in 0..1000")
        return v

    @model_validator(mode="after")
    def _check_bounds(self) -> "WebLnurlCfg":
        if self.max_sat < self.min_sat:
            raise ValueError(
                f"web.lnurl.max_sat ({self.max_sat}) must be >= min_sat "
                f"({self.min_sat})"
            )
        return self


class WebMetricsHistoryCfg(_Base):
    """Time-series history for the per-service resource graphs (``/stats``).

    When enabled, a small ``metrics-sampler`` sidecar (a sibling of the dashboard
    from the same image) records each service-bucket's CPU/RAM/disk/network every
    ``sample_interval_seconds`` into a SQLite store, retained in three tiers:
    raw samples for ``raw_retention_hours``, hourly rollups for
    ``hourly_retention_days``, daily rollups for ``daily_retention_days``. Disk is
    sampled on a slower ``disk_sample_interval_seconds`` cadence because reading it
    forces the Docker daemon to size every volume. Disable to drop the sidecar
    entirely (the live snapshot table is unaffected)."""

    enabled: bool = True
    sample_interval_seconds: int = 60
    disk_sample_interval_seconds: int = 900
    raw_retention_hours: int = 24
    hourly_retention_days: int = 3
    daily_retention_days: int = 365

    @field_validator(
        "sample_interval_seconds",
        "disk_sample_interval_seconds",
        "raw_retention_hours",
        "hourly_retention_days",
        "daily_retention_days",
    )
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("web.metrics_history intervals/retention must be >= 1")
        return v

    @field_validator("sample_interval_seconds")
    @classmethod
    def _sane_interval(cls, v: int) -> int:
        # A floor keeps the sampler from hammering the docker socket; an hour
        # ceiling keeps sub-hourly graphs meaningful (the finest tier is per-sample).
        if not (5 <= v <= 3600):
            raise ValueError(
                "web.metrics_history.sample_interval_seconds must be in 5..3600"
            )
        return v

    @model_validator(mode="after")
    def _check_disk_cadence(self) -> "WebMetricsHistoryCfg":
        # Disk needn't be sampled more often than the base interval (and usually
        # much less, since /system/df is expensive).
        if self.disk_sample_interval_seconds < self.sample_interval_seconds:
            raise ValueError(
                "web.metrics_history.disk_sample_interval_seconds must be >= "
                "sample_interval_seconds"
            )
        return self


class WebCfg(_Base):
    """The Argus dashboard: a host-global web server fronted by the shared Caddy.

    It serves the welcome/landing page and the testnet/ToS/privacy pages, and
    reports live per-service resource usage. Unlike the per-network services it
    spans every enabled network at once (like the shared Caddy layer).
    """

    enabled: bool = True
    ssl: bool = True
    # None => the bare hostname root (443 with ssl, 80 without). Override to serve
    # the dashboard on a dedicated public port instead of the site root.
    port: int | None = None
    default_theme: str = "hacker"
    # theme name -> CSS path served under the app's /static/ root.
    themes: dict[str, str] = Field(
        default_factory=lambda: {
            "hacker": "themes/hacker.css",
            "game": "themes/game.css",
            "bootstrap": "themes/bootstrap.css",
        }
    )
    # Footer "run your own testnet install" link.
    repo_url: str = "https://github.com/BareBits/bitcoin_argus"
    # Site operator shown in the footer ("Hosted by <name>") and the operator's
    # site it links to. Override these to re-brand the dashboard for your install.
    operator_name: str = "BareBits"
    operator_url: str = "https://getbarebits.com"
    # Address shown on the /contact page for testing feedback.
    contact_email: str = "sales@getbarebits.com"
    # LNURL-pay / Lightning Address support (fees@/cashout@/donate@/referral@).
    lnurl: WebLnurlCfg = Field(default_factory=WebLnurlCfg)
    # Time-series resource history + the /stats graphs (sampler sidecar).
    metrics_history: WebMetricsHistoryCfg = Field(
        default_factory=WebMetricsHistoryCfg
    )

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError(f"web.port {v} out of range 1-65535")
        return v

    @field_validator("contact_email")
    @classmethod
    def _check_contact_email(cls, v: str) -> str:
        # A light sanity check (not full RFC 5322): exactly one "@" with a local
        # part and a dotted domain, and no whitespace/control chars that would
        # break the mailto: link or allow header/markup injection.
        if any(c.isspace() for c in v) or "<" in v or ">" in v:
            raise ValueError(f"web.contact_email {v!r} contains invalid characters")
        local, sep, domain = v.partition("@")
        if not sep or not local or "@" in domain or "." not in domain:
            raise ValueError(f"web.contact_email {v!r} is not a valid e-mail address")
        return v

    @model_validator(mode="after")
    def _check_default_theme(self) -> "WebCfg":
        if not self.themes:
            raise ValueError("web.themes must define at least one theme")
        if self.default_theme not in self.themes:
            raise ValueError(
                f"web.default_theme {self.default_theme!r} is not one of the "
                f"defined themes ({sorted(self.themes)})"
            )
        return self


class ArgusConfig(_Base):
    global_: GlobalConfig = Field(alias="global")
    networks: dict[str, NetworkCfg]
    web: WebCfg = Field(default_factory=WebCfg)

    def enabled_networks(self) -> list[tuple[str, NetworkCfg]]:
        """(key, cfg) pairs for enabled networks, in canonical order."""
        from .constants import NETWORK_ORDER

        return [
            (k, self.networks[k])
            for k in NETWORK_ORDER
            if k in self.networks and self.networks[k].enabled
        ]

    def faucet_networks(self) -> list[tuple[str, NetworkCfg]]:
        """(key, cfg) pairs for enabled networks whose faucet is on, in order."""
        return [(k, n) for k, n in self.enabled_networks() if n.faucet.enabled]

    def faucet_approval_name(self, net_key: str) -> str:
        """The approval-function name a network's faucet uses (its own, else the
        global default)."""
        return (
            self.networks[net_key].faucet.approval_function
            or self.global_.faucet_default_approval
        )

    # -- semantic validation (cross-field) -----------------------------------

    def _validate_semantics(self) -> None:
        errors: list[str] = []

        for key in self.networks:
            if key not in NETWORK_SPECS:
                errors.append(
                    f"unknown network {key!r}; valid: {sorted(NETWORK_SPECS)}"
                )

        any_public_ssl = False
        for key, net in self.enabled_networks():
            spec = NETWORK_SPECS[key]

            # A custom signet needs a challenge; if the operator doesn't supply
            # one, Argus auto-generates a matched challenge + signing key into the
            # secret store, so no error is required here.

            # Networks needing 30s-block signet support need a special bitcoind.
            if spec.needs_knots and not self.global_.bitcoind_knots_image:
                errors.append(
                    f"[{key}] needs a signetblocktime-capable bitcoind; set "
                    f"global.bitcoind_knots_image (no public image exists — build "
                    f"one from MutinyWallet/mutiny-net or Bitcoin Knots)"
                )

            # prune is incompatible with the indexes we enable: txindex (Fulcrum/
            # mempool) and blockfilterindex (Bitcart's Neutrino LND via BIP157).
            uses_indexer = (
                bool(net.enabled_indexers())
                or net.mempool_enabled(spec)
                or net.bitcart.enabled
            )
            if net.prune > 0 and uses_indexer:
                errors.append(
                    f"[{key}] prune={net.prune} conflicts with the required indexes "
                    f"(Bitcoin Core cannot run -prune with -txindex/-blockfilterindex). "
                    f"Disable pruning, or the indexer/mempool/bitcart, for this network."
                )

            # Where mining isn't supported (testnet3/4, public signet, mutinynet)
            # the miner flag is simply a no-op (the registry won't include it).
            # regtest and the custom signets are the chains Argus can drive.
            if net.miner.enabled and spec.supports_miner and key not in _MINEABLE_NETWORKS:
                errors.append(
                    f"[{key}] automated mining is not implemented for this network"
                )

            # The liquidity ring needs all three LND nodes; it can self-fund by
            # mining (funding=auto, mineable nets) or wait for externally-sent coins
            # (funding=external, any net). Validate the funding mode against the net
            # and that the ring's three nodes are all present.
            secondary_on = net.lnd_secondary_enabled(spec)
            tertiary_on = net.lnd_tertiary_enabled(spec)
            channels_on = net.lnd_channels_enabled(spec)
            funding = net.lnd_funding_mode(spec)
            if channels_on:
                if not (secondary_on and tertiary_on):
                    errors.append(
                        f"[{key}] lnd.channels (the liquidity ring) needs all three "
                        f"nodes; enable lnd.secondary and lnd.tertiary, or disable "
                        f"lnd.channels"
                    )
                if funding == "auto" and not (spec.supports_miner and net.miner.enabled):
                    errors.append(
                        f"[{key}] lnd.channels.funding='auto' needs a network Argus "
                        f"mines with the miner enabled (it funds the ring by mining); "
                        f"use funding: external, or enable the miner"
                    )
                if net.lnd.channels.channel_btc > net.lnd.channels.fund_btc:
                    errors.append(
                        f"[{key}] lnd.channels.channel_btc "
                        f"({net.lnd.channels.channel_btc}) must be <= fund_btc "
                        f"({net.lnd.channels.fund_btc})"
                    )

            # Auto-reset is only meaningful where Argus drives block production
            # (it re-mines the base chain after the wipe). It defaults on there
            # and off elsewhere; reject an explicit enable on other networks.
            if net.reset.enabled and key not in _MINEABLE_NETWORKS:
                errors.append(
                    f"[{key}] reset (auto chain-size reset) is only supported on "
                    f"networks Argus mines (regtest and the custom signets)"
                )

            # Bitcart requires an admin email; an active liquidity helper needs
            # a cash-out Lightning address.
            if net.bitcart.enabled:
                if not net.bitcart.admin_email:
                    errors.append(f"[{key}] bitcart.admin_email is required")
                # The cash-out address can be supplied explicitly OR auto-wired
                # from the network's own ``cashout-<net>`` LNURL address — but the
                # latter only resolves for paying wallets over clearnet HTTPS, so
                # it counts as "supplied" only when SSL + the dashboard + LNURL are
                # all on.
                lnurl_supplies_cashout = (
                    self.global_.ssl_enabled
                    and self.web.enabled
                    and self.web.lnurl.enabled
                )
                if (
                    not net.bitcart.liquidity.disabled
                    and not net.bitcart.liquidity.cashout_lightning_address
                    and not lnurl_supplies_cashout
                ):
                    errors.append(
                        f"[{key}] bitcart.liquidity.cashout_lightning_address is "
                        f"required when liquidity is enabled (liquidity.disabled="
                        f"false) unless web.lnurl is on with ssl_enabled"
                    )
                # The referral/hosting fee's destination is the referral LNURL
                # address (there is no operator field for it), which only resolves
                # over clearnet HTTPS — so a non-zero rate needs LNURL + SSL.
                if (
                    net.bitcart.liquidity.referral_fee_amount > 0
                    and not lnurl_supplies_cashout
                ):
                    errors.append(
                        f"[{key}] bitcart.liquidity.referral_fee_amount > 0 needs "
                        f"web.lnurl on with ssl_enabled (it supplies the referral "
                        f"payout address)"
                    )

            # CashuPayServer settles payments against this network's Cashu mint,
            # so it requires the mint, and an admin email (its own or Bitcart's).
            if net.cashupayserver.enabled:
                if not net.cashu.enabled:
                    errors.append(
                        f"[{key}] cashupayserver.enabled requires the network's Cashu "
                        f"mint (cashu.enabled): it settles payments against it"
                    )
                if not net.cashupayserver_admin_email():
                    errors.append(
                        f"[{key}] cashupayserver.admin_email is required when "
                        f"CashuPayServer is enabled (or set bitcart.admin_email to "
                        f"share it)"
                    )

            # WooCommerce points its BTCPay plugin at this network's CashuPayServer,
            # so it requires CashuPayServer enabled here, plus an admin email.
            if net.woocommerce.enabled:
                if not net.cashupayserver.enabled:
                    errors.append(
                        f"[{key}] woocommerce.enabled requires cashupayserver.enabled "
                        f"on the same network (its BTCPay plugin points at it)"
                    )
                if not net.woocommerce_admin_email():
                    errors.append(
                        f"[{key}] woocommerce.admin_email is required when WooCommerce "
                        f"is enabled (or set bitcart.admin_email to share it)"
                    )

            # Fedimint: one Lightning gateway is paired with each ring LND node, so
            # the federation can have no more guardians than there are ring nodes to
            # host their gateways. (An unsupported chain is NOT an error — it is
            # auto-disabled with a generation-time warning; see fedimint_enabled.)
            if net.fedimint_enabled(spec):
                available = net.fedimint_available_ring_nodes(spec)
                if net.fedimint.guardians > available:
                    errors.append(
                        f"[{key}] fedimint.guardians ({net.fedimint.guardians}) "
                        f"exceeds the {available} available ring LND node(s) (one "
                        f"gateway is paired with each node); lower guardians, or "
                        f"enable lnd.secondary/lnd.tertiary"
                    )

            # Ark: the CLN bridge opens one channel into the ring, so the chosen
            # target node must actually be deployed (argus1 always is; argus2/3
            # follow the secondary/tertiary toggles). An unsupported chain is NOT
            # an error — it is auto-disabled with a generation-time warning (see
            # ark_enabled). channel_btc < ~0.167 BTC avoids needing wumbo on the
            # bridge; warn-free either way (LND accepts the inbound channel).
            if net.ark_enabled(spec):
                if not net.ark_target_enabled(spec):
                    alias, _svc, _vol = net.ark_channel_target(spec)
                    errors.append(
                        f"[{key}] ark.channel.target_node={alias!r} is not deployed "
                        f"on this network; pick a ring node that exists (argus1 is "
                        f"always on; enable lnd.secondary for argus2 / lnd.tertiary "
                        f"for argus3)"
                    )
                # captaind needs Bitcoin Core >= 29.0 (getblockchaininfo `bits`).
                # Check the effective node image (the Knots image on mutinynet); a
                # version we can't parse from the tag is left to the operator.
                img = (
                    self.global_.bitcoind_knots_image
                    if spec.needs_knots
                    else self.global_.bitcoind_image
                )
                major = _core_major_from_image(img)
                if major is not None and major < _ARK_MIN_CORE_MAJOR:
                    errors.append(
                        f"[{key}] Ark requires Bitcoin Core >= {_ARK_MIN_CORE_MAJOR}.0 "
                        f"(captaind reads getblockchaininfo's `bits`, added in 29.0), "
                        f"but the node image {img!r} looks like Core {major}; set "
                        f"{'global.bitcoind_knots_image' if spec.needs_knots else 'global.bitcoind_image'} "
                        f"to a >= {_ARK_MIN_CORE_MAJOR}.0 build, or disable ark on this network"
                    )

            # track whether any internet-facing SSL service exists (needs ACME email)
            if key != "regtest":
                services_ssl = [
                    net.cashu.enabled and net.cashu.ssl,
                    # The guardian API is fronted by Caddy (its URL goes in the
                    # invite code), so it needs TLS like the other public services.
                    net.fedimint_enabled(spec),
                    # captaind's Ark gRPC is fronted by Caddy (h2c) so bark wallets
                    # reach it over TLS — counts as a public SSL service.
                    net.ark_enabled(spec),
                    net.bitcart.enabled and net.bitcart.ssl,
                    net.cashupayserver.enabled and net.cashupayserver.ssl,
                    net.woocommerce.enabled and net.woocommerce.ssl,
                    net.mempool_enabled(spec) and net.mempool.ssl,
                    any(ix.ssl for ix in net.enabled_indexers()),
                ]
                if any(services_ssl):
                    any_public_ssl = True

        if self.global_.ssl_enabled and any_public_ssl and not self.global_.acme_email:
            errors.append(
                "global.acme_email is required when ssl_enabled is true and a public "
                "network has an SSL-enabled service (Let's Encrypt needs an email)"
            )

        # The LNURL default network (backing the bare fees@/cashout@/donate@/
        # referral@ addresses) must be an enabled network if pinned explicitly.
        dn = self.web.lnurl.default_network
        if self.web.enabled and self.web.lnurl.enabled and dn is not None:
            if dn not in self.networks:
                errors.append(
                    f"web.lnurl.default_network {dn!r} is not a configured network"
                )
            elif not self.networks[dn].enabled:
                errors.append(
                    f"web.lnurl.default_network {dn!r} is not enabled"
                )

        if errors:
            raise ConfigError(
                "configuration is invalid:\n  - " + "\n  - ".join(errors)
            )


def load_config(path: str | Path) -> ArgusConfig:
    """Load, parse, and fully validate a config file.

    Raises :class:`ConfigError` with a readable message on any problem.
    """
    p = Path(path)
    if not p.is_file():
        raise ConfigError(f"config file not found: {p}")

    try:
        raw: Any = yaml.safe_load(p.read_text())
    except yaml.YAMLError as exc:
        raise ConfigError(f"could not parse YAML: {exc}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("top-level config must be a mapping")

    try:
        cfg = ArgusConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigError(f"configuration is invalid:\n{exc}") from exc

    cfg._validate_semantics()
    return cfg
