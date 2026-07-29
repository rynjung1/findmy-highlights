"""Detection regression: run the pipeline on every reference clip and score
it against the hand-written ground truth.

Per clip this reports, for BOTH the motion-only baseline and the refined
pipeline (motion -> veto -> play extension via at-bat boundaries -> padding):
  - recall on REQUIRED events — must be 100% for the fused pipeline.
  - capture of borderline (non-required) events — informational.
  - over-inclusion (flagged seconds / clip seconds).

Hard FAILURE conditions (exit 1):
  1. Fused pipeline misses any required event.
  2. Fused recall is lower than motion-only recall on any clip (the fused
     score is additive, so this should be structurally impossible — if it
     trips, something is broken).
  3. A vetoed segment overlaps any required event window (safety net for
     the no-person-near-motion veto; a distant/occluded fielder must never
     silently erase a real play).
  4. An event marked "check_continuity" is not FULLY covered by a single
     contiguous span of kept segments (these are the defensive-play windows
     Stage 3's play-extension depends on: any internal coverage gap means
     the play would be visibly chopped). The raw fused score is allowed to
     dip below the exit threshold inside the window — a batter frozen
     mid-stance pre-pitch legitimately produces near-zero motion, and
     hysteresis + merging is the mechanism that bridges it — but the dip
     depth/duration is printed for review.

Usage:
    python scripts/regression.py [--clips-dir reference_clips] [--motion-only]
"""

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.segments import (SegmentConfig, scores_to_segments, smooth_scores,
                               segment_covers, total_duration)
from pipeline.fusion import (FusionConfig, apply_veto, fuse, scale_boost_factor,
                             vetoed_overlapping_required, PlateZone)

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
CACHE_DIR = ROOT / ".cache" / "detections"


