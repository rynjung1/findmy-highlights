"""Unit tests for the shared settle logic used by both play extension
(refine.py) and at-bat boundary detection (atbat.py)."""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.settle import (SettleConfig, is_settled_at_end,
                             last_active_time, settled_mask)

CFG = SettleConfig(threshold=0.002, min_quiet_s=1.5)


def series(spans, dt=0.1, total=20.0, hot=0.02, cold=0.0005):
    t = np.arange(0, total, dt)
    s = np.full_like(t, cold)
    for a, b in spans:
        s[(t >= a) & (t < b)] = hot
    return t, s


def test_empty_series_settled_at_end():
    assert is_settled_at_end([], [], CFG) is True


def test_always_quiet_settles_quickly():
    t, s = series([])
    mask = settled_mask(t, s, CFG)
    # settled becomes True once min_quiet_s has elapsed from the start
    first_true = t[np.nonzero(mask)[0][0]]
    assert 1.4 <= first_true <= 1.6


def test_always_hot_never_settles():
    t, s = series([(0, 20)])
    assert not settled_mask(t, s, CFG).any()
    assert not is_settled_at_end(t, s, CFG)


def test_brief_blip_resets_clock_but_resettles():
    # quiet from 0, blip at 5.0-5.2, quiet resumes after
    t, s = series([(5.0, 5.2)])
    mask = settled_mask(t, s, CFG)
    # settled before the blip
    assert mask[np.searchsorted(t, 4.9)]
    # not settled immediately after the blip
    assert not mask[np.searchsorted(t, 5.3)]
    # settled again once 1.5s of quiet has re-accumulated post-blip
    assert mask[np.searchsorted(t, 5.2 + 1.6)]


def test_last_active_time_basic():
    t, s = series([(2.0, 3.0), (10.0, 10.5)])
    assert abs(last_active_time(t, s, CFG) - 10.4) < 0.15


def test_last_active_time_none_when_never_active():
    t, s = series([])
    assert last_active_time(t, s, CFG) is None


def test_is_settled_at_end_true_after_long_quiet_tail():
    t, s = series([(0, 5)])   # hot 0-5, quiet 5-20
    assert is_settled_at_end(t, s, CFG)


def test_is_settled_at_end_false_if_hot_near_end():
    t, s = series([(19.0, 19.5)])  # hot right before the series ends
    assert not is_settled_at_end(t, s, CFG)


def test_shared_by_refine_and_atbat_same_threshold_object():
    # the point of this module: refine.py and atbat.py must consume the
    # exact same config type, not independently-defined threshold fields
    from pipeline.refine import RefineConfig
    from pipeline.atbat import AtBatConfig
    assert isinstance(RefineConfig().settle, SettleConfig)
    assert isinstance(AtBatConfig().settle, SettleConfig)
    # and, absent an explicit override, the numeric defaults agree
    assert RefineConfig().settle == AtBatConfig().settle
