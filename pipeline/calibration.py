"""Plate-zone calibration resolution (Stage 2, extended for Stage 4) and
writing (Stage 6, extended from scripts/calibrate.py so the backend's
calibration endpoint can't drift from the CLI's schema).

One calibration.json applies to every file in a batch by default — the
zero-friction path: click the plate once, it covers the whole session.
If the camera was repositioned mid-session, re-running scripts/calibrate.py
against just the affected file with --output <stem>.calibration.json
creates a per-file override; resolution checks for that override first
and only falls back to the shared file. No new tooling, no new flags on
the common path.
"""

import json
from pathlib import Path

import cv2

from pipeline.fusion import PlateZone

# ~one batter-height around the plate at typical backstop-mounted framing
DEFAULT_RADIUS_FRACTION = 0.26

# Bases sit much farther from a backstop-mounted camera than the plate
# does, so the same PIXEL radius that correctly covers a batter at the
# plate covers a wildly larger real-world area at a base's distance —
# validated directly (Stage 10): plate's default radius at first base's
# typical position engulfed roughly half the visible field (dugout,
# outfield, unrelated players), making occupancy read ~always-true. This
# flat pixel default is a rough, dataset-specific starting point (tuned
# against clip_base1/3/4's framing, not derived from camera geometry),
# not a substitute for letting a real calibration UI set a per-base
# radius explicitly.
DEFAULT_BASE_RADIUS_PX = 80.0


BASE_NAMES = ("first", "second", "third")


def _find_calibration_file(video_path, calib_dir=None) -> Path | None:
    vp = Path(video_path)
    directory = Path(calib_dir) if calib_dir is not None else vp.parent
    per_file = directory / f"{vp.stem}.calibration.json"
    shared = directory / "calibration.json"
    for candidate in (per_file, shared):
        if candidate.exists():
            return candidate
    return None


def resolve_zone(video_path, calib_dir=None) -> PlateZone | None:
    """Resolve the plate zone for one video: <stem>.calibration.json next
    to it (or in calib_dir) takes priority, else the shared
    calibration.json in the same directory, else None.

    Untouched by the Stage 10 `bases` extension below: this function only
    ever reads `plate_xy`/`zone_radius_px`, so an old home-only file and
    a new file with bases both resolve home identically, and every
    existing caller (pipeline.run.process_video, the backend) needs no
    changes to keep working exactly as before."""
    candidate = _find_calibration_file(video_path, calib_dir)
    if candidate is None:
        return None
    c = json.loads(candidate.read_text())
    return PlateZone(center_xy=tuple(c["plate_xy"]),
                     radius_px=c["zone_radius_px"])


def resolve_base_zones(video_path, calib_dir=None) -> dict:
    """Resolve whatever base zones (first/second/third) are marked for
    one video, using the exact same per-file-override-then-shared lookup
    as resolve_zone() -- same file, just reading `bases` instead of
    `plate_xy`. Returns a dict of only the bases actually present (empty
    if the file has no `bases` key, or doesn't exist at all): partial
    calibration -- some bases not visible in a given camera angle -- is
    the expected common case, not an error.

    Stage 10 scope: this is called by base-occupancy computation/
    validation only. Nothing in the detection pipeline (process_video,
    fuse, refine_segments, atbat_start_times) calls this -- wiring the
    result into segment-closing decisions is explicitly Stage 11's job,
    not this function's."""
    candidate = _find_calibration_file(video_path, calib_dir)
    if candidate is None:
        return {}
    c = json.loads(candidate.read_text())
    zones = {}
    for name, spec in c.get("bases", {}).items():
        if name not in BASE_NAMES:
            continue
        zones[name] = PlateZone(center_xy=tuple(spec["xy"]),
                                radius_px=spec["radius_px"])
    return zones


def probe_frame_size(video_path) -> tuple:
    """(width, height) via container metadata — no frame decode needed.
    scripts/calibrate.py's interactive path already has a decoded frame
    on hand (needed to display for clicking) and uses its shape instead;
    this is for callers, like the backend, that only need the dimensions
    to compute a default radius."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"could not open video: {video_path}")
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    cap.release()
    if w <= 0 or h <= 0:
        raise ValueError(f"could not read frame size: {video_path}")
    return w, h


PREVIEW_FRAME_SECONDS = 20.0  # past typical startup shake/lens-cap frames


def grab_preview_frame(video_path, at_seconds: float = PREVIEW_FRAME_SECONDS):
    """A decoded frame for the backend's calibration preview image — a
    fixed offset into the video, not frame 0 (often black/blurry/settling)
    and not an interactive pick like scripts/calibrate.py's grab_frame
    (there's no window to show a user here, just an HTTP response), so a
    early-but-not-the-very-first timestamp is the best fixed default.
    Clamped down for videos shorter than `at_seconds`. Returns a BGR
    numpy array at the video's native resolution — never resized, since
    the frontend's coordinate-scaling math depends on this frame's pixel
    dimensions exactly matching what probe_frame_size()/build_calibration()
    use."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        cap.release()
        raise ValueError(f"could not open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 0.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    duration = (n_frames / fps) if fps else 0.0
    target_s = min(at_seconds, max(duration - 0.5, 0.0)) if duration else 0.0

    if fps:
        cap.set(cv2.CAP_PROP_POS_FRAMES, int(target_s * fps))
    ok, frame = cap.read()
    if not ok:  # fall back to the very first frame rather than fail outright
        cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
        ok, frame = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"could not read a frame from: {video_path}")
    return frame


def build_calibration(frame_size, plate_xy, radius_px=None,
                      created_from: str = "", bases: dict | None = None) -> dict:
    """The calibration.json schema, in one place — both
    scripts/calibrate.py and the backend's calibration endpoint build
    through this, so the two can't silently disagree on field names or
    rounding. `radius_px` defaults to DEFAULT_RADIUS_FRACTION of frame
    height, same default scripts/calibrate.py has always used.

    `bases`, if given, is a dict of name -> (x, y) or (x, y, radius_px)
    for any of "first"/"second"/"third" -- independently optional, a
    camera angle that only shows first base can supply just that one.
    Omitting `bases` entirely (the default) produces the exact same dict
    Stage 6 always has, byte for byte -- this parameter is purely
    additive, existing callers that don't pass it are unaffected.
    A base without an explicit radius gets DEFAULT_BASE_RADIUS_PX, NOT
    the plate's radius_px -- reusing the plate's radius for a base is
    validated to be wrong (see that constant's docstring)."""
    w, h = frame_size
    if radius_px is None:
        radius_px = DEFAULT_RADIUS_FRACTION * h
    x, y = plate_xy
    calibration = {
        "frame_size": [w, h],
        "plate_xy": [round(float(x), 1), round(float(y), 1)],
        "zone_radius_px": round(float(radius_px), 1),
        "created_from": created_from,
    }
    if bases:
        out_bases = {}
        for name, spec in bases.items():
            if name not in BASE_NAMES:
                raise ValueError(f"unknown base {name!r}, expected one of {BASE_NAMES}")
            bx, by = spec[0], spec[1]
            br = spec[2] if len(spec) > 2 else DEFAULT_BASE_RADIUS_PX
            out_bases[name] = {
                "xy": [round(float(bx), 1), round(float(by), 1)],
                "radius_px": round(float(br), 1),
            }
        calibration["bases"] = out_bases
    return calibration


def save_calibration(dest_path, calibration: dict) -> None:
    Path(dest_path).write_text(json.dumps(calibration, indent=2))
