"""The Argus faucet: a small, isolated Flask app that dispenses on-chain testnet
coins from each network's primary LND node.

It runs as a SEPARATE process/container from the dashboard (see ``argus.web``) so
that a bug in the faucet can never take the main page down. The shared Caddy
path-routes ``/<net>/faucet`` to it. Keep this package's import surface light: the
config layer imports :mod:`argus.faucet.approval` during validation.
"""
