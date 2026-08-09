"""Unit tests for pipeline.review: Tier 1 review-queue candidate
generation, selection, and storage. Clip extraction is checked via an
injected fake ffmpeg runner (same pattern as tests/test_stitch.py) so
these run without real video files or ffmpeg, except the small
real-ffmpeg section at the end."""

import json
import random
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import reference_clips_dir
from pipeline.review import (ReviewConfig, boundary_crossing_candidates,
                             generate_review_candidates,
                             hard_cut_dip_candidates, review_priority_key,
                             select_candidates, veto_boundary_candidates,
                             xclip_disagreement)
from pipeline.segments import HardCutConfig, SegmentConfig
from pipeline.stitch import VideoParams


def vp(start_offset=0.0):
    return VideoParams(path="v.mp4", codec_name="h264", width=1920,
                       height=1080, fps=30.0, rotation=0,
                       start_offset=start_offset)


# ---- hard_cut_dip_candidates ----

def test_hard_cut_dip_candidate_basic_fields():
    t = np.arange(0, 10, 0.1)
    s = np.full_like(t, 0.001)
    s[(t >= 4.0) & (t < 4.5)] = 0.0015  # a small bump inside the window
    cfg = HardCutConfig(quiet_thresh=0.002, min_raw_dip_s=0.5)
    candidates = hard_cut_dip_candidates([(3.0, 5.0)], t, s, cfg)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["candidate_type"] == "hard_cut_dip"
    assert c["window"] == {"start_s": 3.0, "end_s": 5.0}
    assert c["pipeline_decision"] == "cut"
    assert c["features_at_label_time"]["peak_score"] == pytest.approx(0.0015)
    assert c["features_at_label_time"]["score_margin"] == pytest.approx(0.002 - 0.0015)
    assert c["features_at_label_time"]["duration_margin"] == pytest.approx(2.0 - 0.5)
    # margin is the smaller of score_margin (0.0005) and duration_margin (1.5)
    assert c["margin"] == pytest.approx(0.0005)


def test_hard_cut_dip_candidate_margin_can_go_negative():
    # the shipped (buffered/merged) window can include content whose peak
    # legitimately exceeds quiet_thresh -- score_margin must be allowed
    # to go negative to flag exactly this as the riskiest case
    t = np.arange(0, 10, 0.1)
    s = np.full_like(t, 0.001)
    s[(t >= 4.0) & (t < 4.1)] = 0.01  # a real spike inside the window
    cfg = HardCutConfig(quiet_thresh=0.002, min_raw_dip_s=0.5)
    candidates = hard_cut_dip_candidates([(3.0, 5.0)], t, s, cfg)
    assert candidates[0]["features_at_label_time"]["score_margin"] < 0
    assert candidates[0]["margin"] < 0


def test_hard_cut_dip_candidates_one_per_window():
    t = np.arange(0, 20, 0.1)
    s = np.full_like(t, 0.001)
    cfg = HardCutConfig(quiet_thresh=0.002, min_raw_dip_s=0.5)
    candidates = hard_cut_dip_candidates([(1.0, 2.0), (10.0, 12.0)], t, s, cfg)
    assert len(candidates) == 2
    assert [c["window"]["start_s"] for c in candidates] == [1.0, 10.0]


def test_hard_cut_dip_candidates_empty_windows():
    assert hard_cut_dip_candidates([], [0.0], [0.0]) == []


# ---- boundary_crossing_candidates ----

def test_boundary_crossing_candidate_fields():
    crossings = [{"time": 3.0, "kind": "enter", "score": 0.006,
                 "threshold": 0.006, "margin": 0.0}]
    candidates = boundary_crossing_candidates(crossings)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["candidate_type"] == "boundary_crossing"
    assert c["window"] == {"start_s": 3.0, "end_s": 3.0}
    assert c["pipeline_decision"] == "enter"
    assert c["margin"] == 0.0
    assert c["features_at_label_time"]["kind"] == "enter"


def test_boundary_crossing_candidates_empty():
    assert boundary_crossing_candidates([]) == []


# ---- veto_boundary_candidates ----

