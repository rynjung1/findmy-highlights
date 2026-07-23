"""Plate-zone calibration resolution (Phase 2, extended for Phase 4).

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

from pipeline.fusion import PlateZone


def resolve_zone(video_path, calib_dir=None) -> PlateZone | None:
    """Resolve the plate zone for one video: <stem>.calibration.json next
    to it (or in calib_dir) takes priority, else the shared
    calibration.json in the same directory, else None."""
    vp = Path(video_path)
    directory = Path(calib_dir) if calib_dir is not None else vp.parent

    per_file = directory / f"{vp.stem}.calibration.json"
    shared = directory / "calibration.json"
    for candidate in (per_file, shared):
        if candidate.exists():
            c = json.loads(candidate.read_text())
            return PlateZone(center_xy=tuple(c["plate_xy"]),
                             radius_px=c["zone_radius_px"])
    return None
