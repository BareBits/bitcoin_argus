"""LNURL-pay / Lightning Address support for the dashboard (LUD-06 + LUD-16).

The dashboard already sits at the installation root (behind the shared Caddy) and
*is* the onion's port-80 service, so it is the natural place to answer the
Lightning Address endpoints:

* ``GET /.well-known/lnurlp/<name>`` -> the LUD-06 ``payRequest`` JSON, and
* ``GET /lnurlp/<name>/callback?amount=<msat>`` -> ``{"pr": <bolt11>}``.

``<name>`` is one of four purposes — ``fees``, ``cashout``, ``donate`` and
``referral`` — each available as a *bare* address (backed by the configured
default network) and a per-network variant ``<purpose>-<net>``. A payer's wallet
lives on one specific chain, and a network's LND node #1 can only mint an invoice
on its own chain, so the per-network form lets the payer target the matching
testnet. The four purposes are otherwise identical: they differ only in the
invoice memo (which lands in the LUD-06 metadata, hashed into the invoice's ``h``
tag).

**How the invoice is minted.** The public web container holds no long-lived LND
credentials. It reads the network's *invoice-only* macaroon and TLS cert from the
``argus-<net>-lnd`` container through the read-only docker-socket-proxy
(``get_archive``, a GET — the same trick the metrics layer uses for the node
pubkey), then POSTs ``addinvoice`` to that node's REST API over the per-network
Docker network. The invoice macaroon cannot move funds, and the socket proxy
stays GET-only. We connect by the unique *container name* (not the ``lnd`` service
alias, which collides across the networks the web container joins); LND's cert
carries that name as a SAN (see :mod:`argus.builders.lnd`).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import tempfile
from dataclasses import dataclass

from ..config import ArgusConfig
from ..constants import LND_INTERNAL_PORTS, LND_NETWORK_KEY, NETWORK_ORDER, NETWORK_SPECS

# The four Lightning-Address purposes. All mint an invoice on node #1; they differ
# only in the human-readable memo carried in the LUD-06 metadata.
PURPOSES: tuple[str, ...] = ("fees", "cashout", "donate", "referral")

# Per-purpose memo template (``{host}`` / ``{net}`` filled in per request).
_MEMOS: dict[str, str] = {
    "fees": "Fee payment to {host}",
    "cashout": "Cashout to {host}",
    "donate": "Donation to {host} ({net} testnet)",
    "referral": "Referral / hosting fee to {host}",
}

# LND data layout inside the node container (see builders/lnd.py: lnddir).
_LNDDIR = "/home/lnd/.lnd"


class LnurlError(Exception):
    """A protocol-level failure carrying a wallet-facing ``reason`` string."""


@dataclass(frozen=True)
class _Resolved:
    purpose: str
    net_key: str
    chain: str  # bitcoind chain (regtest/test/testnet4/signet)


def _lnd_net(chain: str) -> str:
    """LND's on-disk network subdir for a bitcoind ``chain`` (e.g. signet)."""
    try:
        return LND_NETWORK_KEY[chain]
    except KeyError:  # pragma: no cover - every supported chain is mapped
        raise LnurlError("unsupported network")


