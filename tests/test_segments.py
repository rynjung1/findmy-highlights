"""Unit tests for the pure segment logic (no video required)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.segments import (HardCutConfig, SegmentConfig, SkipSuggestionConfig,
                               apply_hard_cuts, find_cut_windows,
                               find_skip_suggestions, hard_cut_overlaps_required,
                               merge_segments, scores_to_segments, segment_covers,
                               smooth_scores, total_duration)


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


def test_enter_side_boost_opens_segment_raw_alone_would_miss():
    """Mirrors exactly what pipeline.run.process_video does with the
    scale boost: raw motion never reaches enter_thresh on its own (a
    distance-degraded real event), but the boosted 'enter' signal does,
    while the UNBOOSTED raw signal still governs the sustain/exit side
    -- confirming the boost only ever affects whether a segment opens,
    never how long it stays open once it has."""
    c = cfg(enter_thresh=0.5, exit_thresh=0.3)
    t = np.arange(0, 10, 0.1)
    raw = np.where((t >= 3) & (t <= 5), 0.4, 0.05)   # peaks at 0.4, below enter_thresh=0.5
    boost = 1.5
    boosted = raw * boost ** 2                        # 0.4*2.25 = 0.9, clears enter_thresh

    assert scores_to_segments(t, raw, c) == []         # raw alone: never opens

    segs = scores_to_segments(t, boosted, c, sustain_scores=raw)
    assert len(segs) == 1
    a, b = segs[0]
    assert abs(a - 3.0) < 0.2
    assert b < 5.2   # closes promptly once raw drops below exit_thresh=0.3

    # a DIFFERENT boost factor (still enough to cross enter_thresh) must
    # produce the exact same close time -- proof the boost magnitude
    # cannot leak into the exit/sustain decision, only sustain_scores can
    other_boosted = raw * 3.0 ** 2
    other_segs = scores_to_segments(t, other_boosted, c, sustain_scores=raw)
    assert other_segs[0][1] == b


def test_smooth_scores_preserves_length_and_mean():
    t = np.arange(0, 10, 0.1)
    s = np.random.RandomState(0).rand(len(t))
    sm = smooth_scores(t, s, 1.0)
    assert len(sm) == len(s)
    assert abs(np.mean(sm) - np.mean(s)) < 0.05


SKIP_CFG = SkipSuggestionConfig(quiet_thresh=0.006, min_dip_s=2.0)


def test_skip_suggestions_finds_real_dip():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)          # active throughout
    s[(t >= 10.0) & (t < 15.0)] = 0.001   # 5s genuine quiet stretch
    out = find_skip_suggestions(0.0, 30.0, t, s, SKIP_CFG)
    assert len(out) == 1
    (a, b), = out
    assert abs(a - 10.0) < 0.15
    assert abs(b - 15.0) < 0.15


def test_skip_suggestions_ignores_short_dip():
    # 1.0s dip, below min_dip_s=2.0 -- not worth a suggestion
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 11.0)] = 0.001
    out = find_skip_suggestions(0.0, 30.0, t, s, SKIP_CFG)
    assert out == []


def test_skip_suggestions_none_when_no_dip():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    assert find_skip_suggestions(0.0, 30.0, t, s, SKIP_CFG) == []


def test_skip_suggestions_dip_touching_segment_end():
    # dip runs right up to the segment's own end (no active sample after
    # it within this segment) -- must still be reported, not dropped
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 20.0)] = 0.001
    out = find_skip_suggestions(0.0, 25.0, t, s, SKIP_CFG)
    assert len(out) == 1
    (a, b), = out
    assert abs(a - 20.0) < 0.15
    assert abs(b - 24.9) < 0.2


def test_skip_suggestions_restricted_to_segment_window():
    # a real dip OUTSIDE [seg_start, seg_end] must not be reported
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 1.0) & (t < 6.0)] = 0.001   # dip before the segment starts
    out = find_skip_suggestions(10.0, 20.0, t, s, SKIP_CFG)
    assert out == []


def test_skip_suggestions_multiple_dips():
    t = np.arange(0, 40, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 5.0) & (t < 8.0)] = 0.001
    s[(t >= 20.0) & (t < 23.5)] = 0.001
    out = find_skip_suggestions(0.0, 40.0, t, s, SKIP_CFG)
    assert len(out) == 2


# ---- HardCutConfig / find_cut_windows / apply_hard_cuts (real, destructive
# cuts, restorable via the Edit Log -- see README's hard-cut writeup) ----

CUT_CFG = HardCutConfig(quiet_thresh=0.006, min_raw_dip_s=0.5, merge_gap_s=1.5,
                        buffer_s=0.5)


def test_hard_cut_config_defaults_match_what_was_validated():
    cfg = HardCutConfig()
    assert cfg.quiet_thresh == 0.002
    assert cfg.min_raw_dip_s == 0.5
    assert cfg.merge_gap_s == 1.5
    assert cfg.buffer_s == 0.5


def test_find_cut_windows_finds_real_quiet_stretch_shrunk_by_buffer():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    out = find_cut_windows(0.0, 30.0, t, s, CUT_CFG)
    assert len(out) == 1
    (a, b), = out
    assert abs(a - 10.5) < 0.15
    assert abs(b - 15.5) < 0.15


def test_find_cut_windows_short_dip_disappears_after_buffer():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 10.8)] = 0.001
    assert find_cut_windows(0.0, 30.0, t, s, CUT_CFG) == []


def test_find_cut_windows_merges_dips_within_merge_gap():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 12.0)] = 0.001
    s[(t >= 13.0) & (t < 16.0)] = 0.001
    out = find_cut_windows(0.0, 30.0, t, s, CUT_CFG)
    assert len(out) == 1
    (a, b), = out
    assert abs(a - 10.5) < 0.15
    assert abs(b - 15.5) < 0.15


def test_find_cut_windows_does_not_merge_distant_dips():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 12.0)] = 0.001
    s[(t >= 14.0) & (t < 17.0)] = 0.001
    out = find_cut_windows(0.0, 30.0, t, s, CUT_CFG)
    assert len(out) == 2


def test_find_cut_windows_restricted_to_segment_window():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 1.0) & (t < 6.0)] = 0.001
    out = find_cut_windows(10.0, 20.0, t, s, CUT_CFG)
    assert out == []


def test_apply_hard_cuts_splits_segment_around_a_real_cut():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    kept, cuts = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG)
    assert len(kept) == 2
    (a1, b1), (a2, b2) = kept
    assert abs(a1 - 5.0) < 0.01 and abs(b1 - 10.5) < 0.15
    assert abs(a2 - 15.5) < 0.15 and abs(b2 - 25.0) < 0.01
    assert len(cuts) == 1


def test_apply_hard_cuts_no_cut_when_nothing_qualifies():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    kept, cuts = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG)
    assert kept == [(5.0, 25.0)]
    assert cuts == []


def test_apply_hard_cuts_treats_multiple_segments_independently():
    t = np.arange(0, 40, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    kept, cuts = apply_hard_cuts([(5.0, 20.0), (25.0, 35.0)], t, s, CUT_CFG)
    assert len(kept) == 3
    assert kept[-1] == (25.0, 35.0)
    assert len(cuts) == 1


# ---- protected_windows: the actual exclusion mechanism (this is what
# was missing in the first real implementation -- nothing anywhere ever
# called apply_hard_cuts with a filter, so "exclusion" only ever
# detected overlaps after the fact via hard_cut_overlaps_required below,
# never prevented them) ----

def test_apply_hard_cuts_protected_window_drops_the_whole_overlapping_cut():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    # the real cut window here is ~(10.5, 15.5); protect a window that
    # only touches part of it -- must drop the WHOLE candidate, not
    # shrink it to the non-overlapping remainder (conservative,
    # all-or-nothing, same as vetoed_overlapping_required's own check)
    kept, cuts = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG,
                                 protected_windows=[(14.0, 20.0)])
    assert kept == [(5.0, 25.0)]
    assert cuts == []


def test_apply_hard_cuts_protected_window_leaves_other_cuts_alone():
    t = np.arange(0, 40, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001   # will be protected
    s[(t >= 25.0) & (t < 31.0)] = 0.001   # not protected, should still cut
    kept, cuts = apply_hard_cuts([(5.0, 36.0)], t, s, CUT_CFG,
                                 protected_windows=[(12.0, 13.0)])
    assert len(cuts) == 1
    ca, cb = cuts[0]
    assert abs(ca - 25.5) < 0.15 and abs(cb - 30.5) < 0.15
    # the protected dip's own span must survive fully intact in kept
    assert any(a <= 10.0 and b >= 16.0 for a, b in kept)


def test_apply_hard_cuts_no_protected_windows_behaves_as_unconditional():
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    kept_none, cuts_none = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG,
                                           protected_windows=None)
    kept_empty, cuts_empty = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG,
                                             protected_windows=[])
    assert kept_none == kept_empty
    assert cuts_none == cuts_empty
    assert len(cuts_none) == 1  # confirms this is genuinely unconditional


def test_hard_cut_overlaps_required_detects_real_overlap():
    bad = hard_cut_overlaps_required(
        [(10.5, 15.5)], [{"id": "e1", "window": [14, 20], "required": True}])
    assert len(bad) == 1
    (window, eid) = bad[0]
    assert window == (10.5, 15.5)
    assert eid == "e1"


def test_hard_cut_overlaps_required_no_overlap_is_clean():
    bad = hard_cut_overlaps_required(
        [(10.5, 12.5)], [{"id": "e1", "window": [14, 20], "required": True}])
    assert bad == []


def test_hard_cut_overlaps_required_is_empty_when_protection_was_real():
    # end-to-end: applying a real protected_windows filter, then checking
    # the SURVIVING cuts against the same required event, must come back
    # clean -- this is the actual property that failed in the first real
    # implementation (nothing was ever filtered, so this check only ever
    # ran against unfiltered cuts and correctly found real overlaps)
    t = np.arange(0, 30, 0.1)
    s = np.full(len(t), 0.02)
    s[(t >= 10.0) & (t < 16.0)] = 0.001
    required = [{"id": "e1", "window": [14, 20], "required": True}]
    protected = [e["window"] for e in required]
    _, cuts = apply_hard_cuts([(5.0, 25.0)], t, s, CUT_CFG,
                              protected_windows=protected)
    assert hard_cut_overlaps_required(cuts, required) == []
