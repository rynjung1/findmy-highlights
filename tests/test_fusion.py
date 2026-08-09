"""Unit tests for signal fusion: ZOH alignment, additive score guarantee,
plate occupancy, and the veto rule with its safety net. Pure numpy."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.fusion import (FusionConfig, PlateZone, apply_veto,
                             boxes_to_grid_mask, calibrated_scale_boost_factor,
                             compute_occupancy, fuse, occupancy_near_times,
                             robust_box_width, scale_boost_factor,
                             vetoed_overlapping_required, FusedResult)

# Geometry used throughout: 1920x1080 source, 480x270 analysis, no border,
# 16x9 grid -> each grid cell is 120x120 source px.
FRAME = (1920, 1080)
ANALYSIS = (480, 270)
BORDER = (0, 0)
GRID = (9, 16)


def make_motion(n=100, dt=0.1, base=0.0):
    times = np.arange(n) * dt
    scores = np.full(n, base)
    grids = np.zeros((n, *GRID))
    return times, scores, grids


def centered_box(cx, cy, w=80, h=200):
    return [cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2]


ZONE = PlateZone(center_xy=(1147, 840), radius_px=280)


def fuse_simple(times, scores, grids, det_times, det_boxes, zone=None, cfg=None):
    return fuse(times, scores, grids, FRAME, ANALYSIS, BORDER,
                det_times, det_boxes, zone, cfg or FusionConfig())


# ---------- additive guarantee ----------

def test_combined_never_below_motion():
    times, scores, grids = make_motion(50)
    rng = np.random.RandomState(0)
    scores[:] = rng.rand(50) * 0.05
    grids[:] = rng.rand(50, *GRID) * 0.1
    det_times = list(np.arange(0, 5, 1.0))
    det_boxes = [[centered_box(400, 500)] for _ in det_times]
    fused = fuse_simple(times, scores, grids, det_times, det_boxes, ZONE)
    assert np.all(fused.combined >= fused.motion - 1e-12)


def test_person_motion_adds_to_score():
    times, scores, grids = make_motion(10)
    scores[:] = 0.01
    grids[:, 4, 4] = 0.5  # motion in cell (r4, c4) ~ source px (480-600, 480-600)
    det_times = [0.0]
    det_boxes = [[centered_box(540, 540)]]  # box over that cell
    fused = fuse_simple(times, scores, grids, det_times, det_boxes, None,
                        FusionConfig(staleness_s=100))
    assert np.all(fused.combined > fused.motion)
    assert np.all(fused.person_motion > 0)


# ---------- ZOH staleness ----------

def test_stale_detection_falls_back_to_motion_only():
    times, scores, grids = make_motion(100)  # 0..9.9s
    det_times = [0.0]                        # only one detection at t=0
    det_boxes = [[centered_box(540, 540)]]
    fused = fuse_simple(times, scores, grids, det_times, det_boxes, ZONE,
                        FusionConfig(staleness_s=0.75))
    late = fused.times > 1.0
    assert not fused.det_valid[late].any()      # stale beyond bound
    assert fused.covered[late].all()            # protective default
    assert np.all(fused.combined[late] == fused.motion[late])


def test_no_detections_at_all_is_pure_motion():
    times, scores, grids = make_motion(20, base=0.02)
    fused = fuse_simple(times, scores, grids, [], [])
    assert np.all(fused.combined == fused.motion)
    assert fused.covered.all()
    assert not fused.det_valid.any()


# ---------- occupancy ----------

def test_stationary_person_in_zone_is_occupied():
    det_times = [0.0, 1.0, 2.0]
    b = centered_box(*ZONE.center_xy)
    det_boxes = [[b], [b], [b]]
    occ = compute_occupancy(det_times, det_boxes, ZONE, stationary_v=0.3)
    assert not occ[0]          # no predecessor sample yet
    assert occ[1] and occ[2]


def test_running_person_in_zone_not_occupied():
    det_times = [0.0, 1.0]
    # moves 400px in 1s with 200px box height -> 2 box-heights/s >> 0.3
    det_boxes = [[centered_box(1000, 840)], [centered_box(1400, 840)]]
    zone = PlateZone(center_xy=(1400, 840), radius_px=280)
    occ = compute_occupancy(det_times, det_boxes, zone, stationary_v=0.3)
    assert not occ.any()


def test_occupied_state_survives_fast_movement_in_zone():
    # batter settles (stationary), then strides fast but stays in zone:
    # hysteresis must hold the occupied state through the stride
    det_times = [0.0, 1.0, 2.0]
    b_settled = centered_box(*ZONE.center_xy)
    b_stride = centered_box(ZONE.center_xy[0] + 250, ZONE.center_xy[1])
    occ = compute_occupancy(det_times, [[b_settled], [b_settled], [b_stride]],
                            ZONE, stationary_v=0.3)
    assert occ[1]      # entered while stationary
    assert occ[2]      # held during fast in-zone movement


def test_occupied_state_clears_when_zone_empties():
    det_times = [0.0, 1.0, 2.0]
    b = centered_box(*ZONE.center_xy)
    far = centered_box(200, 200)
    occ = compute_occupancy(det_times, [[b], [b], [far]], ZONE,
                            stationary_v=0.3)
    assert occ[1] and not occ[2]


def test_person_outside_zone_not_occupied():
    det_times = [0.0, 1.0]
    b = centered_box(200, 200)
    occ = compute_occupancy(det_times, [[b], [b]], ZONE, stationary_v=0.3)
    assert not occ.any()


# ---------- Stage 10: relaxed (non-stationary) entry for base zones ----------

def test_default_entry_still_requires_stationary():
    # calling compute_occupancy() the exact way fuse() always has (no new
    # arg) must be unaffected -- a fast-moving person in the zone on their
    # very first sample there is NOT occupied by default
    det_times = [0.0, 1.0]
    fast = [centered_box(1000, 840), centered_box(1400, 840)]
    zone = PlateZone(center_xy=(1400, 840), radius_px=280)
    occ = compute_occupancy(det_times, [[fast[0]], [fast[1]]], zone,
                            stationary_v=0.3)
    assert not occ.any()


def test_relaxed_entry_occupies_on_fast_arrival():
    # the Stage 10 finding: a fielder arriving at speed to make a play
    # should register as occupied immediately, not wait for them to slow
    # down -- require_stationary_entry=False is the base-zone mode
    det_times = [0.0, 1.0]
    zone = PlateZone(center_xy=(1400, 840), radius_px=280)
    fast_arrival = [centered_box(1000, 840), centered_box(1400, 840)]
    occ = compute_occupancy(det_times, [[fast_arrival[0]], [fast_arrival[1]]],
                            zone, stationary_v=0.3,
                            require_stationary_entry=False)
    assert occ[1]   # in zone on arrival -> occupied immediately, no lag


def test_relaxed_entry_first_sample_can_be_occupied():
    # with no predecessor to check stationarity against, the strict mode
    # can never mark sample 0 occupied (see test_stationary_person_in_
    # zone_is_occupied's occ[0] assertion) -- relaxed mode isn't limited
    # by that, since it only needs presence, not a velocity estimate
    det_times = [0.0]
    b = centered_box(*ZONE.center_xy)
    occ = compute_occupancy(det_times, [[b]], ZONE, stationary_v=0.3,
                            require_stationary_entry=False)
    assert occ[0]


def test_relaxed_entry_still_clears_when_zone_empties():
    # relaxing entry doesn't relax the exit rule -- it's still "occupied
    # only while someone is actually in the zone"
    det_times = [0.0, 1.0, 2.0]
    b = centered_box(*ZONE.center_xy)
    far = centered_box(200, 200)
    occ = compute_occupancy(det_times, [[b], [b], [far]], ZONE,
                            stationary_v=0.3, require_stationary_entry=False)
    assert occ[0] and occ[1] and not occ[2]


def test_compute_all_occupancy_per_named_zone():
    from pipeline.fusion import compute_all_occupancy
    det_times = [0.0, 1.0]
    first = PlateZone(center_xy=(500, 500), radius_px=80)
    third = PlateZone(center_xy=(1500, 500), radius_px=80)
    b_first = centered_box(500, 500)
    boxes = [[b_first], [b_first]]
    occ = compute_all_occupancy(det_times, boxes,
                                {"first": first, "third": third},
                                stationary_v=0.3)
    assert occ["first"].any()
    assert not occ["third"].any()


def test_compute_all_occupancy_defaults_to_relaxed_entry():
    # compute_all_occupancy is new (no existing callers), so it can
    # safely default to what Stage 10's validation found works better
    # for bases, unlike compute_occupancy's own default (True, unchanged
    # for the plate's sake)
    from pipeline.fusion import compute_all_occupancy
    det_times = [0.0, 1.0]
    zone = PlateZone(center_xy=(1400, 840), radius_px=280)
    fast_arrival = [centered_box(1000, 840), centered_box(1400, 840)]
    occ = compute_all_occupancy(det_times, [[fast_arrival[0]], [fast_arrival[1]]],
                                {"first": zone}, stationary_v=0.3)
    assert occ["first"][1]


def test_occupancy_adds_boost():
    times, scores, grids = make_motion(30, base=0.001)
    det_times = list(np.arange(0.0, 3.5, 1.0))
    b = centered_box(*ZONE.center_xy)
    det_boxes = [[b] for _ in det_times]
    cfg = FusionConfig(w_occupancy=0.004, staleness_s=0.75)
    fused = fuse_simple(times, scores, grids, det_times, det_boxes, ZONE, cfg)
    assert fused.occupied.any()
    boosted = fused.combined[fused.occupied]
    assert np.allclose(boosted, 0.001 + 0.004)


# ---------- box -> grid mapping ----------

def test_box_grid_mask_marks_correct_cells():
    mask = boxes_to_grid_mask([centered_box(540, 540, 100, 100)],
                              FRAME, ANALYSIS, BORDER, GRID)
    assert mask[4, 4]
    assert not mask[0, 0]
    assert not mask[8, 15]


def test_box_grid_mask_out_of_frame_clipped():
    mask = boxes_to_grid_mask([[-500, -500, -100, -100]],
                              FRAME, ANALYSIS, BORDER, GRID)
    assert not mask.any()


# ---------- veto ----------

def veto_fixture(covered_pattern, det_valid_pattern):
    n = len(covered_pattern)
    times = np.arange(n) * 1.0
    return FusedResult(
        times=times, combined=np.ones(n), motion=np.ones(n),
        person_motion=np.zeros(n),
        occupied=np.zeros(n, dtype=bool),
        covered=np.array(covered_pattern, dtype=bool),
        det_valid=np.array(det_valid_pattern, dtype=bool))


def test_veto_drops_fully_uncovered_segment():
    fused = veto_fixture([False] * 10, [True] * 10)
    kept, vetoed = apply_veto([(2.0, 7.0)], fused)
    assert kept == [] and vetoed == [(2.0, 7.0)]


def test_one_covered_sample_prevents_veto():
    cov = [False] * 10
    cov[5] = True
    fused = veto_fixture(cov, [True] * 10)
    kept, vetoed = apply_veto([(2.0, 7.0)], fused)
    assert kept == [(2.0, 7.0)] and vetoed == []


def test_one_stale_sample_prevents_veto():
    valid = [True] * 10
    valid[4] = False   # stale sample inside segment -> covered=True protectively
    cov = [False] * 10
    cov[4] = True      # fusion sets covered True when det invalid
    fused = veto_fixture(cov, valid)
    kept, vetoed = apply_veto([(2.0, 7.0)], fused)
    assert kept == [(2.0, 7.0)] and vetoed == []


def test_veto_safety_net_flags_required_overlap():
    bad = vetoed_overlapping_required(
        [(10.0, 15.0), (30.0, 32.0)],
        [{"id": "e9", "window": [14, 20]}])
    assert bad == [((10.0, 15.0), "e9")]


def test_veto_safety_net_clean_when_no_overlap():
    assert vetoed_overlapping_required(
        [(30.0, 32.0)], [{"id": "e9", "window": [14, 20]}]) == []


# ---------- robust_box_width ----------

def test_robust_box_width_returns_median():
    boxes = [[centered_box(1147, 840, w=300)] for _ in range(6)]
    boxes[2] = [centered_box(1147, 840, w=100)]   # outlier, still not clipped
    times = list(range(6))
    w = robust_box_width(times, boxes, FRAME)
    assert w == 300.0   # median of five 300s and one 100 is 300


def test_robust_box_width_ignores_bottom_edge_clipping():
    # box bottom sits 5px from the 1080-tall frame's edge (feet cropped) --
    # must NOT be excluded: vertical cropping doesn't corrupt a WIDTH
    # reading (this is the exact distance_test_close.mov shape; an
    # earlier version of this function wrongly excluded it -- see the
    # function's own docstring).
    clipped = [[627, 400, 991, 1075]] * 6
    boxes = [clipped for _ in range(6)]
    assert robust_box_width(list(range(6)), boxes, FRAME) == 364.0


def test_robust_box_width_ignores_top_edge_clipping():
    clipped = [[627, 2, 991, 400]] * 6
    boxes = [clipped for _ in range(6)]
    assert robust_box_width(list(range(6)), boxes, FRAME) == 364.0


def test_robust_box_width_excludes_left_edge_clipped():
    clipped = [[2, 400, 400, 900]] * 6
    boxes = [clipped for _ in range(6)]
    assert robust_box_width(list(range(6)), boxes, FRAME) is None


def test_robust_box_width_excludes_right_edge_clipped():
    clipped = [[1500, 400, 1918, 900]] * 6
    boxes = [clipped for _ in range(6)]
    assert robust_box_width(list(range(6)), boxes, FRAME) is None


def test_robust_box_width_respects_zone():
    in_zone = centered_box(1147, 840, w=300)
    out_zone = centered_box(50, 50, w=100)
    boxes = [[in_zone, out_zone] for _ in range(6)]
    w = robust_box_width(list(range(6)), boxes, FRAME, zone=ZONE)
    assert w == 300.0   # the out-of-zone box never counted


def test_robust_box_width_none_below_min_samples():
    boxes = [[centered_box(1147, 840, w=300)] for _ in range(3)]
    assert robust_box_width(list(range(3)), boxes, FRAME, min_samples=5) is None


# ---------- scale_boost_factor ----------

def test_scale_boost_is_noop_at_reference_distance():
    # w_batch == reference -> factor is exactly 1.0
    boxes = [[centered_box(1147, 840, w=200)] for _ in range(6)]
    f = scale_boost_factor(list(range(6)), boxes, FRAME, ZONE, reference_width_px=200)
    assert f == 1.0


def test_scale_boost_is_noop_when_batch_is_closer_than_reference():
    # w_batch BIGGER than reference (closer camera) -> never reduce
    boxes = [[centered_box(1147, 840, w=400)] for _ in range(6)]
    f = scale_boost_factor(list(range(6)), boxes, FRAME, ZONE, reference_width_px=200)
    assert f == 1.0


def test_scale_boost_raises_factor_when_batch_is_farther():
    # w_batch HALF the reference -> boost factor is exactly 2.0 (linear;
    # caller squares it before applying to a score)
    boxes = [[centered_box(1147, 840, w=100)] for _ in range(6)]
    f = scale_boost_factor(list(range(6)), boxes, FRAME, ZONE, reference_width_px=200)
    assert f == 2.0


def test_scale_boost_is_noop_with_no_reliable_width_signal():
    # too few in-zone detections to trust a reading -> exactly 1.0, never a guess
    boxes = [[centered_box(1147, 840, w=100)] for _ in range(2)]
    f = scale_boost_factor(list(range(2)), boxes, FRAME, ZONE, reference_width_px=200)
    assert f == 1.0


# ---- calibrated_scale_boost_factor (real-distance replacement for the
# box-width proxy above -- same max(1.0, reference/batch) shape, driven by
# pipeline.calibration.resolve_calibrated_scale_px instead of box width) ----

def test_calibrated_scale_boost_is_noop_at_reference_distance():
    # batch == reference -> factor is exactly 1.0, same as the box-width case
    f = calibrated_scale_boost_factor(batch_scale_px=421.4, reference_scale_px=421.4)
    assert f == 1.0


def test_calibrated_scale_boost_is_noop_when_batch_is_closer_than_reference():
    # batch BIGGER than reference (closer camera) -> never reduce below 1.0
    f = calibrated_scale_boost_factor(batch_scale_px=800.0, reference_scale_px=400.0)
    assert f == 1.0


def test_calibrated_scale_boost_raises_factor_when_batch_is_farther():
    # batch HALF the reference -> boost factor is exactly 2.0 (linear; caller
    # squares it before applying to a motion score, same as scale_boost_factor)
    f = calibrated_scale_boost_factor(batch_scale_px=200.0, reference_scale_px=400.0)
    assert f == 2.0


def test_calibrated_scale_boost_matches_box_width_shape_at_matching_ratios():
    # same batch/reference ratio (100/200 == 0.5) as
    # test_scale_boost_raises_factor_when_batch_is_farther above -> both
    # boost implementations must agree on the shape, not just coincidentally
    # on one number
    calibrated = calibrated_scale_boost_factor(batch_scale_px=100.0, reference_scale_px=200.0)
    boxes = [[centered_box(1147, 840, w=100)] for _ in range(6)]
    box_width = scale_boost_factor(list(range(6)), boxes, FRAME, ZONE, reference_width_px=200)
    assert calibrated == box_width == 2.0


def test_calibrated_scale_boost_falls_back_to_noop_when_batch_scale_is_none():
    # None is resolve_calibrated_scale_px's real "no first-base calibration"
    # signal (see pipeline/calibration.py) -- the caller (pipeline.run.
    # process_video) is documented to fall back to scale_boost_factor
    # itself in that case, but this function must still be a safe no-op on
    # its own rather than raising or guessing
    f = calibrated_scale_boost_factor(batch_scale_px=None, reference_scale_px=421.4)
    assert f == 1.0


def test_calibrated_scale_boost_falls_back_to_noop_when_batch_scale_is_zero_or_negative():
    assert calibrated_scale_boost_factor(batch_scale_px=0.0, reference_scale_px=421.4) == 1.0
    assert calibrated_scale_boost_factor(batch_scale_px=-5.0, reference_scale_px=421.4) == 1.0


# ---- occupancy_near_times (enter-side debounce investigation) ----

def test_occupancy_near_times_true_within_window():
    times = np.array([0.0, 1.0, 2.0, 5.0, 10.0])
    det_times = np.array([1.9])
    occupied = np.array([True])
    result = occupancy_near_times(times, det_times, occupied, window_s=0.5)
    assert list(result) == [False, False, True, False, False]


def test_occupancy_near_times_no_occupied_samples_at_all():
    times = np.array([0.0, 1.0, 2.0])
    det_times = np.array([0.5, 1.5])
    occupied = np.array([False, False])
    result = occupancy_near_times(times, det_times, occupied, window_s=1.0)
    assert list(result) == [False, False, False]


def test_occupancy_near_times_uses_nearest_of_multiple_occupied_samples():
    times = np.array([5.0])
    det_times = np.array([0.0, 4.8, 20.0])
    occupied = np.array([True, True, True])
    # nearest real occupied sample (4.8) is 0.2s away -- within window
    result = occupancy_near_times(times, det_times, occupied, window_s=0.3)
    assert list(result) == [True]


def test_occupancy_near_times_exactly_at_window_boundary_is_true():
    times = np.array([3.0])
    det_times = np.array([1.0])
    occupied = np.array([True])
    result = occupancy_near_times(times, det_times, occupied, window_s=2.0)
    assert list(result) == [True]  # distance == window_s, inclusive
