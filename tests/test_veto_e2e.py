"""End-to-end positive-case evidence for the veto.

The reference clips never exercise the veto (their motion is always
human — measured longest person-free motion run: 0.6s), so this test
constructs the scenario the veto exists for: sustained non-person motion
(think wind-blown branches / shadows) with no person anywhere — and runs
it through the REAL pipeline, including actual RF-DETR inference, to
prove the veto fires. A paired control adds a person-shaped photo cutout
riding the same motion; detection must find it and the veto must NOT fire.

Marked 'e2e': slower than the unit tests (real model inference) and needs
the pretrained weights (auto-downloaded on first use). Run explicitly:

    pytest tests/test_veto_e2e.py -m e2e
"""

import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import apply_veto, fuse
from pipeline.segments import SegmentConfig, scores_to_segments

pytestmark = pytest.mark.e2e

W, H, FPS, SECONDS = 640, 360, 30, 8


def textured_background():
    rng = np.random.RandomState(11)
    bg = rng.randint(40, 110, (H, W, 3), dtype=np.uint8)
    return cv2.GaussianBlur(bg, (7, 7), 0)


def write_wind_video(path):
    """Sustained wavy blob motion (top-right) over a static background —
    localized, persistent, non-person motion (a wind-blown branch)."""
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    assert vw.isOpened()
    bg = textured_background()
    rng = np.random.RandomState(3)
    blob = rng.randint(120, 220, (60, 90, 3), dtype=np.uint8)
    still_after = int(FPS * (SECONDS - 1.5))
    for i in range(FPS * SECONDS):
        f = bg.copy()
        # motion STOPS 1.5s before the end: the last motion samples of a
        # file sit beyond the detection staleness bound and are protected
        # from vetoing by design, so the segment must close before then
        j = min(i, still_after)
        x = 420 + int(60 * np.sin(j / 5.0))
        y = 40 + int(18 * np.sin(j / 3.0))
        f[y:y + 60, x:x + 90] = blob
        vw.write(f)
    vw.release()


def write_moving_person_video(path, patch):
    """The MOVING thing is a person (real photo crop sliding across the
    frame), so the motion overlaps the detected person box. The veto's
    rule is 'no person near the motion' — a person standing still while a
    branch waves elsewhere is legitimately vetoable, so the control must
    make the person the source of the motion."""
    vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H))
    assert vw.isOpened()
    bg = textured_background()
    ph, pw = patch.shape[:2]
    still_after = int(FPS * (SECONDS - 1.5))
    for i in range(FPS * SECONDS):
        f = bg.copy()
        j = min(i, still_after)  # same tail-stillness as the wind fixture
        x = 150 + int(120 * np.sin(j / 10.0))  # brisk pacing back and forth
        f[120:120 + ph, x:x + pw] = patch
        vw.write(f)
    vw.release()


def person_patch():
    """A real person crop from a reference clip frame, resized — something
    RF-DETR genuinely recognizes as a person (synthetic stick figures
    aren't reliable detections and would test nothing)."""
    clip = Path(__file__).parent.parent / "reference_clips" / "clip_60.mkv"
    if not clip.exists():
        pytest.skip("reference clip not available for person patch")
    cap = cv2.VideoCapture(str(clip))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(139 * cap.get(cv2.CAP_PROP_FPS)))
    ok, frame = cap.read()
    cap.release()
    if not ok:
        pytest.skip("could not read reference frame")
    batter = frame[560:920, 950:1180]  # batter at the plate in this frame
    return cv2.resize(batter, (130, 200))


def run_pipeline(video_path, tmp_path):
    motion = compute_motion(str(video_path))
    det = detect_persons(str(video_path), DetectionConfig(),
                         cache_dir=str(tmp_path / "cache"))
    fused = fuse(motion.times, motion.scores, motion.grids,
                 motion.frame_size, motion.analysis_size, motion.border_px,
                 det.times, det.boxes, None)
    # matches the real pipeline (scripts/detect.py): motion alone opens
    # AND closes raw segments; Phase 3's play extension replaced the old
    # fused-score sustain, so this must NOT pass sustain_scores, or the
    # veto would be exercised against a pipeline shape that no longer
    # exists in production
    segs = scores_to_segments(fused.times, fused.motion, SegmentConfig())
    return apply_veto(segs, fused), det


def test_veto_fires_on_sustained_person_free_motion(tmp_path):
    p = tmp_path / "wind.mp4"
    write_wind_video(p)
    (kept, vetoed), det = run_pipeline(p, tmp_path)
    assert sum(len(b) for b in det.boxes) == 0, \
        "control violated: model hallucinated a person in the wind video"
    assert vetoed, "expected the person-free motion segment to be vetoed"
    assert not kept


def test_veto_holds_fire_when_person_is_the_motion(tmp_path):
    p = tmp_path / "moving_person.mp4"
    write_moving_person_video(p, person_patch())
    (kept, vetoed), det = run_pipeline(p, tmp_path)
    assert any(len(b) > 0 for b in det.boxes), \
        "model failed to detect the real-person patch; control invalid"
    assert not vetoed, "veto must not fire when the motion IS a person"
    assert kept
