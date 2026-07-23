"""Confirms the Phase 3/4 boundary decision holds: play extension and
at-bat state never cross a file boundary, regardless of gap size.

Rather than re-testing extension/at-bat math (already covered in
test_refine.py / test_atbat.py), this specifically proves NO STATE LEAKS
between two independent per-file calls — the actual risk the decision
guards against. Two synthetic per-file timelines are run through the same
functions scripts/detect_multi.py calls once per file, back to back, with
adversarial values chosen so that if state ever leaked (a shared/global
variable, a carried-over 'still armed' or 'still extending' flag), the
second file's output would visibly differ from processing it alone.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.atbat import AtBatConfig, atbat_start_times
from pipeline.refine import RefineConfig, refine_segments
from pipeline.segments import SegmentConfig, scores_to_segments, smooth_scores
from pipeline.settle import SettleConfig


def make_file(duration, motion_hot_ranges, occ_ranges, dt=0.1, det_dt=1.0):
    """Build one file's synthetic motion + occupancy timeline."""
    times = np.arange(0, duration, dt)
    scores = np.full_like(times, 0.0005)
    for a, b in motion_hot_ranges:
        scores[(times >= a) & (times < b)] = 0.02
    det_times = np.arange(0, duration, det_dt)
    occ = np.zeros(len(det_times), dtype=bool)
    for a, b in occ_ranges:
        occ[(det_times >= a) & (det_times < b)] = True
    return times, scores, det_times, occ


def process_file(duration, motion_hot_ranges, occ_ranges):
    """Mirrors what scripts/detect_multi.py does per file, without any
    video I/O or model inference — pure pipeline logic on synthetic data,
    called completely independently for each file (no shared objects
    passed in, matching the real multi-file loop)."""
    times, scores, det_times, occ = make_file(duration, motion_hot_ranges,
                                              occ_ranges)
    seg_cfg = SegmentConfig()
    settle_cfg = SettleConfig()
    raw = scores_to_segments(times, scores, seg_cfg)
    sm = smooth_scores(times, scores, seg_cfg.smooth_window_s)
    fires = atbat_start_times(det_times, occ, times, sm,
                              AtBatConfig(settle=settle_cfg))
    final = refine_segments(raw, times, sm, det_times, occ, fires,
                            duration, RefineConfig(settle=settle_cfg))
    return final, fires


def test_segment_never_exceeds_its_own_file_duration():
    # File A: an unresolved "play" — motion still hot right at the very
    # last sample, batter departed just before file end, so extension
    # WOULD keep going past file end if it could reach past it
    duration = 20.0
    final, _ = process_file(duration,
                            motion_hot_ranges=[(15.0, 20.0)],
                            occ_ranges=[(0.0, 15.0)])  # departs at t=15
    assert final, "expected at least one segment"
    for a, b in final:
        assert b <= duration + 1e-6, \
            f"segment end {b} exceeds this file's own duration {duration}"


def test_second_file_atbat_detector_starts_fresh_not_mid_fire():
    # File A ends with the plate VACANT and motion still active (an
    # ambiguous, "mid-something" state — the kind of state that COULD
    # leak if atbat_start_times used any module-level or carried mutable
    # state instead of fresh locals each call)
    duration_a = 15.0
    _, fires_a = process_file(duration_a,
                              motion_hot_ranges=[(10.0, 15.0)],
                              occ_ranges=[(0.0, 5.0)])  # vacant 5-15

    # File B is processed by an entirely separate call — nothing from
    # file A is passed in. It opens with a batter already settled
    # (occupied from t=0), motion already quiet: under fresh clip-start
    # arming (armed=True at t=0, the documented rule), this should fire
    # near t=0 once the sustain window confirms it. If file A's state had
    # somehow leaked as "already fired / not armed", this would NOT fire.
    duration_b = 15.0
    _, fires_b = process_file(duration_b,
                              motion_hot_ranges=[],
                              occ_ranges=[(0.0, 15.0)])
    assert fires_b, "file B's at-bat detector must arm fresh at its own " \
        "clip start, independent of file A's trailing state"
    assert fires_b[0] < 5.0


def test_pending_rearm_at_file_end_does_not_carry_into_next_file():
    """Specifically targets the PENDING RE-ARM mechanism (atbat.py's
    "once armed, every occupied sample is evaluated until the detector
    fires or the plate empties again"), not just general armed/unarmed
    state.

    File A: the plate is occupied continuously from t=5 all the way to
    its LAST sample, with motion never settling — so the detector is
    armed and re-evaluating on every one of those occupied samples
    (genuinely "pending": neither firing nor going back to vacant) right
    up until the file simply ends mid-evaluation. This is the specific
    state a naive "carry a pending flag/counter forward" bug would leak.

    File B: opens with the plate ALSO occupied from t=0, but with motion
    already quiet. If file B's detector starts fresh (armed=True, no
    memory of file A's stalled evaluation), it fires quickly once
    occupancy sustains. If file A's pending state had somehow leaked
    (e.g. as "already mid-evaluation, don't restart the sustain window"),
    the fire timing or presence would differ from processing file B in
    isolation — which the third assertion checks directly.
    """
    duration_a = 15.0
    final_a, fires_a = process_file(duration_a,
                                    motion_hot_ranges=[(5.0, 15.0)],  # never settles
                                    occ_ranges=[(5.0, 15.0)])          # occupied to EOF
    # precondition: file A really was left pending — armed, occupied,
    # unresolved — not vacant and not fired
    assert fires_a == [], \
        "test setup invalid: file A must end without ever firing"

    duration_b = 15.0
    final_b, fires_b = process_file(duration_b,
                                    motion_hot_ranges=[],
                                    occ_ranges=[(0.0, 15.0)])
    assert fires_b, "file B must fire on its own fresh evaluation"

    # the decisive check: file B processed after a "pending" file A must
    # be IDENTICAL to file B processed completely alone
    final_b_alone, fires_b_alone = process_file(duration_b,
                                                motion_hot_ranges=[],
                                                occ_ranges=[(0.0, 15.0)])
    assert fires_b == fires_b_alone
    assert final_b == final_b_alone


def test_processing_order_of_files_does_not_change_either_result():
    # Process file A then B, and separately B then A (as if the two
    # scripts/detect_multi.py loop iterations happened in the other
    # order) — each file's own result must be identical either way, since
    # neither call can see the other's data or ordering.
    def a():
        return process_file(20.0, [(15.0, 20.0)], [(0.0, 15.0)])

    def b():
        return process_file(15.0, [], [(0.0, 15.0)])

    a_first, b_first = a(), b()
    b_second, a_second = b(), a()

    assert a_first[0] == a_second[0] and a_first[1] == a_second[1]
    assert b_first[0] == b_second[0] and b_first[1] == b_second[1]
