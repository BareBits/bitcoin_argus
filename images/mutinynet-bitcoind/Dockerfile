# Mutinynet-compatible bitcoind — a signetblocktime-capable Bitcoin Core build,
# which stock Core/lncm images do NOT provide. Binaries come from benthecarman's
# fork (the source the public Mutinynet tracks).
#
# Pick the release whose consensus matches the network you intend to follow and
# pass its x86_64-linux-gnu tarball as URL. Default tracks the latest Mutinynet
# (Inquisition-based) build.
#
#   docker build -t argus/bitcoind-mutinynet:latest \
#     --build-arg URL=<release .tar.gz> images/mutinynet-bitcoind
#
# Then set in config.yaml:  global.bitcoind_knots_image: argus/bitcoind-mutinynet:latest
#
# The image matches the lncm/bitcoind interface Argus expects: entrypoint
# `bitcoind`, `bitcoin-cli` on PATH, data at /data (Argus passes
# -datadir=/data -conf=/data/bitcoin.conf).
FROM debian:bookworm-slim

ARG URL=https://github.com/benthecarman/bitcoin/releases/download/mutinynet-inq-template-hash/bitcoin-2fda7bfc027e-x86_64-linux-gnu.tar.gz

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates libstdc++6 \
 && curl -fsSL "$URL" -o /tmp/btc.tar.gz \
 && tar xzf /tmp/btc.tar.gz -C /tmp \
 && d="$(find /tmp -maxdepth 1 -type d -name 'bitcoin-*')" \
 && install -m 0755 "$d"/bin/bitcoind "$d"/bin/bitcoin-cli /usr/local/bin/ \
 && rm -rf /tmp/btc.tar.gz "$d" /var/lib/apt/lists/* \
 && apt-get purge -y curl && apt-get autoremove -y \
 && bitcoind -version | head -1

VOLUME /data
# bitcoind p2p / rpc / zmq are configured by Argus's generated bitcoin.conf.
ENTRYPOINT ["bitcoind"]