def recall_line(tag, segments, truth, duration):
    required = [e for e in truth["events"] if e["required"]]
    borderline = [e for e in truth["events"] if not e["required"]]
    req_hit = [e for e in required if segment_covers(segments, e["window"])]
    bord_hit = [e for e in borderline if segment_covers(segments, e["window"])]
    flagged = total_duration(segments)
    print(f"    {tag:<12} recall {len(req_hit)}/{len(required)}"
          f" | borderline {len(bord_hit)}/{len(borderline)}"
          f" | flagged {flagged:.0f}s ({100 * flagged / duration:.0f}%)")
    missed = [e for e in required if not segment_covers(segments, e["window"])]
    for e in missed:
        print(f"      MISSED {e['id']} {e['type']} window={e['window']}")
    return len(req_hit), len(required), missed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-dir", default=str(ROOT / "reference_clips"))
    ap.add_argument("--motion-only", action="store_true",
                    help="skip the fused pipeline (Stage 1 baseline only)")
    args = ap.parse_args()
    clips_dir = Path(args.clips_dir)

    calib_file = clips_dir / "calibration.json"
    zone = None
    if calib_file.exists():
        c = json.loads(calib_file.read_text())
        zone = PlateZone(center_xy=tuple(c["plate_xy"]),
                         radius_px=c["zone_radius_px"])

    seg_cfg = SegmentConfig()
    failures = []

    for tf in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        truth = json.loads(tf.read_text())
        clip_path = clips_dir / truth["clip"]
        if not clip_path.exists():
            print(f"[skip] {truth['clip']}: not found in {clips_dir}")
            failures.append(f"{truth['clip']}: clip missing")
            continue

        print(f"  {truth['clip']}:")
        motion = compute_motion(str(clip_path))
        base_segs = scores_to_segments(motion.times, motion.scores, seg_cfg)
        base_rec, base_tot, _ = recall_line("motion-only", base_segs, truth,
                                            motion.duration)
        if args.motion_only:
            if base_rec < base_tot:
                failures.append(f"{truth['clip']}: motion-only missed events")
            continue

        from pipeline.atbat import AtBatConfig, atbat_start_times
        from pipeline.calibration import resolve_base_zones
        from pipeline.detection import DetectionConfig, detect_persons
        from pipeline.fusion import compute_occupancy, compute_zone_velocity
        from pipeline.refine import RefineConfig, refine_segments
        from pipeline.settle import SettleConfig
        det = detect_persons(str(clip_path), DetectionConfig(),
                             cache_dir=str(CACHE_DIR))
        fused = fuse(motion.times, motion.scores, motion.grids,
                     motion.frame_size, motion.analysis_size, motion.border_px,
                     det.times, det.boxes, zone)
        # mirrors pipeline.run.process_video's enter-side scale boost --
        # kept in sync deliberately so this script measures the SAME
        # pipeline that actually ships, not a stale reimplementation.
        boost = 1.0
        if zone is not None:
            boost = scale_boost_factor(det.times, det.boxes, motion.frame_size,
                                       zone, seg_cfg.reference_plate_box_width_px)
        enter_scores = motion.scores * (boost ** 2)
        raw = scores_to_segments(motion.times, enter_scores, seg_cfg,
                                 sustain_scores=motion.scores)
        raw_kept, vetoed = apply_veto(raw, fused)
        if boost > 1.0:
            print(f"    scale boost: {boost:.3f}x (linear), "
                  f"{boost**2:.3f}x applied to enter-side score")
        sm_motion = smooth_scores(motion.times, motion.scores,
                                  seg_cfg.smooth_window_s)
        # one SettleConfig shared explicitly between both consumers
        settle_cfg = SettleConfig()
        if zone is not None:
            occ = compute_occupancy(det.times, det.boxes, zone, 0.30)
            fires = atbat_start_times(det.times, occ, motion.times, sm_motion,
                                      AtBatConfig(settle=settle_cfg))
        else:
            occ = [False] * len(det.times)
            fires = []
        # Stage 11: base zones, same per-file-override-then-shared lookup
        # as the plate zone. A clip with no "bases" key yields {}, so
        # refine_segments behaves exactly as before Stage 11.
        base_zones = resolve_base_zones(clip_path, calib_dir=clips_dir)
        zone_velocities = {name: (det.times, compute_zone_velocity(det.times, det.boxes, z))
                          for name, z in base_zones.items()}
        # Stage 11 tier 1: same mechanism, plate zone -- see pipeline/run.py.
        if zone is not None:
            zone_velocities["plate"] = (det.times, compute_zone_velocity(det.times, det.boxes, zone))
        kept = refine_segments(raw_kept, motion.times, sm_motion, det.times,
                               occ, fires, motion.duration,
                               RefineConfig(settle=settle_cfg), zone_velocities)
        fus_rec, fus_tot, fus_missed = recall_line("refined", kept, truth,
                                                   motion.duration)
        print(f"    at-bat fires: {[round(f, 1) for f in fires]}")
        for a, b in kept:
            print(f"       seg {a:7.1f} - {b:7.1f}  ({b - a:5.1f}s)")
        for a, b in vetoed:
            print(f"       VETOED {a:7.1f} - {b:7.1f}  (no person near motion)")

        # pinned at-bat expectations (frame-verified fire / no-fire windows)
        exp = truth.get("atbat_expectations", {})
        for lo, hi in exp.get("fire_within", []):
            if not any(lo <= f <= hi for f in fires):
                failures.append(f"{truth['clip']}: expected at-bat fire in "
                                f"[{lo},{hi}], fires={fires}")
        for lo, hi in exp.get("no_fire_within", []):
            bad = [f for f in fires if lo <= f <= hi]
            if bad:
                failures.append(f"{truth['clip']}: forbidden at-bat fire(s) "
                                f"{bad} inside [{lo},{hi}]")

        required = [e for e in truth["events"] if e["required"]]

        # failure 1: fused missed a required event
        if fus_rec < fus_tot:
            failures.append(f"{truth['clip']}: fused missed "
                            f"{[e['id'] for e in fus_missed]}")
        # failure 2: fused worse than baseline
        if fus_rec < base_rec:
            failures.append(f"{truth['clip']}: fused recall {fus_rec} < "
                            f"motion-only {base_rec} (should be impossible)")
        # failure 3: veto safety net
        bad_vetoes = vetoed_overlapping_required(vetoed, required)
        for (a, b), eid in bad_vetoes:
            failures.append(f"{truth['clip']}: vetoed segment "
                            f"({a:.1f},{b:.1f}) overlaps required {eid}")
        # failure 4: contiguous kept-segment coverage of flagged
        # defensive-play windows (see docstring for why this is
        # segment-level, not raw-score-level)
        sm = sm_motion
        for e in truth["events"]:
            if not e.get("check_continuity"):
                continue
            ws, we = e["window"]
            covering = [s for s in kept if s[0] <= we and s[1] >= ws]
            # window is contiguously covered iff one kept segment spans it
            ok = any(a <= ws and b >= we for a, b in covering)
            idx = (fused.times >= ws) & (fused.times <= we)
            lo = float(sm[idx].min()) if idx.any() else 0.0
            print(f"    continuity {e['id']} window={e['window']}: "
                  f"{'contiguously covered' if ok else 'COVERAGE GAP'}"
                  f" by {[(round(float(a),1), round(float(b),1)) for a, b in covering]}"
                  f" | min smoothed score in window {lo:.4f}"
                  f" (exit {seg_cfg.exit_thresh:.4f})")
            occ = fused.occupied[idx]
            print(f"      plate occupied {int(occ.sum())}/{int(idx.sum())} "
                  f"samples in window")
            if not ok:
                failures.append(
                    f"{truth['clip']}: {e['id']} window not contiguously "
                    f"covered by kept segments")

    print()
    if failures:
        for f in failures:
            print(f"FAIL: {f}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
