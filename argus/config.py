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
    DEFAULT_RESET_CHECK_INTERVAL,
    DEFAULT_RESET_MAX_SIZE_GB,
    NETWORK_SPECS,
    NetworkSpec,
)


class ConfigError(Exception):
    """Raised for any invalid configuration (syntactic or semantic)."""


# Hostname per RFC 1123 (labels of a-z, 0-9, hyphen; not leading/trailing hyphen).
_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")

# Networks whose block production Argus can drive itself.
_MINEABLE_NETWORKS = {"regtest", "custom-signet"}


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
    bitcoind_image: str = "lncm/bitcoind:v28.0"
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
    resources: ResourcesCfg = Field(default_factory=ResourcesCfg)
    tor: TorCfg = Field(default_factory=TorCfg)

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        if not _is_valid_host(v):
            raise ValueError(f"invalid hostname or IP: {v!r}")
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
    """A second LND node on the same network (for inter-node channels).

    Only valid on networks Argus mines (regtest/custom-signet). ``enabled`` is
    tri-state: None => on for those networks, off elsewhere.
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


class LndChannelsCfg(_Base):
    """Auto-fund both LND nodes and open channels between them at startup.

    Only valid on networks Argus mines. ``enabled`` is tri-state (None => on for
    those networks). Each node is funded with ``fund_btc`` on-chain and opens one
    channel of ``channel_btc`` to the other (two channels total).
    """

    enabled: bool | None = None
    fund_btc: float = Field(default=25.0, gt=0)
    channel_btc: float = Field(default=10.0, gt=0)


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
    log_level: str = "INFO"  # LIQUIDITYHELPER_LOG_LEVEL


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
    """Regtest/custom-signet block production."""

    enabled: bool = True
    block_interval_seconds: int = Field(default=60, ge=1)
    initial_blocks: int = Field(default=101, ge=0)  # coinbase maturity on regtest


class ResetCfg(_Base):
    """Auto-reset a mined network when its chain outgrows a size cap.

    Only meaningful on the networks Argus mines (regtest/custom-signet) — see
    ``constants.RESET_NETWORKS``. When the network's bitcoind ``size_on_disk``
    reaches ``max_size_gb``, the whole installation for that network is torn down
    (``docker compose down -v``) and re-deployed to its base config — wiping all
    coins, Lightning channels, transactions, mempool/Fulcrum/Cashu state, and
    Bitcart. ``enabled`` is tri-state: None => on for the mined networks, off
    elsewhere. (A custom signet keeps its challenge/signing key, so it resets to
    genesis as the *same* signet.)
    """

    enabled: bool | None = None
    max_size_gb: float = Field(default=DEFAULT_RESET_MAX_SIZE_GB, gt=0)
    check_interval_seconds: int = Field(default=DEFAULT_RESET_CHECK_INTERVAL, ge=1)


class NetworkCfg(_Base):
    enabled: bool = False
    prune: int = Field(default=0, ge=0)  # MiB; 0 = no pruning
    signet_challenge: str | None = None
    addnode: list[str] = Field(default_factory=list)

    bitcoind: BitcoindCfg = Field(default_factory=BitcoindCfg)
    lnd: LndCfg = Field(default_factory=LndCfg)
    cashu: CashuCfg = Field(default_factory=CashuCfg)
    bitcart: BitcartCfg = Field(default_factory=BitcartCfg)
    indexers: list[IndexerCfg] = Field(default_factory=lambda: [IndexerCfg()])
    mempool: MempoolCfg = Field(default_factory=MempoolCfg)
    miner: MinerCfg = Field(default_factory=MinerCfg)
    reset: ResetCfg = Field(default_factory=ResetCfg)
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

    def lnd_secondary_enabled(self, spec: NetworkSpec) -> bool:
        """Whether a second LND node is deployed.

        Defaults on for mined networks (so the auto-channel pair exists), but only
        when the miner is on — disabling the miner cleanly opts out of the default
        rather than forcing an error. An explicit value always wins.
        """
        v = self.lnd.secondary.enabled
        return (spec.supports_miner and self.miner.enabled) if v is None else v

    def lnd_channels_enabled(self, spec: NetworkSpec) -> bool:
        """Whether auto-funding + channel setup runs.

        Defaults on for mined networks with the miner enabled (it funds the
        channels). An explicit value always wins (and is validated).
        """
        v = self.lnd.channels.enabled
        return (spec.supports_miner and self.miner.enabled) if v is None else v

    def bitcoind_p2p_gated(self, net_key: str, spec: NetworkSpec) -> bool:
        """Whether bitcoind self-gates its P2P listener (regtest auto-channels):
        it keeps inbound P2P closed until LND channel setup completes, then
        restarts with P2P open — so funding can't be reorged during setup.

        Only regtest needs this — on custom-signet outsiders can't produce valid
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

    @field_validator("port")
    @classmethod
    def _check_port(cls, v: int | None) -> int | None:
        if v is not None and not (1 <= v <= 65535):
            raise ValueError(f"web.port {v} out of range 1-65535")
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
            # regtest and custom-signet are the chains Argus can drive.
            if net.miner.enabled and spec.supports_miner and key not in _MINEABLE_NETWORKS:
                errors.append(
                    f"[{key}] automated mining is not implemented for this network"
                )

            # Secondary LND + auto-channels are only meaningful where Argus drives
            # block production (it funds the channels by mining). Both default on
            # there and off elsewhere; reject an explicit enable on other networks.
            secondary_on = net.lnd_secondary_enabled(spec)
            channels_on = net.lnd_channels_enabled(spec)
            if secondary_on and not spec.supports_miner:
                errors.append(
                    f"[{key}] lnd.secondary is only supported on networks Argus "
                    f"mines (regtest/custom-signet)"
                )
            if channels_on:
                if not spec.supports_miner:
                    errors.append(
                        f"[{key}] lnd.channels auto-setup is only supported on "
                        f"networks Argus mines (regtest/custom-signet)"
                    )
                if not secondary_on:
                    errors.append(
                        f"[{key}] lnd.channels requires lnd.secondary (two nodes are "
                        f"needed to open channels between them)"
                    )
                if not net.miner.enabled:
                    errors.append(
                        f"[{key}] lnd.channels requires the miner (block production "
                        f"funds the channels); enable miner or disable lnd.channels"
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
                    f"networks Argus mines (regtest/custom-signet)"
                )

            # Bitcart requires an admin email; an active liquidity helper needs
            # a cash-out Lightning address.
            if net.bitcart.enabled:
                if not net.bitcart.admin_email:
                    errors.append(f"[{key}] bitcart.admin_email is required")
                if (
                    not net.bitcart.liquidity.disabled
                    and not net.bitcart.liquidity.cashout_lightning_address
                ):
                    errors.append(
                        f"[{key}] bitcart.liquidity.cashout_lightning_address is "
                        f"required when liquidity is enabled (liquidity.disabled=false)"
                    )

            # track whether any internet-facing SSL service exists (needs ACME email)
            if key != "regtest":
                services_ssl = [
                    net.cashu.enabled and net.cashu.ssl,
                    net.bitcart.enabled and net.bitcart.ssl,
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
