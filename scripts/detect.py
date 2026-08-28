"""Run action detection on one video and print candidate segments.

Usage:
    python scripts/detect.py path/to/video.mkv [--motion-only] [--json]

By default runs the fused pipeline (motion + person detection + plate
occupancy). Requires a calibration.json next to the video for the plate
zone (see scripts/calibrate.py); without one, plate occupancy is skipped
and a warning is printed. --motion-only skips person detection entirely
(the Stage 1 baseline).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.calibration import resolve_zone
from pipeline.run import DEFAULT_CACHE_DIR, process_video
from pipeline.segments import SegmentConfig, total_duration

CACHE_DIR = DEFAULT_CACHE_DIR


def fmt_ts(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}"


def run(video: str, motion_only: bool):
    """Thin CLI wrapper: resolve calibration, run the shared pipeline.
    Returns (final_segments, vetoed, duration, motion_result,
    hard_cut_windows, walkup_gate_windows)."""
    zone = None if motion_only else resolve_zone(video)
    warn = (lambda msg: print(f"warning: {msg}", file=sys.stderr))
    return process_video(video, zone, motion_only, cache_dir=CACHE_DIR,
                         warn=warn)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--motion-only", action="store_true",
                    help="Stage 1 baseline: skip person detection")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--manifest", metavar="PATH",
                    help="write a manifest JSON of kept segments and cut gaps")
    args = ap.parse_args()

    segments, vetoed, duration, motion, hard_cut_windows, walkup_gate_windows = run(
        args.video, args.motion_only)

    if args.manifest:
        import numpy as np
        from pipeline.manifest import build_manifest, save_manifest
        from pipeline.segments import find_skip_suggestions, smooth_scores
        sm = smooth_scores(motion.times, motion.scores,
                           SegmentConfig().smooth_window_s)

        def peak_score(a, b):
            idx = (motion.times >= a) & (motion.times <= b)
            return float(sm[idx].max()) if idx.any() else 0.0

        def skip_fn(a, b):
            return find_skip_suggestions(a, b, motion.times, motion.scores)

        manifest = build_manifest(Path(args.video).name, duration, segments,
                                  score_fn=peak_score, skip_fn=skip_fn,
                                  hard_cut_windows=hard_cut_windows,
                                  walkup_gate_windows=walkup_gate_windows)
        save_manifest(manifest, args.manifest)
        print(f"manifest written to {args.manifest}", file=sys.stderr)

    if args.json:
        print(json.dumps({
            "video": args.video,
            "duration": duration,
            "segments": [{"start": a, "end": b} for a, b in segments],
            "vetoed": [{"start": a, "end": b} for a, b in vetoed],
        }, indent=2))
        return

    print(f"{args.video}: {duration:.1f}s, "
          f"{len(segments)} candidate segments, "
          f"{total_duration(segments):.1f}s flagged "
          f"({100 * total_duration(segments) / duration:.0f}% of clip)")
    for a, b in segments:
        print(f"  {fmt_ts(a)} - {fmt_ts(b)}  ({b - a:5.1f}s)")
    for a, b in vetoed:
        print(f"  vetoed (no person near motion): {fmt_ts(a)} - {fmt_ts(b)}")


if __name__ == "__main__":
    main()
