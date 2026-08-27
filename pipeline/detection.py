"""Person detection on sampled frames (Stage 2 signal).

Uses RF-DETR Base (Apache-2.0, code and weights) at an elevated input
resolution — validated on the reference footage to catch distant fielders
down to ~19px box height, matching Faster R-CNN while running ~3x faster.

Detection runs at ~1 fps (vs ~10 fps motion sampling); results are cached
to JSON keyed by file mtime + config so re-runs are free. The model import
is lazy so the rest of the pipeline (and the unit tests) work without
torch installed.
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2


@dataclass
class DetectionConfig:
    sample_fps: float = 1.0
    resolution: int = 1120        # RF-DETR input res; must be divisible by 56
    threshold: float = 0.4
    person_class_id: int = 1      # COCO 'person'
    device: str = "auto"          # auto -> mps if available, else cpu
    # Investigated 2026-08-25 (real detect-stage speed profiling): on this
    # project's Apple Silicon/MPS hardware, inference is 99.3% of real
    # detect-stage wall clock (frame grab/convert/postprocess are all
    # <1% combined), so this is the one lever worth exposing.
    #
    # Flipped to "small" 2026-08-27, real evidence, not a guess: RFDETRSmall
    # measures a real 1.34x inference speedup and passed the FULL 9-clip
    # regression suite both ways (ALL PASS, 100% required recall on every
    # clip, hard-cut exclusion validated, every stitch clean). The one
    # flagged difference (clip_base3's kept segment +2.4s under Small)
    # was frame-verified directly before flipping this default, not just
    # gate-passed: the extra window (t=18.6-21.0) is confirmed post-play
    # downtime (the next batter's walkup, matching this event's own
    # ground-truth note that "defense relaxed... by ~18s") -- not real
    # play content at risk, and the hard-cut mechanism trims most of the
    # difference back out regardless (final kept duration differs by
    # <1s after hard-cut). See docs/INVESTIGATION_LOG.md for the full
    # writeup of both the 2026-08-25 investigation and the 2026-08-27
    # frame-verification that preceded this flip. Revert to "base" only
    # with the same rigor (full 9-clip regression, not a guess).
    model_variant: str = "small"   # base | medium | small | nano


@dataclass
class DetectionResult:
    times: list                   # seconds, one per sampled frame
    boxes: list                   # per sample: list of [x1, y1, x2, y2] (orig px)
    frame_size: tuple             # (width, height)
    config: DetectionConfig = field(default_factory=DetectionConfig)


def _model_version() -> str:
    # Part of the cache key: a model upgrade must invalidate cached boxes.
    from importlib.metadata import version
    return f"rfdetr-{version('rfdetr')}"


def _cache_key(video_path: Path, cfg: DetectionConfig) -> str:
    stat = video_path.stat()
    raw = f"{video_path.name}:{stat.st_size}:{stat.st_mtime_ns}:" \
          f"{_model_version()}:{cfg.sample_fps}:{cfg.resolution}:" \
          f"{cfg.threshold}:{cfg.person_class_id}:{cfg.model_variant}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


_MODEL_CLASSES = {"base": "RFDETRBase", "medium": "RFDETRMedium",
                  "small": "RFDETRSmall", "nano": "RFDETRNano"}


def _resolve_model_class(model_variant: str):
    import rfdetr
    try:
        class_name = _MODEL_CLASSES[model_variant]
    except KeyError:
        raise ValueError(f"unknown model_variant {model_variant!r}, "
                         f"expected one of {sorted(_MODEL_CLASSES)}") from None
    return getattr(rfdetr, class_name)


def _resolve_device(device: str) -> str:
    if device != "auto":
        return device
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def detect_persons(video_path: str, config: DetectionConfig | None = None,
                   cache_dir: str | None = None,
                   progress_cb=None) -> DetectionResult:
    """Detect persons on sampled frames; returns boxes in original pixels."""
    cfg = config or DetectionConfig()
    vp = Path(video_path)

    cache_file = None
    if cache_dir is not None:
        cache_file = Path(cache_dir) / f"{vp.stem}_{_cache_key(vp, cfg)}.json"
        if cache_file.exists():
            data = json.loads(cache_file.read_text())
            return DetectionResult(times=data["times"], boxes=data["boxes"],
                                   frame_size=tuple(data["frame_size"]),
                                   config=cfg)

    from PIL import Image

    model_cls = _resolve_model_class(cfg.model_variant)
    model = model_cls(device=_resolve_device(cfg.device),
                      resolution=cfg.resolution)

    cap = cv2.VideoCapture(str(vp))
    if not cap.isOpened():
        raise IOError(f"could not open video: {video_path}")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = n_frames / fps if n_frames else 0.0
    step = max(1, round(fps / cfg.sample_fps))

    times, boxes_per_sample = [], []
    frame_size = None
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step != 0:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        t = idx / fps
        idx += 1
        if frame_size is None:
            frame_size = (frame.shape[1], frame.shape[0])

        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        det = model.predict(img, threshold=cfg.threshold)
        boxes = [det.xyxy[i].tolist() for i in range(len(det.class_id))
                 if det.class_id[i] == cfg.person_class_id]
        times.append(t)
        boxes_per_sample.append(boxes)
        if progress_cb is not None:
            progress_cb(t, duration)
    cap.release()

    if frame_size is None:
        raise ValueError(f"no readable frames in video: {video_path}")

    result = DetectionResult(times=times, boxes=boxes_per_sample,
                             frame_size=frame_size, config=cfg)
    if cache_file is not None:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({
            "times": result.times, "boxes": result.boxes,
            "frame_size": list(result.frame_size)}))
    return result
