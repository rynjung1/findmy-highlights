"""Permanent regression check for the enter_thresh camera-distance finding.

`distance_test_close.mov` / `distance_test_far.mov`: same camera, same
subject, same motion pattern, filmed at two different subject-to-camera
distances, specifically to test whether pipeline.motion's score actually
scales down with distance the way the enter_thresh margin investigation's
quadratic hypothesis predicted -- rather than trust that finding as a
one-time measurement, this re-derives it every run. Measured against this
real footage, the exponent comes out closer to linear (~1.09) than pure
quadratic (2.0) -- see the ratio test below for why, and why the
tolerance band is exponent-based rather than a tight quadratic check.

Marked 'e2e': needs real video decode and RF-DETR inference (the box-width
measurement). Run explicitly: pytest tests/test_distance_scaling.py -m e2e
"""

import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import robust_box_width
from pipeline.motion import compute_motion
from pipeline.segments import SegmentConfig, smooth_scores

pytestmark = pytest.mark.e2e

ROOT = Path(__file__).resolve().parent.parent
CLIPS_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"


def _skip_if_missing():
    for name in ("distance_test_close.mov", "distance_test_far.mov"):
        if not (CLIPS_DIR / name).exists():
            pytest.skip(f"{name} not present in {CLIPS_DIR}")


def test_close_scores_higher_than_far():
    """Sanity floor: the closer clip must show more motion, not less --
    if this ever flips, something upstream (file swap, camera move
    between takes) broke the fixture, not just the scaling math below."""
    _skip_if_missing()
    close = compute_motion(str(CLIPS_DIR / "distance_test_close.mov"))
    far = compute_motion(str(CLIPS_DIR / "distance_test_far.mov"))
    cfg = SegmentConfig()
    close_sm = smooth_scores(close.times, close.scores, cfg.smooth_window_s)
    far_sm = smooth_scores(far.times, far.scores, cfg.smooth_window_s)
    assert close_sm.max() > far_sm.max()


def test_box_width_ratio_matches_motion_score_ratio_directionally():
    """The actual regression: re-measure the linear-size ratio (box width,
    NOT height -- height is corrupted in distance_test_close by bottom-
    frame cropping, see robust_box_width's docstring) and confirm the
    motion-score ratio still tracks it, roughly between linear and
    quadratic scaling.

    NOT a pure-quadratic check, despite the original hypothesis being
    "score scales with the square of linear size": measured against this
    real footage, the implied exponent (ln(score_ratio)/ln(width_ratio))
    comes out ~1.09 -- much closer to linear than the pure geometric
    theory predicted, most likely because a roughly-fixed background
    motion floor (lighting, minor camera shake, compression noise) dilutes
    the pure subject-area scaling more at the far distance, where the
    subject's own contribution is smaller relative to that floor. That's
    a real, honest finding from real data, not a bug to force back to
    exactly width_ratio**2 -- so the tolerance band below is deliberately
    exponent-based (0.5 to 3.0), wide enough to hold real behavioral
    variance between two non-frame-synced takes of "the same" motion,
    while still failing hard if the relationship ever collapses to flat
    (exponent ~0) or inverts (negative) -- e.g. if a future pipeline.motion
    change broke camera-motion compensation or the analysis resolution.
    """
    _skip_if_missing()
    cfg = SegmentConfig()

    close_motion = compute_motion(str(CLIPS_DIR / "distance_test_close.mov"))
    far_motion = compute_motion(str(CLIPS_DIR / "distance_test_far.mov"))
    close_sm = smooth_scores(close_motion.times, close_motion.scores, cfg.smooth_window_s)
    far_sm = smooth_scores(far_motion.times, far_motion.scores, cfg.smooth_window_s)
    score_ratio = close_sm.max() / far_sm.max()

    close_det = detect_persons(str(CLIPS_DIR / "distance_test_close.mov"),
                               DetectionConfig(), cache_dir=str(CACHE_DIR))
    far_det = detect_persons(str(CLIPS_DIR / "distance_test_far.mov"),
                             DetectionConfig(), cache_dir=str(CACHE_DIR))
    close_w = robust_box_width(close_det.times, close_det.boxes, close_det.frame_size)
    far_w = robust_box_width(far_det.times, far_det.boxes, far_det.frame_size)
    assert close_w is not None and far_w is not None, \
        "not enough non-edge-clipped detections to measure scale"

    width_ratio = close_w / far_w
    # confirms distance_test_close really does look closer (bigger) than far
    assert width_ratio > 1.0

    low, high = width_ratio ** 0.5, width_ratio ** 3.0
    assert low <= score_ratio <= high, (
        f"score_ratio={score_ratio:.2f} outside [{low:.2f}, {high:.2f}] "
        f"(width_ratio={width_ratio:.2f}, implying an exponent of "
        f"{math.log(score_ratio) / math.log(width_ratio):.2f} "
        f"if positive) -- the scale-dependence this test exists to guard "
        f"may have disappeared or inverted")


def test_far_still_clears_enter_thresh_by_some_margin():
    """Not a safety guarantee (this one motion pattern isn't the thinnest
    real event on record -- clip_foul1 is), just a canary: if the far
    clip's own margin ever collapses to near/below 1x, that's worth
    knowing immediately, independent of the ratio check above."""
    _skip_if_missing()
    cfg = SegmentConfig()
    far = compute_motion(str(CLIPS_DIR / "distance_test_far.mov"))
    far_sm = smooth_scores(far.times, far.scores, cfg.smooth_window_s)
    assert far_sm.max() / cfg.enter_thresh > 1.0
