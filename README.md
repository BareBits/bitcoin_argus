# Bitcoin Argus

Self-contained, reproducible **Bitcoin testnet development environments**. From a
single `config.yaml`, Argus generates an isolated Docker Compose project per
enabled network, each bundling the services a developer needs to build against
that network.

> **Status: all phases complete.** The full stack — bitcoind, LND, Fulcrum,
> Cashu, mempool, Bitcart (with its Neutrino LND), and the shared Caddy/TLS layer
> plus the firewall script — is implemented and has been deployed and verified on
> a live test server. See [Roadmap](#roadmap).

## What it deploys (per enabled network)

| Service | Purpose | Internet exposure |
| --- | --- | --- |
| **bitcoind** (Knots for mutinynet) | The Bitcoin node | RPC/ZMQ closed (loopback/internal only) |
| **LND** (`argus1`, + `argus2` on mined nets) | Lightning node(s); on regtest/custom-signet two nodes are auto-funded and wired with channels | P2P open; gRPC/REST closed |
| **Fulcrum** (≥1) | Electrum server for light wallets + mempool backend | Electrum port open |
| **Cashu** (nutshell) | Ecash mint | HTTP via shared proxy |
| **Bitcart** | Payment processor (its own LND) | HTTP via shared proxy |
| **mempool** | Block explorer | HTTP via shared proxy |
| **miner** (regtest / custom-signet) | Produces a (signed, for signet) block every minute | — |

A single host-level **Caddy** terminates TLS for all HTTP services across all
networks (one certificate for the shared hostname; services differ by port).

A host-level **dashboard** (`generated/web/`) serves the welcome page at the site
root (`https://<hostname>/`) and reports live, per-service disk/RAM usage. See
[Dashboard](#dashboard).

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

# 2. Deploy on the server (copy generated/ across, then per network)
cd generated/regtest && docker compose up -d         # the core stack
bash generated/regtest/bitcart/deploy-bitcart.sh     # Bitcart (if enabled)
cd generated/shared && docker compose up -d           # the shared Caddy
cd generated/web && docker compose up -d --build       # the dashboard (builds its image)
sudo bash generated/firewall.sh                       # open the public ports
```

On **regtest**, two LND nodes (`argus1` + `argus2`) come up, get funded 25 BTC
each, and open a 10 BTC channel to each other. bitcoind keeps its P2P (mining)
port closed until that channel setup completes — so the funding can't be reorged
out from under us — then **reopens it automatically**. Once open, anyone can
attach a regtest node and mine (see the recipe on the dashboard). Disable with
`lnd.secondary.enabled: false` / `lnd.channels.enabled: false`.

> When the set of Caddy sites/ports changes, **restart the Caddy container**
> (`docker restart argus-caddy`) — a hot `caddy reload` won't bind new
> host-mode listeners.

## Dashboard

`argus generate` also writes **`generated/web/`** — a small Flask app (served by
gunicorn, built into its own image) that the shared Caddy fronts at the site root
(`https://<hostname>/`). It welcomes visitors, explains the available test
networks (with pros/cons and Bitcoin Core / Electrum attach recipes), links to
each running service, and shows live per-service and whole-host disk/RAM usage.

- **Themes** — `hacker` (default), `game`, and `bootstrap`, switchable from a
  selector at the top of the page; the CSS files and default are set under
  `web:` in `config.yaml`.
- **Live metrics** — read through a **read-only `docker-socket-proxy`** sidecar
  (the app never touches the Docker socket directly) and **cached for one hour**
  in a SQLite DB (peewee). Host disk totals come from the host root mounted
  read-only at `/host`.
- **Pages** — `/` (welcome + status), `/tos` (acceptable use), `/privacy`.
- **Config** — the `web:` block (`enabled`, `ssl`, `port`, `default_theme`,
  `themes`, `repo_url`). Default port is the bare 443/80 root; set `web.port` to
  serve elsewhere. The dashboard's runtime deps are the `web` extra
  (`pip install -e ".[web]"`) / `argus/web/requirements.txt`.

## Bitcart

Bitcart is deployed by the **BareBits installer** (`deploy_bitcart_liquidity_lnd`),
not as part of a network's compose project. Argus generates, per enabled
network, `generated/<net>/bitcart/{bitcart.env, deploy-bitcart.sh}`. Run the
wrapper on the host to deploy that network's Bitcart (it sets `DEPLOY_NAME` for
multi-instance, `REVERSEPROXY=none`, the per-net ports, and attaches its Neutrino
`btclnd` to the network's bitcoind). The shared Caddy fronts store/admin/api.

> Requires the installer's multi-instance `DEPLOY_NAME` mode
> (`deploy_bitcart_liquidity_lnd`); and the public service ports must be open in
> the firewall (see below) — the store/admin SSR also fetches the public API URL.

## Testing

```bash
pip install -e ".[test]"
pytest
```

Unit tests cover the deterministic core — config validation, port allocation,
and the full generation step (compose, `bitcoin.conf`/`lnd.conf`, Cashu/mempool
env, Caddyfile, firewall, Bitcart env) — and run in CI on every push/PR. A
Docker-gated test additionally validates the generated compose with
`docker compose config` when Docker is present. Runtime behaviour that needs real
chains/daemons (sync, Lightning, ACME, Bitcart's installer) is verified by
deploying to a host, not in unit tests.

## Configuration

`config.yaml` has a `global` block (shared hostname, SSL switch, ACME email,
images) and a `networks` block. Each network can be `enabled` independently and
takes per-service overrides (extra bitcoind/LND/Bitcart/Cashu args & env, prune
level, indexer list, mempool toggle, explicit ports). See the comments in
`config.yaml` for the full option list.

### SSL

SSL defaults **on** per service; the global `ssl_enabled: false` switch turns it
off everywhere (used for local/test runs). With SSL on, the shared Caddy obtains
**one Let's Encrypt certificate for the hostname** and serves it across every
service port — so it needs a real DNS name pointing at the host and ports
**80/443 open** (the generated firewall script opens them). With SSL off, Caddy
serves plain HTTP and runs no ACME. (Fulcrum's Electrum-TLS port is a documented
follow-up; the plaintext Electrum port works today.)

### Resources (disk/RAM)

A `resources` block (global and per-network; per-network wins) tunes footprint.
A `profile` (`low` / `medium` (default) / `high`) sets baseline values for
`bitcoind_dbcache`, `bitcoind_maxmempool`, `fulcrum_db_mem`,
`fulcrum_db_max_open_files`, and `mempool_mariadb_buffer_mb`; any of those can be
overridden explicitly (explicit > profile). Two disk-hygiene measures are on by
default (each toggleable): **Docker log rotation** (`log_rotation`, on every
Argus-generated service — not Bitcart's installer-managed containers) and **LND**
disk hygiene (`lnd.auto_compact` → bbolt auto-compact + canceled-invoice GC).
**mempool statistics** (historical fee/mempool graphs) are also **on by default**
(`mempool.statistics: true`); they're the biggest MariaDB grower, so set it
`false` on disk-constrained hosts. Note: bitcoind `prune`/`txindex` can't be
reduced while Fulcrum is enabled (Fulcrum requires `txindex` + an un-pruned node).

A local mempool also indexes the **Lightning network** by default
(`mempool.lightning: true`): its backend reads the primary LND node's graph over
REST (LND's data volume is mounted read-only for the TLS cert + readonly
macaroon), so the explorer's `/lightning` pages — and the dashboard's per-node
links — are populated, and the explorer's top-level **Lightning Explorer**
(`/lightning`) section is enabled. A node only appears once it has at least one
channel, so on single-node networks with no auto-opened channels (e.g. mutinynet)
the page stays empty until you open one. The dashboard links each LND node to its
Lightning page on the local mempool when one runs, otherwise to mempool.space
for the networks it covers (public signet, testnet3, testnet4).

The real testnets run in their **native mempool network** (testnet3 → testnet,
testnet4 → testnet4, custom-signet/mutinynet → signet): the explorer is served at
the root path (`ROOT_NETWORK`) with the other networks hidden, so the selector
lists only that network and mempool shows its own built-in "test coins have no
value" warning. (The frontend image's nginx only proxies the root `/api`, hence
the single network at root.)

**regtest** is a special case: mempool's frontend hardcodes regtest out of *both*
its testnet-warning list and its Lightning-supported-network list. To keep the
Lightning Explorer working we run regtest in mempool's **mainnet** slot
(`network=""`, which mempool *does* allow Lightning for), and Argus injects its
own red warning banner via an nginx `sub_filter` (a small generated
`mempool/web-banner.sh`) so the "no real value" notice still shows.

## Ports & firewall

Each network owns a 1000-port block; `argus ports` prints the assignment.
**Public:** LND P2P, the Electrum port, and the Caddy HTTP ports (cashu /
mempool / bitcart), plus Bitcart's btclnd P2P pool. **Closed (bound to
`127.0.0.1`):** bitcoind RPC/ZMQ, LND gRPC/REST, Fulcrum admin, mempool API/DB,
and Bitcart's component ports.

`argus generate` writes **`generated/firewall.sh`** — run it on the host to
`ufw allow` exactly the public ports (it keeps SSH open; closed services need no
rule since they're loopback-bound). Because Docker publishes ports past `ufw`,
this script is what makes the public ports reachable once you enforce a
default-deny incoming policy (`ufw default deny incoming && ufw --force enable`).

## Per-network notes

- **regtest** — self-mined (1 block/min by default); self-hosted explorer (no
  public one exists).
- **signet** — default public signet; explorer off by default (use
  mempool.space/signet).
- **testnet3 / testnet4** — explorers off by default (mempool.space hosts them).
- **mutinynet** — custom 30s-block signet; explorer on by default. Requires
  `global.bitcoind_knots_image` (a `signetblocktime`-capable bitcoind — no public
  image exists, so build one from MutinyWallet/mutiny-net or Bitcoin Knots).
- **custom-signet** — a **self-mined** custom signet, **on by default**. Argus
  auto-generates a matched signet challenge + block-signing key into
  `secrets/custom-signet/` (supply your own `signet_challenge` only if you run
  your own signer) and runs a signet-miner sidecar that signs and produces blocks
  on an interval (default 1/min), just like regtest. The miner image is built
  from the stock bitcoind image plus the vendored Bitcoin Core signet miner (see
  `argus/signet_miner/`); no Knots build is needed.

## Roadmap

- [x] Phase 1 — config + validation + port allocator + regtest chain & miner
- [x] Phase 2 — standalone LND (bitcoind-backed, auto-init wallet)
- [x] Phase 3 — Fulcrum (Electrum server; one+ per network)
- [x] Phase 4 — shared Caddy (host-level TLS) + Cashu mint
- [x] Phase 5 — mempool explorer (Fulcrum-backed; default-on regtest/custom-signet/mutinynet)
- [x] Phase 6 — Bitcart (BareBits installer, own Neutrino LND → our bitcoind, behind Caddy)
- [x] Phase 7 — all networks (testnet3/4, mutinynet, custom-signet) wired + validated
- [x] Phase 8 — firewall script, SSL path, deploy docs
- [x] Dashboard — welcome/status web server (themes, live per-service metrics)
- [x] Self-mined custom signet — auto challenge/key + signet-miner sidecar
