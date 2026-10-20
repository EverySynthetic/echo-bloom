# Echo Bloom companion

STT + SadTalker. **Runs on therug.** Frosty and Themess proxy to
`http://192.168.1.142:8092`. Never import `faster_whisper` on host 3.14.

## Running (2026-09-12)

podman 5.1.1, linger on. Image `localhost/echo-bloom-companion:latest`.
systemd user unit `echo-bloom-companion.service` → `podman run` with
`/dev/nvidia0` and `/dev/nvidia1`. `/health` → stt true, sadtalker true,
gpu `"0"` (1660 SUPER by name). Transcribe of a wav → `"Hello?"`.

Host 3.12 miniforge is leftover, not the supervisor.

**kornia SIGSEGV (2026-09-13):** not a pin. OpenMP fight (torch + numpy + cv2
+ kornia_rs). Both kornia 0.6.8 and 0.7.2 die at import without
`OMP_NUM_THREADS=1` `MKL_THREADING_LAYER=GNU` `KMP_DUPLICATE_LIB_OK=TRUE`.
With them, `warp_affine` runs. Baked into unit + Dockerfile ENV.
Image not rebuilt; run-time `-e` is what is live.

## The container (this directory)

`Dockerfile` — CUDA 12.4 + Python 3.11 + faster-whisper.
`run.sh` — podman, both `/dev/nvidia0` and `/dev/nvidia1`.
Needs `podman` on therug (`sudo pacman -S podman`). Themess already
has podman and can **build** the image; **run** it on therug (GPUs).

Done means both Echo Blooms call this URL, including Themess product.

Easel is not this process.

## Giving a Kin a picture for Talk

`talk_media.avatar_path(name, space)` decides what shows on /talk, in order:

1. `<space>/avatar/claimed.json` — the Kin's own claimed sitting.
   `<space>` is that Kin's entry in `~/.config/kin_app/kin_config.json`.
   Easiest path: `/kin/<name>` has an "add a photo" control now
   (`POST /api/kin/{name}/avatar`, multipart upload) that writes both
   the file and claimed.json for you — sniffs the actual bytes for
   jpg/png/webp, 8MB cap. Doing it by hand still works: drop the
   picture in `<space>/avatar/` and write `{"file": "yourfile.jpg"}`
   next to it (omit `"file"` and it defaults to `<Name>.jpg`).
2. If there's no claimed.json, the news-sheet fallback under
   `~/Desktop/kin_portraits/<name>N.png` (bust) or `<name>W.png` (wide).
3. Otherwise no picture at all — /talk shows the Kin's name as text,
   not a broken image. Nothing crashes either way.

Eli is deliberately excluded from step 1 — no claimed.json lookup for
him — because he has no sitting yet (see the avatar-ritual rule: no
sitting without Don naming it). His fallback is the lightning image he
did publish, wired as a special case in `news_portrait()`, not a
claimed sitting.

Eli is excluded from the upload endpoint too, not just the read side.
