/**
 * static/js/photo_share.js
 *
 * Echo Bloom photo sharing frontend engine.
 * Shared by templates/kin.html (1-to-1 chat) and templates/room.html (The Room).
 *
 * Contract:
 * - POST /api/photo/describe (multipart: image, kin)
 * - GET  /api/photo/settings
 * - POST /api/photo/settings
 * - POST /api/photo/purge
 * - POST /api/photo/models/pull (SSE stream)
 * - GET  /api/photo/<id>
 *
 * Hard rules:
 * - Never use clipboard read API (fails over LAN plain HTTP).
 * - Never use browser alert or confirm popups; all notices and confirms are inline.
 * - Text paste is completely untouched.
 */

(function () {
  'use strict';

  const MAX_PHOTOS = 4;

  class PhotoShareEngine {
    constructor() {
      this.kinName = 'room';
      this.isRoom = false;
      this.chips = [];
      this.retention = 'none';
      this.keptCount = 0;
      this.dragCounter = 0;
      this.originalSendText = null;

      this.els = {
        chatPanel: null,
        dropOverlay: null,
        chipsContainer: null,
        attachBtn: null,
        fileInput: null,
        retentionSelect: null,
        keptCount: null,
        purgeBtn: null,
        purgeConfirm: null,
        purgeYes: null,
        purgeCancel: null,
        modelsCard: null,
        modelsText: null,
        modelsDlBtn: null,
        modelsProgress: null,
        modelsBar: null,
        modelsStatus: null,
        sendBtn: null,
        input: null,
        inlineNotice: null,
        video: null,
        canvas: null,
      };
    }

    init(options = {}) {
      this.kinName = options.kinName || this.kinName;
      this.isRoom = !!options.isRoom || (this.kinName === 'room');

      // Bind DOM elements
      this.els.chatPanel = options.chatPanel || document.getElementById('chat-panel') || document.querySelector('.chat-panel') || document.querySelector('.wrap') || document.body;
      this.els.dropOverlay = options.dropOverlay || document.getElementById('photo-drop-overlay');
      this.els.chipsContainer = options.chipsContainer || document.getElementById('photo-chips');
      this.els.attachBtn = options.attachBtn || document.getElementById('photo-attach-btn');
      this.els.fileInput = options.fileInput || document.getElementById('photo-file-input');
      this.els.retentionSelect = options.retentionSelect || document.getElementById('photo-retention-select');
      this.els.keptCount = options.keptCount || document.getElementById('photo-kept-count');
      this.els.purgeBtn = options.purgeBtn || document.getElementById('photo-purge-btn');
      this.els.purgeConfirm = options.purgeConfirm || document.getElementById('photo-purge-confirm');
      this.els.purgeYes = options.purgeYes || document.getElementById('photo-purge-yes');
      this.els.purgeCancel = options.purgeCancel || document.getElementById('photo-purge-cancel');
      this.els.modelsCard = options.modelsCard || document.getElementById('photo-models-card');
      this.els.modelsText = options.modelsText || document.getElementById('photo-models-text');
      this.els.modelsDlBtn = options.modelsDlBtn || document.getElementById('photo-models-dl-btn');
      this.els.modelsProgress = options.modelsProgress || document.getElementById('photo-models-progress');
      this.els.modelsBar = options.modelsBar || document.getElementById('photo-models-bar');
      this.els.modelsStatus = options.modelsStatus || document.getElementById('photo-models-status');
      this.els.sendBtn = options.sendBtn || document.getElementById('chat-send-btn') || document.getElementById('room-send');
      this.els.input = options.input || document.getElementById('chat-input') || document.getElementById('room-input');
      this.els.inlineNotice = options.inlineNotice || document.getElementById('photo-inline-notice');
      this.els.video = options.video || document.getElementById('vision-video');
      this.els.canvas = options.canvas || document.getElementById('vision-canvas');

      this.setupDropOverlay();
      this.setupPasteHandler();
      this.setupFilePicker();
      this.setupRetention();
      this.setupModelsCard();
      this.loadSettings();

      return this;
    }

    targetLabel() {
      return this.isRoom ? 'everyone' : this.kinName;
    }

    showNotice(msg, isError = false) {
      if (this.els.inlineNotice) {
        this.els.inlineNotice.textContent = msg;
        this.els.inlineNotice.className = 'photo-inline-notice ' + (isError ? 'error' : 'info');
        this.els.inlineNotice.style.display = 'block';
        clearTimeout(this._noticeTimer);
        this._noticeTimer = setTimeout(() => {
          if (this.els.inlineNotice) this.els.inlineNotice.style.display = 'none';
        }, 5000);
      } else {
        console.warn('[PhotoShare]', msg);
      }
    }

    // ── Drag & Drop ─────────────────────────────────────────────────────────────

    setupDropOverlay() {
      const panel = this.els.chatPanel || document.body;
      const overlay = this.els.dropOverlay;

      if (overlay) {
        const overlayText = overlay.querySelector('.photo-drop-text') || overlay;
        const textMsg = `Drop to show ${this.targetLabel()}`;
        if (overlay.querySelector('.photo-drop-text')) {
          overlay.querySelector('.photo-drop-text').textContent = textMsg;
        } else {
          overlay.textContent = textMsg;
        }
      }

      const hasFiles = (e) => {
        if (!e.dataTransfer || !e.dataTransfer.types) return false;
        const types = Array.from(e.dataTransfer.types);
        return types.includes('Files') || types.includes('application/x-moz-file');
      };

      panel.addEventListener('dragenter', (e) => {
        if (!hasFiles(e)) return;
        this.dragCounter++;
        if (overlay) {
          overlay.style.display = 'flex';
          overlay.classList.add('active');
        }
      });

      panel.addEventListener('dragover', (e) => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        if (e.dataTransfer) e.dataTransfer.dropEffect = 'copy';
      });

      panel.addEventListener('dragleave', (e) => {
        if (!hasFiles(e)) return;
        this.dragCounter--;
        if (this.dragCounter <= 0) {
          this.dragCounter = 0;
          if (overlay) {
            overlay.style.display = 'none';
            overlay.classList.remove('active');
          }
        }
      });

      panel.addEventListener('drop', (e) => {
        this.dragCounter = 0;
        if (overlay) {
          overlay.style.display = 'none';
          overlay.classList.remove('active');
        }
        if (!e.dataTransfer) return;

        const files = Array.from(e.dataTransfer.files || []);
        const imageFiles = files.filter(f => f.type && f.type.startsWith('image/'));
        if (imageFiles.length > 0) {
          e.preventDefault();
          imageFiles.forEach(f => this.handleImageFile(f));
        }
      });
    }

    // ── Paste Event (Ctrl+V, Ctrl+Shift+V, Right-Click Paste) ───────────────────

    setupPasteHandler() {
      // Must listen globally so paste anywhere on the chat page works
      document.addEventListener('paste', (e) => {
        const cd = e.clipboardData;
        if (!cd) return;

        const imageFiles = [];

        // Check clipboardData.files
        if (cd.files && cd.files.length) {
          for (let i = 0; i < cd.files.length; i++) {
            const f = cd.files[i];
            if (f.type && f.type.startsWith('image/')) {
              imageFiles.push(f);
            }
          }
        }

        // If no files found directly, check clipboardData.items
        if (imageFiles.length === 0 && cd.items && cd.items.length) {
          for (let i = 0; i < cd.items.length; i++) {
            const item = cd.items[i];
            if (item.type && item.type.startsWith('image/')) {
              const f = item.getAsFile();
              if (f) imageFiles.push(f);
            }
          }
        }

        // Only preventDefault if image items were found! Plain text paste is untouched.
        if (imageFiles.length > 0) {
          e.preventDefault();
          imageFiles.forEach(f => this.handleImageFile(f));
        }
      });
    }

    // ── File Picker (📎 button) ──────────────────────────────────────────────────

    setupFilePicker() {
      if (this.els.attachBtn && this.els.fileInput) {
        this.els.attachBtn.addEventListener('click', (e) => {
          e.preventDefault();
          this.els.fileInput.click();
        });

        this.els.fileInput.addEventListener('change', () => {
          const files = Array.from(this.els.fileInput.files || []);
          files.forEach(f => this.handleImageFile(f));
          this.els.fileInput.value = '';
        });
      }
    }

    // ── Webcam Button (◉) ────────────────────────────────────────────────────────

    async captureWebcam(triggerBtn) {
      const btn = triggerBtn || document.getElementById('cam-btn') || document.getElementById('room-cam-btn');
      const video = this.els.video || document.getElementById('vision-video');
      const canvas = this.els.canvas || document.getElementById('vision-canvas');

      if (!video || !canvas) {
        this.showNotice('Webcam elements not found on page.', true);
        return;
      }

      const origText = btn ? btn.textContent : '';
      if (btn) {
        btn.textContent = '…';
        btn.disabled = true;
      }

      try {
        const stream = await navigator.mediaDevices.getUserMedia({ video: true });
        video.srcObject = stream;
        await new Promise(resolve => { video.onloadedmetadata = resolve; });
        await new Promise(resolve => setTimeout(resolve, 600)); // warm up

        canvas.width = video.videoWidth || 640;
        canvas.height = video.videoHeight || 480;
        const ctx = canvas.getContext('2d');
        ctx.drawImage(video, 0, 0);

        stream.getTracks().forEach(t => t.stop());
        video.srcObject = null;

        canvas.toBlob((blob) => {
          if (!blob) {
            this.showNotice('Could not capture photo from webcam.', true);
            return;
          }
          const file = new File([blob], `webcam-${Date.now()}.jpg`, { type: 'image/jpeg' });
          this.handleImageFile(file);
        }, 'image/jpeg', 0.9);

      } catch (err) {
        this.showNotice(`Camera error: ${err.message}`, true);
      } finally {
        if (btn) {
          btn.textContent = origText || '◉';
          btn.disabled = false;
        }
      }
    }

    // ── Unified Image File Intake ────────────────────────────────────────────────

    handleImageFile(file) {
      if (!file) return;

      if (!file.type || !file.type.startsWith('image/')) {
        this.showNotice('Only images (PNG, JPEG, WebP, GIF, BMP) are supported.', true);
        return;
      }

      if (this.chips.length >= MAX_PHOTOS) {
        this.showNotice(`Up to ${MAX_PHOTOS} photos per message.`, true);
        return;
      }

      const chipId = 'chip_' + Math.random().toString(36).substring(2, 9);
      let objectUrl = '';
      try {
        objectUrl = URL.createObjectURL(file);
      } catch (e) {
        console.error('URL.createObjectURL failed', e);
      }

      const chip = {
        id: chipId,
        file: file,
        objectUrl: objectUrl,
        status: 'looking',
        data: null,
        error: null,
        el: null,
      };

      this.chips.push(chip);
      this.renderChip(chip);
      this.updateSendGating();

      // Trigger server describe
      this.describeChip(chip);
    }

    // ── Chip Rendering & Lifecycle ───────────────────────────────────────────────

    renderChip(chip) {
      if (!this.els.chipsContainer) return;

      const div = document.createElement('div');
      div.className = 'photo-chip';
      div.id = chip.id;
      div.setAttribute('role', 'status');
      div.setAttribute('aria-live', 'polite');

      div.innerHTML = `
        <div class="photo-chip-thumb-wrap">
          <img src="${chip.objectUrl}" alt="Photo thumbnail" class="photo-chip-thumb" />
          <button type="button" class="photo-chip-remove" aria-label="Remove photo" title="Remove photo">✕</button>
        </div>
        <div class="photo-chip-content">
          <div class="photo-chip-status-line">
            <span class="photo-chip-status looking">
              <span class="photo-chip-spinner" aria-hidden="true"></span>
              <span class="photo-chip-status-text">looking…</span>
            </span>
          </div>
          <details class="photo-chip-details" style="display:none">
            <summary class="photo-chip-summary">What ${this.targetLabel()} will be told</summary>
            <div class="photo-chip-for-kin"></div>
            <div class="photo-chip-eyes"></div>
          </details>
        </div>
      `;

      div.querySelector('.photo-chip-remove').addEventListener('click', (e) => {
        e.preventDefault();
        this.removeChip(chip.id);
      });

      chip.el = div;
      this.els.chipsContainer.appendChild(div);
      this.els.chipsContainer.style.display = 'flex';
    }

    updateChipUI(chip) {
      if (!chip.el) return;

      const statusLine = chip.el.querySelector('.photo-chip-status-line');
      const details = chip.el.querySelector('.photo-chip-details');
      const forKinEl = chip.el.querySelector('.photo-chip-for-kin');
      const eyesEl = chip.el.querySelector('.photo-chip-eyes');

      if (chip.status === 'looking') {
        statusLine.innerHTML = `
          <span class="photo-chip-status looking">
            <span class="photo-chip-spinner" aria-hidden="true"></span>
            <span class="photo-chip-status-text">looking…</span>
          </span>
        `;
        if (details) details.style.display = 'none';
      } else if (chip.status === 'ready' && chip.data) {
        statusLine.innerHTML = `<span class="photo-chip-status ready">ready</span>`;
        if (forKinEl) forKinEl.textContent = chip.data.for_kin || '';

        if (eyesEl) {
          eyesEl.innerHTML = '';
          if (chip.data.eyes && Array.isArray(chip.data.eyes) && chip.data.eyes.length > 0) {
            const rawTitle = document.createElement('div');
            rawTitle.className = 'photo-chip-eyes-title';
            rawTitle.textContent = 'Cameras raw observations:';
            eyesEl.appendChild(rawTitle);

            chip.data.eyes.forEach(eye => {
              const eyeDiv = document.createElement('div');
              eyeDiv.className = 'photo-chip-eye-item';
              eyeDiv.innerHTML = `<strong>${this.escapeHtml(eye.model || 'camera')}</strong>: <span>${this.escapeHtml(eye.text || '')}</span>`;
              eyesEl.appendChild(eyeDiv);
            });
          }
        }
        if (details) details.style.display = 'block';
      } else if (chip.status === 'needs_models') {
        statusLine.innerHTML = `<span class="photo-chip-status needs-models">needs vision model</span>`;
        if (details) details.style.display = 'none';
      } else if (chip.status === 'error') {
        statusLine.innerHTML = `<span class="photo-chip-status error">${this.escapeHtml(chip.error || 'Failed')}</span>`;
        if (details) details.style.display = 'none';
      }
    }

    removeChip(chipId) {
      const idx = this.chips.findIndex(c => c.id === chipId);
      if (idx !== -1) {
        const chip = this.chips[idx];
        if (chip.objectUrl) {
          try { URL.revokeObjectURL(chip.objectUrl); } catch (e) {}
        }
        if (chip.el && chip.el.parentNode) {
          chip.el.parentNode.removeChild(chip.el);
        }
        this.chips.splice(idx, 1);
      }

      if (this.chips.length === 0 && this.els.chipsContainer) {
        this.els.chipsContainer.style.display = 'none';
      }

      this.updateSendGating();
    }

    clearChips() {
      for (const chip of this.chips) {
        if (chip.objectUrl) {
          try { URL.revokeObjectURL(chip.objectUrl); } catch (e) {}
        }
      }
      this.chips = [];
      if (this.els.chipsContainer) {
        this.els.chipsContainer.innerHTML = '';
        this.els.chipsContainer.style.display = 'none';
      }
      this.updateSendGating();
    }

    async describeChip(chip) {
      // The first photo loads two vision models one after the other: about a
      // minute and a half on a 6 GB card (walked on the A15). Say so, rather
      // than leave "looking…" to look stuck.
      const slowHint = setTimeout(() => {
        const t = chip.el && chip.el.querySelector('.photo-chip-status-text');
        if (chip.status === 'looking' && t) t.textContent = 'looking… the first photo can take a minute or two';
      }, 15000);
      try {
        // The file itself as the body (not FormData): the server streams it
        // against the size cap and never spools it to disk.
        const resp = await fetch('/api/photo/describe?kin=' + encodeURIComponent(this.kinName), {
          method: 'POST',
          headers: {'Content-Type': chip.file.type || 'application/octet-stream'},
          body: chip.file,
        });

        const data = await resp.json().catch(() => ({}));

        if (data.ok) {
          chip.status = 'ready';
          chip.data = data;
        } else if (data.needs_models && Array.isArray(data.needs_models)) {
          chip.status = 'needs_models';
          chip.error = data.error || 'Vision models missing';
          this.showModelsCard(data.needs_models);
        } else {
          chip.status = 'error';
          chip.error = data.error || 'Could not describe photo';
        }
      } catch (err) {
        chip.status = 'error';
        chip.error = err.message || 'Network error';
      } finally {
        clearTimeout(slowHint);
        this.updateChipUI(chip);
        this.updateSendGating();
      }
    }

    // ── Send Button Gating ───────────────────────────────────────────────────────

    updateSendGating() {
      const btn = this.els.sendBtn;
      if (!btn) return;

      const hasLooking = this.chips.some(c => c.status === 'looking');

      if (hasLooking) {
        if (!this.originalSendText) {
          this.originalSendText = btn.textContent;
        }
        btn.disabled = true;
        btn.textContent = 'LOOKING…';
      } else {
        if (this.originalSendText) {
          btn.textContent = this.originalSendText;
          this.originalSendText = null;
        }
        // Only re-enable if chat input isn't locked by assistant reply
        const inputDisabled = this.els.input && this.els.input.disabled;
        if (!inputDisabled) {
          btn.disabled = false;
        }
      }
    }

    hasLookingChips() {
      return this.chips.some(c => c.status === 'looking');
    }

    getReadyChips() {
      return this.chips.filter(c => c.status === 'ready' && c.data);
    }

    // ── Outgoing Message Assembly ───────────────────────────────────────────────

    assembleMessage(userTypedText) {
      const ready = this.getReadyChips();
      const text = (userTypedText || '').trim();

      if (ready.length === 0 && !text) {
        return null;
      }

      const blocks = ready.map(c => c.data.for_kin).filter(Boolean);
      let fullMessage = '';

      if (blocks.length > 0) {
        fullMessage = blocks.join('\n\n');
        if (text) {
          fullMessage += '\n' + text;
        }
      } else {
        fullMessage = text;
      }

      const photosMeta = ready.map(c => ({
        id: c.data.id || null,
        thumb_url: c.data.kept ? c.data.thumb_url : null,
        kept: !!c.data.kept,
        for_kin: c.data.for_kin || '',
        local_thumb: c.objectUrl || null,
      }));

      return {
        fullMessage,
        photosMeta,
        readyChips: ready,
      };
    }

    // ── Retention Control (/api/photo/settings & /api/photo/purge) ───────────────

    setupRetention() {
      const sel = this.els.retentionSelect;
      const purgeBtn = this.els.purgeBtn;
      const purgeConfirm = this.els.purgeConfirm;
      const purgeYes = this.els.purgeYes;
      const purgeCancel = this.els.purgeCancel;

      if (sel) {
        sel.addEventListener('change', async () => {
          const val = sel.value;
          try {
            await fetch('/api/photo/settings', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ retention: val }),
            });
            this.retention = val;
          } catch (e) {
            this.showNotice('Could not save retention setting.', true);
          }
        });
      }

      if (purgeBtn && purgeConfirm) {
        purgeBtn.addEventListener('click', (e) => {
          e.preventDefault();
          purgeConfirm.style.display = 'inline-flex';
          purgeBtn.style.display = 'none';
        });

        if (purgeCancel) {
          purgeCancel.addEventListener('click', (e) => {
            e.preventDefault();
            purgeConfirm.style.display = 'none';
            purgeBtn.style.display = 'inline-block';
          });
        }

        if (purgeYes) {
          purgeYes.addEventListener('click', async (e) => {
            e.preventDefault();
            purgeYes.disabled = true;
            try {
              const r = await fetch('/api/photo/purge', { method: 'POST' });
              const d = await r.json().catch(() => ({}));
              this.keptCount = 0;
              this.updateKeptCountDisplay();
              this.showNotice(`Deleted ${d.deleted || 0} kept photos.`);
            } catch (err) {
              this.showNotice(`Failed to purge photos: ${err.message}`, true);
            } finally {
              purgeYes.disabled = false;
              purgeConfirm.style.display = 'none';
              purgeBtn.style.display = 'inline-block';
            }
          });
        }
      }
    }

    async loadSettings() {
      try {
        const resp = await fetch('/api/photo/settings');
        if (!resp.ok) return;
        const data = await resp.json();
        this.retention = data.retention || 'none';
        this.keptCount = typeof data.kept_count === 'number' ? data.kept_count : 0;

        if (this.els.retentionSelect) {
          this.els.retentionSelect.value = this.retention;
        }
        this.updateKeptCountDisplay();
      } catch (e) {
        // Silent catch on load
      }
    }

    updateKeptCountDisplay() {
      if (this.els.keptCount) {
        this.els.keptCount.textContent = `${this.keptCount} kept`;
      }
    }

    // ── Missing Models Card & SSE Stream ────────────────────────────────────────

    setupModelsCard() {
      if (this.els.modelsDlBtn) {
        this.els.modelsDlBtn.addEventListener('click', (e) => {
          e.preventDefault();
          this.startModelsDownload();
        });
      }
    }

    showModelsCard(needsModels) {
      if (!this.els.modelsCard) return;

      const totalGb = needsModels.reduce((acc, m) => acc + (m.size_gb || 0), 0);
      const sizeStr = totalGb > 0 ? `${totalGb.toFixed(1)} GB` : 'a few GB';

      if (this.els.modelsText) {
        this.els.modelsText.textContent = `Photos need a vision model (about ${sizeStr}). Download now?`;
      }
      if (this.els.modelsDlBtn) {
        this.els.modelsDlBtn.style.display = 'inline-block';
        this.els.modelsDlBtn.disabled = false;
      }
      if (this.els.modelsProgress) {
        this.els.modelsProgress.style.display = 'none';
      }

      this.els.modelsCard.style.display = 'block';
    }

    async startModelsDownload() {
      if (this.els.modelsDlBtn) this.els.modelsDlBtn.style.display = 'none';
      if (this.els.modelsProgress) this.els.modelsProgress.style.display = 'block';
      if (this.els.modelsBar) this.els.modelsBar.style.width = '0%';
      if (this.els.modelsStatus) this.els.modelsStatus.textContent = 'Connecting…';

      try {
        const resp = await fetch('/api/photo/models/pull', { method: 'POST' });
        if (!resp.ok) {
          throw new Error(`Download request failed (${resp.status})`);
        }

        const reader = resp.body.getReader();
        const dec = new TextDecoder();
        let buf = '';

        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split('\n');
          buf = lines.pop();

          for (const line of lines) {
            if (!line.startsWith('data: ')) continue;
            const payload = line.slice(6).trim();
            if (!payload || payload === '[DONE]') continue;
            let ev;
            try { ev = JSON.parse(payload); } catch (e) { continue; }

            // A pull that fails must not end in "downloaded!" and a full bar.
            if (ev.error) throw new Error(ev.error);
            if (ev.percent != null && this.els.modelsBar) {
              this.els.modelsBar.style.width = `${ev.percent}%`;
            }
            if (this.els.modelsStatus && ev.model) {
              const pct = ev.percent != null ? `${ev.percent}%` : '';
              const step = ev.steps > 1 ? ` (${ev.step} of ${ev.steps})` : '';
              this.els.modelsStatus.textContent = `Downloading ${ev.model}${step} ${pct}`.trim();
            }
          }
        }

        if (this.els.modelsStatus) this.els.modelsStatus.textContent = 'Vision models downloaded!';
        if (this.els.modelsBar) this.els.modelsBar.style.width = '100%';

        setTimeout(() => {
          if (this.els.modelsCard) this.els.modelsCard.style.display = 'none';
          // Re-describe any chips that were paused waiting for models
          const pausedChips = this.chips.filter(c => c.status === 'needs_models');
          pausedChips.forEach(c => {
            c.status = 'looking';
            this.updateChipUI(c);
            this.describeChip(c);
          });
        }, 1500);

      } catch (err) {
        if (this.els.modelsStatus) this.els.modelsStatus.textContent = `Download failed: ${err.message}`;
        if (this.els.modelsDlBtn) {
          this.els.modelsDlBtn.style.display = 'inline-block';
          this.els.modelsDlBtn.disabled = false;
        }
      }
    }

    // ── Chat Bubble Rendering Helper ────────────────────────────────────────────

    renderBubbleContent(bodyEl, fullContent, photosMeta = null) {
      if (!bodyEl) return;
      bodyEl.innerHTML = '';

      // Regex to parse [Don shared a photo...] ... [/photo]
      const photoRegex = /\[([^\]]*shared a photo[^\]]*)\]([\s\S]*?)\[\/photo\]/g;
      const photoBlocks = [];
      let match;
      let lastIndex = 0;
      let remainingText = fullContent || '';

      while ((match = photoRegex.exec(fullContent)) !== null) {
        photoBlocks.push({
          header: match[1].trim(),
          desc: match[2].trim(),
        });
      }

      // If photo blocks were found, strip them from the displayed user text
      if (photoBlocks.length > 0) {
        remainingText = fullContent.replace(photoRegex, '').trim();
      }

      // Render photo blocks
      if (photoBlocks.length > 0 || (photosMeta && photosMeta.length > 0)) {
        const wrap = document.createElement('div');
        wrap.className = 'chat-photos-wrap';

        const count = Math.max(photoBlocks.length, photosMeta ? photosMeta.length : 0);
        for (let i = 0; i < count; i++) {
          const pb = photoBlocks[i] || { header: 'Photo shared', desc: '' };
          const meta = (photosMeta && photosMeta[i]) ? photosMeta[i] : null;

          const item = document.createElement('div');
          item.className = 'chat-photo-item';

          // Thumbnail or "photo (not kept)"
          const thumbUrl = (meta && meta.thumb_url) ? meta.thumb_url : (meta && meta.local_thumb ? meta.local_thumb : null);
          const wasKept = meta ? meta.kept : (thumbUrl != null);

          if (thumbUrl && wasKept) {
            const img = document.createElement('img');
            img.src = thumbUrl;
            img.alt = 'Shared photo';
            img.className = 'chat-photo-thumb';
            item.appendChild(img);
          } else {
            const notKept = document.createElement('div');
            notKept.className = 'chat-photo-not-kept';
            notKept.textContent = '📷 photo (not kept)';
            item.appendChild(notKept);
          }

          // Collapsible "What <target> was told"
          const details = document.createElement('details');
          details.className = 'chat-photo-desc';
          details.innerHTML = `
            <summary class="chat-photo-desc-summary">What ${this.targetLabel()} was told</summary>
            <div class="chat-photo-desc-body">${this.escapeHtml(pb.desc || (meta ? meta.for_kin : ''))}</div>
          `;
          item.appendChild(details);
          wrap.appendChild(item);
        }
        bodyEl.appendChild(wrap);
      }

      // Render remaining user-typed text
      if (remainingText) {
        const userDiv = document.createElement('div');
        userDiv.className = 'chat-user-text';
        userDiv.textContent = remainingText;
        bodyEl.appendChild(userDiv);
      }
    }

    escapeHtml(s) {
      if (!s) return '';
      return String(s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
    }
  }

  // Singleton instance available globally
  window.PhotoShare = new PhotoShareEngine();

})();
