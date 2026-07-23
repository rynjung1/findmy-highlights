"""Unit tests for plate-zone calibration resolution: shared-by-default,
per-file override takes priority (Phase 4)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.calibration import resolve_zone


def write_calib(path, x, y, r):
    Path(path).write_text(json.dumps({
        "frame_size": [1920, 1080], "plate_xy": [x, y],
        "zone_radius_px": r, "created_from": "test"}))


def test_no_calibration_returns_none(tmp_path):
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_zone(str(video)) is None


def test_shared_calibration_used_by_default(tmp_path):
    write_calib(tmp_path / "calibration.json", 100, 200, 50)
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    zone = resolve_zone(str(video))
    assert zone.center_xy == (100, 200)
    assert zone.radius_px == 50


def test_shared_calibration_applies_to_multiple_files(tmp_path):
    write_calib(tmp_path / "calibration.json", 100, 200, 50)
    for name in ("part1.mkv", "part2.mkv"):
        (tmp_path / name).write_bytes(b"")
        zone = resolve_zone(str(tmp_path / name))
        assert zone.center_xy == (100, 200)


def test_per_file_override_takes_priority(tmp_path):
    write_calib(tmp_path / "calibration.json", 100, 200, 50)
    write_calib(tmp_path / "part2.calibration.json", 900, 700, 30)
    (tmp_path / "part1.mkv").write_bytes(b"")
    (tmp_path / "part2.mkv").write_bytes(b"")

    zone1 = resolve_zone(str(tmp_path / "part1.mkv"))
    zone2 = resolve_zone(str(tmp_path / "part2.mkv"))
    assert zone1.center_xy == (100, 200)     # falls back to shared
    assert zone2.center_xy == (900, 700)     # override wins


def test_per_file_override_without_shared_fallback(tmp_path):
    # only an override exists, no shared calibration.json at all
    write_calib(tmp_path / "part1.calibration.json", 42, 43, 10)
    (tmp_path / "part1.mkv").write_bytes(b"")
    (tmp_path / "part2.mkv").write_bytes(b"")
    assert resolve_zone(str(tmp_path / "part1.mkv")).center_xy == (42, 43)
    assert resolve_zone(str(tmp_path / "part2.mkv")) is None  # no override, no shared
