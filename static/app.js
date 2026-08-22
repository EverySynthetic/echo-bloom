/* app.js — minimal global helpers */

/* ── Shared API failure handling ──────────────────────────────────────────────
 * Every /api/* route returns 401 JSON when the session expires and 402 JSON
 * when the licence lapses. None of the stream readers checked resp.ok, so both
 * cases produced a silently empty reply, a progress bar stuck forever, or a
 * "BUILD FAILED" with an empty log — with no way for the user to know why.
 *
 * ebFetch is the default for JSON calls: non-2xx throws with .status set, so
 * a 402 cannot render as an empty vault / empty scan / "no tunnel". New fetch
 * sites should use ebFetch (JSON) or ebCheck (streams), not raw fetch.
 */
window.ebReason = function (body) {
  if (!body || typeof body !== 'object') return null;
  if (typeof body.reason === 'string' && body.reason.trim()) return body.reason.trim();
  const d = body.detail;
  if (typeof d === 'string' && d.trim() && d !== 'license required') return d.trim();
  if (d && typeof d === 'object') {
    if (typeof d.reason === 'string' && d.reason.trim()) return d.reason.trim();
    if (typeof d.message === 'string' && d.message.trim()) return d.message.trim();
  }
  if (typeof body.error === 'string' && body.error.trim()) return body.error.trim();
  if (typeof body.message === 'string' && body.message.trim()) return body.message.trim();
  return null;
};

window.ebApiError = function (resp, body) {
  if (resp.ok) return null;
  if (resp.status === 401) return 'Your session expired. Reload the page and log in again.';
  if (resp.status === 402) {
    // 402 is every blocking licence state, not only an ended trial.
    // unverifiable (key on disk, cryptography missing) used to be mapped
    // to "your trial has ended" and told a paying customer to pay again.
    const reason = window.ebReason(body);
    const tail = ' Your memories are still on this machine. Visit the License page.';
    if (reason) return reason + tail;
    return 'This install is not currently entitled to run.' + tail;
  }
  if (resp.status === 404) return 'That endpoint is missing — the app may need updating.';
  return `The server returned an error (HTTP ${resp.status}).`;
};

window.ebCheck = function (resp, body) {
  const msg = window.ebApiError(resp, body);
  if (msg) {
    const err = new Error(msg);
    err.status = resp.status;
    err.body = body || null;
    throw err;
  }
  return resp;
};

window.ebFetch = async function (url, opts) {
  const r = await fetch(url, opts);
  const ct = r.headers.get('content-type') || '';
  const isJson = ct.indexOf('application/json') !== -1;
  let body = null;
  if (isJson) {
    body = await r.json().catch(() => null);
  }
  window.ebCheck(r, body);
  return isJson ? body : r;
};

// Auto-refresh dashboard cluster status every 60 seconds
if (document.querySelector('.dashboard')) {
  setInterval(async () => {
    try {
      const data = await ebFetch('/api/cluster');
      for (const node of data.nodes) {
        const cards = document.querySelectorAll('.node-card');
        for (const card of cards) {
          if (card.querySelector('.node-name')?.textContent === node.name) {
            card.classList.toggle('node-up',   node.up);
            card.classList.toggle('node-down', !node.up);
          }
        }
      }
    } catch(e) {}
  }, 60000);
}
