"""Motion-based action detection (Phase 1 baseline).

Measures frame-to-frame motion intensity with a basic correction for
camera motion: the global translation between consecutive frames is
estimated with phase correlation and removed before differencing, so a
pan/tilt of the whole frame does not read as subject motion. What's left
after compensation is localized motion — players moving, a swing, a run.

This is deliberately simple. It cannot tell a swing from a coach walking
across the infield; that discrimination is Phase 2's job. Per the project
priority rule, thresholds downstream are set permissively: over-flagging
is acceptable, missing a real play is not.
"""

from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class MotionConfig:
    # Frames are downscaled to this width before analysis (speed + noise
    # suppression; fine detail like the fence mesh mostly averages out).
    analysis_width: int = 480
    # Approximate rate at which frames are sampled for analysis. The real
    # rate is the closest integer division of the source fps.
    sample_fps: float = 10.0
    # Per-pixel absolute grayscale difference (0-255) counted as "moving".
    pixel_diff_thresh: int = 18
    # Global shift (pixels, at analysis scale) above which the frame is
    # considered camera motion. Compensation still runs, but the frame is
    # flagged so downstream logic can treat its score with suspicion.
    camera_motion_px: float = 3.0
    # Border fraction ignored after warp compensation (warp edge artifacts).
    border_frac: float = 0.03
    # Coarse spatial grid (cols x rows) of per-block moving-pixel fractions,
    # emitted alongside the scalar score so downstream fusion can ask WHERE
    # the motion was (e.g. inside a person's bounding box).
    grid_cols: int = 16
    grid_rows: int = 9


@dataclass
class MotionResult:
    times: np.ndarray          # seconds, one entry per analyzed frame pair
    scores: np.ndarray         # fraction of pixels moving (0..1), camera-compensated
    camera_shift: np.ndarray   # global translation magnitude per frame pair (px)
    grids: np.ndarray          # (n, grid_rows, grid_cols) per-block moving fractions
    fps: float                 # source video fps
    duration: float            # seconds
    frame_size: tuple          # original (width, height)
    analysis_size: tuple       # downscaled (width, height) used for analysis
    border_px: tuple           # (bw, bh) border trimmed from analysis frame
    config: MotionConfig = field(default_factory=MotionConfig)


def compute_motion(video_path: str, config: MotionConfig | None = None,
                   progress_cb=None) -> MotionResult:
    """Scan a video and return per-sample motion scores.

    progress_cb, if given, is called with (seconds_done, seconds_total).
    """
    cfg = config or MotionConfig()
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise IOError(f"could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = n_frames / fps if n_frames else 0.0
    step = max(1, round(fps / cfg.sample_fps))

    times, scores, shifts, grids = [], [], [], []
    frame_size = analysis_size = border_px = None
    prev = None
    prev_t = 0.0
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

        h, w = frame.shape[:2]
        scale = cfg.analysis_width / w
        small = cv2.resize(frame, (cfg.analysis_width, int(h * scale)),
                           interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (5, 5), 0)
        gray_f = gray.astype(np.float32)

        if prev is not None:
            (dx, dy), _resp = cv2.phaseCorrelate(prev, gray_f)
            shift_mag = float(np.hypot(dx, dy))
            # Undo the global translation so camera pans don't count.
            m = np.float32([[1, 0, dx], [0, 1, dy]])
            warped_prev = cv2.warpAffine(
                prev, m, (gray_f.shape[1], gray_f.shape[0]),
                flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
            diff = cv2.absdiff(gray_f, warped_prev)
            bh = int(diff.shape[0] * cfg.border_frac)
            bw = int(diff.shape[1] * cfg.border_frac)
            core = diff[bh:diff.shape[0] - bh, bw:diff.shape[1] - bw]
            mask = (core > cfg.pixel_diff_thresh).astype(np.float32)
            moving_frac = float(np.mean(mask))
            grid = cv2.resize(mask, (cfg.grid_cols, cfg.grid_rows),
                              interpolation=cv2.INTER_AREA)
            times.append(prev_t)
            scores.append(moving_frac)
            shifts.append(shift_mag)
            grids.append(grid)
            if frame_size is None:
                frame_size = (w, h)
                analysis_size = (gray_f.shape[1], gray_f.shape[0])
                border_px = (bw, bh)

        prev = gray_f
        prev_t = t
        if progress_cb is not None:
            progress_cb(t, duration)

    cap.release()
    if not times:
        raise ValueError(f"no analyzable frames in video: {video_path}")
    return MotionResult(
        times=np.asarray(times), scores=np.asarray(scores),
        camera_shift=np.asarray(shifts), grids=np.asarray(grids),
        fps=fps, duration=duration or (times[-1] if times else 0.0),
        frame_size=frame_size, analysis_size=analysis_size,
        border_px=border_px, config=cfg)
