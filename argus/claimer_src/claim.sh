#!/bin/sh
# Generated build context: the testnet min-difficulty block claimer.
# claim.py waits for bitcoind RPC + sync itself, then runs its watch loop.
set -eu
exec python3 /claimer/claim.py