def test_veto_boundary_candidate_basic_fields():
    t = np.arange(0, 10, 0.1)
    s = np.full_like(t, 0.001)
    s[(t >= 4.0) & (t < 4.5)] = 0.004  # real motion despite the veto
    cfg = SegmentConfig(enter_thresh=0.006)
    candidates = veto_boundary_candidates([(3.0, 5.0)], t, s, cfg)
    assert len(candidates) == 1
    c = candidates[0]
    assert c["candidate_type"] == "veto_boundary"
    assert c["window"] == {"start_s": 3.0, "end_s": 5.0}
    assert c["pipeline_decision"] == "cut"
    assert c["features_at_label_time"]["peak_score"] == pytest.approx(0.004)
    # margin = enter_thresh - peak_score
    assert c["margin"] == pytest.approx(0.006 - 0.004)


def test_veto_boundary_candidate_margin_more_negative_for_riskier_windows():
    # a vetoed window whose peak motion sits FAR above enter_thresh
    # (real motion the pipeline discarded anyway) must sort ahead of one
    # that's only barely above -- i.e. a more negative/smaller margin
    t = np.arange(0, 10, 0.1)
    risky = np.full_like(t, 0.001)
    risky[(t >= 4.0) & (t < 4.5)] = 0.05  # way above enter_thresh
    mild = np.full_like(t, 0.001)
    mild[(t >= 4.0) & (t < 4.5)] = 0.0065  # barely above enter_thresh
    cfg = SegmentConfig(enter_thresh=0.006)
    risky_margin = veto_boundary_candidates([(3.0, 5.0)], t, risky, cfg)[0]["margin"]
    mild_margin = veto_boundary_candidates([(3.0, 5.0)], t, mild, cfg)[0]["margin"]
    assert risky_margin < mild_margin


def test_veto_boundary_candidates_empty_input():
    assert veto_boundary_candidates([], [0.0], [0.0]) == []


# ---- select_candidates ----

def test_select_candidates_lowest_margin_first_and_capped():
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": i, "end_s": i + 1},
          "margin": 10 - i, "pipeline_decision": "cut", "features_at_label_time": {}}
         for i in range(8)]
    cfg = ReviewConfig(max_candidates_per_video=3, control_sample_rate=0.0)
    chosen = select_candidates(hc, [], [], [], 100.0, cfg, rng=random.Random(0))
    assert len(chosen) == 3
    # margins run [10,9,...,3] for i=0..7 -> the three lowest are 3,4,5
    assert [c["margin"] for c in chosen] == [3, 4, 5]


def test_select_candidates_uncapped_when_max_is_none():
    # scripts/mine_review_candidates.py's whole reason to exist: None
    # means every real candidate, not a magic large number.
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": i, "end_s": i + 1},
          "margin": 10 - i, "pipeline_decision": "cut", "features_at_label_time": {}}
         for i in range(40)]
    cfg = ReviewConfig(max_candidates_per_video=None, control_sample_rate=0.0)
    chosen = select_candidates(hc, [], [], [], 100.0, cfg, rng=random.Random(0))
    assert len(chosen) == 40


def test_select_candidates_type_filter_excludes_other_types():
    # real footage: hard_cut_dip margins run systematically lower, so an
    # unrestricted mining pass only ever surfaces this type -- the whole
    # reason candidate_types exists (see ReviewConfig's own docstring).
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": -5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    bc = [{"candidate_type": "boundary_crossing", "window": {"start_s": 2, "end_s": 2},
          "margin": 3.0, "pipeline_decision": "enter", "features_at_label_time": {}}]
    vb = [{"candidate_type": "veto_boundary", "window": {"start_s": 4, "end_s": 5},
          "margin": 4.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=None, control_sample_rate=0.0,
                       candidate_types=frozenset({"boundary_crossing", "veto_boundary"}))
    chosen = select_candidates(hc, bc, [], [], 100.0, cfg, rng=random.Random(0),
                               veto_candidates=vb)
    assert {c["candidate_type"] for c in chosen} == {"boundary_crossing", "veto_boundary"}
    assert len(chosen) == 2


def test_select_candidates_type_filter_none_considers_everything():
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": -5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    bc = [{"candidate_type": "boundary_crossing", "window": {"start_s": 2, "end_s": 2},
          "margin": 3.0, "pipeline_decision": "enter", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=None, control_sample_rate=0.0)
    chosen = select_candidates(hc, bc, [], [], 100.0, cfg, rng=random.Random(0))
    assert len(chosen) == 2


