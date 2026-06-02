# mutinynet-bitcoind image

Mutinynet runs a **custom signet with 30-second blocks**, which requires a
bitcoind that supports `signetblocktime`. Stock Bitcoin Core (and the `lncm`
images Argus uses elsewhere) do not, and no such image is published on a public
registry — so Argus needs an operator-supplied one for the `mutinynet` network
(`global.bitcoind_knots_image`).

This builds that image from prebuilt binaries in
[benthecarman/bitcoin](https://github.com/benthecarman/bitcoin/releases) (the
fork the public Mutinynet tracks). No compilation — it just wraps the release
binary.

## Build

```bash
docker build -t argus/bitcoind-mutinynet:latest images/mutinynet-bitcoind
```

To follow a different consensus variant (Mutinynet has periodically activated
experimental soft forks), pass the matching release's x86_64 tarball:

```bash
docker build -t argus/bitcoind-mutinynet:latest \
  --build-arg URL=https://github.com/benthecarman/bitcoin/releases/download/<tag>/<file>-x86_64-linux-gnu.tar.gz \
  images/mutinynet-bitcoind
```

## Use

```yaml
# config.yaml
global:
  bitcoind_knots_image: argus/bitcoind-mutinynet:latest
networks:
  mutinynet:
    enabled: true
```

Argus then runs it with its generated `bitcoin.conf` (signet + Mutinynet's
`signetchallenge` + `signetblocktime=30` + seed node). The image exposes
`bitcoind` as entrypoint with `bitcoin-cli` on PATH and data at `/data`, matching
what the rest of Argus expects.
