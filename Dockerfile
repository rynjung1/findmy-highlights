# Backend image: FastAPI + the full real pipeline (motion, RF-DETR
# person detection, fusion/segments/refine/hard-cut, ffmpeg stitch/export)
# and demo mode. Does NOT serve the frontend -- see README's Deployment
# section for why that's a separate static build (frontend/dist/) meant
# for a static host, not this container.
#
# Built and run locally against this exact Dockerfile before it was
# considered done: `docker build`, `docker run`, a real health check,
# and a real end-to-end demo run through the running container -- same
# "verify before claiming done" standard as every other change in this
# project. See README's Deployment section for those real numbers.
FROM python:3.11-slim

# ffmpeg: real system dependency (README's Setup step 1), not a pip
# package -- pipeline.stitch shells out to it directly.
# libgl1/libglib2.0-0: opencv-python's real runtime dependency on
# Debian slim (it links against libGL even for video-file-only,
# no-display usage; omitting these fails at `import cv2`, not at
# build time, so this was verified with a real container start, not
# assumed from opencv's own docs).
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# CPU-only torch/torchvision wheels, installed BEFORE the rest of
# requirements.txt and from PyTorch's own CPU index rather than PyPI's
# default (which resolves to CUDA-bundled wheels, several GB larger and
# entirely wasted on a CPU-only demo host with no GPU). The exact pinned
# versions match requirements.txt -- pip sees them already satisfied
# during the next install step, so nothing gets silently upgraded or
# reinstalled as a GPU build.
COPY requirements.txt .
RUN grep -E "^torch==|^torchvision==" requirements.txt > /tmp/torch-pins.txt \
    && pip install --no-cache-dir -r /tmp/torch-pins.txt --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# App code. demo_assets/ (the bundled sample clip + its precomputed
# detection cache) is the one video file this project's otherwise-broad
# .dockerignore video-extension exclusion explicitly lets through -- see
# .dockerignore and backend/demo.py.
COPY pipeline/ pipeline/
COPY backend/ backend/
COPY demo_assets/ demo_assets/

# RF-DETR's Apache-2.0 pretrained weights (~355MB, see README's Setup
# step 5) are baked into the image at build time rather than downloaded
# on a visitor's first real request -- the whole point of demo mode is a
# fast, reliable first impression, and a cold multi-hundred-MB download
# on whatever network the deploy host has is exactly the kind of
# real-but-invisible risk that would undermine that. Uses the demo clip
# itself as a real (not synthetic) warm-up input, through the same
# detect_persons() call path a real request uses -- if this step fails,
# the image fails to build, not a visitor's first click.
ENV RF_HOME=/data/cache/roboflow
RUN mkdir -p /data/cache/roboflow /data/uploads /data/cache/detections \
    && python -c "from pipeline.detection import DetectionConfig, detect_persons; \
detect_persons('demo_assets/clip_whiff1.mkv', DetectionConfig(), cache_dir=None)"

# Real persistent-volume mount point for uploads (real user data) and
# the detection cache (a performance cache, safe to lose but nice to
# keep warm across restarts) -- see README's Deployment section for the
# `docker run -v` example. A deployment with no volume mounted here
# still works, it just starts from a cold cache and loses uploads/
# output on every container restart, same tradeoff any ephemeral
# container filesystem has.
ENV FMH_UPLOADS_ROOT=/data/uploads
ENV FMH_DETECTION_CACHE_DIR=/data/cache/detections
VOLUME ["/data"]

# Non-root: defense in depth, not because anything here is known to be
# exploitable -- this process handles untrusted user-uploaded video
# files. Ownership of /data and /app must be fixed up AFTER the RF-DETR
# warm-up step above (which needs to write into /data/cache/roboflow as
# whatever user ran the build, i.e. root) and after all COPY steps.
RUN useradd -m -u 1000 fmh && chown -R fmh:fmh /app /data
USER fmh

# Shell form (not exec/JSON form) deliberately -- ${PORT:-8420} needs a
# shell to expand it. Railway (and most PaaS hosts) inject a real PORT
# env var at runtime and route traffic to whatever port the app actually
# binds, NOT to EXPOSE's value (which is documentation/legacy Docker
# networking only) -- verified directly against Railway's own docs
# before writing this, not assumed. The :-8420 fallback keeps a plain
# local `docker run -p 8420:8420` (no PORT set) working exactly as
# before. `exec` is real, not decorative: without it the shell stays PID
# 1 and uvicorn never receives SIGTERM directly, so a redeploy/restart
# has to wait out a hard-kill timeout instead of shutting down cleanly
# -- caught by Docker's own build-time linter, not assumed correct.
EXPOSE 8420
CMD exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8420}