def test_select_candidates_mixes_both_types_by_margin():
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": 5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    bc = [{"candidate_type": "boundary_crossing", "window": {"start_s": 2, "end_s": 2},
          "margin": 0.1, "pipeline_decision": "enter", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0)
    chosen = select_candidates(hc, bc, [], [], 100.0, cfg, rng=random.Random(0))
    assert [c["candidate_type"] for c in chosen] == ["boundary_crossing", "hard_cut_dip"]


def test_select_candidates_includes_veto_candidates_in_ranking():
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": 5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    vb = [{"candidate_type": "veto_boundary", "window": {"start_s": 2, "end_s": 3},
          "margin": -1.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0)
    chosen = select_candidates(hc, [], [], [], 100.0, cfg, rng=random.Random(0),
                               veto_candidates=vb)
    assert [c["candidate_type"] for c in chosen] == ["veto_boundary", "hard_cut_dip"]


def test_select_candidates_veto_candidates_defaults_to_none_unaffected():
    # every pre-veto call site (positional, no veto_candidates arg) must
    # behave exactly as before
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": 5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0)
    chosen = select_candidates(hc, [], [], [], 100.0, cfg, rng=random.Random(0))
    assert len(chosen) == 1 and chosen[0]["candidate_type"] == "hard_cut_dip"


def test_select_candidates_no_control_when_rate_zero():
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0)
    chosen = select_candidates([], [], [(10.0, 20.0)], [], 100.0, cfg,
                               rng=random.Random(0))
    assert chosen == []


def test_select_candidates_control_added_when_rate_one():
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=1.0)
    # the kept segment fills the whole duration -> no gaps exist, so the
    # only possible control pool is "kept", making the outcome deterministic
    chosen = select_candidates([], [], [(0.0, 100.0)], [], 100.0, cfg,
                               rng=random.Random(0))
    assert len(chosen) == 1
    assert chosen[0]["candidate_type"] == "control"
    assert chosen[0]["margin"] is None
    assert chosen[0]["pipeline_decision"] == "kept"
    assert 0.0 < chosen[0]["window"]["start_s"] < 100.0


def test_select_candidates_control_can_land_in_an_ordinary_gap():
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=1.0)
    # no kept segments at all -> control must come from the gap [0, 100]
    chosen = select_candidates([], [], [], [], 100.0, cfg, rng=random.Random(1))
    assert len(chosen) == 1
    assert chosen[0]["pipeline_decision"] == "cut"


def test_select_candidates_control_never_lands_inside_a_hard_cut_window():
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=1.0)
    for seed in range(20):
        chosen = select_candidates(
            [], [], [], [(40.0, 60.0)], 100.0, cfg, rng=random.Random(seed))
        if chosen:
            t = chosen[0]["window"]["start_s"]
            assert not (40.0 <= t <= 60.0)


def test_select_candidates_no_control_when_video_too_short():
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=1.0)
    chosen = select_candidates([], [], [], [], 1.0, cfg, rng=random.Random(0))
    assert chosen == []


# ---- generate_review_candidates (storage + clip extraction, fake runner) ----

