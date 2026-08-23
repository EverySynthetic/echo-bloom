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

/* ── Help agent (product docs, not a Kin) ──────────────────────────────────
 * Lives in the nav so it is available on every authenticated page, including
 * SETUP — that is the screen people are looking at when they need it.
 * Separate busy-lock and presence name on the server; this client lock only
 * stops a double-click from firing two requests.
 */
(function () {
  const openBtn = document.getElementById('help-open');
  const backdrop = document.getElementById('help-backdrop');
  const closeBtn = document.getElementById('help-close');
  const askBtn = document.getElementById('help-ask');
  const questionEl = document.getElementById('help-question');
  const warnEl = document.getElementById('help-warn');
  const errEl = document.getElementById('help-err');
  const metaEl = document.getElementById('help-meta');
  const resultEl = document.getElementById('help-result');
  if (!openBtn || !backdrop || !askBtn || !questionEl) return;

  let asking = false;

  function setHidden(el, on) {
    if (!el) return;
    el.hidden = !!on;
  }

  function showErr(msg) {
    errEl.textContent = msg || '';
    setHidden(errEl, !msg);
  }

  function showWarn(msg) {
    warnEl.textContent = msg || '';
    setHidden(warnEl, !msg);
  }

  function openHelp() {
    backdrop.hidden = false;
    openBtn.setAttribute('aria-expanded', 'true');
    questionEl.focus();
  }

  function closeHelp() {
    backdrop.hidden = true;
    openBtn.setAttribute('aria-expanded', 'false');
    openBtn.focus();
  }

  async function askHelp() {
    const question = (questionEl.value || '').trim();
    showErr('');
    if (!question) {
      showErr('Empty question');
      return;
    }
    if (asking) return;
    asking = true;
    askBtn.disabled = true;
    setHidden(resultEl, true);
    setHidden(metaEl, true);
    showWarn('');
    metaEl.textContent = 'Working...';
    setHidden(metaEl, false);
    try {
      const d = await ebFetch('/api/agent/help', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question: question }),
      });
      if (d.warning) showWarn(d.warning);
      if (!d.ok) {
        showErr(d.error || (d.busy ? 'Already answering another help question.' : 'Help failed'));
        setHidden(metaEl, true);
        return;
      }
      resultEl.textContent = d.result || '';
      setHidden(resultEl, false);
      const bits = ['Not a Kin'];
      if (d.model) bits.push(d.model);
      metaEl.textContent = bits.join(' · ');
      setHidden(metaEl, false);
    } catch (e) {
      showErr(e.message || String(e));
      setHidden(metaEl, true);
    } finally {
      asking = false;
      askBtn.disabled = false;
    }
  }

  openBtn.addEventListener('click', openHelp);
  if (closeBtn) closeBtn.addEventListener('click', closeHelp);
  askBtn.addEventListener('click', askHelp);
  backdrop.addEventListener('click', function (e) {
    if (e.target === backdrop) closeHelp();
  });
  document.addEventListener('keydown', function (e) {
    if (backdrop.hidden) return;
    if (e.key === 'Escape') {
      e.preventDefault();
      closeHelp();
    }
  });
  questionEl.addEventListener('keydown', function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      askHelp();
    }
  });
  if (window.location.hash === '#help') openHelp();
})();
