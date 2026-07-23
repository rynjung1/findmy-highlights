"""Unit tests for the at-bat-start detector, including the multi-step-out
pattern: a batter stepping out of the box more than once before finally
settling in must produce exactly one fire, at the final settle."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.atbat import AtBatConfig, atbat_start_times

CFG = AtBatConfig(vacancy_arm_s=2.0, sustain_s=4.0, occ_frac=0.8,
                  settle_thresh=0.003)


def timeline(n=60):
    """det samples at 1 fps, motion at 10 fps, both starting at 0."""
    det_t = np.arange(n, dtype=float)
    m_t = np.arange(0, n, 0.1)
    return det_t, m_t


def occ_from_runs(det_t, runs):
    occ = np.zeros(len(det_t), dtype=bool)
    for a, b in runs:
        occ[(det_t >= a) & (det_t < b)] = True
    return occ


def motion_profile(m_t, hot_ranges, hot=0.02, cold=0.001):
    m = np.full(len(m_t), cold)
    for a, b in hot_ranges:
        m[(m_t >= a) & (m_t < b)] = hot
    return m


def test_multi_step_out_fires_once_at_final_settle():
    det_t, m_t = timeline(60)
    # prior at-bat 0-8; batter departs (hit); defensive play motion 8-20;
    # on-deck steps in early 14-17 (motion still hot); then the next batter
    # steps out TWICE (2-3s occupancies at 20 and 25) before settling at 31
    occ = occ_from_runs(det_t, [(0, 8), (14, 17), (20, 22), (25, 28), (31, 45)])
    motion = motion_profile(m_t, [(0, 20)])
    fires = atbat_start_times(det_t, occ, m_t, motion, CFG)
    assert len(fires) == 1, f"expected exactly one fire, got {fires}"
    assert 30.5 <= fires[0] <= 32.5   # at the final settle, not any step-out
    # explicitly: no fire during the hot early re-occupancy at 14-17
    assert not any(f < 20 for f in fires)


def test_short_flicker_vacancy_does_not_rearm():
    det_t, m_t = timeline(40)
    # continuous at-bat with a 1s detection flicker at t=10 (below
    # vacancy_arm_s) — the detector must not re-arm and must not fire again
    occ = occ_from_runs(det_t, [(0, 10), (11, 30)])
    motion = motion_profile(m_t, [])          # everything settled
    fires = atbat_start_times(det_t, occ, m_t, motion, CFG)
    assert len(fires) == 1                    # only the clip-start arm fires
    assert fires[0] <= 1.0


def test_fire_waits_for_settle_via_rearm():
    det_t, m_t = timeline(40)
    # plate re-occupied at t=10 while motion is still hot until t=18;
    # pending re-arm must delay the fire until the settle, not skip it
    occ = occ_from_runs(det_t, [(10, 35)])
    motion = motion_profile(m_t, [(0, 18)])
    fires = atbat_start_times(det_t, occ, m_t, motion, CFG)
    assert len(fires) == 1
    assert 17.0 <= fires[0] <= 20.0


def test_no_fire_when_occupancy_never_sustained():
    det_t, m_t = timeline(40)
    occ = occ_from_runs(det_t, [(10, 12), (16, 18), (22, 24)])  # all < 4s
    motion = motion_profile(m_t, [])
    assert atbat_start_times(det_t, occ, m_t, motion, CFG) == []


def test_no_fire_when_motion_never_settles():
    det_t, m_t = timeline(40)
    occ = occ_from_runs(det_t, [(10, 35)])
    motion = motion_profile(m_t, [(0, 40)])   # hot the whole time
    assert atbat_start_times(det_t, occ, m_t, motion, CFG) == []


def test_empty_inputs():
    assert atbat_start_times([], [], [], [], CFG) == []
