/* Faucet proof-of-work solver (Web Worker).
 *
 * Searches for a `solution` string such that H(token || solution) < target,
 * where H is the challenge's algorithm. Runs off the main thread so the page
 * stays responsive, and reports progress periodically. Multiple workers cover
 * disjoint solution spaces by prefixing solutions with a per-worker id.
 *
 * Two primitives, matching the server (argus/faucet/pow.py):
 *   - "sha256d": double SHA-256, implemented here in plain JS (dev/test + the
 *     low-security fallback);
 *   - "yespower": the CI-built WASM module (same bytes the server verifies with),
 *     exporting memory / alloc(len) / yespower_hash(inPtr, inLen, outPtr).
 *
 * The token is ASCII; solutions are ASCII decimal-ish strings, so the preimage
 * is just the concatenated UTF-8 (== ASCII) bytes, identical to the server's
 * `token.encode('ascii') + solution.encode('ascii')`.
 */

'use strict';

/* ---- SHA-256 (FIPS 180-4), operating on byte arrays ---- */

const K = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
  0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
  0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
  0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
  0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
  0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
  0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
  0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
  0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function sha256(bytes) {
  const h = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
    0x1f83d9ab, 0x5be0cd19,
  ]);
  const bitLen = bytes.length * 8;
  // padding: 0x80, then zeros, then 64-bit big-endian length
  const withPad = new Uint8Array((((bytes.length + 8) >> 6) + 1) << 6);
  withPad.set(bytes);
  withPad[bytes.length] = 0x80;
  const dv = new DataView(withPad.buffer);
  dv.setUint32(withPad.length - 4, bitLen >>> 0, false);
  dv.setUint32(withPad.length - 8, Math.floor(bitLen / 0x100000000), false);

  const w = new Uint32Array(64);
  for (let off = 0; off < withPad.length; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = dv.getUint32(off + i * 4, false);
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) | 0;
    }
    let [a, b, c, d, e, f, g, hh] = h;
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (hh + S1 + ch + K[i] + w[i]) | 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const t2 = (S0 + maj) | 0;
      hh = g; g = f; f = e; e = (d + t1) | 0;
      d = c; c = b; b = a; a = (t1 + t2) | 0;
    }
    h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0; h[2] = (h[2] + c) | 0;
    h[3] = (h[3] + d) | 0; h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
    h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
  }
  const out = new Uint8Array(32);
  const odv = new DataView(out.buffer);
  for (let i = 0; i < 8; i++) odv.setUint32(i * 4, h[i] >>> 0, false);
  return out;
}

function rotr(x, n) {
  return (x >>> n) | (x << (32 - n));
}

function sha256d(bytes) {
  return sha256(sha256(bytes));
}

/* ---- yespower via the CI-built WASM ---- */

async function loadYespower(wasmUrl) {
  const resp = await fetch(wasmUrl);
  const { instance } = await WebAssembly.instantiate(
    await resp.arrayBuffer(), {}
  );
  const ex = instance.exports;
  const memory = ex.memory;
  return function (bytes) {
    const inPtr = ex.alloc(bytes.length);
    const outPtr = ex.alloc(32);
    new Uint8Array(memory.buffer, inPtr, bytes.length).set(bytes);
    ex.yespower_hash(inPtr, bytes.length, outPtr);
    return new Uint8Array(memory.buffer, outPtr, 32).slice();
  };
}

/* ---- comparison + search loop ---- */

function lessThanTarget(digest, target) {
  // both 32-byte big-endian
  for (let i = 0; i < 32; i++) {
    if (digest[i] !== target[i]) return digest[i] < target[i];
  }
  return false; // equal is not strictly less
}

function hexToBytes32(hex) {
  hex = hex.padStart(64, '0');
  const out = new Uint8Array(32);
  for (let i = 0; i < 32; i++) out[i] = parseInt(hex.substr(i * 2, 2), 16);
  return out;
}

const enc = new TextEncoder();

async function run(msg) {
  const { algorithm, token, target, wasmUrl, workerId, batch } = msg;
  const targetBytes = hexToBytes32(target);
  const tokenBytes = enc.encode(token);
  const prefix = workerId + '-';

  let hash;
  if (algorithm === 'sha256d') {
    hash = sha256d;
  } else if (algorithm === 'yespower') {
    hash = await loadYespower(wasmUrl);
  } else {
    postMessage({ type: 'error', error: 'unknown algorithm: ' + algorithm });
    return;
  }

  const reportEvery = batch || 256;
  let counter = 0;
  let sinceReport = 0;
  // Reusable preimage buffer grows as needed.
  for (;;) {
    const sol = prefix + counter;
    const solBytes = enc.encode(sol);
    const preimage = new Uint8Array(tokenBytes.length + solBytes.length);
    preimage.set(tokenBytes);
    preimage.set(solBytes, tokenBytes.length);
    const digest = hash(preimage);
    if (lessThanTarget(digest, targetBytes)) {
      postMessage({ type: 'solved', solution: sol, hashes: counter + 1 });
      return;
    }
    counter++;
    if (++sinceReport >= reportEvery) {
      postMessage({ type: 'progress', hashes: sinceReport });
      sinceReport = 0;
      // Yield so a cancel/terminate can land between batches.
      await Promise.resolve();
    }
  }
}

onmessage = function (e) {
  if (e.data && e.data.type === 'start') {
    run(e.data).catch((err) =>
      postMessage({ type: 'error', error: String(err) })
    );
  }
};
