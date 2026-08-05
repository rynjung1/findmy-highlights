"""Pose-based feature extraction: wrist displacement of the near-plate
batter, via MediaPipe's BlazePose GHUM 3D pose landmarker.

License, checked directly against the primary source before writing any
of this (not trusted secondhand): both MediaPipe itself and the
pose_landmarker model (Lite/Full/Heavy, all three) are Apache License,
Version 2.0 -- confirmed from Google's own official model card
(storage.googleapis.com/mediapipe-assets/Model Card BlazePose GHUM
3D.pdf), "LICENSED UNDER: Apache License, Version 2.0".

Scope, per that same card: the model's own stated out-of-scope
conditions include "people too far away from the camera (e.g. further
than 14 feet/4 meters)" and it's designed for a single, centered
subject with "a margin around the square circumscribing full-body
calculated as 25% of size". This module's crop -- the near-plate
person's own RF-DETR box, padded 25% and squared -- exists specifically
to keep every frame handed to the pose model inside that validated
scope: never the full frame (a real game frame routinely has the
subject far smaller and off-center than the model card's own training
distribution), and never simply "the largest box in frame" (which can
pick a fielder standing closer to the camera instead of the batter --
the exact mistake an earlier investigation caught and corrected before
trusting any result).

This is Tier 1 investigative instrumentation for the review queue (see
pipeline/review.py), not a cutting signal. Whether wrist displacement
ends up informing any real kept/cut decision depends entirely on the
real, measured separation reported once validated -- see the README's
Task 2 pose+audio writeup for those numbers, not this module's own
existence.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
# Overridable via FMH_POSE_MODEL_PATH (same convention as
# pipeline.run.DEFAULT_CACHE_DIR) -- this file has no auto-download (see
# README's Setup step 6), so a deployment that wants the Tier 1
# review-queue instrumentation must mount it at this path explicitly.
DEFAULT_MODEL_PATH = Path(os.environ.get(
    "FMH_POSE_MODEL_PATH", str(ROOT / ".cache" / "models" / "pose_landmarker_full.task")))

# BlazePose GHUM 3D topology (see the model card): landmark 15 = left
# wrist, 16 = right wrist.
_LEFT_WRIST, _RIGHT_WRIST = 15, 16


@dataclass
class PoseConfig:
    model_path: str = str(DEFAULT_MODEL_PATH)
    # Per the model card's own recommended attribute: "There should be a
    # margin around the square circumscribing full-body calculated as
    # 25% of size" -- not an arbitrary padding choice.
    crop_margin_frac: float = 0.25
    # Half-window on each side of the candidate instant, seconds --
    # matches pipeline.review.ReviewConfig.clip_padding_s, since this
    # runs against the same short clip a reviewer would watch.
    window_s: float = 1.5
    # Below this, a landmark's own probability of being a real,
    # unoccluded point is too low to trust for a displacement
    # measurement -- MediaPipe always emits a coordinate for every
    # landmark regardless of confidence (see the model card's own
    # "degrades gracefully by predicting average point location"), so
    # this is a real filter, not defensive dead code.
    visibility_thresh: float = 0.3


def _near_plate_box(det_times, det_boxes, zone, t):
    """The near-plate person's own RF-DETR box at the sample nearest t:
    among boxes whose CENTER falls inside `zone` (same in-zone test
    pipeline.fusion.compute_zone_velocity already uses), the largest by
    area -- the batter standing at the plate, not whichever box happens
    to be first or biggest in the whole frame (which can be a fielder).
    None if no detection sample exists or nothing is in zone."""
    det_times = np.asarray(det_times, dtype=float)
    if len(det_times) == 0:
        return None
    idx = int(np.argmin(np.abs(det_times - t)))
    boxes = det_boxes[idx]
    cx0, cy0 = zone.center_xy
    r2 = zone.radius_px ** 2
    in_zone = [b for b in boxes
              if (0.5 * (b[0] + b[2]) - cx0) ** 2
              + (0.5 * (b[1] + b[3]) - cy0) ** 2 <= r2]
    if not in_zone:
        return None
    return max(in_zone, key=lambda b: (b[2] - b[0]) * (b[3] - b[1]))


def _padded_crop_bounds(box, frame_w, frame_h, margin_frac):
    x1, y1, x2, y2 = box
    side = max(x2 - x1, y2 - y1)  # square ROI, matching the model's own recommendation
    cx, cy = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
    half = side * (1 + margin_frac) / 2
    cx0 = max(0, int(round(cx - half)))
    cy0 = max(0, int(round(cy - half)))
    cx1 = min(frame_w, int(round(cx + half)))
    cy1 = min(frame_h, int(round(cy + half)))
    return cx0, cy0, cx1, cy1


def build_landmarker(model_path=None):
    """Real MediaPipe PoseLandmarker, IMAGE mode (one independent
    detection per frame -- this module tracks displacement itself
    rather than relying on MediaPipe's own VIDEO-mode temporal
    smoothing, so a real fast movement isn't smoothed away before the
    displacement measurement sees it). Callers processing many windows
    should build one landmarker and pass it in via `landmarker=`, rather
    than paying model-load cost per candidate."""
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision
    base_options = mp_python.BaseOptions(
        model_asset_path=model_path or str(DEFAULT_MODEL_PATH))
    options = vision.PoseLandmarkerOptions(
        base_options=base_options, running_mode=vision.RunningMode.IMAGE)
    return vision.PoseLandmarker.create_from_options(options)


def _wrist_landmarks(landmarker, frame_bgr, crop_bounds):
    """Both wrists' (x, y, visibility) in ABSOLUTE frame pixel
    coordinates (not crop-local) -- displacement must be measured in one
    consistent coordinate system even though the crop's own bounds are
    recomputed fresh each call from the same fixed box. None if the crop
    is degenerate or no pose was found in it."""
    import mediapipe as mp
    x0, y0, x1, y1 = crop_bounds
    if x1 <= x0 or y1 <= y0:
        return None
    crop = frame_bgr[y0:y1, x0:x1]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    result = landmarker.detect(mp_image)
    if not result.pose_landmarks:
        return None
    lm = result.pose_landmarks[0]
    ch, cw = crop.shape[0], crop.shape[1]
    left = lm[_LEFT_WRIST]
    right = lm[_RIGHT_WRIST]
    return {
        "left": (x0 + left.x * cw, y0 + left.y * ch, float(left.visibility)),
        "right": (x0 + right.x * cw, y0 + right.y * ch, float(right.visibility)),
    }


def _peak_consecutive_displacement(traj):
    """Max Euclidean distance between CONSECUTIVE trusted samples of one
    wrist's trajectory -- a frame-to-frame speed proxy, not total range,
    matching what a real swing's sharp, brief motion looks like versus a
    slow ambient drift covering the same total distance."""
    if len(traj) < 2:
        return None
    peak = 0.0
    for (_, x0, y0), (_, x1, y1) in zip(traj, traj[1:]):
        peak = max(peak, float(np.hypot(x1 - x0, y1 - y0)))
    return peak


def wrist_displacement(video_path, det_times, det_boxes, zone, center_s,
                       config: PoseConfig | None = None, landmarker=None):
    """Peak frame-to-frame wrist displacement (pixels) in a real decoded
    window around center_s, cropped every frame to the near-plate
    person's own RF-DETR box (see module docstring for why).

    Returns None if there's no near-plate box near center_s, the video
    can't be decoded there, or fewer than 2 trusted (above
    visibility_thresh) samples of either wrist were found -- callers
    must treat None as "no pose signal available", not zero displacement.
    Otherwise: {"peak_displacement_px", "min_visibility" (the lowest
    visibility seen across BOTH wrists and all decoded frames, reported
    even for frames below threshold, so a real confidence drop during
    the fastest part of a real swing is visible rather than silently
    filtered out), "n_frames_with_pose"}.
    """
    cfg = config or PoseConfig()
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    box = _near_plate_box(det_times, det_boxes, zone, center_s)
    if box is None:
        cap.release()
        return None
    crop_bounds = _padded_crop_bounds(box, frame_w, frame_h, cfg.crop_margin_frac)

    start_s = max(0.0, center_s - cfg.window_s)
    cap.set(cv2.CAP_PROP_POS_MSEC, start_s * 1000)
    n_frames = int(round(2 * cfg.window_s * fps))

    own_landmarker = landmarker is None
    if own_landmarker:
        landmarker = build_landmarker(cfg.model_path)

    left_traj, right_traj, all_visibilities = [], [], []
    try:
        for i in range(n_frames):
            ok, frame = cap.read()
            if not ok:
                break
            wrists = _wrist_landmarks(landmarker, frame, crop_bounds)
            if wrists is None:
                continue
            t = start_s + i / fps
            for name, traj in (("left", left_traj), ("right", right_traj)):
                x, y, vis = wrists[name]
                all_visibilities.append(vis)
                if vis >= cfg.visibility_thresh:
                    traj.append((t, x, y))
    finally:
        cap.release()
        if own_landmarker:
            landmarker.close()

    left_peak = _peak_consecutive_displacement(left_traj)
    right_peak = _peak_consecutive_displacement(right_traj)
    peaks = [p for p in (left_peak, right_peak) if p is not None]
    if not peaks or not all_visibilities:
        return None

    return {
        "peak_displacement_px": max(peaks),
        "min_visibility": float(min(all_visibilities)),
        "n_frames_with_pose": len(set(t for t, _, _ in left_traj) | set(t for t, _, _ in right_traj)),
    }
