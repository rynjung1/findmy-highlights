"""Unit tests for plate-zone calibration resolution: shared-by-default,
per-file override takes priority (Stage 4)."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.calibration import (build_calibration, resolve_base_zones,
                                  resolve_calibrated_scale_px, resolve_zone)


def write_calib(path, x, y, r, bases=None):
    doc = {"frame_size": [1920, 1080], "plate_xy": [x, y],
          "zone_radius_px": r, "created_from": "test"}
    if bases is not None:
        doc["bases"] = bases
    Path(path).write_text(json.dumps(doc))


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


# ---- Stage 10: base zones, backward compatibility ----

def test_old_home_only_file_has_no_base_zones(tmp_path):
    # a pre-Stage-10 calibration.json, byte for byte -- no "bases" key at all
    write_calib(tmp_path / "calibration.json", 100, 200, 50)
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_base_zones(str(video)) == {}
    # and home resolution is completely unaffected
    assert resolve_zone(str(video)).center_xy == (100, 200)


def test_no_calibration_file_returns_no_base_zones(tmp_path):
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_base_zones(str(video)) == {}


def test_partial_base_set_resolves_only_whats_present(tmp_path):
    # some bases may not be visible in a given camera angle -- partial
    # calibration must be allowed, per the Stage 10 spec
    write_calib(tmp_path / "calibration.json", 100, 200, 50,
               bases={"first": {"xy": [900, 600], "radius_px": 80}})
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    zones = resolve_base_zones(str(video))
    assert set(zones) == {"first"}
    assert zones["first"].center_xy == (900, 600)
    assert zones["first"].radius_px == 80


def test_all_three_bases_resolve(tmp_path):
    write_calib(tmp_path / "calibration.json", 100, 200, 50, bases={
        "first": {"xy": [900, 600], "radius_px": 80},
        "second": {"xy": [960, 400], "radius_px": 70},
        "third": {"xy": [400, 600], "radius_px": 80},
    })
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    zones = resolve_base_zones(str(video))
    assert set(zones) == {"first", "second", "third"}
    assert zones["second"].center_xy == (960, 400)


def test_base_zones_respect_per_file_override(tmp_path):
    write_calib(tmp_path / "calibration.json", 100, 200, 50,
               bases={"first": {"xy": [1, 1], "radius_px": 80}})
    write_calib(tmp_path / "part2.calibration.json", 900, 700, 30,
               bases={"first": {"xy": [999, 888], "radius_px": 90}})
    (tmp_path / "part1.mkv").write_bytes(b"")
    (tmp_path / "part2.mkv").write_bytes(b"")
    assert resolve_base_zones(str(tmp_path / "part1.mkv"))["first"].center_xy == (1, 1)
    assert resolve_base_zones(str(tmp_path / "part2.mkv"))["first"].center_xy == (999, 888)


def test_build_calibration_without_bases_is_byte_identical_to_before(tmp_path):
    # the exact call shape every pre-Stage-10 caller uses
    calib = build_calibration((1920, 1080), (100.0, 200.0), radius_px=50.0,
                              created_from="test.mkv")
    assert calib == {
        "frame_size": [1920, 1080],
        "plate_xy": [100.0, 200.0],
        "zone_radius_px": 50.0,
        "created_from": "test.mkv",
    }
    assert "bases" not in calib


def test_build_calibration_with_bases_round_trips_through_resolve(tmp_path):
    calib = build_calibration((1920, 1080), (100.0, 200.0), radius_px=50.0,
                              created_from="test.mkv",
                              bases={"first": (900.0, 600.0)})
    (tmp_path / "calibration.json").write_text(json.dumps(calib))
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    zones = resolve_base_zones(str(video))
    assert zones["first"].center_xy == (900.0, 600.0)
    # no explicit radius given -> DEFAULT_BASE_RADIUS_PX, NOT the plate's
    # radius_px (validated to be wrong for bases, see calibration.py)
    from pipeline.calibration import DEFAULT_BASE_RADIUS_PX
    assert zones["first"].radius_px == DEFAULT_BASE_RADIUS_PX
    assert zones["first"].radius_px != 50.0


def test_build_calibration_with_explicit_base_radius(tmp_path):
    calib = build_calibration((1920, 1080), (100.0, 200.0),
                              bases={"first": (900.0, 600.0, 65.0)})
    assert calib["bases"]["first"]["radius_px"] == 65.0


def test_build_calibration_rejects_unknown_base_name():
    import pytest
    with pytest.raises(ValueError):
        build_calibration((1920, 1080), (100.0, 200.0),
                          bases={"home_plate_typo": (1, 2)})


# ---- resolve_calibrated_scale_px (calibrated-distance scale boost) ----

def test_calibrated_scale_px_none_when_no_calibration_file_at_all(tmp_path):
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_calibrated_scale_px(str(video)) is None


def test_calibrated_scale_px_none_for_home_only_calibration(tmp_path):
    # a pre-Stage-10 file, no "bases" key at all -- must fall back, not guess
    write_calib(tmp_path / "calibration.json", 100, 200, 50)
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_calibrated_scale_px(str(video)) is None


def test_calibrated_scale_px_none_when_bases_present_but_first_missing(tmp_path):
    # second/third marked, first not -- still None, per the function's own
    # "first is what the multi-base UI always marks alongside home" contract
    write_calib(tmp_path / "calibration.json", 100, 200, 50, bases={
        "second": {"xy": [960, 400], "radius_px": 70},
        "third": {"xy": [400, 600], "radius_px": 80},
    })
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_calibrated_scale_px(str(video)) is None


def test_calibrated_scale_px_home_and_first_only_returns_hypot_distance(tmp_path):
    write_calib(tmp_path / "calibration.json", 0, 0, 50,
               bases={"first": {"xy": [3, 4], "radius_px": 80}})
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_calibrated_scale_px(str(video)) == 5.0  # 3-4-5 triangle


def test_calibrated_scale_px_ignores_second_and_third_uses_only_first(tmp_path):
    # all 4 points present -- result must be identical to the home+first-only
    # case above, home-to-third and home-to-second are NOT interchangeable
    # measurements (see the function's own docstring on why mixing segments
    # would silently corrupt the ratio)
    write_calib(tmp_path / "calibration.json", 0, 0, 50, bases={
        "first": {"xy": [3, 4], "radius_px": 80},
        "second": {"xy": [960, 400], "radius_px": 70},
        "third": {"xy": [400, 600], "radius_px": 80},
    })
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    assert resolve_calibrated_scale_px(str(video)) == 5.0


def test_calibrated_scale_px_known_good_regression_case(tmp_path):
    # real values from the multi-base calibration smoke test (2026-08-08):
    # plate (960, 850), first (1400, 700) -> 464.86557...px, confirmed
    # against a direct hypot() calculation at the time
    import pytest
    write_calib(tmp_path / "calibration.json", 960, 850, 40,
               bases={"first": {"xy": [1400, 700], "radius_px": 35}})
    video = tmp_path / "clip.mkv"
    video.write_bytes(b"")
    scale = resolve_calibrated_scale_px(str(video))
    assert scale == pytest.approx(464.86557196677836)
