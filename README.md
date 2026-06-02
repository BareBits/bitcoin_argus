# Bitcoin Argus

Self-contained, reproducible **Bitcoin testnet development environments**. From a
single `config.yaml`, Argus generates an isolated Docker Compose project per
enabled network, each bundling the services a developer needs to build against
that network.

> **Status: early development (Phase 1).** The config engine, port allocator, and
> the regtest chain + auto-miner are implemented. LND, Fulcrum, Cashu, Bitcart,
> mempool, and the shared TLS proxy are being added network-by-network. See
> [Roadmap](#roadmap).

## What it deploys (per enabled network)

| Service | Purpose | Internet exposure |
| --- | --- | --- |
| **bitcoind** (Knots for mutinynet) | The Bitcoin node | RPC/ZMQ closed (loopback/internal only) |
| **LND** | Lightning node (used by Cashu) | P2P open; gRPC/REST closed |
| **Fulcrum** (≥1) | Electrum server for light wallets + mempool backend | Electrum port open |
| **Cashu** (nutshell) | Ecash mint | HTTP via shared proxy |
| **Bitcart** | Payment processor (its own LND) | HTTP via shared proxy |
| **mempool** | Block explorer | HTTP via shared proxy |
| **miner** (regtest) | Produces a block every minute | — |

A single host-level **Caddy** terminates TLS for all HTTP services across all
networks (one certificate for the shared hostname; services differ by port).

## How it works

```
config.yaml ──► argus (validate ─► allocate ports ─► render) ──► generated/<net>/
                                                                   ├── docker-compose.yml
                                                                   ├── .env            (gitignored)
                                                                   ├── bitcoin/bitcoin.conf
                                                                   └── miner/mine.sh
```

The CLI only *generates* plain Docker Compose files; you deploy them with
`docker compose`. Generated output and secrets are gitignored.

## Prerequisites

- A Linux host (bare metal or VPS). Docker is **not** required to *generate*
  files, only to *run* them.
- One hostname pointed at the host (for SSL). Different services use different
  ports on that one hostname.
- Open the ports Argus marks public (LND P2P, Electrum); keep the rest closed.

## Quick start

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 1. Edit config.yaml (hostname, which networks are enabled, etc.)
python -m argus validate          # check the config
python -m argus ports             # review the host-port allocation
python -m argus generate          # render enabled networks into generated/

# 2. Deploy a network (on the server)
cd generated/regtest
docker compose up -d
```

## Configuration

`config.yaml` has a `global` block (shared hostname, SSL switch, ACME email,
images) and a `networks` block. Each network can be `enabled` independently and
takes per-service overrides (extra bitcoind/LND/Bitcart/Cashu args & env, prune
level, indexer list, mempool toggle, explicit ports). See the comments in
`config.yaml` for the full option list.

### SSL

SSL defaults **on** per service; the global `ssl_enabled: false` switch turns it
off everywhere for testing. ACME (Let's Encrypt) uses `:80`; regtest uses
internal self-signed certs (no public DNS).

## Ports & firewall

Each network owns a 1000-port block; `argus ports` prints the assignment.
Public: **LND P2P** and the **Electrum** port. Closed (bound to `127.0.0.1`):
**bitcoind RPC/ZMQ** and **LND gRPC/REST**. Because Docker bypasses `ufw`, closed
services are bound to loopback in the publish spec; a firewall script is also
generated as defense-in-depth.

## Per-network notes

- **regtest** — self-mined (1 block/min by default); self-hosted explorer (no
  public one exists).
- **signet** — default public signet; explorer off by default (use
  mempool.space/signet).
- **testnet3 / testnet4** — explorers off by default (mempool.space hosts them).
- **mutinynet** — custom 30s-block signet; requires **Bitcoin Knots**; explorer
  on by default.
- **custom-signet** — you must supply `signet_challenge`; mining is a later phase.

## Roadmap

- [x] Phase 1 — config + validation + port allocator + regtest chain & miner
- [x] Phase 2 — standalone LND (bitcoind-backed, auto-init wallet)
- [x] Phase 3 — Fulcrum (Electrum server; one+ per network)
- [ ] Phase 4 — shared Caddy + Cashu
- [ ] Phase 5 — mempool explorer
- [ ] Phase 6 — Bitcart (own LND, behind Caddy)
- [ ] Phase 7 — all networks (signet, testnet3/4, mutinynet, custom-signet)
- [ ] Phase 8 — SSL hardening, firewall script, docs
