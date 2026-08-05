"""Demo mode: a bundled, pre-cached short clip a visitor can run through
the full real pipeline in well under a minute, with no upload and no
manual calibration step -- the default first thing a new visitor sees,
kept separate from a real game upload.

Real pipeline, not a canned response: motion computation, fusion,
segment detection, at-bat extension, hard-cut, and export all run live
here, through the exact same code path a real upload takes
(backend.pipeline_runner.run_detect_then_export_job, unchanged). The one
thing pre-computed is the RF-DETR detection pass itself -- the only real
ML inference in the core pipeline, and the one step slow/expensive
enough to threaten a "well under a minute" demo on unknown deployment
hardware. It's seeded into the real detection cache
(pipeline.detection.detect_persons' own cache_dir mechanism -- the exact
thing that already makes any repeat run of the same file free, not a
demo-specific shortcut) under a fixed, deterministic mtime stamp, since
neither `git` nor `docker build` reliably preserve a source file's real
mtime, and the cache key is derived from it.

demo_assets/ ships in the Docker image (see .gitignore's explicit
exception for demo_assets/clip_whiff1.mkv) and holds:
  - clip_whiff1.mkv: real footage from this project's own reference set
    -- a genuine, clean swing-and-miss (tests/ground_truth/clip_whiff1.json).
  - calibration.json: the plate zone for this camera setup (shared with
    most of this project's reference clips).
  - detections_cache.json: precomputed pipeline.detection.detect_persons
    output, in the exact {"times", "boxes", "frame_size"} shape that
    module's own cache file already uses -- see
    scripts/precompute_demo_cache.py.
"""

import os
import shutil
from pathlib import Path

# _cache_key is "private" by leading-underscore convention, not by any
# real encapsulation boundary -- reused directly here (rather than
# duplicating its hashing formula) so a future change to that formula
# can't silently desync this module's seeded cache key from what
# detect_persons() actually looks up.
from pipeline.detection import DetectionConfig, _cache_key

ROOT = Path(__file__).resolve().parent.parent
DEMO_ASSETS_DIR = ROOT / "demo_assets"
DEMO_VIDEO_NAME = "clip_whiff1.mkv"
# Arbitrary but fixed -- the exact value doesn't matter, only that it's
# always the same one both here and in scripts/precompute_demo_cache.py.
DEMO_FIXED_MTIME_NS = 1735689600 * 1_000_000_000  # 2025-01-01T00:00:00Z


def seed_demo_batch(bdir: Path, detection_cache_dir) -> None:
    """Copies the bundled demo clip + calibration into a fresh batch dir
    (stamped with the fixed mtime so its detection cache key is
    deterministic regardless of this file's real on-disk mtime), and
    seeds detection_cache_dir with the precomputed detection result
    under that exact key -- a real cache hit on the very first demo run,
    not a special-cased fake pipeline path. Idempotent: safe to call
    even if the seeded cache file already exists from an earlier run."""
    bdir = Path(bdir)
    video_dest = bdir / DEMO_VIDEO_NAME
    shutil.copy(DEMO_ASSETS_DIR / DEMO_VIDEO_NAME, video_dest)
    os.utime(video_dest, ns=(DEMO_FIXED_MTIME_NS, DEMO_FIXED_MTIME_NS))

    shutil.copy(DEMO_ASSETS_DIR / "calibration.json", bdir / "calibration.json")

    cfg = DetectionConfig()  # must match process_video's own default config
    key = _cache_key(video_dest, cfg)
    cache_file = Path(detection_cache_dir) / f"{video_dest.stem}_{key}.json"
    if not cache_file.exists():
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text((DEMO_ASSETS_DIR / "detections_cache.json").read_text())
