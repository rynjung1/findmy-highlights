"""Run motion-based detection on one video and print candidate segments.

Usage:
    python scripts/detect.py path/to/video.mkv [--json]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.segments import SegmentConfig, scores_to_segments, total_duration


def fmt_ts(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    result = compute_motion(args.video)
    segments = scores_to_segments(result.times, result.scores, SegmentConfig())

    if args.json:
        print(json.dumps({
            "video": args.video,
            "duration": result.duration,
            "segments": [{"start": a, "end": b} for a, b in segments],
        }, indent=2))
        return

    print(f"{args.video}: {result.duration:.1f}s, "
          f"{len(segments)} candidate segments, "
          f"{total_duration(segments):.1f}s flagged "
          f"({100 * total_duration(segments) / result.duration:.0f}% of clip)")
    for a, b in segments:
        print(f"  {fmt_ts(a)} - {fmt_ts(b)}  ({b - a:5.1f}s)")


if __name__ == "__main__":
    main()
