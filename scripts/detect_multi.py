"""Run action detection across multiple video files as one game timeline.

Usage:
    python scripts/detect_multi.py file1.mkv file2.mkv ... [--manifest PATH]

Ordering is determined from file creation-time metadata. If that signal
is missing or ambiguous (two files whose timestamps are implausibly close
for footage this long — see pipeline/multifile.py), this script does NOT
guess: it prints the ambiguity reason and a suggested order, and exits
non-zero — but this is not a dead end. Pass
--order file1.mkv,file2.mkv,... with your confirmed order (the exact
command is printed for you) to proceed explicitly; this is the "ask the
user to confirm/reorder" fallback the project spec calls for. Passing
--order also skips automatic metadata probing entirely (including the
mismatched-resolution/fps and gap-detection warnings below) — an explicit
order is taken as full confirmation, not second-guessed.

Per the Stage 3/4 design decision, a file boundary is always a hard
boundary: play extension and at-bat state never cross it, regardless of
gap size. Each file is processed independently by the same pipeline
scripts/detect.py uses for one file (pipeline/run.py's process_video) —
there is no shared state between files to leak across a boundary.

Calibration: a shared calibration.json in the same directory applies to
every file by default. A per-file override (<stem>.calibration.json)
takes priority for a specific file if the camera moved mid-session — see
pipeline/calibration.py.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.calibration import resolve_zone
from pipeline.manifest import build_multi_file_manifest, save_manifest
from pipeline.multifile import AmbiguousOrderError, order_files, resolve_order
from pipeline.run import DEFAULT_CACHE_DIR, process_video
from pipeline.segments import SegmentConfig, smooth_scores, total_duration

CACHE_DIR = DEFAULT_CACHE_DIR


def fmt_ts(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("videos", nargs="+")
    ap.add_argument("--order", metavar="f1,f2,...",
                    help="explicit confirmed order, required if automatic "
                    "ordering is ambiguous")
    ap.add_argument("--motion-only", action="store_true")
    ap.add_argument("--manifest", metavar="PATH")
    args = ap.parse_args()

    result = None if args.order else order_files(args.videos)
    try:
        ordered_paths = resolve_order(args.videos, args.order, result)
    except ValueError as e:
        sys.exit(str(e))
    except AmbiguousOrderError as e:
        sys.exit(
            f"{e}\n"
            f"this is NOT a dead end: re-run with\n"
            f"  --order {','.join(e.suggested_order)}\n"
            f"(substituting your own confirmed order if the suggestion "
            f"above is wrong) to proceed")

    if result is not None:
        if result.mismatched_resolution or result.mismatched_fps:
            print(f"warning: input files have mismatched "
                 f"{'resolution' if result.mismatched_resolution else ''}"
                 f"{' and ' if result.mismatched_resolution and result.mismatched_fps else ''}"
                 f"{'frame rate' if result.mismatched_fps else ''} "
                 f"(Stage 5 stitching will need to re-encode)", file=sys.stderr)
        for path, gap in zip(ordered_paths, result.gaps_s):
            if gap is not None and gap > 1.0:
                print(f"note: {gap:.0f}s gap detected before {path}",
                     file=sys.stderr)

    print(f"processing order: {', '.join(ordered_paths)}", file=sys.stderr)

    files_for_manifest = []
    for path in ordered_paths:
        zone = None if args.motion_only else resolve_zone(path)
        warn = (lambda msg, p=path: print(f"warning: {msg}", file=sys.stderr))
        segments, vetoed, duration, motion = process_video(
            path, zone, args.motion_only, cache_dir=CACHE_DIR, warn=warn)

        print(f"{path}: {duration:.1f}s, {len(segments)} segments, "
             f"{total_duration(segments):.1f}s flagged "
             f"({100 * total_duration(segments) / duration:.0f}%)")
        for a, b in segments:
            print(f"  {fmt_ts(a)} - {fmt_ts(b)}  ({b - a:5.1f}s)")

        sm = smooth_scores(motion.times, motion.scores,
                           SegmentConfig().smooth_window_s)

        def peak_score(a, b, sm=sm, mt=motion.times):
            idx = (mt >= a) & (mt <= b)
            return float(sm[idx].max()) if idx.any() else 0.0

        files_for_manifest.append({
            "source_file": Path(path).name, "duration": duration,
            "kept_segments": segments, "score_fn": peak_score,
        })

    if args.manifest:
        manifest = build_multi_file_manifest(files_for_manifest)
        save_manifest(manifest, args.manifest)
        print(f"manifest written to {args.manifest}", file=sys.stderr)


if __name__ == "__main__":
    main()
