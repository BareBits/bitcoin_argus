# Vendored Bitcoin Core signet miner

`miner` and the `test_framework/` subset here are vendored verbatim from
[Bitcoin Core](https://github.com/bitcoin/bitcoin) at tag **v28.0**
(`contrib/signet/miner` plus the minimal `test/functional/test_framework`
modules the miner imports). They are MIT-licensed (Copyright (c) The Bitcoin Core
developers).

They are bundled so the custom-signet block producer can run in a container built
from the stock bitcoind image (which ships `bitcoin-cli`/`bitcoin-util` but not
Python or the signet miner). See `argus/builders/miner.py` for how the build
context is generated, and `mine-signet.sh` for the entrypoint loop.

To update: re-fetch the same files from the matching Bitcoin Core tag.
