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


def test_duration_uses_actually_decoded_frames_not_container_metadata(
        tmp_path, monkeypatch):
    """Regression test for a real bug found via a user-provided test file:
    a video split from another with ffmpeg's stream copy at a non-keyframe
    point can end up with CAP_PROP_FRAME_COUNT overstating what actually
    decodes (observed: container claimed 4080 frames/85.08s, only 3861
    frames/80.52s were retrievable). Trusting the nominal count made
    compute_motion() report a duration ~4.7s longer than anything was ever
    analyzed for — which the manifest then rendered as confirmed dead time
    that was, in fact, never-analyzed footage. duration must never exceed
    what grab()/retrieve() actually produced.

    Reproduced here by wrapping a real (correctly-encoded) capture and
    lying specifically about CAP_PROP_FRAME_COUNT — the real pathology is
    codec/container-specific and didn't reproduce with a simple synthetic
    re-encode, but this isolates exactly the logic the fix depends on:
    grab() truthfully running out before the inflated count is reached.
    """
    p = tmp_path / "video.mp4"
    write_video(p, static_frames(60), fps=30)  # ~2.0s of real content

    real_capture_cls = cv2.VideoCapture

    class LyingCapture:
        def __init__(self, path):
            self._real = real_capture_cls(path)

        def isOpened(self):
            return self._real.isOpened()

        def get(self, prop):
            if prop == cv2.CAP_PROP_FRAME_COUNT:
                return 90.0  # claims 50% more frames than actually exist
            return self._real.get(prop)

        def grab(self):
            return self._real.grab()      # truthfully exhausts at ~60

        def retrieve(self):
            return self._real.retrieve()

        def release(self):
            self._real.release()

    monkeypatch.setattr(cv2, "VideoCapture", LyingCapture)
    result = compute_motion(str(p))
    monkeypatch.setattr(cv2, "VideoCapture", real_capture_cls)

    nominal_duration = 90 / 30  # what the lying frame count implies: 3.0s
    assert result.duration < nominal_duration
    assert result.duration <= 2.1  # must reflect what was actually decoded
    assert result.duration >= result.times[-1]
