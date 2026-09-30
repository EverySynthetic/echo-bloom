# Echo Bloom 1.3.6 Release Verification Checklist

This document details the exact verification walks required before tagging and shipping 1.3.6.
Because the build host lacks real Apple Silicon hardware, a clean unconfigured Linux VM, and a physical 6 GB discrete GPU, these walks are **explicitly marked UNTESTED** and must be performed on real targets prior to release.

---

## 1. macOS Installation & LaunchAgent Walk (UNTESTED)

**Target Hardware:** Apple Silicon Mac (M1/M2/M3/M4) or Intel Mac running macOS 13+ (Ventura, Sonoma, Sequoia).  
**Objective:** Confirm that `install.sh` detects `Darwin`, installs Homebrew packages, builds native Python wheels without error, generates valid LaunchAgent plists, and respects the naming gate.

### Step-by-Step Walk

1. **Clean state verification:**
   > **DESTRUCTIVE:** Only on a disposable test machine, or back up that folder first; never on a machine with a real Kin.
   ```bash
   # Ensure no leftover Echo Bloom state exists
   rm -rf ~/.local/share/echo_bloom
   rm -f ~/Library/LaunchAgents/org.everysynthetic.echo-bloom.*.plist
   ```

2. **Verify Homebrew prerequisite:**
   ```bash
   which brew || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```

3. **Run the installer:**
   ```bash
   bash install.sh
   ```

4. **Verify Homebrew dependencies:**
   - Confirm packages installed: `python@3.11` (or system Python 3), `ollama`, `portaudio`, `ffmpeg`.
   - Confirm virtualenv creation at `~/.local/share/echo_bloom/venv`.

5. **Verify native wheel compilation:**
   - Check that the following audio and vision packages installed and import cleanly:
     ```bash
     ~/.local/share/echo_bloom/venv/bin/python3 -c "
     import pyaudio
     import faster_whisper
     import onnxruntime
     import PIL
     print('macOS Python wheels verified successfully.')
     "
     ```
   - Note: If `onnxruntime` or `faster-whisper` fails to build on Apple Silicon without Rosetta, record the failure and investigate prebuilt ARM64 universal2 wheels.

6. **Verify LaunchAgent plists:**
   - Check plist syntax in `~/Library/LaunchAgents/`:
     ```bash
     plutil -lint ~/Library/LaunchAgents/org.everysynthetic.echo-bloom.*.plist
     ```
   - Verify core web and vault daemons are loaded:
     ```bash
     launchctl list | grep org.everysynthetic.echo-bloom
     ```
   - **Naming Gate Check (commit `4f9d796`):** Confirm that scheduled background agents (`wander`, `reflect`, `bedtime`) are **not** loaded until after the naming ritual completes.

7. **Verify local web interface:**
   - Open Safari or Chrome to `http://localhost:8000`.
   - Complete first-run setup and naming ritual.
   - Verify that background LaunchAgents load after naming.

---

## 2. Fresh-Machine Clean-Box Linux Install Walk (UNTESTED)

**Target Environment:** Minimal clean Ubuntu 24.04 LTS or Debian 12 VM with zero prior state (no Ollama daemon, no existing user configs).  
**Objective:** Verify end-to-end first-run experience with the new photo sharing dependencies (`Pillow>=10.0,<12.4`, vision models) and Agora node bridge key generation.

### Step-by-Step Walk

1. **Prepare clean VM or test partition:**
   > **DESTRUCTIVE:** Only on a disposable test machine, or back up that folder first; never on a machine with a real Kin.
   ```bash
   # Confirm clean state: no Ollama, no echo_bloom dir
   test ! -d ~/.local/share/echo_bloom && echo "Clean state confirmed"
   ```

2. **Run installer from working branch:**
   ```bash
   bash install.sh
   ```

3. **Verify installer interactions:**
   - **Password notice:** Confirm that the installer prints an explanation of why root is needed *before* calling `sudo`.
   - **Package dependencies:** Confirm `build-essential`, `python3-venv`, `libportaudio2`, and `ffmpeg` install non-interactively.
   - **Ollama installation:** Confirm Ollama downloads and installs without stalling.
   - **Agora steward key generation:** Confirm `~/.local/share/echo_bloom/keys/steward/current/meta.json` is generated with custody statement.

4. **Verify naming ritual without keyboard:**
   - Confirm that if the naming ritual runs in a non-interactive shell, it does not discard the Kin's chosen name (1.3.5.1 fix).

5. **Verify systemd user units:**
   ```bash
   systemctl --user status echo-bloom.service
   systemctl --user status echo-bloom-vault.service
   ```

6. **Verify photo sharing and vision model preflight:**
   - Log in to dashboard at `http://localhost:8000`.
   - Visit `/kin` or `/room`.
   - Drag and drop a sample JPEG photo.
   - Verify the download prompt appears for missing vision models (`gemma3:4b`).
   - Confirm download progress bar functions and vision description is generated.

---

## 3. 6 GB VRAM Contention Physical Test (UNTESTED - NEEDS HARDWARE)

