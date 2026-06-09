#!/bin/sh
# Build the faucet's yespower proof-of-work primitive to a standalone WASM module.
#
# Run inside an emscripten toolchain (see the wasmbuild stage of the generated
# web Dockerfile). Downloads the pinned yespower reference source, verifies its
# checksum, and compiles it together with the wrapper (yespower_wasm.c) into a
# single import-light module exporting alloc / yespower_hash / memory — usable
# unchanged by the browser solver and the server-side wasmtime verifier.
#
# Output: $OUT_DIR/yespower.wasm  (default /out)
set -eu

SRC_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
OUT_DIR="${OUT_DIR:-/out}"
WORK="${WORK:-/tmp/yespower-build}"

# Pinned upstream release. The checksum is verified before use; update both
# together when bumping the version. (Confirmed against the published tarball at
# deploy time — see the feature's deploy/verify step.)
YESPOWER_VERSION="1.0.1"
YESPOWER_URL="https://www.openwall.com/yespower/yespower-${YESPOWER_VERSION}.tar.gz"
YESPOWER_SHA256="${YESPOWER_SHA256:-0e1eb612e3fa23a0b19aa5bbe944000b501b17d8bbb38cfa383a1f4376ead029}"

mkdir -p "$WORK" "$OUT_DIR"
cd "$WORK"

echo "[wasm] fetching yespower ${YESPOWER_VERSION}"
curl -fsSL "$YESPOWER_URL" -o yespower.tar.gz

if [ "$YESPOWER_SHA256" != "PLACEHOLDER_CONFIRM_AT_DEPLOY" ]; then
  echo "${YESPOWER_SHA256}  yespower.tar.gz" | sha256sum -c -
else
  echo "[wasm] WARNING: checksum pin not set; recording actual digest:"
  sha256sum yespower.tar.gz
fi

tar xzf yespower.tar.gz
cd "yespower-${YESPOWER_VERSION}"

echo "[wasm] compiling to standalone wasm"
# -mexec-model=reactor + --no-entry: a library module (no _start), so the host
#   just calls the exported functions.
# STANDALONE_WASM + ALLOW_MEMORY_GROWTH: malloc-backed heap, memory exported.
# --no-entry makes this a "reactor" module (no main): the host just calls the
# exported functions. A fixed 32 MiB heap (no memory growth) and no filesystem
# keep the module IMPORT-FREE, so both the browser and the wasmtime verifier can
# instantiate it with an empty import object. The linear memory is exported as
# "memory"; yespower's ~2 MiB working set fits comfortably.
emcc \
  -O3 \
  -I. \
  yespower-opt.c sha256.c "$SRC_DIR/yespower_wasm.c" \
  -sSTANDALONE_WASM=1 \
  -sALLOW_MEMORY_GROWTH=0 \
  -sINITIAL_MEMORY=33554432 \
  -sFILESYSTEM=0 \
  -sEXPORTED_FUNCTIONS=_alloc,_yespower_hash \
  -sERROR_ON_UNDEFINED_SYMBOLS=1 \
  -Wl,--no-entry \
  -o "$OUT_DIR/yespower.wasm"

echo "[wasm] wrote $OUT_DIR/yespower.wasm"
ls -l "$OUT_DIR/yespower.wasm"
