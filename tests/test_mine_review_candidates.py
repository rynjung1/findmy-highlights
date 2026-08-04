import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mine_review_candidates import (REAL_CANDIDATE_TYPES,  # noqa: E402
                                    _existing_windows_for_video, discover_batches)


def _write_record(reviews_dir, record_id, video_path, start_s, end_s,
                  candidate_type="hard_cut_dip"):
    reviews_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": record_id,
        "source": {"video_path": str(video_path), "source_file": "v.mkv"},
        "window": {"start_s": start_s, "end_s": end_s},
        "candidate_type": candidate_type,
    }
    (reviews_dir / f"{record_id}.json").write_text(json.dumps(record))


def _make_batch(uploads_root, batch_id, files, with_manifest=True, with_files_json=True):
    bdir = uploads_root / batch_id
    bdir.mkdir(parents=True)
    if with_files_json:
        (bdir / "files.json").write_text(json.dumps({"files": files}))
    for f in files:
        (bdir / f).write_bytes(b"fake video bytes")
    if with_manifest:
        (bdir / "manifest.json").write_text("{}")
    return bdir


def test_discover_batches_finds_already_processed_only(tmp_path):
    _make_batch(tmp_path, "batch_done", ["game.mkv"], with_manifest=True)
    _make_batch(tmp_path, "batch_pending", ["game2.mkv"], with_manifest=False)

    found, skipped = discover_batches(tmp_path)

    assert [b[0] for b in found] == ["batch_done"]
    assert found[0][2] == ["game.mkv"]
    assert skipped == ["batch_pending"]


def test_discover_batches_skips_missing_files_json(tmp_path):
    _make_batch(tmp_path, "batch_odd", ["game.mkv"], with_manifest=True,
               with_files_json=False)

    found, skipped = discover_batches(tmp_path)

    assert found == []
    assert skipped == ["batch_odd"]


def test_discover_batches_empty_uploads_root(tmp_path):
    found, skipped = discover_batches(tmp_path / "does_not_exist")
    assert found == []
    assert skipped == []


def test_discover_batches_ignores_non_directory_entries(tmp_path):
    (tmp_path / "stray_file.txt").write_text("not a batch")
    _make_batch(tmp_path, "batch_done", ["game.mkv"], with_manifest=True)

    found, skipped = discover_batches(tmp_path)

    assert [b[0] for b in found] == ["batch_done"]
    assert skipped == []


def test_existing_windows_for_video_matches_only_this_video(tmp_path):
    reviews_dir = tmp_path / "reviews"
    _write_record(reviews_dir, "hc_a", "game.mkv", 1.0, 2.0)
    _write_record(reviews_dir, "hc_b", "game.mkv", 5.0, 6.0)
    _write_record(reviews_dir, "hc_c", "other_game.mkv", 9.0, 10.0)

    windows = _existing_windows_for_video(reviews_dir, "game.mkv")

    assert windows == {(1.0, 2.0), (5.0, 6.0)}


def test_existing_windows_for_video_empty_when_no_reviews_dir(tmp_path):
    windows = _existing_windows_for_video(tmp_path / "does_not_exist", "game.mkv")
    assert windows == set()


def test_existing_windows_for_video_no_match_for_different_video(tmp_path):
    reviews_dir = tmp_path / "reviews"
    _write_record(reviews_dir, "hc_a", "game.mkv", 1.0, 2.0)

    windows = _existing_windows_for_video(reviews_dir, "a_different_game.mkv")

    assert windows == set()


def test_existing_windows_for_video_scoped_to_requested_types(tmp_path):
    # Real regression test for the real bug found tonight: mining
    # boundary_crossing-only against a video that already had 83
    # hard_cut_dip records sized its budget as 83+30 (since the old
    # version counted every type), even though none of those 83 windows
    # could ever match a boundary_crossing one -- every "new" candidate
    # came back genuinely new and the run overshot --limit 30 by 83.
    # Scoping the count to the same types being requested is the fix.
    reviews_dir = tmp_path / "reviews"
    _write_record(reviews_dir, "hc_a", "game.mkv", 1.0, 2.0, candidate_type="hard_cut_dip")
    _write_record(reviews_dir, "hc_b", "game.mkv", 3.0, 4.0, candidate_type="hard_cut_dip")
    _write_record(reviews_dir, "bc_a", "game.mkv", 5.0, 5.0, candidate_type="boundary_crossing")

    unscoped = _existing_windows_for_video(reviews_dir, "game.mkv")
    assert len(unscoped) == 3  # old (buggy) behavior: every type counts

    scoped = _existing_windows_for_video(
        reviews_dir, "game.mkv", candidate_types=frozenset({"boundary_crossing"}))
    assert scoped == {(5.0, 5.0)}  # only the real boundary_crossing window


def test_real_candidate_types_excludes_control():
    # control samples are a separate, probabilistic addition (see
    # pipeline.review._control_candidate), never part of the ranked
    # pool select_candidates filters by candidate_types -- --candidate-types
    # control would silently mine nothing, so it must not validate.
    assert REAL_CANDIDATE_TYPES == {"hard_cut_dip", "boundary_crossing", "veto_boundary"}
