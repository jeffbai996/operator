/* Latest-frame delivery primitives, shared by the cockpit and contract tests. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.OperatorFrameFeed = api;
})(typeof globalThis === 'object' ? globalThis : this, function () {
  'use strict';
  // Application payload budget, with one complete JPEG allowed as the burst.
  // Headers/TLS overhead and other Operator endpoints are outside this budget.
  const BYTES_PER_SECOND = Object.freeze({hi: 2048 * 1024, lo: 1024 * 1024, eco: 96 * 1024});
  function delay({tier = 'hi', elapsed = 0, bytes = 0, quiet = false, typing = false}) {
    const interval = quiet ? 900 : typing ? 400 : tier === 'eco' ? 260 : tier === 'lo' ? 1000 / 15 : 1000 / 25;
    const byteInterval = bytes * 1000 / (BYTES_PER_SECOND[tier] || BYTES_PER_SECOND.hi);
    return Math.ceil(Math.max(0, Math.max(interval, byteInterval) - elapsed));
  }
  function bounded(promise, timeoutMs = 3000) {
    let timer;
    const deadline = new Promise((_, reject) => { timer = setTimeout(() => reject(Error('frame deadline')), timeoutMs); });
    return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
  }
  async function read(url, {fetchImpl = fetch, timeoutMs = 8000} = {}) {
    const startedAt = performance.now();
    const controller = new AbortController();
    let timer;
    const deadline = new Promise((_, reject) => {
      timer = setTimeout(() => {
        reject(Error('frame transfer deadline'));
        controller.abort();
      }, timeoutMs);
    });
    try {
      return await Promise.race([deadline, (async () => {
        const response = await fetchImpl(url, {cache: 'no-store', signal: controller.signal});
        if (!response.ok) throw Error('http ' + response.status);
        const headersAt = performance.now();
        const bodyStarted = headersAt;
        const blob = response.status === 204 ? null : await response.blob();
        return {response, blob, startedAt, headersAt, bodyMs: performance.now() - bodyStarted};
      })()]);
    } finally { clearTimeout(timer); }
  }
  const timingSamples = [];
  function presented(transfer, {paintAt, decodedAt, bytes}) {
    const h = transfer.response.headers;
    const requestAt = Number(h.get('X-Operator-Request-At')) * 1000;
    const responseAt = Number(h.get('X-Operator-Response-At')) * 1000;
    const capturedAt = Number(h.get('X-Operator-Capture-Started')) * 1000;
    const finishedAt = Number(h.get('X-Operator-Capture-Finished')) * 1000;
    if (!(capturedAt > 0 && finishedAt >= capturedAt && responseAt >= finishedAt)) return null;
    // The network's clock-offset uncertainty is retained as bounds, not hidden as an exact age.
    const offsetLow = responseAt - transfer.headersAt;
    const offsetHigh = requestAt - transfer.startedAt;
    if (offsetLow > offsetHigh + 1) return null;
    const sample = {captureMs: finishedAt - capturedAt, bodyMs: transfer.bodyMs,
      decodeMs: decodedAt - transfer.headersAt - transfer.bodyMs,
      paintMs: paintAt - decodedAt, bytes,
      captureToPaintMinMs: Math.max(0, paintAt - capturedAt + offsetLow),
      captureToPaintMaxMs: Math.max(0, paintAt - capturedAt + offsetHigh)};
    if (!Object.values(sample).every(v => Number.isFinite(v) && v >= 0)) return null;
    timingSamples.push(Object.freeze(sample));
    if (timingSamples.length > 60) timingSamples.shift();
    return sample;
  }
  function samples() { return timingSamples.slice(); }
  return {BYTES_PER_SECOND, delay, bounded, read, presented, samples};
});