def test_generate_review_candidates_writes_json_and_calls_extractor(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)
    hard_cut_windows = [(10.0, 11.0)]
    final_segments = [(0.0, 30.0)]
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        # actually create the file so downstream code doesn't need real ffmpeg
        out_path = Path(cmd[-1])
        out_path.write_bytes(b"fake")

    written = generate_review_candidates(
        final_segments=final_segments, hard_cut_windows=hard_cut_windows,
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(start_offset=2.0),
        clip_runner=fake_runner, xclip_feature_fn=lambda *a, **k: None)

    assert len(written) == 1
    assert len(calls) == 1
    reviews_dir = tmp_path / "reviews"
    json_files = list(reviews_dir.glob("*.json"))
    mp4_files = list(reviews_dir.glob("*.mp4"))
    assert len(json_files) == 1
    assert len(mp4_files) == 1
    record = json.loads(json_files[0].read_text())
    assert record["candidate_type"] == "hard_cut_dip"
    assert record["window"] == {"start_s": 10.0, "end_s": 11.0}
    assert record["label"] is None
    assert record["labeled_at"] is None
    assert record["source"]["source_file"] == "v.mp4"
    assert "config_hash" in record and record["config_hash"]
    assert record["id"].startswith("hc_")


def test_generate_review_candidates_shifts_ss_to_by_start_offset(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake")

    generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(start_offset=4.266),
        clip_runner=fake_runner, xclip_feature_fn=lambda *a, **k: None)

    cmd = calls[0]
    ss = float(cmd[cmd.index("-ss") + 1])
    to = float(cmd[cmd.index("-to") + 1])
    # window (10.0, 11.0) padded 1.5s each side -> (8.5, 12.5), shifted by
    # start_offset=4.266
    assert ss == pytest.approx(8.5 + 4.266)
    assert to == pytest.approx(12.5 + 4.266)


def test_generate_review_candidates_pad_never_goes_negative(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)
        Path(cmd[-1]).write_bytes(b"fake")

    generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(0.5, 1.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(start_offset=0.0),
        clip_runner=fake_runner, xclip_feature_fn=lambda *a, **k: None)

    ss = float(calls[0][calls[0].index("-ss") + 1])
    assert ss == pytest.approx(0.0)  # 0.5 - 1.5 would be negative -> clamped


def test_generate_review_candidates_extraction_failure_is_non_fatal(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)
    warnings = []

    def failing_runner(cmd):
        raise RuntimeError("ffmpeg exploded")

    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(),
        clip_runner=failing_runner, warn=warnings.append,
        xclip_feature_fn=lambda *a, **k: None)

    assert written == []
    assert len(warnings) == 1
    assert "ffmpeg exploded" in warnings[0]
    # no orphaned json record for the candidate whose clip failed
    assert list((tmp_path / "reviews").glob("*.json")) == []


def test_generate_review_candidates_nothing_to_do_creates_no_directory(tmp_path):
    written = generate_review_candidates(
        final_segments=[], hard_cut_windows=[], motion_times=[], motion_scores=[],
        enter_scores=[], video_path="v.mp4", source_file="v.mp4",
        training_data_dir=tmp_path, duration=0.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0))
    assert written == []
    assert not (tmp_path / "reviews").exists()


def test_generate_review_candidates_extra_source_info_merged(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        extra_source_info={"batch_id": "abc123"},
        xclip_feature_fn=lambda *a, **k: None)

    assert written[0]["source"]["batch_id"] == "abc123"
    assert written[0]["source"]["source_file"] == "v.mp4"


def test_config_hash_changes_when_thresholds_change(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    w1 = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path / "a", duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        seg_cfg=SegmentConfig(enter_thresh=0.006),
        xclip_feature_fn=lambda *a, **k: None)
    w2 = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path / "b", duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        seg_cfg=SegmentConfig(enter_thresh=0.009),
        xclip_feature_fn=lambda *a, **k: None)
    assert w1[0]["config_hash"] != w2[0]["config_hash"]


# ---- generate_review_candidates: veto-boundary + pose/audio wiring ----

def test_generate_review_candidates_includes_veto_boundary(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)
    s[(t >= 20.0) & (t < 20.5)] = 0.05  # real motion inside the vetoed window

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        vetoed_segments=[(19.0, 21.0)],
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        xclip_feature_fn=lambda *a, **k: None)

    # the real motion spike used to make this vetoed window "risky"
    # legitimately also crosses enter_thresh, producing real
    # boundary_crossing candidates too -- this test only asserts the
    # veto_boundary candidate itself flows all the way through storage,
    # not that it's the only candidate type produced
    veto_written = [w for w in written if w["candidate_type"] == "veto_boundary"]
    assert len(veto_written) == 1
    assert veto_written[0]["id"].startswith("vb_")
    assert veto_written[0]["window"] == {"start_s": 19.0, "end_s": 21.0}


