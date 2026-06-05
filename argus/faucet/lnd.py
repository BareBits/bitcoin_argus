"""Minimal LND REST client for the faucet: read the on-chain balance and send
coins from a network's primary LND node (node #1).

Unlike the dashboard's LNURL path — which reads an *invoice-only* macaroon via
the read-only socket proxy and cannot move funds — the faucet must spend, so it
reads the node's ``admin.macaroon`` and ``tls.cert`` from the LND data volume
mounted read-only at ``$LND_DIR_ROOT/<net>`` (wired in :mod:`argus.web_gen`). It
dials the node by its unique container name ``argus-<net>-lnd``, whose TLS cert
carries that name as a SAN (see :mod:`argus.builders.lnd`).
"""

from __future__ import annotations

import os

from ..constants import LND_INTERNAL_PORTS, LND_NETWORK_KEY

_LND_DIR_ROOT = os.environ.get("LND_DIR_ROOT", "/lnd")


class FaucetLndError(Exception):
    """A node-level failure carrying a short, user-facing message."""


class FaucetLnd:
    """Talks to one network's node #1 over REST. Credentials are read fresh per
    call (cheap; avoids serving a stale cert/macaroon after a rotation)."""

    def __init__(self, net_key: str, chain: str) -> None:
        try:
            self._lndnet = LND_NETWORK_KEY[chain]
        except KeyError:
            raise FaucetLndError("unsupported network")
        self.net_key = net_key
        self._base_dir = f"{_LND_DIR_ROOT}/{net_key}"
        self._container = f"argus-{net_key}-lnd"

    # -- credentials -------------------------------------------------------

    def _macaroon_hex(self) -> str:
        path = (
            f"{self._base_dir}/data/chain/bitcoin/{self._lndnet}/admin.macaroon"
        )
        try:
            with open(path, "rb") as fh:
                return fh.read().hex()
        except OSError:
            raise FaucetLndError(
                "the Lightning node credentials are not available yet"
            )

    def _cert_path(self) -> str:
        path = f"{self._base_dir}/tls.cert"
        if not os.path.exists(path):
            raise FaucetLndError(
                "the Lightning node credentials are not available yet"
            )
        return path

    def _url(self, path: str) -> str:
        return f"https://{self._container}:{LND_INTERNAL_PORTS['rest']}{path}"

    # -- operations --------------------------------------------------------

    def balance_sat(self) -> int:
        """Confirmed on-chain wallet balance, in satoshis."""
        import requests

        macaroon = self._macaroon_hex()
        cert = self._cert_path()
        try:
            resp = requests.get(
                self._url("/v1/balance/blockchain"),
                headers={"Grpc-Metadata-macaroon": macaroon},
                verify=cert,
                timeout=10,
            )
            resp.raise_for_status()
            return int(resp.json().get("confirmed_balance", 0))
        except FaucetLndError:
            raise
        except Exception:
            raise FaucetLndError("the Lightning node is not reachable")

    def send(self, address: str, amount_sat: int, sat_per_vbyte: int) -> str:
        """Send ``amount_sat`` to ``address``; returns the txid.

        Surfaces the node's own error (e.g. insufficient funds, invalid address)
        as a :class:`FaucetLndError` so the caller can report a payment failure."""
        import requests

        macaroon = self._macaroon_hex()
        cert = self._cert_path()
        body = {
            "addr": address,
            "amount": str(amount_sat),
            "sat_per_vbyte": str(sat_per_vbyte),
        }
        try:
            resp = requests.post(
                self._url("/v1/transactions"),
                json=body,
                headers={"Grpc-Metadata-macaroon": macaroon},
                verify=cert,
                timeout=30,
            )
        except FaucetLndError:
            raise
        except Exception:
            raise FaucetLndError("the Lightning node is not reachable")
        if resp.status_code != 200:
            raise FaucetLndError(_error_message(resp))
        txid = resp.json().get("txid")
        if not txid:
            raise FaucetLndError(
                "the Lightning node did not return a transaction id"
            )
        return txid


def _error_message(resp) -> str:
    """A short, user-safe message from an LND REST error response."""
    try:
        data = resp.json()
        msg = data.get("message") or data.get("error") or ""
    except Exception:
        msg = ""
    msg = (msg or "the payment could not be completed").strip()
    return msg[:200]
