"""Unit tests for pipeline.pose: near-plate box selection, crop bounds,
and (gated on real reference footage) a real MediaPipe smoke test."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.pose import (PoseConfig, _near_plate_box, _padded_crop_bounds,
                           _peak_consecutive_displacement, wrist_displacement)


class FakeZone:
    def __init__(self, cx, cy, r):
        self.center_xy = (cx, cy)
        self.radius_px = r


# ---- _near_plate_box ----

def test_near_plate_box_picks_the_box_in_zone_not_largest_overall():
    # a big box OUTSIDE the zone (a fielder much closer to camera) must
    # not be picked over a smaller box that's actually near the plate --
    # the exact mistake ("largest box picked a fielder by mistake") an
    # earlier investigation caught and corrected
    zone = FakeZone(100.0, 100.0, 30.0)
    det_times = [5.0]
    det_boxes = [[
        [500, 500, 700, 900],   # huge box, far from the zone
        [90, 90, 110, 110],     # small box, centered right in the zone
    ]]
    box = _near_plate_box(det_times, det_boxes, zone, t=5.0)
    assert box == [90, 90, 110, 110]


def test_near_plate_box_picks_largest_among_multiple_in_zone():
    zone = FakeZone(100.0, 100.0, 50.0)
    det_times = [5.0]
    det_boxes = [[
        [90, 90, 105, 105],    # smaller, in zone
        [70, 70, 130, 130],    # larger, in zone
    ]]
    box = _near_plate_box(det_times, det_boxes, zone, t=5.0)
    assert box == [70, 70, 130, 130]


def test_near_plate_box_none_when_nothing_in_zone():
    zone = FakeZone(100.0, 100.0, 10.0)
    det_times = [5.0]
    det_boxes = [[[500, 500, 700, 900]]]
    assert _near_plate_box(det_times, det_boxes, zone, t=5.0) is None


def test_near_plate_box_none_on_empty_det_times():
    zone = FakeZone(100.0, 100.0, 10.0)
    assert _near_plate_box([], [], zone, t=5.0) is None


def test_near_plate_box_picks_nearest_time_sample():
    zone = FakeZone(100.0, 100.0, 50.0)
    det_times = [1.0, 10.0]
    det_boxes = [
        [[80, 80, 120, 120]],   # sample at t=1.0
        [[85, 85, 115, 115]],   # sample at t=10.0
    ]
    box = _near_plate_box(det_times, det_boxes, zone, t=9.0)
    assert box == [85, 85, 115, 115]  # nearer to t=10.0


# ---- _padded_crop_bounds ----

def test_padded_crop_bounds_is_square_and_padded():
    # box is 100 wide x 50 tall -> square side = 100, padded 25% -> 125
    bounds = _padded_crop_bounds([100, 100, 200, 150], frame_w=1000,
                                 frame_h=1000, margin_frac=0.25)
    x0, y0, x1, y1 = bounds
    assert (x1 - x0) == pytest.approx(125, abs=1)
    assert (y1 - y0) == pytest.approx(125, abs=1)


def test_padded_crop_bounds_clips_to_frame():
    # box right at the top-left corner -> padding would go negative
    bounds = _padded_crop_bounds([0, 0, 20, 20], frame_w=1000, frame_h=1000,
                                 margin_frac=0.25)
    x0, y0, x1, y1 = bounds
    assert x0 >= 0 and y0 >= 0


def test_padded_crop_bounds_clips_to_frame_bottom_right():
    bounds = _padded_crop_bounds([980, 980, 1000, 1000], frame_w=1000,
                                 frame_h=1000, margin_frac=0.25)
    x0, y0, x1, y1 = bounds
    assert x1 <= 1000 and y1 <= 1000


# ---- _peak_consecutive_displacement ----

def test_peak_consecutive_displacement_finds_the_biggest_single_jump():
    traj = [(0.0, 0.0, 0.0), (0.1, 1.0, 0.0), (0.2, 1.0, 100.0), (0.3, 1.0, 105.0)]
    # jumps: 1.0, 100.0, 5.0 -- peak is the middle one
    assert _peak_consecutive_displacement(traj) == pytest.approx(100.0)


def test_peak_consecutive_displacement_none_for_short_trajectory():
    assert _peak_consecutive_displacement([]) is None
    assert _peak_consecutive_displacement([(0.0, 0.0, 0.0)]) is None


# ---- real MediaPipe smoke test ----
# Deliberately NOT a swing-vs-ambient separation assertion: exactly one
# real example of each is the "1-vs-1, never scaled" state this feature
# was explicitly rebuilt to move past (see README's Task 2 pose+audio
# writeup for the real, scaled validation) -- asserting a real inequality
# from a single pair here would just reintroduce that same small-sample
# overconfidence in a different place. This only proves the real
# pipeline runs end-to-end and returns plausible, well-formed output.

@pytest.mark.e2e
def test_wrist_displacement_real_smoke():
    ref = Path(__file__).resolve().parent.parent / "reference_clips" / "clip_base3.mkv"
    model = Path(__file__).resolve().parent.parent / ".cache" / "models" / "pose_landmarker_full.task"
    if not ref.exists() or not model.exists():
        pytest.skip("reference_clips/clip_base3.mkv or the pose model is not available")

    from pipeline.calibration import resolve_zone
    from pipeline.detection import DetectionConfig, detect_persons

    zone = resolve_zone(str(ref))
    det = detect_persons(str(ref), DetectionConfig(),
                         cache_dir=str(Path(__file__).resolve().parent.parent / ".cache" / "detections"))

    result = wrist_displacement(str(ref), det.times, det.boxes, zone, center_s=12.0)
    assert result is not None
    assert result["peak_displacement_px"] >= 0
    assert 0.0 <= result["min_visibility"] <= 1.0
    assert result["n_frames_with_pose"] > 0
