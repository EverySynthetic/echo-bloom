/* app.js — minimal global helpers */

// Auto-refresh dashboard cluster status every 60 seconds
if (document.querySelector('.dashboard')) {
  setInterval(async () => {
    try {
      const r    = await fetch('/api/cluster');
      const data = await r.json();
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

/* ── Shared API failure handling ──────────────────────────────────────────────
 * Every /api/* route returns 401 JSON when the session expires and 402 JSON
 * when the licence lapses. None of the stream readers checked resp.ok, so both
 * cases produced a silently empty reply, a progress bar stuck forever, or a
 * "BUILD FAILED" with an empty log — with no way for the user to know why.
 */
window.ebApiError = function (resp) {
  if (resp.ok) return null;
  if (resp.status === 401) return 'Your session expired. Reload the page and log in again.';
  if (resp.status === 402) return 'Your trial has ended. Visit the License page to enter a key.';
  if (resp.status === 404) return 'That endpoint is missing — the app may need updating.';
  return `The server returned an error (HTTP ${resp.status}).`;
};

/* Throw on a failed response so a caller's existing catch() surfaces it. */
window.ebCheck = function (resp) {
  const msg = window.ebApiError(resp);
  if (msg) throw new Error(msg);
  return resp;
};
