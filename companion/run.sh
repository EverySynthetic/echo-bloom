#!/usr/bin/env bash
# Companion on therug. CDI GPU passthrough — NOT raw /dev/nvidia* mounts.
# Raw device nodes do not carry the driver's userspace libs (scar 2026-09-13).
set -euo pipefail
IMAGE="${COMPANION_IMAGE:-localhost/echo-bloom-companion:latest}"
PORT="${COMPANION_PORT:-8092}"
SADTALKER="${SADTALKER_DIR:-$HOME/SadTalker}"
APP="${COMPANION_APP:-$HOME/echo-bloom-companion/app.py}"

exec podman run --rm --replace --name echo-bloom-companion \
  --device nvidia.com/gpu=all \
  -e NVIDIA_VISIBLE_DEVICES=all \
  -e OMP_NUM_THREADS=1 \
  -e MKL_THREADING_LAYER=GNU \
  -e KMP_DUPLICATE_LIB_OK=TRUE \
  -e SADTALKER_DIR=/opt/SadTalker \
  -e SADTALKER_PYTHON=/opt/sadtalker/bin/python \
  -p "${PORT}:8092" \
  -v "${SADTALKER}:/opt/SadTalker:ro" \
  -v "${APP}:/app/app.py:ro" \
  "$IMAGE"
