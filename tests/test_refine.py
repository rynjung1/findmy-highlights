"""Unit tests for play extension, padding, and final merge (pure logic)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.refine import (RefineConfig, departure_times, extend_segments,
                             pad_and_merge, refine_segments)
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