def test_generate_review_candidates_attaches_pose_audio_and_xclip_features(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    calls = {"pose": [], "audio": [], "xclip": []}

    def fake_pose(video_path, center_s):
        calls["pose"].append(center_s)
        return {"peak_displacement_px": 42.0, "min_visibility": 0.9,
               "n_frames_with_pose": 10}

    def fake_audio(video_path, center_s):
        calls["audio"].append(center_s)
        return {"peak_amplitude": 0.5, "peak_t": center_s, "rise_time_s": 0.01}

    def fake_xclip(video_path, center_s):
        calls["xclip"].append(center_s)
        return {"p_swinging": 0.83, "pos_prompt": "a baseball player swinging a bat",
               "neg_prompt": "baseball players standing idle"}

    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        pose_feature_fn=fake_pose, audio_feature_fn=fake_audio,
        xclip_feature_fn=fake_xclip)

    assert len(written) == 1
    assert written[0]["features_at_label_time"]["pose"]["peak_displacement_px"] == 42.0
    assert written[0]["features_at_label_time"]["audio"]["rise_time_s"] == 0.01
    assert written[0]["features_at_label_time"]["xclip"]["p_swinging"] == 0.83
    # called at the candidate window's midpoint (10.0, 11.0) -> 10.5
    assert calls["pose"] == [10.5]
    assert calls["audio"] == [10.5]
    assert calls["xclip"] == [10.5]
    # the candidate's own pre-existing features (e.g. peak_score for a
    # hard-cut dip) must still be present alongside pose/audio/xclip, not
    # overwritten by them
    assert "peak_score" in written[0]["features_at_label_time"]


def test_generate_review_candidates_pose_skipped_without_zone(tmp_path):
    # no pose_feature_fn given and zone=None (the default) -> pose
    # feature must simply be absent, not attempted/crash
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    def fake_audio(video_path, center_s):
        return {"peak_amplitude": 0.5, "peak_t": center_s, "rise_time_s": 0.01}

    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        audio_feature_fn=fake_audio, xclip_feature_fn=lambda *a, **k: None)

    assert "pose" not in written[0]["features_at_label_time"]
    assert "audio" in written[0]["features_at_label_time"]


def test_generate_review_candidates_pose_audio_xclip_failures_are_non_fatal(tmp_path):
    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    def failing_pose(video_path, center_s):
        raise RuntimeError("mediapipe exploded")

    def failing_audio(video_path, center_s):
        raise RuntimeError("ffmpeg exploded")

    def failing_xclip(video_path, center_s):
        raise RuntimeError("xclip exploded")

    warnings = []
    written = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        pose_feature_fn=failing_pose, audio_feature_fn=failing_audio,
        xclip_feature_fn=failing_xclip, warn=warnings.append)

    # the record itself is still written -- a pose/audio/xclip failure
    # doesn't cost the whole candidate, same non-fatal philosophy as clip
    # extraction
    assert len(written) == 1
    assert "pose" not in written[0]["features_at_label_time"]
    assert "audio" not in written[0]["features_at_label_time"]
    assert "xclip" not in written[0]["features_at_label_time"]
    assert any("mediapipe exploded" in w for w in warnings)
    assert any("ffmpeg exploded" in w for w in warnings)
    assert any("xclip exploded" in w for w in warnings)


def test_generate_review_candidates_xclip_model_load_failure_is_non_fatal(tmp_path, monkeypatch):
    # xclip_feature_fn NOT given -> generate_review_candidates tries to
    # build a real pipeline.xclip.XClipModel itself; a real-world model
    # load failure (no network on first download, etc.) must cost only
    # the xclip feature, never the rest of the run.
    import pipeline.review as review_module

    def failing_build_xclip(*a, **k):
        raise RuntimeError("no network")

    monkeypatch.setattr("pipeline.xclip.build_xclip", failing_build_xclip)

    t = np.arange(0, 30, 0.1)
    s = np.full_like(t, 0.001)

    def fake_runner(cmd):
        Path(cmd[-1]).write_bytes(b"fake")

    warnings = []
    written = review_module.generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path, duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        warn=warnings.append)

    assert len(written) == 1
    assert "xclip" not in written[0]["features_at_label_time"]
    assert any("no network" in w for w in warnings)


