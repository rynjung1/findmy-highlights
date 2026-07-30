"""Unit tests for play extension, padding, and final merge (pure logic)."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.refine import (RefineConfig, departure_times, dynamic_pad_and_merge,
                             dynamic_post_pad, dynamic_pre_pad, extend_segments,
                             pad_and_merge, refine_segments,
                             zone_close_candidate)
from pipeline.settle import SettleConfig

CFG = RefineConfig(settle=SettleConfig(threshold=0.002, min_quiet_s=1.5),
                   trail_cap_s=12.0, occupancy_lookback_s=5.0,
                   departure_slack_s=2.0, pre_pad_s=4.0, post_pad_s=2.0,
                   final_merge_gap_s=1.0)


def motion(n=200, dt=0.1, base=0.001):
    t = np.arange(n) * dt
    return t, np.full(n, base)


def test_departure_times():
    det_t = [0, 1, 2, 3, 4, 5]
    occ = [True, True, False, False, True, False]
    assert departure_times(det_t, occ) == [2.0, 5.0]


def test_no_departure_no_extension():
    t, m = motion()
    m[(t > 5) & (t < 9)] = 0.01   # residual activity after the segment
    out = extend_segments([(2.0, 5.0)], t, m, [], [], 20.0, CFG)
    assert out == [(2.0, 5.0)]


def test_extension_holds_while_motion_above_settle_floor():
    t, m = motion(base=0.0005)
    m[(t >= 5) & (t < 9)] = 0.0025   # above settle_low, below exit
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG)
    (a, e), = out
    assert a == 2.0
    assert 8.7 <= e <= 9.2           # held through the residual tail


def test_extension_bridges_brief_dip_below_settle_floor():
    # motion dips below settle_low for less than min_quiet_s, then resumes
    # (mirrors clip_60's real trouble spot: a 0.9s lull mid-fielding-play).
    # The old "stop at the first low sample" behavior would end the
    # extension inside the dip; the fix must bridge through to the second
    # burst.
    t, m = motion(base=0.0005)
    m[(t >= 5.0) & (t < 6.0)] = 0.0025   # first burst
    m[(t >= 6.0) & (t < 6.8)] = 0.0005   # 0.8s dip, below min_quiet_s=1.5
    m[(t >= 6.8) & (t < 8.0)] = 0.0025   # second burst
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG)
    (_, e), = out
    assert e >= 7.9   # extended through to the end of the second burst


def test_extension_stops_after_sustained_quiet():
    # a genuinely quiet stretch >= min_quiet_s must end the extension at
    # the last active sample, not run on to a later unrelated burst
    t, m = motion(base=0.0005)
    m[(t >= 5.0) & (t < 6.0)] = 0.0025    # real play tail
    m[(t >= 6.0) & (t < 9.0)] = 0.0005    # 3s genuinely quiet (>= min_quiet_s)
    m[(t >= 12.0) & (t < 13.0)] = 0.0025  # unrelated later burst
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG)
    (_, e), = out
    assert 5.9 <= e <= 6.1   # ends at the last active sample, not at 13


def test_extension_respects_trail_cap():
    t, m = motion(n=400, base=0.0025)  # never settles
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 40.0, CFG)
    (_, e), = out
    assert abs(e - 17.0) < 0.2       # 5.0 + trail_cap 12.0


def test_trail_cap_origin_is_raw_end_not_a_moving_target():
    # oscillating activity that individually never reaches min_quiet_s
    # (each dip is 1.0s, below the 1.5s debounce) — repeated near-misses
    # must not drift cap_end forward; it stays anchored to the RAW
    # segment end b, regardless of how much "almost settled" scanning
    # happens in between
    t, m = motion(n=400, base=0.0025)
    for k in range(6, 17, 2):           # 1.0s quiet gaps every 2s, 6..16
        m[(t >= k) & (t < k + 1.0)] = 0.0005
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 40.0, CFG)
    (_, e), = out
    cap_end = 5.0 + CFG.trail_cap_s      # == 17.0
    assert e <= cap_end + 0.15           # never exceeds the cap
    assert abs(e - cap_end) < 0.3        # and reaches ~exactly the cap,
                                          # since nothing ever settles


def test_trail_cap_scales_with_raw_end_not_extension_start():
    # same oscillation pattern, but the raw segment ends later — cap_end
    # must shift with it, proving cap_end is derived from b each call
    t, m = motion(n=400, base=0.0025)
    for k in range(11, 22, 2):
        m[(t >= k) & (t < k + 1.0)] = 0.0005
    out = extend_segments([(2.0, 10.0)], t, m, [9.0], [], 40.0, CFG)
    (_, e), = out
    cap_end = 10.0 + CFG.trail_cap_s     # == 22.0
    assert e <= cap_end + 0.15
    assert abs(e - cap_end) < 0.3


def test_atbat_fire_closes_extension_early():
    t, m = motion(n=400, base=0.0025)  # would run to the cap otherwise
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [9.5], 40.0, CFG)
    (_, e), = out
    assert abs(e - 9.5) < 0.2


def test_departure_lookback_before_open_counts():
    # batter left 3s before the segment opened (hit in a quiet instant)
    t, m = motion(base=0.0025)
    out = extend_segments([(10.0, 12.0)], t, m, [7.5], [], 20.0, CFG)
    (_, e), = out
    assert e > 12.0                   # extension applied


def test_departure_shortly_after_end_counts():
    # occupancy is a lagging signal: the recorded departure can land just
    # after the raw segment end (hit at seg end, zone held by the catcher
    # for another moment) — extension must still apply
    t, m = motion(base=0.0025)
    out = extend_segments([(2.0, 5.0)], t, m, [6.5], [], 20.0, CFG)
    (_, e), = out
    assert e > 5.0


def test_departure_too_far_after_end_ignored():
    t, m = motion(base=0.0025)
    out = extend_segments([(2.0, 5.0)], t, m, [8.0], [], 20.0, CFG)
    assert out == [(2.0, 5.0)]


def test_departure_too_far_back_ignored():
    t, m = motion(base=0.0025)
    out = extend_segments([(10.0, 12.0)], t, m, [3.0], [], 20.0, CFG)
    assert out == [(10.0, 12.0)]


def test_extension_clamped_to_duration():
    t, m = motion(base=0.0025)
    out = extend_segments([(15.0, 18.0)], t, m, [17.0], [], 19.0, CFG)
    (_, e), = out
    assert e <= 19.0


def test_padding_and_clamp():
    out = pad_and_merge([(2.0, 5.0), (30.0, 32.0)], 33.0, CFG)
    assert out[0] == (0.0, 7.0)       # pre-pad clamped at 0
    assert out[1] == (26.0, 33.0)     # post-pad clamped at duration


def test_padding_merges_touching_segments():
    # padded: (0,9) and (8,15) overlap -> one segment
    out = pad_and_merge([(4.0, 7.0), (12.0, 13.0)], 60.0, CFG)
    assert out == [(0.0, 15.0)]


def _zone_series(dt=0.1, n=200, spike_start=5.2, spike_end=5.6,
                 spike_v=0.5, rest_v=0.05):
    t = np.arange(n) * dt
    v = np.full(n, rest_v)
    v[(t >= spike_start) & (t < spike_end)] = spike_v
    return t, v


ZONE_SETTLE = SettleConfig(threshold=0.20, min_quiet_s=1.5)


def test_zone_close_candidate_no_arrival_returns_none():
    # resting fielder: velocity never crosses the arrival gate -- must
    # never produce a candidate (Stage 10's resting-fielder confound)
    t, v = _zone_series(spike_v=0.05)
    assert zone_close_candidate(t, v, 0.0, 20.0, 0.20, ZONE_SETTLE) is None


def test_zone_close_candidate_arrival_never_settles_returns_none():
    t, v = _zone_series(spike_start=5.0, spike_end=20.0, spike_v=0.5)
    assert zone_close_candidate(t, v, 0.0, 20.0, 0.20, ZONE_SETTLE) is None


def test_zone_close_candidate_arrival_then_settle():
    t, v = _zone_series()  # spike 5.2-5.6
    cand = zone_close_candidate(t, v, 0.0, 20.0, 0.20, ZONE_SETTLE)
    assert cand is not None
    assert 6.9 <= cand <= 7.3   # ~5.6 + min_quiet_s 1.5


def test_zone_tightens_when_whole_frame_tail_is_monotonic():
    # whole-frame stays elevated 5.0-8.0 as one continuous tail (never
    # dips and rises), quiet after -- the same real event the zone
    # tracked, just seen field-wide instead of at-the-base
    t = np.arange(200) * 0.1
    m = np.full(200, 0.0005)
    m[(t >= 5.0) & (t < 8.0)] = 0.0025
    zt, zv = _zone_series()   # arrival 5.2-5.6, settles ~7.1
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG,
                          zone_velocities={"first": (zt, zv)})
    (_, e), = out
    assert e < 7.5     # tightened relative to whole-frame-only (~7.9)
    assert e >= 6.9     # roughly at the zone's own settle time


def test_zone_does_not_tighten_across_a_real_reactivation():
    # whole-frame: active 5.0-5.6 (the play), a 1.4s dip (bridges, per
    # the existing settle logic), reactivates 7.0-7.5 -- simulating a
    # relay throw / continued play elsewhere in frame -- quiet after 7.5.
    # The base zone itself settles at ~6.9 and never sees the
    # reactivation (it's local to the base, the relay isn't). This is
    # exactly the double-play/relay case flagged as a known limitation
    # in pipeline/refine.py's ZONE-LOCAL CLOSE docstring: the guard here
    # is what keeps it from silently over-tightening in the common case
    # where the continued play shows up in whole-frame motion.
    t = np.arange(300) * 0.1
    m = np.full(300, 0.0005)
    m[(t >= 5.0) & (t < 5.6)] = 0.0025
    m[(t >= 7.0) & (t < 7.5)] = 0.0025
    zt, zv = _zone_series(spike_start=5.2, spike_end=5.35)  # settles ~6.9
    out = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG,
                          zone_velocities={"first": (zt, zv)})
    (_, e), = out
    assert e >= 7.3   # NOT tightened -- whole-frame's reactivation wins


def test_zone_velocities_empty_dict_is_unchanged():
    t, m = motion()
    m[(t > 5) & (t < 9)] = 0.0025
    out_without = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG)
    out_with_empty = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG,
                                     zone_velocities={})
    assert out_without == out_with_empty


def test_zone_velocities_default_none_is_unchanged():
    t, m = motion()
    m[(t > 5) & (t < 9)] = 0.0025
    with_default = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG)
    explicit_none = extend_segments([(2.0, 5.0)], t, m, [4.0], [], 20.0, CFG, None)
    assert with_default == explicit_none


DYN_CFG = RefineConfig(pre_pad_s=4.0, post_pad_s=2.0,
                       pre_pad_floor_s=1.0, post_pad_floor_s=0.5,
                       pad_quiet_thresh=0.002, pre_pad_min_contig_s=1.5,
                       post_pad_min_contig_s=0.5, pad_safety_buffer_s=1.0,
                       final_merge_gap_s=0.5)


def test_dynamic_pre_pad_shrinks_when_fully_quiet():
    # entire eligible window [a-4.0, a-1.0] reads quiet -> shrinks toward
    # (ceiling - eligible_span + buffer), never below the floor
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0005)
    result = dynamic_pre_pad(10.0, t, s, DYN_CFG)
    assert 1.0 <= result < 4.0        # shrank, but never below the floor
    assert result == pytest.approx(2.0, abs=0.2)


def test_dynamic_pre_pad_stays_at_ceiling_when_active_throughout():
    # real motion present across the whole eligible window -- no quiet
    # run to trust at all, must stay at the full ceiling
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0025)   # above pad_quiet_thresh
    result = dynamic_pre_pad(10.0, t, s, DYN_CFG)
    assert result == 4.0


def test_dynamic_pre_pad_stays_at_ceiling_when_quiet_run_too_short():
    # only 1.0s of contiguous quiet from the far edge, below
    # pre_pad_min_contig_s=1.5 -- must not be trusted at all
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0025)
    s[(t >= 6.0) & (t < 7.0)] = 0.0005   # 1.0s quiet right at the far edge
    result = dynamic_pre_pad(10.0, t, s, DYN_CFG)
    assert result == 4.0


def test_dynamic_pre_pad_stance_shift_case_not_clipped():
    # the exact risk this design targets: the window looks quiet for the
    # FAR portion, then a slow ramp begins partway through (a stance shift
    # before an early swing) -- the ramp itself must never be trimmed into,
    # and the safety buffer must keep real margin between the trim point
    # and where the ramp actually starts.
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0005)
    s[(t >= 8.2) & (t < 10.0)] = 0.003   # ramp starts at 8.2, well inside
                                          # the eligible window [6.0, 9.0]
    result = dynamic_pre_pad(10.0, t, s, DYN_CFG)
    kept_from = 10.0 - result
    assert kept_from <= 8.2 - 0.5   # real margin before the ramp, not clipped


def test_dynamic_post_pad_direction_matches_pre_pad():
    # regression test for a real bug found while validating this design:
    # the far-edge-inward scan must handle the post side's DESCENDING time
    # order correctly (far edge has the LARGER timestamp) -- an earlier
    # version computed a negative duration here and silently never shrank
    # the post side at all, on any clip. Mirrors the pre-side quiet case
    # exactly, just on the trailing side.
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0005)
    result = dynamic_post_pad(10.0, t, s, DYN_CFG)
    assert 0.5 <= result < 2.0
    assert result == pytest.approx(1.5, abs=0.2)


def test_dynamic_post_pad_stays_at_ceiling_when_active_throughout():
    t = np.arange(0, 150) * 0.1
    s = np.full(len(t), 0.0025)
    result = dynamic_post_pad(10.0, t, s, DYN_CFG)
    assert result == 2.0


def test_dynamic_pad_floor_never_violated_even_when_entire_clip_silent():
    t = np.arange(0, 300) * 0.1
    s = np.full(len(t), 0.0)
    assert dynamic_pre_pad(10.0, t, s, DYN_CFG) >= DYN_CFG.pre_pad_floor_s
    assert dynamic_post_pad(10.0, t, s, DYN_CFG) >= DYN_CFG.post_pad_floor_s


def test_dynamic_pad_and_merge_shrinks_and_merges():
    t = np.arange(0, 300) * 0.1
    s = np.full(len(t), 0.0005)   # quiet everywhere
    out = dynamic_pad_and_merge([(10.0, 12.0)], 29.0, t, s, DYN_CFG)
    (a, b), = out
    assert a > 10.0 - 4.0     # shrank below the fixed ceiling
    assert b < 12.0 + 2.0
    assert a >= 10.0 - 4.0 and b <= 12.0 + 2.0   # never exceeds the ceiling


def test_refine_segments_without_motion_scores_matches_pad_and_merge():
    # backward compatibility: omitting motion_scores must be byte-for-byte
    # identical to the previous fixed-padding behavior
    t, m = motion(n=600, base=0.0005)
    m[(t >= 20) & (t < 26)] = 0.0025
    det_t = list(range(0, 60))
    occ = [5 <= x < 19 for x in det_t]
    via_refine = refine_segments([(10.0, 20.0)], t, m, det_t, occ, [40.0], 60.0, CFG)
    extended = extend_segments([(10.0, 20.0)], t, m, departure_times(det_t, occ),
                               [40.0], 60.0, CFG)
    via_static = pad_and_merge(extended, 60.0, CFG)
    assert via_refine == via_static


def test_refine_segments_with_motion_scores_shrinks_padding():
    # a genuinely quiet run-up (raw scores, separate from motion_smooth)
    # lets refine_segments shrink pre-padding below the fixed ceiling
    t, m = motion(n=600, base=0.0005)
    m[(t >= 20) & (t < 26)] = 0.0025
    raw = np.full(len(t), 0.0005)   # quiet raw scores throughout
    det_t = list(range(0, 60))
    occ = [5 <= x < 19 for x in det_t]
    dyn_cfg = RefineConfig(settle=CFG.settle, trail_cap_s=CFG.trail_cap_s,
                           occupancy_lookback_s=CFG.occupancy_lookback_s,
                           departure_slack_s=CFG.departure_slack_s,
                           pre_pad_s=CFG.pre_pad_s, post_pad_s=CFG.post_pad_s,
                           final_merge_gap_s=CFG.final_merge_gap_s,
                           pre_pad_floor_s=1.0, post_pad_floor_s=0.5,
                           pre_pad_min_contig_s=1.5, post_pad_min_contig_s=0.5,
                           pad_safety_buffer_s=1.0)
    final = refine_segments([(10.0, 20.0)], t, m, det_t, occ, [40.0], 60.0,
                            dyn_cfg, motion_scores=raw)
    (a, b), = final
    assert a > 10.0 - CFG.pre_pad_s   # pre-pad shrank below the fixed 4.0 ceiling


def test_full_refine_pipeline():
    t, m = motion(n=600, base=0.0005)
    m[(t >= 20) & (t < 26)] = 0.0025   # play tail after raw end at 20
    det_t = list(range(0, 60))
    occ = [5 <= x < 19 for x in det_t]  # batter departs at 19, inside segment
    final = refine_segments([(10.0, 20.0)], t, m, det_t, occ, [40.0],
                            60.0, CFG)
    (a, b), = final
    assert a == 6.0                    # 10 - pre_pad
    assert 27.0 <= b <= 28.5           # extended to ~25.9 + post_pad 2
