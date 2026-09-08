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
# scripts/: the only way to create a real account
# (scripts/create_org.py, scripts/create_user.py -- deliberately
# CLI-only, no self-service endpoint, see backend/auth.py) is
# `docker exec` into the running container, which needs these actually
# present in the image. Copies the whole directory rather than
# cherry-picking the two files -- simpler, and every other script here
# is a plain, dependency-light CLI tool (no reference_clips/-only or
# venv-only assumptions baked into what gets copied).
COPY scripts/ scripts/

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
RUN mkdir -p /data/cache/roboflow /data/uploads /data/cache/detections /data/auth \
    && python -c "from pipeline.detection import DetectionConfig, detect_persons; \
detect_persons('demo_assets/clip_whiff1.mkv', DetectionConfig(), cache_dir=None)"

# Real persistent-volume mount point for uploads (real user data), the
# detection cache (a performance cache, safe to lose but nice to keep
# warm across restarts), and the auth store (org/user/session records
# and password hashes, see backend/auth.py -- NOT safe to lose: without
# this ENV line auth/ defaults to ROOT/"auth", i.e. /app/auth, which
# sits outside this volume and gets wiped on every container
# restart/redeploy, silently deleting every account) -- see README's
# Deployment section for the `docker run -v` example. A deployment with
# no volume mounted here still works, it just starts from a cold cache
# and loses uploads/output/accounts on every container restart, same
# tradeoff any ephemeral container filesystem has.
ENV FMH_UPLOADS_ROOT=/data/uploads
ENV FMH_DETECTION_CACHE_DIR=/data/cache/detections
ENV FMH_AUTH_ROOT=/data/auth
VOLUME ["/data"]

# Non-root: defense in depth, not because anything here is known to be
# exploitable -- this process handles untrusted user-uploaded video
# files. Ownership of /data and /app must be fixed up AFTER the RF-DETR
# warm-up step above (which needs to write into /data/cache/roboflow as
# whatever user ran the build, i.e. root) and after all COPY steps.
RUN useradd -m -u 1000 fmh && chown -R fmh:fmh /app /data
USER fmh

# Shell form (not exec/JSON form) deliberately -- ${PORT:-8420} needs a
# shell to expand it. Not required by this project's actual deployment
# target (a GCP Compute Engine VM chooses its own port at `docker run`
# time, there's no platform-injected PORT to respect) -- kept anyway
# since it's harmless, general-purpose, and costs nothing: PORT unset
# (the real GCP case, and plain local `docker run -p 8420:8420`) falls
# through to 8420 exactly as before; PORT set (e.g. a future PaaS-style
# host) is honored automatically with no Dockerfile change needed.
# `exec` is real, not decorative: without it the shell stays PID 1 and
# uvicorn never receives SIGTERM directly, so a restart has to wait out
# a hard-kill timeout instead of shutting down cleanly -- caught by
# Docker's own build-time linter, verified by timing `docker stop`
# before/after (see README's Deployment section).
EXPOSE 8420
CMD exec uvicorn backend.app:app --host 0.0.0.0 --port ${PORT:-8420}