# ---- xclip_disagreement / review_priority_key ----

def _record(pipeline_decision, margin=1.0, p_swinging=None, label=None):
    features = {}
    if p_swinging is not None:
        features["xclip"] = {"p_swinging": p_swinging}
    return {"pipeline_decision": pipeline_decision, "margin": margin,
           "features_at_label_time": features, "label": label}


def test_xclip_disagreement_none_without_xclip_feature():
    r = _record("cut", p_swinging=None)
    assert xclip_disagreement(r) is None


def test_xclip_disagreement_none_for_unrecognized_decision():
    r = _record("something_else", p_swinging=0.9)
    assert xclip_disagreement(r) is None


def test_xclip_disagreement_high_when_downtime_claim_but_high_p_swinging():
    # pipeline says "cut" (claims downtime), xclip says 0.95 (looks very
    # swing-like) -- maximal disagreement
    r = _record("cut", p_swinging=0.95)
    assert xclip_disagreement(r) == pytest.approx(0.95)


def test_xclip_disagreement_high_when_real_action_claim_but_low_p_swinging():
    # pipeline says "enter" (claims real_action), xclip says 0.05 (looks
    # very idle-like) -- maximal disagreement
    r = _record("enter", p_swinging=0.05)
    assert xclip_disagreement(r) == pytest.approx(0.95)


def test_xclip_disagreement_low_when_signals_agree():
    r = _record("cut", p_swinging=0.05)  # both say downtime
    assert xclip_disagreement(r) == pytest.approx(0.05)


def test_review_priority_key_disagreement_sorts_before_margin_only():
    # a record with real (even mild) xclip disagreement outranks a
    # record with no xclip data at all, regardless of margin
    with_xclip = _record("cut", margin=100.0, p_swinging=0.51)  # barely disagrees
    no_xclip = _record("cut", margin=0.0001)  # extremely borderline margin, no xclip
    ranked = sorted([no_xclip, with_xclip], key=review_priority_key)
    assert ranked[0] is with_xclip


def test_review_priority_key_higher_disagreement_sorts_first():
    mild = _record("cut", margin=1.0, p_swinging=0.6)
    strong = _record("cut", margin=1.0, p_swinging=0.99)
    ranked = sorted([mild, strong], key=review_priority_key)
    assert ranked[0] is strong


def test_review_priority_key_falls_back_to_margin_when_no_xclip_data():
    # matches the pre-existing lowest-margin-first behavior exactly for
    # records xclip has no opinion on
    low_margin = _record("cut", margin=1.0)
    high_margin = _record("cut", margin=5.0)
    ranked = sorted([high_margin, low_margin], key=review_priority_key)
    assert ranked[0] is low_margin


def test_review_priority_key_control_samples_still_sort_last_within_fallback():
    control = _record("kept", margin=None)
    real_margin = _record("cut", margin=100.0)
    ranked = sorted([control, real_margin], key=review_priority_key)
    assert ranked[0] is real_margin
    assert ranked[1] is control


# ---- real ffmpeg smoke test ----

def test_extract_review_clip_real_ffmpeg(tmp_path):
    ref = reference_clips_dir() / "clip_60.mkv"
    if not ref.exists():
        pytest.skip("reference_clips/clip_60.mkv not available")
    t = np.arange(0, 5, 0.1)
    s = np.full_like(t, 0.001)

    written = generate_review_candidates(
        final_segments=[(0.0, 5.0)], hard_cut_windows=[(2.0, 2.5)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path=str(ref),
        source_file="clip_60.mkv", training_data_dir=tmp_path, duration=5.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0))

    assert len(written) == 1
    clip_path = tmp_path / "reviews" / f"{written[0]['id']}.mp4"
    assert clip_path.exists()
    assert clip_path.stat().st_size > 0
