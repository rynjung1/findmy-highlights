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

from pipeline.review import (ReviewConfig, boundary_crossing_candidates,
                             generate_review_candidates,
                             hard_cut_dip_candidates, select_candidates)
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


def test_select_candidates_mixes_both_types_by_margin():
    hc = [{"candidate_type": "hard_cut_dip", "window": {"start_s": 0, "end_s": 1},
          "margin": 5.0, "pipeline_decision": "cut", "features_at_label_time": {}}]
    bc = [{"candidate_type": "boundary_crossing", "window": {"start_s": 2, "end_s": 2},
          "margin": 0.1, "pipeline_decision": "enter", "features_at_label_time": {}}]
    cfg = ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0)
    chosen = select_candidates(hc, bc, [], [], 100.0, cfg, rng=random.Random(0))
    assert [c["candidate_type"] for c in chosen] == ["boundary_crossing", "hard_cut_dip"]


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
        clip_runner=fake_runner)

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
        clip_runner=fake_runner)

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
        clip_runner=fake_runner)

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
        clip_runner=failing_runner, warn=warnings.append)

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
        extra_source_info={"batch_id": "abc123"})

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
        seg_cfg=SegmentConfig(enter_thresh=0.006))
    w2 = generate_review_candidates(
        final_segments=[(0.0, 30.0)], hard_cut_windows=[(10.0, 11.0)],
        motion_times=t, motion_scores=s, enter_scores=s, video_path="v.mp4",
        source_file="v.mp4", training_data_dir=tmp_path / "b", duration=30.0,
        review_cfg=ReviewConfig(max_candidates_per_video=5, control_sample_rate=0.0),
        rng=random.Random(0), prober=lambda p: vp(), clip_runner=fake_runner,
        seg_cfg=SegmentConfig(enter_thresh=0.009))
    assert w1[0]["config_hash"] != w2[0]["config_hash"]


# ---- real ffmpeg smoke test ----

def test_extract_review_clip_real_ffmpeg(tmp_path):
    ref = Path(__file__).resolve().parent.parent / "reference_clips" / "clip_60.mkv"
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
