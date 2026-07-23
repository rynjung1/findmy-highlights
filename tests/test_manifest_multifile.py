"""Unit tests for the multi-file manifest: order locking, per-file span
isolation, and — per explicit request — that a segment sitting right at a
file boundary can still be individually restored/toggled in the Edit Log
without disturbing its neighbor in the other file."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.manifest import (build_multi_file_manifest, kept_spans_by_file,
                               load_manifest, save_manifest, set_status)


def two_file_setup():
    return [
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(20.0, 30.0)]},   # kept right up to file end
        {"source_file": "part2.mp4", "duration": 40.0,
         "kept_segments": [(0.0, 5.0)]},     # kept right from file start
    ]


def test_source_files_order_locked():
    m = build_multi_file_manifest(two_file_setup())
    assert m["source_files"] == ["part1.mp4", "part2.mp4"]


def test_source_file_index_matches_position():
    m = build_multi_file_manifest(two_file_setup())
    idx_by_file = {s["source_file"]: s["source_file_index"]
                  for s in m["segments"]}
    assert idx_by_file["part1.mp4"] == 0
    assert idx_by_file["part2.mp4"] == 1


def test_segment_ids_globally_unique_across_files():
    m = build_multi_file_manifest(two_file_setup())
    ids = [s["id"] for s in m["segments"]]
    assert len(ids) == len(set(ids))


def test_duration_s_is_sum_of_file_durations():
    m = build_multi_file_manifest(two_file_setup())
    assert m["duration_s"] == pytest.approx(70.0)


def test_local_timestamps_not_globally_offset():
    # part2's kept segment (0.0-5.0) must stay in part2's OWN local time,
    # not be shifted by part1's 30s duration
    m = build_multi_file_manifest(two_file_setup())
    part2_kept = [s for s in m["segments"]
                 if s["source_file"] == "part2.mp4" and s["status"] == "kept"]
    assert part2_kept[0]["start_s"] == 0.0
    assert part2_kept[0]["end_s"] == 5.0


def test_kept_spans_by_file_never_merges_across_boundary():
    # part1 kept through its own end (20-30), part2 kept from its own
    # start (0-5) — numerically these look "adjacent" if naively
    # concatenated (part1 ends at local 30, part2 "restarts" at local 0),
    # but they are different files and must NEVER be merged into one span
    m = build_multi_file_manifest(two_file_setup())
    by_file = kept_spans_by_file(m)
    assert by_file[0]["source_file"] == "part1.mp4"
    assert by_file[0]["spans"] == [(20.0, 30.0)]
    assert by_file[1]["source_file"] == "part2.mp4"
    assert by_file[1]["spans"] == [(0.0, 5.0)]


def test_kept_spans_by_file_merges_within_a_file_only():
    files = [{"source_file": "a.mp4", "duration": 20.0,
             "kept_segments": [(2.0, 5.0), (5.0, 8.0)]},   # touching, same file
            {"source_file": "b.mp4", "duration": 20.0,
             "kept_segments": [(2.0, 5.0)]}]
    m = build_multi_file_manifest(files)
    by_file = kept_spans_by_file(m)
    assert by_file[0]["spans"] == [(2.0, 8.0)]   # merged within a.mp4
    assert by_file[1]["spans"] == [(2.0, 5.0)]   # untouched


def test_boundary_segment_restore_toggle_is_isolated(tmp_path):
    """The specifically-requested case: a cut gap sitting right at a file
    boundary must be individually restorable without affecting the
    neighboring file's segment, and the toggle must persist through a
    save/load round trip (the actual Edit Log path: build -> save ->
    (later) load -> restore -> save)."""
    files = [
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(0.0, 25.0)]},   # cut gap 25.0-30.0 (near end)
        {"source_file": "part2.mp4", "duration": 30.0,
         "kept_segments": [(5.0, 30.0)]},   # cut gap 0.0-5.0 (near start)
    ]
    m = build_multi_file_manifest(files)
    p = tmp_path / "manifest.json"
    save_manifest(m, p)

    loaded = load_manifest(p)
    boundary_cuts = [s for s in loaded["segments"] if s["status"] == "cut"]
    assert len(boundary_cuts) == 2
    part1_gap = next(s for s in boundary_cuts
                     if s["source_file"] == "part1.mp4")
    part2_gap = next(s for s in boundary_cuts
                     if s["source_file"] == "part2.mp4")
    assert (part1_gap["start_s"], part1_gap["end_s"]) == (25.0, 30.0)
    assert (part2_gap["start_s"], part2_gap["end_s"]) == (0.0, 5.0)

    # restore ONLY part1's boundary gap
    set_status(loaded, part1_gap["id"], "kept")
    save_manifest(loaded, p)

    reloaded = load_manifest(p)
    by_file = kept_spans_by_file(reloaded)
    # part1 now fully kept (0-25 merged with restored 25-30)
    assert by_file[0]["spans"] == [(0.0, 30.0)]
    # part2 completely untouched by part1's restore
    assert by_file[1]["spans"] == [(5.0, 30.0)]

    # un-restore and confirm it cleanly reverts, again without touching part2
    set_status(reloaded, part1_gap["id"], "cut")
    by_file = kept_spans_by_file(reloaded)
    assert by_file[0]["spans"] == [(0.0, 25.0)]
    assert by_file[1]["spans"] == [(5.0, 30.0)]


def test_single_file_list_matches_build_manifest_shape():
    # build_multi_file_manifest with one file should be structurally
    # equivalent to build_manifest (source_file_index 0 throughout)
    m = build_multi_file_manifest(
        [{"source_file": "only.mp4", "duration": 10.0,
         "kept_segments": [(2.0, 5.0)]}])
    assert m["source_files"] == ["only.mp4"]
    assert all(s["source_file_index"] == 0 for s in m["segments"])


def test_score_fn_per_file():
    files = [{"source_file": "a.mp4", "duration": 10.0,
             "kept_segments": [(2.0, 5.0)], "score_fn": lambda a, b: 0.5},
            {"source_file": "b.mp4", "duration": 10.0,
             "kept_segments": [(2.0, 5.0)], "score_fn": lambda a, b: 0.9}]
    m = build_multi_file_manifest(files)
    scores = {s["source_file"]: s["detection_score"]
             for s in m["segments"] if s["status"] == "kept"}
    assert scores["a.mp4"] == 0.5
    assert scores["b.mp4"] == 0.9
