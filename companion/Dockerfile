# Echo Bloom companion — STT + SadTalker. Python 3.11, never host 3.14.
# Two venvs: STT wants numpy 2.x, SadTalker wants 1.23. Do not merge them.
#
# Build on a machine with podman. Run on therug (2x 1660 SUPER).
#   podman build -t echo-bloom-companion .
#   ./run.sh

FROM docker.io/nvidia/cuda:12.4.1-cudnn-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    MKL_THREADING_LAYER=GNU \
    KMP_DUPLICATE_LIB_OK=TRUE

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
        ffmpeg git ca-certificates \
        build-essential libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN python3.11 -m venv /opt/stt \
    && /opt/stt/bin/pip install --no-cache-dir -U pip \
    && /opt/stt/bin/pip install --no-cache-dir -r /app/requirements.txt

# Separate venv so SadTalker's old numpy does not poison STT.
# Pins loosened: imageio 2.19 vs skimage 0.22 do not co-install on 3.11.
RUN python3.11 -m venv /opt/sadtalker \
    && /opt/sadtalker/bin/pip install --no-cache-dir -U pip \
    && /opt/sadtalker/bin/pip install --no-cache-dir \
        torch torchvision --index-url https://download.pytorch.org/whl/cu124
RUN /opt/sadtalker/bin/pip install --no-cache-dir \
        face-alignment imageio imageio-ffmpeg \
        librosa resampy pydub \
        kornia yacs pyyaml joblib \
        scikit-image basicsr facexlib \
        gfpgan av safetensors tqdm

COPY app.py /app/app.py
ENV PATH="/opt/stt/bin:$PATH" \
    SADTALKER_DIR=/opt/SadTalker \
    SADTALKER_PYTHON=/opt/sadtalker/bin/python

EXPOSE 8092
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8092"]
