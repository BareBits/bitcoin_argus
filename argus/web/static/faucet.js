/* Faucet page controller: drives the proof-of-work "earn an extra claim" flow.
 *
 * The one free claim per day submits the form normally (works with JS disabled).
 * Once that free claim is used, this script intercepts the submit, fetches a
 * server-issued challenge bound to the entered address+amount, runs the WASM/JS
 * solver across several Web Workers, shows live progress, and resubmits the form
 * with the solution. All enforcement is server-side; this is purely the UX.
 */

'use strict';

(function () {
  var cfgEl = document.getElementById('faucet-pow');
  if (!cfgEl) return;

  var cfg = {
    enabled: cfgEl.dataset.enabled === 'true',
    algorithm: cfgEl.dataset.algorithm || '',
    freeAvailable: cfgEl.dataset.freeAvailable === 'true',
    maxPerDay: parseInt(cfgEl.dataset.maxPerDay || '0', 10),
    challengeUrl: cfgEl.dataset.challengeUrl,
    wasmUrl: cfgEl.dataset.wasmUrl,
    workerUrl: cfgEl.dataset.workerUrl,
  };
  if (!cfg.enabled) return;

  var form = document.getElementById('faucet-form');
  if (!form) return;
  var addressEl = form.querySelector('[name="address"]');
  var amountEl = form.querySelector('[name="amount"]');
  var tokenEl = form.querySelector('[name="pow_token"]');
  var solutionEl = form.querySelector('[name="pow_solution"]');
  var panel = document.getElementById('faucet-pow-panel');
  var statusEl = document.getElementById('faucet-pow-status');
  var barEl = document.getElementById('faucet-pow-bar');
  var cancelBtn = document.getElementById('faucet-pow-cancel');
  var submitBtn = form.querySelector('button[type="submit"]');

  var workers = [];
  var solving = false;

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }
  function setBar(frac) {
    if (barEl) barEl.style.width = Math.max(0, Math.min(1, frac)) * 100 + '%';
  }

  function stopWorkers() {
    workers.forEach(function (w) { w.terminate(); });
    workers = [];
  }

  function fmtTime(sec) {
    if (!isFinite(sec) || sec < 0) return '…';
    if (sec < 90) return Math.ceil(sec) + 's';
    if (sec < 5400) return Math.round(sec / 60) + 'm';
    return Math.round(sec / 3600) + 'h';
  }

  function endSolving() {
    solving = false;
    stopWorkers();
    if (panel) panel.hidden = true;
    if (submitBtn) submitBtn.disabled = false;
  }

  function startSolving(ch) {
    solving = true;
    if (panel) panel.hidden = false;
    if (submitBtn) submitBtn.disabled = true;
    setBar(0);

    var expected = ch.expected_hashes || 1;
    var done = 0;
    var started = Date.now();
    var nWorkers = Math.max(1, Math.min(navigator.hardwareConcurrency || 4, 8));
    setStatus('Starting proof-of-work (' + nWorkers + ' threads)…');

    function onMessage(e) {
      var m = e.data;
      if (!solving) return;
      if (m.type === 'progress') {
        done += m.hashes;
        var elapsed = (Date.now() - started) / 1000;
        var rate = done / Math.max(0.001, elapsed);
        var remain = (expected - done) / Math.max(1, rate);
        setBar(done / expected);
        setStatus(
          'Working… ' +
          Math.round(rate).toLocaleString() + ' H/s · ~' +
          fmtTime(remain) + ' left (estimate)'
        );
      } else if (m.type === 'solved') {
        setBar(1);
        setStatus('Solved — submitting…');
        tokenEl.value = ch.token;
        solutionEl.value = m.solution;
        solving = false;
        stopWorkers();
        form.submit();
      } else if (m.type === 'error') {
        endSolving();
        setStatus('');
        alert('Proof-of-work failed: ' + m.error);
      }
    }

    for (var i = 0; i < nWorkers; i++) {
      var w = new Worker(cfg.workerUrl);
      w.onmessage = onMessage;
      w.postMessage({
        type: 'start',
        algorithm: ch.algorithm,
        token: ch.token,
        target: ch.target,
        wasmUrl: cfg.wasmUrl,
        workerId: i,
        batch: 256,
      });
      workers.push(w);
    }
  }

  if (cancelBtn) {
    cancelBtn.addEventListener('click', function () {
      endSolving();
      setStatus('');
    });
  }

  form.addEventListener('submit', function (ev) {
    // The free daily claim (or a fresh page state) submits normally.
    if (cfg.freeAvailable) return;
    if (solving) { ev.preventDefault(); return; }
    // Clear any stale proof so a plain fallback submit isn't mis-flagged.
    tokenEl.value = '';
    solutionEl.value = '';

    var address = (addressEl.value || '').trim();
    var amount = (amountEl.value || '').trim();
    if (!address || !amount) return; // let HTML5 required-validation handle it

    ev.preventDefault();
    setStatus('Requesting a challenge…');
    if (panel) panel.hidden = false;
    if (submitBtn) submitBtn.disabled = true;

    var url = cfg.challengeUrl +
      '?address=' + encodeURIComponent(address) +
      '&amount=' + encodeURIComponent(amount);
    fetch(url, { headers: { Accept: 'application/json' } })
      .then(function (r) { return r.json(); })
      .then(function (ch) {
        if (!ch.available) {
          // No PoW available (bad input, or data unavailable): submit plain so
          // the server returns its canonical message.
          endSolving();
          form.submit();
          return;
        }
        startSolving(ch);
      })
      .catch(function () {
        endSolving();
        form.submit(); // fall back to a plain submit on a network error
      });
  });
})();
