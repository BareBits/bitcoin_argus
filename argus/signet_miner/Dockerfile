# Generated build context: the custom-signet block producer.
# Built from the stock bitcoind image (which already has bitcoin-cli / bitcoin-util)
# plus python3 and the vendored signet miner (see NOTICE.md).
ARG BITCOIND_IMAGE=lncm/bitcoind:v28.0
FROM ${BITCOIND_IMAGE}
USER root
RUN apk add --no-cache python3
COPY miner /signet/miner
COPY test_framework /signet/test_framework
COPY mine-signet.sh /signet/mine-signet.sh
ENTRYPOINT ["/bin/sh", "/signet/mine-signet.sh"]
