# Generated build context: the testnet3/testnet4 min-difficulty block claimer.
# Built from the stock bitcoind image (which already ships bitcoin-cli /
# bitcoin-util) plus python3 and the vendored test_framework (shared with the
# signet miner — see argus/signet_miner/NOTICE.md for provenance).
ARG BITCOIND_IMAGE=lncm/bitcoind:v28.0
FROM ${BITCOIND_IMAGE}
USER root
RUN apk add --no-cache python3
COPY test_framework /claimer/test_framework
COPY claim.py /claimer/claim.py
COPY claim.sh /claimer/claim.sh
ENTRYPOINT ["/bin/sh", "/claimer/claim.sh"]
