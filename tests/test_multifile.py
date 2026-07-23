"""Unit tests for multi-file ordering (pure logic) plus one real-ffprobe
smoke test confirming metadata actually gets read off disk."""

import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from pipeline.multifile import (AMBIGUITY_THRESHOLD_S, AmbiguousOrderError,
                                FileInfo, order_infos, probe_file,
                                resolve_order)

BASE = datetime(2026, 6, 1, 18, 0, 0)


def info(path, minutes_offset, duration_s=1800.0, w=1920, h=1080, fps=30.0,
        creation_time_override=...):
    ct = (BASE + timedelta(minutes=minutes_offset)) \
        if creation_time_override is ... else creation_time_override
    return FileInfo(path=path, width=w, height=h, fps=fps,
                    duration_s=duration_s, creation_time=ct)


def test_clean_two_file_case_orders_correctly():
    # part2 created well after part1 ends (a real inning-break gap)
    infos = [info("part2.mp4", 35), info("part1.mp4", 0)]
    r = order_infos(infos)
    assert r.ordered_paths == ["part1.mp4", "part2.mp4"]
    assert not r.ambiguous
    assert r.gaps_s[0] is None
    assert abs(r.gaps_s[1] - (35 * 60 - 1800.0)) < 0.01


def test_missing_metadata_is_ambiguous():
    infos = [info("a.mp4", 0), info("b.mp4", 30, creation_time_override=None)]
    r = order_infos(infos)
    assert r.ambiguous
    assert "creation_time" in r.reason
    assert r.ordered_paths == ["a.mp4", "b.mp4"]  # alphabetical fallback


def test_near_identical_timestamps_is_ambiguous():
    # exactly the case the user is constructing: two files whose creation
    # times are too close to trust for footage this long
    infos = [info("a.mp4", 0), info("b.mp4", 0, duration_s=1800.0,
                                    creation_time_override=BASE + timedelta(seconds=2))]
    r = order_infos(infos)
    assert r.ambiguous
    assert "within" in r.reason and f"{AMBIGUITY_THRESHOLD_S}s" in r.reason


def test_gap_just_over_threshold_not_ambiguous():
    # short duration so this isolates the raw-closeness check from the
    # separate overlap check (a 60s+ "gap" from a multi-hour file would
    # itself trip the overlap check, which is tested separately below)
    infos = [info("a.mp4", 0, duration_s=1.0),
            info("b.mp4", 0, creation_time_override=BASE
                 + timedelta(seconds=AMBIGUITY_THRESHOLD_S + 1))]
    r = order_infos(infos)
    assert not r.ambiguous


def test_single_file_never_ambiguous():
    r = order_infos([info("only.mp4", 0)])
    assert not r.ambiguous
    assert r.ordered_paths == ["only.mp4"]
    assert r.gaps_s == [None]


def test_empty_input():
    r = order_infos([])
    assert r.ordered_paths == []
    assert not r.ambiguous


def test_three_files_correct_order_and_gaps():
    infos = [info("c.mp4", 70, duration_s=1200.0),
            info("a.mp4", 0, duration_s=1200.0),
            info("b.mp4", 35, duration_s=1200.0)]
    r = order_infos(infos)
    assert r.ordered_paths == ["a.mp4", "b.mp4", "c.mp4"]
    assert not r.ambiguous
    assert r.gaps_s[0] is None
    assert r.gaps_s[1] > 0   # real gap between a and b
    assert r.gaps_s[2] > 0   # real gap between b and c


def test_mismatched_resolution_flagged():
    infos = [info("a.mp4", 0, w=1920, h=1080),
            info("b.mp4", 35, w=1280, h=720)]
    r = order_infos(infos)
    assert r.mismatched_resolution
    assert not r.mismatched_fps


def test_mismatched_fps_flagged():
    infos = [info("a.mp4", 0, fps=30.0), info("b.mp4", 35, fps=59.94)]
    r = order_infos(infos)
    assert r.mismatched_fps


def test_matching_resolution_and_fps_not_flagged():
    infos = [info("a.mp4", 0), info("b.mp4", 35)]
    r = order_infos(infos)
    assert not r.mismatched_resolution
    assert not r.mismatched_fps


