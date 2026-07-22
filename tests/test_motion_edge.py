"""Edge-case tests for the motion pipeline using small synthetic videos
(generated with cv2.VideoWriter — no real footage needed)."""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.segments import SegmentConfig, scores_to_segments


def write_video(path, frames, fps=30):
    h, w = frames[0].shape[:2]
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    assert vw.isOpened()
    for f in frames:
        vw.write(f)
    vw.release()


def static_frames(n, w=320, h=240):
    rng = np.random.RandomState(42)
    base = rng.randint(0, 255, (h, w, 3), dtype=np.uint8)
    return [base.copy() for _ in range(n)]


def test_static_video_yields_no_segments(tmp_path):
    p = tmp_path / "static.mp4"
    write_video(p, static_frames(90))  # 3s of a frozen frame
    r = compute_motion(str(p))
    segs = scores_to_segments(r.times, r.scores, SegmentConfig())
    assert segs == []


def test_very_short_video_does_not_crash(tmp_path):
    p = tmp_path / "short.mp4"
    write_video(p, static_frames(8))  # ~0.27s
    r = compute_motion(str(p))
    assert len(r.scores) >= 1
    segs = scores_to_segments(r.times, r.scores, SegmentConfig())
    assert isinstance(segs, list)


def test_moving_blob_is_detected(tmp_path):
    # A bright square sweeping across a static TEXTURED background =
    # localized subject motion. The texture matters: on a featureless
    # background, phase correlation locks onto the moving object itself
    # and cancels it as apparent camera motion (a known limitation).
    rng = np.random.RandomState(7)
    bg = rng.randint(0, 120, (240, 320, 3), dtype=np.uint8)
    frames = []
    for i in range(90):
        f = bg.copy()
        x = 10 + i * 2
        f[80:160, x:x + 60] = 255
        frames.append(f)
    p = tmp_path / "moving.mp4"
    write_video(p, frames)
    r = compute_motion(str(p))
    segs = scores_to_segments(r.times, r.scores, SegmentConfig())
    assert len(segs) >= 1


def test_missing_file_raises_cleanly():
    with pytest.raises(IOError):
        compute_motion("/nonexistent/nope.mp4")


def test_corrupt_file_raises_cleanly(tmp_path):
    p = tmp_path / "garbage.mp4"
    p.write_bytes(b"this is not a video file at all" * 100)
    with pytest.raises((IOError, ValueError)):
        compute_motion(str(p))