class LnurlService:
    """Resolves Lightning-Address names and mints invoices on node #1.

    Constructed once per app from the validated config. Stateless except for a
    small per-network credentials cache (macaroon hex + a temp cert file), which
    is safe to keep for the process lifetime — the testnet wallets are stable.
    """

    def __init__(self, cfg: ArgusConfig) -> None:
        self._cfg = cfg
        lnurl = cfg.web.lnurl
        self.enabled = bool(cfg.web.enabled and lnurl.enabled)
        self.min_sat = lnurl.min_sat
        self.max_sat = lnurl.max_sat
        self.comment_length = lnurl.comment_length
        # Enabled network keys in canonical order; the first is the fallback for
        # the bare addresses when no default_network is pinned.
        self._enabled = [k for k, _ in cfg.enabled_networks()]
        self.default_network = lnurl.default_network or (
            self._enabled[0] if self._enabled else None
        )
        # net_key -> (macaroon_hex, cert_path); populated lazily.
        self._creds: dict[str, tuple[str, str]] = {}

    # -- name resolution ---------------------------------------------------

    def parse_name(self, name: str) -> _Resolved | None:
        """Map a Lightning-Address local part to ``(purpose, net)`` or None.

        ``donate`` -> default network; ``donate-signet`` -> signet. Network keys
        may contain a hyphen (``custom-signet``), so we split on the *first*
        hyphen only — the purpose never contains one.
        """
        if not self.enabled or not name:
            return None
        net_key: str | None
        if name in PURPOSES:
            purpose, net_key = name, self.default_network
        else:
            purpose, sep, rest = name.partition("-")
            if not sep or purpose not in PURPOSES or rest not in self._enabled:
                return None
            net_key = rest
        if net_key is None or net_key not in self._enabled:
            return None
        return _Resolved(purpose, net_key, NETWORK_SPECS[net_key].chain)

    def address(self, purpose: str, net_key: str, host: str) -> str:
        """The canonical Lightning Address for a purpose on a network."""
        local = purpose if net_key == self.default_network else f"{purpose}-{net_key}"
        return f"{local}@{host}"

    # -- LUD-06 / LUD-16 ---------------------------------------------------

    def _metadata(self, res: _Resolved, identifier: str) -> str:
        memo = _MEMOS[res.purpose].format(host=identifier.split("@", 1)[-1], net=res.net_key)
        return json.dumps([["text/plain", memo], ["text/identifier", identifier]])

    def pay_request(self, name: str, host: str, callback_url: str) -> dict:
        """The LUD-06 ``payRequest`` document for ``name`` (raises LnurlError)."""
        res = self.parse_name(name)
        if res is None:
            raise LnurlError("unknown Lightning Address")
        metadata = self._metadata(res, self.address(res.purpose, res.net_key, host))
        return {
            "tag": "payRequest",
            "callback": callback_url,
            "minSendable": self.min_sat * 1000,
            "maxSendable": self.max_sat * 1000,
            "metadata": metadata,
            "commentAllowed": self.comment_length,
        }

    def invoice(self, name: str, host: str, amount_msat: int, comment: str | None) -> str:
        """Validate the request and return a bolt11 for ``amount_msat``.

        ``comment`` (LUD-12) is accepted for attribution but not stored — LND
        binds the invoice description to the metadata hash, not the comment.
        """
        res = self.parse_name(name)
        if res is None:
            raise LnurlError("unknown Lightning Address")
        if amount_msat < self.min_sat * 1000 or amount_msat > self.max_sat * 1000:
            raise LnurlError(
                f"amount out of range ({self.min_sat}-{self.max_sat} sat)"
            )
        if amount_msat % 1000 != 0:
            raise LnurlError("amount must be a whole number of sats")
        if comment and len(comment) > self.comment_length:
            raise LnurlError(f"comment too long (max {self.comment_length})")
        metadata = self._metadata(res, self.address(res.purpose, res.net_key, host))
        descr_hash = base64.b64encode(hashlib.sha256(metadata.encode()).digest()).decode()
        return self._mint(res, amount_msat, descr_hash)

    # -- invoice minting against node #1 -----------------------------------

    def _docker_client(self):
        import docker

        return docker.from_env()  # honours DOCKER_HOST (the read-only socket proxy)

    def _read_container_file(self, client, container: str, path: str) -> bytes:
        ct = client.containers.get(container)
        bits, _ = ct.get_archive(path)
        tf = tarfile.open(fileobj=io.BytesIO(b"".join(bits)))
        member = tf.extractfile(tf.getmembers()[0])
        if member is None:  # pragma: no cover - defensive
            raise LnurlError("could not read node credentials")
        return member.read()

    def _credentials(self, net_key: str, chain: str) -> tuple[str, str]:
        """``(macaroon_hex, cert_path)`` for a network's node #1, cached.

        Reads the invoice-only macaroon and TLS cert from the LND container via
        the read-only docker socket proxy (``get_archive``). The cert is written
        to a temp file so ``requests`` can verify against it.
        """
        cached = self._creds.get(net_key)
        if cached is not None:
            return cached
        container = f"argus-{net_key}-lnd"
        try:
            client = self._docker_client()
            mac = self._read_container_file(
                client,
                container,
                f"{_LNDDIR}/data/chain/bitcoin/{_lnd_net(chain)}/invoice.macaroon",
            )
            cert = self._read_container_file(client, container, f"{_LNDDIR}/tls.cert")
        except LnurlError:
            raise
        except Exception:
            raise LnurlError("the Lightning node is not reachable yet")
        cert_file = tempfile.NamedTemporaryFile(
            prefix=f"lnd-{net_key}-", suffix=".cert", delete=False
        )
        cert_file.write(cert)
        cert_file.close()
        creds = (mac.hex(), cert_file.name)
        self._creds[net_key] = creds
        return creds

    def _mint(self, res: _Resolved, amount_msat: int, descr_hash: str) -> str:
        import requests

        macaroon_hex, cert_path = self._credentials(res.net_key, res.chain)
        container = f"argus-{res.net_key}-lnd"
        url = f"https://{container}:{LND_INTERNAL_PORTS['rest']}/v1/invoices"
        body = {"value_msat": str(amount_msat), "description_hash": descr_hash}
        try:
            resp = requests.post(
                url,
                json=body,
                headers={"Grpc-Metadata-macaroon": macaroon_hex},
                verify=cert_path,
                timeout=15,
            )
            resp.raise_for_status()
            pr = resp.json().get("payment_request")
        except Exception:
            # Drop any cached creds so a rotated cert/macaroon is re-read next time.
            self._creds.pop(res.net_key, None)
            raise LnurlError("could not create an invoice on the Lightning node")
        if not pr:
            raise LnurlError("the Lightning node returned no invoice")
        return pr
