"""One-time (re-run only if the demo clip or DetectionConfig changes):
stamps demo_assets/clip_whiff1.mkv with the fixed mtime backend/demo.py
uses for its cache-key seeding, runs the real RF-DETR detection pass on
it, and writes demo_assets/detections_cache.json in the exact shape
pipeline.detection.detect_persons' own file cache uses -- see
backend/demo.py's module docstring for why the mtime has to be pinned
explicitly rather than trusted from git/docker.

Usage:
    ./venv/bin/python scripts/precompute_demo_cache.py
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.demo import DEMO_ASSETS_DIR, DEMO_FIXED_MTIME_NS, DEMO_VIDEO_NAME
from pipeline.detection import DetectionConfig, detect_persons


def main():
    video_path = DEMO_ASSETS_DIR / DEMO_VIDEO_NAME
    if not video_path.exists():
        raise SystemExit(f"missing {video_path} -- copy the demo clip in first")

    os.utime(video_path, ns=(DEMO_FIXED_MTIME_NS, DEMO_FIXED_MTIME_NS))

    print(f"running real RF-DETR detection on {video_path} ...")
    result = detect_persons(str(video_path), DetectionConfig(), cache_dir=None)

    out_path = DEMO_ASSETS_DIR / "detections_cache.json"
    out_path.write_text(json.dumps({
        "times": result.times, "boxes": result.boxes,
        "frame_size": list(result.frame_size)}))
    print(f"wrote {out_path} ({len(result.times)} detection samples)")

    # re-stamp: detect_persons doesn't touch the source file, but this
    # makes the "the mtime is exactly what backend/demo.py expects"
    # invariant explicit and re-verified on every run of this script,
    # not just assumed from the earlier call above.
    os.utime(video_path, ns=(DEMO_FIXED_MTIME_NS, DEMO_FIXED_MTIME_NS))
    print(f"stamped {video_path} with fixed mtime {DEMO_FIXED_MTIME_NS}ns")


if __name__ == "__main__":
    main()