def test_overlapping_creation_times_still_flagged_via_deltas():
    # b "starts" before a's duration would have finished — physically
    # inconsistent metadata; the delta-based ambiguity check must catch
    # it even though both files technically have creation_time
    infos = [info("a.mp4", 0, duration_s=3600.0),
            info("b.mp4", 1)]  # b starts only 1 min after a, but a runs 60 min
    r = order_infos(infos)
    assert r.ambiguous


# --- resolve_order: the actual "confirm/reorder" mechanism, not just the
# ambiguity signal. This is what scripts/detect_multi.py's CLI calls, and
# what proves the ambiguous case is not a dead end. ---

def test_resolve_order_uses_automatic_when_unambiguous():
    infos = [info("b.mp4", 35), info("a.mp4", 0)]
    r = order_infos(infos)
    assert resolve_order(["b.mp4", "a.mp4"], None, r) == ["a.mp4", "b.mp4"]


def test_resolve_order_raises_actionable_error_when_ambiguous():
    infos = [info("a.mp4", 0), info("b.mp4", 0,
                                    creation_time_override=BASE + timedelta(seconds=1))]
    r = order_infos(infos)
    with pytest.raises(AmbiguousOrderError) as exc_info:
        resolve_order(["a.mp4", "b.mp4"], None, r)
    err = exc_info.value
    # the exception must carry enough for the caller to offer a real path
    # forward, not just say "no" — a suggested order to confirm or override
    assert err.reason
    assert set(err.suggested_order) == {"a.mp4", "b.mp4"}
    assert "a.mp4" in str(err) and "b.mp4" in str(err)


def test_resolve_order_explicit_order_overrides_ambiguity():
    # THE key behavior for point 3: even when automatic ordering is
    # ambiguous, passing an explicit order proceeds — there is always a
    # way forward, the tool just won't guess on your behalf
    infos = [info("a.mp4", 0), info("b.mp4", 0,
                                    creation_time_override=BASE + timedelta(seconds=1))]
    r = order_infos(infos)
    assert resolve_order(["a.mp4", "b.mp4"], "b.mp4,a.mp4", r) \
        == ["b.mp4", "a.mp4"]


def test_resolve_order_explicit_order_works_without_any_metadata_at_all():
    # explicit order must work even when order_files() was never called /
    # metadata couldn't be read at all (result=None) — the user's own
    # confirmation is sufficient on its own, no probing required
    assert resolve_order(["x.mp4", "y.mp4"], "y.mp4,x.mp4", None) \
        == ["y.mp4", "x.mp4"]


def test_resolve_order_explicit_order_mismatch_raises_value_error():
    infos = [info("a.mp4", 0), info("b.mp4", 35)]
    r = order_infos(infos)
    with pytest.raises(ValueError, match="mismatch"):
        resolve_order(["a.mp4", "b.mp4"], "a.mp4,c.mp4", r)


def test_resolve_order_explicit_order_missing_file_raises():
    with pytest.raises(ValueError):
        resolve_order(["a.mp4", "b.mp4"], "a.mp4", None)


def test_resolve_order_explicit_order_extra_file_raises():
    with pytest.raises(ValueError):
        resolve_order(["a.mp4", "b.mp4"], "a.mp4,b.mp4,c.mp4", None)


# --- real ffprobe smoke test (needs ffmpeg on PATH, no reference clips) ---

def write_tiny_video_with_creation_time(path, creation_time: datetime,
                                        seconds=1, fps=5):
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=blue:s=64x48:d={seconds}:r={fps}",
         "-metadata", f"creation_time={creation_time.isoformat()}Z",
         "-y", str(path)], check=True)


def test_probe_file_reads_real_metadata(tmp_path):
    p = tmp_path / "tiny.mp4"
    ct = datetime(2026, 5, 1, 12, 0, 0)
    write_tiny_video_with_creation_time(p, ct)
    info_ = probe_file(str(p))
    assert info_.width == 64 and info_.height == 48
    assert abs(info_.duration_s - 1.0) < 0.3
    assert info_.creation_time is not None
    assert info_.creation_time.replace(tzinfo=None) == ct


def test_probe_file_missing_metadata_is_none(tmp_path):
    p = tmp_path / "no_meta.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi",
                    "-i", "color=c=red:s=64x48:d=1:r=5",
                    "-y", str(p)], check=True)
    info_ = probe_file(str(p))
    assert info_.creation_time is None
