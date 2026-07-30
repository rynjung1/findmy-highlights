"""Unit tests for manifest build/save/load and kept/cut status logic."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.manifest import (build_manifest, fmt_ts, kept_spans,
                               load_manifest, parse_ts, save_manifest,
                               set_status)


def test_ts_round_trip():
    for v in (0.0, 12.5, 61.001, 3671.25):
        assert abs(parse_ts(fmt_ts(v)) - v) < 0.002
    assert fmt_ts(12.5) == "00:00:12.500"


def test_build_covers_whole_timeline_once():
    m = build_manifest("game.mp4", 100.0, [(10.0, 20.0), (50.0, 70.0)])
    spans = [(s["start_s"], s["end_s"]) for s in m["segments"]]
    assert spans == [(0.0, 10.0), (10.0, 20.0), (20.0, 50.0),
                     (50.0, 70.0), (70.0, 100.0)]
    statuses = [s["status"] for s in m["segments"]]
    assert statuses == ["cut", "kept", "cut", "kept", "cut"]
    assert all(s["source_file"] == "game.mp4" for s in m["segments"])


def test_build_segment_at_clip_edges_no_empty_gaps():
    m = build_manifest("g.mp4", 30.0, [(0.0, 10.0), (20.0, 30.0)])
    spans = [(s["start_s"], s["end_s"], s["status"]) for s in m["segments"]]
    assert spans == [(0.0, 10.0, "kept"), (10.0, 20.0, "cut"),
                     (20.0, 30.0, "kept")]


def test_build_empty_detection_single_cut_span():
    m = build_manifest("g.mp4", 30.0, [])
    assert [(s["start_s"], s["end_s"], s["status"]) for s in m["segments"]] \
        == [(0.0, 30.0, "cut")]


def test_score_fn_applied():
    m = build_manifest("g.mp4", 20.0, [(5.0, 10.0)],
                       score_fn=lambda a, b: (a + b) / 100)
    scores = {s["id"]: s["detection_score"] for s in m["segments"]}
    assert scores["seg_002"] == pytest.approx(0.15)


def test_skip_suggestions_default_empty():
    # backward compatible: no skip_fn given -> every segment (kept or cut)
    # gets an empty list, existing manifests/callers are unaffected
    m = build_manifest("g.mp4", 20.0, [(5.0, 10.0)])
    assert all(s["skip_suggestions"] == [] for s in m["segments"])


def test_skip_fn_applied_only_to_kept_segments():
    def skip_fn(a, b):
        return [(a + 1, a + 2)]

    m = build_manifest("g.mp4", 20.0, [(5.0, 10.0)], skip_fn=skip_fn)
    by_status = {s["status"]: s["skip_suggestions"] for s in m["segments"]}
    assert by_status["kept"] == [{"start_s": 6.0, "end_s": 7.0}]
    # gap/cut entries never call skip_fn -- nothing plays there to skip
    assert by_status["cut"] == []


def test_save_load_round_trip(tmp_path):
    m = build_manifest("g.mp4", 20.0, [(5.0, 10.0)])
    p = tmp_path / "manifest.json"
    save_manifest(m, p)
    assert load_manifest(p) == m


def test_load_rejects_unknown_version(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"version": 99, "segments": []}')
    with pytest.raises(ValueError):
        load_manifest(p)


def test_restore_gap_changes_kept_spans():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    assert kept_spans(m) == [(10.0, 20.0)]
    set_status(m, "seg_001", "kept")          # restore the 0-10 gap
    assert kept_spans(m) == [(0.0, 20.0)]     # adjacent spans merge
    set_status(m, "seg_001", "cut")           # un-restore
    assert kept_spans(m) == [(10.0, 20.0)]


def test_restore_all_and_none():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    for s in m["segments"]:
        set_status(m, s["id"], "kept")
    assert kept_spans(m) == [(0.0, 30.0)]     # full original length
    for s in m["segments"]:
        set_status(m, s["id"], "cut")
    assert kept_spans(m) == []


def test_set_status_unknown_id_raises():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    with pytest.raises(KeyError):
        set_status(m, "seg_999", "kept")


def test_set_status_invalid_value_raises():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    with pytest.raises(ValueError):
        set_status(m, "seg_001", "restored")
