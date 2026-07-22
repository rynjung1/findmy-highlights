"""Unit tests for the pure segment logic (no video required)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.segments import (SegmentConfig, merge_segments, scores_to_segments,
                               segment_covers, smooth_scores, total_duration)


def cfg(**kw):
    base = dict(smooth_window_s=0.0, enter_thresh=0.5, exit_thresh=0.3,
                merge_gap_s=1.0, min_len_s=0.5)
    base.update(kw)
    return SegmentConfig(**base)


def test_empty_input():
    assert scores_to_segments([], [], cfg()) == []


def test_no_action_flat_low():
    t = np.arange(0, 10, 0.1)
    s = np.full_like(t, 0.01)
    assert scores_to_segments(t, s, cfg()) == []


def test_single_burst():
    t = np.arange(0, 10, 0.1)
    s = np.where((t >= 3) & (t <= 5), 1.0, 0.0)
    segs = scores_to_segments(t, s, cfg())
    assert len(segs) == 1
    a, b = segs[0]
    assert abs(a - 3.0) < 0.2
    assert abs(b - 5.1) < 0.2


def test_hysteresis_keeps_segment_open_through_dip():
    # dips to 0.4 (below enter, above exit) — must NOT split
    t = np.arange(0, 10, 0.1)
    s = np.where((t >= 2) & (t <= 8), 1.0, 0.0)
    s[(t >= 4.5) & (t <= 5.5)] = 0.4
    segs = scores_to_segments(t, s, cfg())
    assert len(segs) == 1


def test_merge_close_segments():
    t = np.arange(0, 10, 0.1)
    s = np.zeros_like(t)
    s[(t >= 1) & (t <= 2)] = 1.0
    s[(t >= 2.5) & (t <= 3.5)] = 1.0  # 0.5s gap < merge_gap_s=1.0
    segs = scores_to_segments(t, s, cfg())
    assert len(segs) == 1


def test_distant_segments_not_merged():
    t = np.arange(0, 20, 0.1)
    s = np.zeros_like(t)
    s[(t >= 1) & (t <= 2)] = 1.0
    s[(t >= 10) & (t <= 11)] = 1.0
    segs = scores_to_segments(t, s, cfg())
    assert len(segs) == 2


def test_min_len_drops_blip():
    t = np.arange(0, 10, 0.1)
    s = np.zeros_like(t)
    s[(t >= 5.0) & (t <= 5.2)] = 1.0  # 0.2s blip < min_len_s=0.5
    assert scores_to_segments(t, s, cfg()) == []


def test_segment_open_at_end_is_closed():
    t = np.arange(0, 10, 0.1)
    s = np.where(t >= 8, 1.0, 0.0)
    segs = scores_to_segments(t, s, cfg())
    assert len(segs) == 1
    assert abs(segs[0][1] - t[-1]) < 1e-9


def test_merge_segments_unsorted_and_overlapping():
    segs = merge_segments([(5, 7), (1, 3), (2.5, 4)], max_gap_s=0.0)
    assert segs == [(1, 4), (5, 7)]


def test_merge_segments_empty():
    assert merge_segments([], 1.0) == []


def test_segment_covers():
    segs = [(10, 20), (30, 40)]
    assert segment_covers(segs, (15, 18))      # inside
    assert segment_covers(segs, (5, 12))       # overlaps start
    assert segment_covers(segs, (38, 45))      # overlaps end
    assert segment_covers(segs, (0, 100))      # spans all
    assert not segment_covers(segs, (21, 29))  # in the gap
    assert not segment_covers([], (0, 1))


def test_total_duration():
    assert total_duration([(0, 2), (10, 13)]) == 5.0
    assert total_duration([]) == 0.0


def test_dual_sustain_extends_but_never_opens():
    t = np.arange(0, 20, 0.1)
    open_s = np.zeros_like(t)
    open_s[(t >= 2) & (t <= 4)] = 1.0          # motion burst 2-4
    sustain = open_s.copy()
    sustain[(t >= 4) & (t <= 6)] = 0.4          # sustain-only signal 4-6
    sustain[(t >= 14) & (t <= 16)] = 1.0        # sustain high with NO open signal
    segs = scores_to_segments(t, open_s, cfg(), sustain_scores=sustain)
    assert len(segs) == 1                       # 14-16 must NOT open a segment
    a, b = segs[0]
    assert abs(a - 2.0) < 0.2
    assert b > 5.9                              # exit delayed by sustain signal


def test_dual_equals_single_when_sustain_is_same():
    t = np.arange(0, 10, 0.1)
    s = np.where((t >= 3) & (t <= 5), 1.0, 0.0)
    assert (scores_to_segments(t, s, cfg(), sustain_scores=s)
            == scores_to_segments(t, s, cfg()))


def test_smooth_scores_preserves_length_and_mean():
    t = np.arange(0, 10, 0.1)
    s = np.random.RandomState(0).rand(len(t))
    sm = smooth_scores(t, s, 1.0)
    assert len(sm) == len(s)
    assert abs(np.mean(sm) - np.mean(s)) < 0.05
