import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from review_stats import FEATURES, _motion_score, auc, feature_label_pattern  # noqa: E402


def test_auc_perfect_separation():
    assert auc([5, 6, 7], [1, 2, 3]) == 1.0


def test_auc_reversed_is_zero():
    assert auc([1, 2, 3], [5, 6, 7]) == 0.0


def test_auc_tie_counts_half():
    assert auc([5], [5]) == 0.5


def test_auc_empty_side_returns_none():
    assert auc([], [1, 2]) is None
    assert auc([1, 2], []) is None


def test_motion_score_prefers_peak_score():
    record = {"features_at_label_time": {"peak_score": 0.01, "score": 0.02}}
    assert _motion_score(record) == 0.01


def test_motion_score_falls_back_to_score():
    record = {"features_at_label_time": {"score": 0.02}}
    assert _motion_score(record) == 0.02


def test_motion_score_none_for_control_samples():
    record = {"features_at_label_time": {}}
    assert _motion_score(record) is None


def _rec(label, **features):
    return {"label": label, "features_at_label_time": features}


def test_feature_label_pattern_splits_by_real_label():
    records = [
        _rec("real_action", xclip={"p_swinging": 0.9}),
        _rec("real_action", xclip={"p_swinging": 0.8}),
        _rec("downtime", xclip={"p_swinging": 0.2}),
        _rec("downtime", xclip={"p_swinging": 0.3}),
    ]
    extractor = lambda r: (r["features_at_label_time"].get("xclip") or {}).get("p_swinging")
    p = feature_label_pattern(records, extractor, higher_is_real_action=True)
    assert p["real"]["n"] == 2
    assert p["downtime"]["n"] == 2
    assert p["real"]["mean"] == pytest.approx(0.85)
    assert p["auc"] == 1.0
    assert p["n_skipped"] == 0


def test_feature_label_pattern_inverted_direction_audio():
    # audio: SHORTER rise time = more real-action-like -- a real_action
    # group with genuinely shorter times than downtime should still read
    # as a high (correctly-oriented) AUC, not a low one.
    records = [
        _rec("real_action", audio={"rise_time_s": 0.01}),
        _rec("real_action", audio={"rise_time_s": 0.02}),
        _rec("downtime", audio={"rise_time_s": 0.10}),
        _rec("downtime", audio={"rise_time_s": 0.12}),
    ]
    extractor = lambda r: (r["features_at_label_time"].get("audio") or {}).get("rise_time_s")
    p = feature_label_pattern(records, extractor, higher_is_real_action=False)
    assert p["auc"] == 1.0


def test_feature_label_pattern_counts_skipped_missing_feature():
    records = [
        _rec("real_action", xclip={"p_swinging": 0.9}),
        _rec("real_action"),  # no xclip feature at all -- e.g. model load failed
        _rec("downtime", xclip={"p_swinging": 0.2}),
    ]
    extractor = lambda r: (r["features_at_label_time"].get("xclip") or {}).get("p_swinging")
    p = feature_label_pattern(records, extractor, higher_is_real_action=True)
    assert p["real"]["n"] == 1
    assert p["n_skipped"] == 1


def test_feature_label_pattern_not_enough_data_when_one_side_empty():
    records = [_rec("real_action", xclip={"p_swinging": 0.9})]
    extractor = lambda r: (r["features_at_label_time"].get("xclip") or {}).get("p_swinging")
    p = feature_label_pattern(records, extractor, higher_is_real_action=True)
    assert p["real"] is not None
    assert p["downtime"] is None


def test_features_list_covers_all_four_instrumentation_signals():
    names = {name for name, _, _ in FEATURES}
    assert names == {"motion score", "pose peak_displacement_px",
                     "audio rise_time_s", "xclip p_swinging"}