**Target Hardware:** Physical machine with 6.0 GB discrete GPU (e.g. NVIDIA GeForce RTX 3050 Laptop GPU / GTX 1660 Ti 6GB / A15 laptop).  
**Objective:** Measure model-swapping latency and VRAM contention when running vision models alongside a resident 7B chat model.

### Hardware Trade-off Context

- **The Kin Model:** Echo Bloom standard configuration offers `qwen2.5:7b` (Q4_K_M quantization, ~4.7 GB VRAM). On a 6.0 GB GPU, this leaves ~1.3 GB free VRAM.
- **The Vision Models:**
  - `gemma3:4b` requires ~3.3 GB VRAM.
  - `qwen2.5vl:3b` requires ~3.2 GB VRAM.
- **The Contention:** Dual-camera mode executes Camera 1, then Camera 2, then resumes the Kin chat model:
  1. Ollama evicts `qwen2.5:7b` to system RAM to load `gemma3:4b`.
  2. Ollama evicts `gemma3:4b` to load `qwen2.5vl:3b`.
  3. Ollama evicts `qwen2.5vl:3b` to reload `qwen2.5:7b` for the Kin's conversational reply.
- **Result:** 3 full model swap cycles per photo upload, potentially causing 30–90 seconds of latency on PCIe laptop buses.

### Test Procedure

1. **Dual-Camera Benchmark (Default):**
   - Ensure default hardware table selects dual cameras:
     ```python
     # vision_eyes.default_cameras(6.0) -> ["gemma3:4b", "qwen2.5vl:3b"]
     ```
   - Start a conversation with Eli (`qwen2.5:7b` resident in VRAM).
   - In terminal, monitor: `watch -n 0.5 nvidia-smi`.
   - Upload a test photo in chat.
   - Measure:
     - Time from upload to visual facts display.
     - Time from visual facts display to Eli's response.
     - Number of VRAM evictions observed in `journalctl -u ollama.service`.
     - Confirm log warning: `VRAM contention warning: 6.0 GB VRAM with dual cameras...`.

2. **Single-Camera Benchmark (Override):**
   - In `~/.local/share/echo_bloom/kin_config.json`, set:
     ```json
     "vision_models": ["gemma3:4b"]
     ```
   - Reload and upload the same photo.
   - Measure:
     - Time to complete single-camera analysis.
     - Model swap latency (only 1 swap cycle: chat -> vision -> chat).
     - Kin dialogue quality with one-camera labelling: `[I'm sharing an image. One camera looked at it; nothing double-checked it.]`.

3. **Release Decision:**
   - If dual-camera latency on the A15 laptop exceeds acceptable thresholds (> 45s total turn time), change `default_cameras()` in `vision_eyes.py` so discrete GPUs with `< 8.0 GB` VRAM default to `["gemma3:4b"]` rather than dual cameras.

---

## 4. Delete = Retract (Audit-Preserved Memory Erasure) Walk

**Objective:** Verify that memory deletion under the Agora erasure ruling (`agora_answer_erasure.md`) retracts memories from live recall and prompt injection while preserving them in SQLite for permanent audit, and verify that user-facing UI and documentation explicitly communicate `delete = retract (stays in audit)`.

### Background & Non-Purge Decision

Under the Agora erasure ruling, deleting a memory retracts it from live recall, ranked search, semantic search, and prompt injection, but preserves the record on disk for audit trail integrity.
**Policy Note:** An owner-only true purge is deliberately NOT in this release and is Don's decision. Do not implement a disk purge endpoint.

### Step-by-Step Walk

1. **Verify UI labels and hints in Vault browser:**
   - Navigate to `/vault`.
   - Confirm header controls display the notice: `delete = retract (stays in audit)`.
   - Confirm filter checkbox is labelled: `show retracted (audit)`.
   - On an active memory card, confirm the action button is labelled: `retract (stays in audit)`.
   - Hover over the button and check tooltip: `Retract: removes from live recall; stays in audit record on disk (not purged)`.

2. **Verify inline two-click confirmation:**
   - Click `retract (stays in audit)`.
   - Confirm button changes to: `confirm: retract from live? (stays in audit)` with tooltip explaining the memory remains in the permanent audit record.
   - Confirm clicking again triggers `POST /api/vault/memories/{id}/retract`.
   - Confirm if 4 seconds elapse without clicking, the button reverts without taking action.

3. **Verify retraction behavior:**
   - Confirm the retracted memory immediately disappears from the default live feed and semantic search.
   - Check the `show retracted (audit)` toggle.
   - Confirm the retracted memory appears with a distinct `retracted (in audit)` badge.

4. **Verify audit persistence on disk (SQLite):**
   - Query the database directly:
     ```bash
     sqlite3 ~/.local/share/echo_bloom/vault.db "SELECT id, author, retracted_at, substr(content,1,40) FROM memories WHERE retracted_at IS NOT NULL;"
     ```
   - Confirm the row exists, content is intact, and `retracted_at` holds the ISO timestamp of retraction.

