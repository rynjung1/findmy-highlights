"""Unit tests for manifest build/save/load and kept/cut status logic."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.manifest import (build_manifest, build_multi_file_manifest,
                               fmt_ts, hard_cut_boundary_starts_by_file,
                               kept_spans, load_manifest, parse_ts,
                               save_manifest, set_status)


def _origins(m):
    return {(s["start_s"], s["end_s"]): s["origin"] for s in m["segments"]}


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


# ---- hard_cut_windows / origin="hard_cut" labeling ----

def test_origin_defaults_to_detected_and_gap_without_hard_cut_windows():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    origins = {(s["start_s"], s["end_s"]): s["origin"] for s in m["segments"]}
    assert origins[(10.0, 20.0)] == "detected"
    assert origins[(0.0, 10.0)] == "gap"
    assert origins[(20.0, 30.0)] == "gap"


def test_hard_cut_window_labels_matching_gap_as_hard_cut():
    # a gap between two kept spans that overlaps a real hard-cut window
    # must be labeled "hard_cut", not the default "gap"
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       hard_cut_windows=[(11.0, 14.0)])
    origins = {(s["start_s"], s["end_s"]): s["origin"] for s in m["segments"]}
    assert origins[(10.0, 15.0)] == "hard_cut"
    assert origins[(25.0, 30.0)] == "gap"   # untouched by any hard-cut window


def test_hard_cut_window_never_labels_a_kept_segment():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)], hard_cut_windows=[(12.0, 14.0)])
    kept = next(s for s in m["segments"] if s["status"] == "kept")
    assert kept["origin"] == "detected"


def test_hard_cut_windows_purely_additive_when_omitted():
    # existing callers that never pass hard_cut_windows are unaffected
    m1 = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    m2 = build_manifest("g.mp4", 30.0, [(10.0, 20.0)], hard_cut_windows=None)
    assert m1 == m2


def test_multi_file_manifest_hard_cut_windows_are_per_file():
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)],
         "hard_cut_windows": [(11.0, 14.0)]},
        {"source_file": "part2.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)]},
    ])
    part1_gap = next(s for s in m["segments"]
                     if s["source_file"] == "part1.mp4" and s["status"] == "cut"
                     and s["start_s"] == 10.0)
    part2_gap = next(s for s in m["segments"]
                     if s["source_file"] == "part2.mp4" and s["status"] == "cut"
                     and s["start_s"] == 10.0)
    assert part1_gap["origin"] == "hard_cut"
    assert part2_gap["origin"] == "gap"   # part2 got no hard_cut_windows at all


# ---- hard_cut_boundary_starts_by_file ----
# Regression coverage for the origin-aware merge fix: pipeline.stitch's
# merge_overlapping_spans used to bridge a hard-cut gap back together
# whenever it was shorter than the source's GOP -- exactly the gap size
# a hard cut is by design -- silently un-cutting real destructive cuts
# despite the manifest correctly marking them origin="hard_cut". This is
# the function that identifies which kept-span boundaries are real cuts
# that must never be bridged.

def test_hard_cut_boundary_flags_the_kept_span_right_after_a_hard_cut():
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       hard_cut_windows=[(11.0, 14.0)])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == {15.0}


def test_no_hard_cut_boundaries_when_no_hard_cut_windows():
    m = build_manifest("g.mp4", 30.0, [(0.0, 10.0), (15.0, 20.0)])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == set()


def test_ordinary_gap_after_hard_cut_gap_is_not_flagged():
    # only the kept segment DIRECTLY after a hard_cut gap is a protected
    # boundary -- a later, ordinary gap must not be swept in too
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       hard_cut_windows=[(11.0, 14.0)])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert 30.0 not in boundaries[0]


def test_hard_cut_boundary_cleared_once_restored():
    # restoring a hard_cut entry flips its status to "kept" -- it's no
    # longer a cut at all, so the following kept span's boundary is no
    # longer flagged as protected (nothing left there to protect)
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       hard_cut_windows=[(11.0, 14.0)])
    gap = next(s for s in m["segments"] if s["start_s"] == 10.0)
    set_status(m, gap["id"], "kept")
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == set()


def test_multi_file_hard_cut_boundaries_are_per_file():
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)],
         "hard_cut_windows": [(11.0, 14.0)]},
        {"source_file": "part2.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)],
         "hard_cut_windows": [(11.0, 14.0)]},
    ])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == {15.0}
    assert boundaries[1] == {15.0}


def test_hard_cut_boundary_never_flags_the_very_first_segment():
    m = build_manifest("g.mp4", 10.0, [(0.0, 5.0)], hard_cut_windows=[(0.0, 0.0)])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == set()


# ---- walkup_gate_windows / origin="walkup_gate" labeling ----
# Mirrors the hard_cut_windows suite above -- same mechanism (a purely
# additive labeling input over cut spans), same reason both need the
# identical treatment (see pipeline/segments.py's WalkupGateConfig).

def test_walkup_gate_window_labels_matching_gap_as_walkup_gate():
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       walkup_gate_windows=[(11.0, 14.0)])
    origins = _origins(m)
    assert origins[(10.0, 15.0)] == "walkup_gate"
    assert origins[(25.0, 30.0)] == "gap"   # untouched by any walkup-gate window


def test_walkup_gate_window_never_labels_a_kept_segment():
    m = build_manifest("g.mp4", 30.0, [(10.0, 20.0)], walkup_gate_windows=[(12.0, 14.0)])
    kept = next(s for s in m["segments"] if s["status"] == "kept")
    assert kept["origin"] == "detected"


def test_walkup_gate_windows_purely_additive_when_omitted():
    m1 = build_manifest("g.mp4", 30.0, [(10.0, 20.0)])
    m2 = build_manifest("g.mp4", 30.0, [(10.0, 20.0)], walkup_gate_windows=None)
    assert m1 == m2


def test_multi_file_manifest_walkup_gate_windows_are_per_file():
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)],
         "walkup_gate_windows": [(11.0, 14.0)]},
        {"source_file": "part2.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 10.0), (15.0, 25.0)]},
    ])
    part1_gap = next(s for s in m["segments"]
                     if s["source_file"] == "part1.mp4" and s["status"] == "cut"
                     and s["start_s"] == 10.0)
    part2_gap = next(s for s in m["segments"]
                     if s["source_file"] == "part2.mp4" and s["status"] == "cut"
                     and s["start_s"] == 10.0)
    assert part1_gap["origin"] == "walkup_gate"
    assert part2_gap["origin"] == "gap"


def test_hard_cut_and_walkup_gate_windows_coexist_without_interference():
    # a real manifest can have both kinds of trim in different gaps --
    # each gets its own origin, neither leaks into the other's span
    m = build_manifest(
        "g.mp4", 60.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0), (45.0, 55.0)],
        hard_cut_windows=[(11.0, 14.0)],
        walkup_gate_windows=[(41.0, 44.0)])
    origins = _origins(m)
    assert origins[(10.0, 15.0)] == "hard_cut"
    assert origins[(40.0, 45.0)] == "walkup_gate"
    assert origins[(25.0, 30.0)] == "gap"


# ---- hard_cut_boundary_starts_by_file also protects walkup_gate ----
# Same real stitch-safety requirement as a hard-cut window (see that
# function's docstring): a walkup-gate window is just as deliberately
# short by design and just as vulnerable to being silently re-bridged.

def test_hard_cut_boundary_flags_the_kept_span_right_after_a_walkup_gate():
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       walkup_gate_windows=[(11.0, 14.0)])
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == {15.0}


def test_walkup_gate_boundary_cleared_once_restored():
    m = build_manifest("g.mp4", 40.0, [(0.0, 10.0), (15.0, 25.0), (30.0, 40.0)],
                       walkup_gate_windows=[(11.0, 14.0)])
    gap = next(s for s in m["segments"] if s["start_s"] == 10.0)
    set_status(m, gap["id"], "kept")
    boundaries = hard_cut_boundary_starts_by_file(m)
    assert boundaries[0] == set()
