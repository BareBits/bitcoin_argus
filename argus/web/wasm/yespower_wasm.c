/* Thin WASM wrapper around the yespower reference implementation.
 *
 * Exposes exactly what the faucet solver (browser, argus/web/static/*.js) and
 * the server verifier (Python via wasmtime, argus/faucet/pow.py) need:
 *
 *   alloc(len)                  -> ptr   (bump/heap allocation in linear memory)
 *   yespower_hash(in, len, out) -> 0/-1  (writes a 32-byte digest at `out`)
 *   memory                      (the module's exported linear memory)
 *
 * Parameters are fixed here so client and server always agree: yespower 1.0,
 * N=2048, r=8 (~2 MiB working set — CPU/memory-hard, so GPUs/ASICs get little
 * advantage over the consumer laptop the difficulty is calibrated against).
 *
 * Compiled to a standalone, import-light WASM module — see build-wasm.sh.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>

#include "yespower.h"

/* yespower 1.0, N=2048, r=8, no personalisation. */
static const yespower_params_t PARAMS = {YESPOWER_1_0, 2048, 8, NULL, 0};

__attribute__((export_name("alloc"))) void *alloc(size_t n) {
  return malloc(n);
}

__attribute__((export_name("yespower_hash"))) int
yespower_hash(const uint8_t *in, size_t inlen, uint8_t *out) {
  yespower_binary_t dst;
  if (yespower_tls(in, inlen, &PARAMS, &dst) != 0)
    return -1;
  for (int i = 0; i < 32; i++)
    out[i] = ((const uint8_t *)&dst)[i];
  return 0;
}
