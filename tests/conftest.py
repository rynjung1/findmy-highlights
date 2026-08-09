"""Shared test helpers.

reference_clips_dir() mirrors scripts/regression.py's own --clips-dir
override: reference_clips/*.mkv is gitignored, so a fresh CI checkout
(a separate clone from wherever the real footage actually lives) has
none of the real video files, only the tracked *.calibration.json
sidecars. FMH_REFERENCE_CLIPS_DIR lets CI point tests at the real
directory without changing anything for local dev, where it's unset
and every test keeps resolving reference_clips/ next to this repo
exactly as before.
"""
import os
from pathlib import Path


def reference_clips_dir() -> Path:
    default = Path(__file__).resolve().parent.parent / "reference_clips"
    return Path(os.environ.get("FMH_REFERENCE_CLIPS_DIR", str(default)))
