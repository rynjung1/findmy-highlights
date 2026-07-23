"""Run action detection on one video and print candidate segments.

Usage:
    python scripts/detect.py path/to/video.mkv [--motion-only] [--json]

By default runs the fused pipeline (motion + person detection + plate
occupancy). Requires a calibration.json next to the video for the plate
zone (see scripts/calibrate.py); without one, plate occupancy is skipped
and a warning is printed. --motion-only skips person detection entirely
(the Phase 1 baseline).
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.segments import SegmentConfig, scores_to_segments, total_duration

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / ".cache" / "detections"


def fmt_ts(seconds: float) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}"


def load_zone(video_path: Path):
    from pipeline.fusion import PlateZone
    calib_file = video_path.parent / "calibration.json"
    if not calib_file.exists():
        return None
    c = json.loads(calib_file.read_text())
    return PlateZone(center_xy=tuple(c["plate_xy"]),
                     radius_px=c["zone_radius_px"])


def run(video: str, motion_only: bool):
    """Full pipeline: motion -> veto -> play extension -> padding.
    Returns (final_segments, vetoed, duration, motion_result)."""
    from pipeline.fusion import apply_veto, fuse

    motion = compute_motion(video)
    if motion_only:
        segs = scores_to_segments(motion.times, motion.scores, SegmentConfig())
        return segs, [], motion.duration, motion

    from pipeline.atbat import AtBatConfig, atbat_start_times
    from pipeline.detection import DetectionConfig, detect_persons
    from pipeline.fusion import compute_occupancy
    from pipeline.refine import RefineConfig, refine_segments
    from pipeline.segments import smooth_scores
    from pipeline.settle import SettleConfig

    vp = Path(video)
    zone = load_zone(vp)
    if zone is None:
        print(f"warning: no calibration.json next to {video}; "
              f"plate-occupancy signals disabled", file=sys.stderr)
    det = detect_persons(video, DetectionConfig(), cache_dir=str(CACHE_DIR))
    fused = fuse(motion.times, motion.scores, motion.grids,
                 motion.frame_size, motion.analysis_size, motion.border_px,
                 det.times, det.boxes, zone)
    # motion alone owns segment open AND raw exit (Phase 3 replaced the
    # Phase 2 score-sustain with the explicit play-extension below)
    raw = scores_to_segments(motion.times, motion.scores, SegmentConfig())
    kept, vetoed = apply_veto(raw, fused)

    sm = smooth_scores(motion.times, motion.scores,
                       SegmentConfig().smooth_window_s)
    # one SettleConfig, shared explicitly between the at-bat detector and
    # play extension so "has motion settled" can't silently drift apart
    settle_cfg = SettleConfig()
    if zone is not None:
        occ = compute_occupancy(det.times, det.boxes, zone, 0.30)
        fires = atbat_start_times(det.times, occ, motion.times, sm,
                                  AtBatConfig(settle=settle_cfg))
    else:
        occ = [False] * len(det.times)
        fires = []
    final = refine_segments(kept, motion.times, sm, det.times, occ, fires,
                            motion.duration, RefineConfig(settle=settle_cfg))
    return final, vetoed, motion.duration, motion


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--motion-only", action="store_true",
                    help="Phase 1 baseline: skip person detection")
    ap.add_argument("--json", action="store_true", help="emit JSON")
    ap.add_argument("--manifest", metavar="PATH",
                    help="write a manifest JSON of kept segments and cut gaps")
    args = ap.parse_args()

    segments, vetoed, duration, motion = run(args.video, args.motion_only)

    if args.manifest:
        import numpy as np
        from pipeline.manifest import build_manifest, save_manifest
        from pipeline.segments import smooth_scores
        sm = smooth_scores(motion.times, motion.scores,
                           SegmentConfig().smooth_window_s)

        def peak_score(a, b):
            idx = (motion.times >= a) & (motion.times <= b)
            return float(sm[idx].max()) if idx.any() else 0.0

        manifest = build_manifest(Path(args.video).name, duration, segments,
                                  score_fn=peak_score)
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
