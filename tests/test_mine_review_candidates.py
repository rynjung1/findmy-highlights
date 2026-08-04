import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from mine_review_candidates import discover_batches  # noqa: E402


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
