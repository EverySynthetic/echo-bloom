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
