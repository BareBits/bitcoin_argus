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
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .constants import NETWORK_SPECS, NetworkSpec


class ConfigError(Exception):
    """Raised for any invalid configuration (syntactic or semantic)."""


# Hostname per RFC 1123 (labels of a-z, 0-9, hyphen; not leading/trailing hyphen).
_HOSTNAME_LABEL = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")


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
    caddy_image: str = "caddy:2"
    mempool_backend_image: str = "mempool/backend:v3.3.1"
    mempool_frontend_image: str = "mempool/frontend:v3.3.1"
    mariadb_image: str = "mariadb:10.5.21"

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        if not _is_valid_host(v):
            raise ValueError(f"invalid hostname or IP: {v!r}")
        return v


class BitcoindCfg(_Base):
    extra_args: list[str] = Field(default_factory=list)


class LndCfg(_Base):
    extra_args: list[str] = Field(default_factory=list)
    extra_env: dict[str, str] = Field(default_factory=dict)


class CashuCfg(_Base):
    enabled: bool = True
    ssl: bool = True
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


class MinerCfg(_Base):
    """Regtest/custom-signet block production."""

    enabled: bool = True
    block_interval_seconds: int = Field(default=60, ge=1)
    initial_blocks: int = Field(default=101, ge=0)  # coinbase maturity on regtest


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


class ArgusConfig(_Base):
    global_: GlobalConfig = Field(alias="global")
    networks: dict[str, NetworkCfg]

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

            # signet challenge requirements
            challenge = net.signet_challenge or spec.default_signet_challenge
            if spec.requires_challenge and not challenge:
                errors.append(
                    f"[{key}] is a custom signet and requires 'signet_challenge'"
                )

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
            # custom-signet IS mineable in principle, but signet mining isn't
            # implemented yet — surface that clearly if someone enables it.
            if net.miner.enabled and spec.supports_miner and key != "regtest":
                errors.append(
                    f"[{key}] automated mining is only implemented for regtest so far"
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
