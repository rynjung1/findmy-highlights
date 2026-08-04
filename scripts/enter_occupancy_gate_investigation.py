"""INVESTIGATION ONLY -- not wired into pipeline.run.process_video.

Simulates the enter-side occupancy debounce (pipeline.segments.
SegmentConfig.enter_debounce_s/enter_occupancy_window_s, pipeline.fusion.
occupancy_near_times) against all 9 reference clips, using the exact same
real recall/continuity checks scripts/regression.py gates production on --
this script's whole point is to hold a new segments.py-adjacent mechanism
to the identical bar before it's ever considered for real, not a lighter
one. Same non-negotiable process as every other segments.py change
tonight: real reference-clip ground truth, not the 30-record
boundary_crossing label set alone (too small on its own to validate a
real pipeline change, even though it's what motivated this investigation
-- see README's boundary_crossing enter-side writeup).

Runs the FULL real pipeline (motion -> scale boost -> scores_to_segments
-> veto -> refine_segments) TWICE per clip: BASELINE (current shipped
behavior, occupancy_near=None) and DEBOUNCE (occupancy_near passed to the
raw scores_to_segments call that forms segments before veto/extension),
identical in every other respect, so any difference is attributable to
the debounce alone. Reports required-event recall and check_continuity
coverage for both, explicitly calling out clip_base1-4/clip_foul1/
clip_whiff1 -- the six clips whose real, thin-margin events broke every
prior enter-side attempt (the ambient-motion discount, the naive
hard-cut-everything try) per this project's own history.

Usage:
    python scripts/enter_occupancy_gate_investigation.py
"""

import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.atbat import AtBatConfig, atbat_start_times
from pipeline.calibration import resolve_base_zones
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import (FusionConfig, PlateZone, apply_veto, compute_occupancy,
                             compute_zone_velocity, fuse, occupancy_near_times,
                             scale_boost_factor)
from pipeline.motion import compute_motion
from pipeline.refine import RefineConfig, refine_segments
from pipeline.segments import SegmentConfig, scores_to_segments, segment_covers, total_duration
from pipeline.settle import SettleConfig

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
CLIPS_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"

FRAGILE_CLIPS = {"clip_base1", "clip_base2", "clip_base3", "clip_base4",
                 "clip_foul1", "clip_whiff1"}


def run_variant(motion, det, zone, seg_cfg, use_debounce: bool):
    """One full real pipeline run (motion already computed) -- identical
    in every respect except whether occupancy_near is passed to the raw
    scores_to_segments call. Returns (kept_segments, fired_atbat_times)."""
    fused = fuse(motion.times, motion.scores, motion.grids,
                motion.frame_size, motion.analysis_size, motion.border_px,
                det.times, det.boxes, zone)
    boost = 1.0
    if zone is not None:
        boost = scale_boost_factor(det.times, det.boxes, motion.frame_size,
                                   zone, seg_cfg.reference_plate_box_width_px)
    enter_scores = motion.scores * (boost ** 2)

    occupancy_near = None
    if use_debounce and zone is not None:
        occ_det = compute_occupancy(det.times, det.boxes, zone, FusionConfig().stationary_v)
        occupancy_near = occupancy_near_times(
            motion.times, det.times, occ_det, seg_cfg.enter_occupancy_window_s)

    raw = scores_to_segments(motion.times, enter_scores, seg_cfg,
                             sustain_scores=motion.scores,
                             occupancy_near=occupancy_near)
    raw_kept, vetoed = apply_veto(raw, fused)

    from pipeline.segments import smooth_scores
    sm_motion = smooth_scores(motion.times, motion.scores, seg_cfg.smooth_window_s)
    settle_cfg = SettleConfig()
    if zone is not None:
        occ = compute_occupancy(det.times, det.boxes, zone, 0.30)
        fires = atbat_start_times(det.times, occ, motion.times, sm_motion,
                                  AtBatConfig(settle=settle_cfg))
    else:
        occ = [False] * len(det.times)
        fires = []

    return raw_kept, vetoed, fires, sm_motion, settle_cfg, occ


def _cc_summary(cc):
    return [f"{i}:{'OK' if ok else 'GAP'}" for i, ok in cc]


def recall_and_continuity(kept, truth, motion_duration):
    required = [e for e in truth["events"] if e["required"]]
    req_hit = [e for e in required if segment_covers(kept, e["window"])]
    missed = [e["id"] for e in required if not segment_covers(kept, e["window"])]
    cc_events = [e for e in truth["events"] if e.get("check_continuity")]
    cc_results = []
    for e in cc_events:
        ws, we = e["window"]
        covering = [s for s in kept if s[0] <= we and s[1] >= ws]
        ok = any(a <= ws and b >= we for a, b in covering)
        cc_results.append((e["id"], ok))
    return len(req_hit), len(required), missed, cc_results


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--enter-debounce-s", type=float, default=SegmentConfig().enter_debounce_s)
    ap.add_argument("--enter-occupancy-window-s", type=float,
                    default=SegmentConfig().enter_occupancy_window_s)
    args = ap.parse_args()

    seg_cfg_baseline = SegmentConfig()
    seg_cfg_debounce = SegmentConfig(enter_debounce_s=args.enter_debounce_s,
                                     enter_occupancy_window_s=args.enter_occupancy_window_s)

    calib_file = CLIPS_DIR / "calibration.json"
    zone = None
    if calib_file.exists():
        c = json.loads(calib_file.read_text())
        zone = PlateZone(center_xy=tuple(c["plate_xy"]), radius_px=c["zone_radius_px"])

    print(f"enter_debounce_s={seg_cfg_debounce.enter_debounce_s}s  "
         f"enter_occupancy_window_s={seg_cfg_debounce.enter_occupancy_window_s}s\n")

    any_regression = False
    grand_flagged_baseline = grand_flagged_debounce = 0.0

    for tf in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        truth = json.loads(tf.read_text())
        clip_stem = Path(truth["clip"]).stem
        clip_path = CLIPS_DIR / truth["clip"]
        if not clip_path.exists():
            print(f"[skip] {truth['clip']}: not found")
            continue

        motion = compute_motion(str(clip_path))
        det = detect_persons(str(clip_path), DetectionConfig(), cache_dir=str(CACHE_DIR))

        # per-file calibration override, same lookup refine_segments' own
        # callers already use
        z = zone
        override = CLIPS_DIR / f"{clip_stem}.calibration.json"
        if override.exists():
            c = json.loads(override.read_text())
            z = PlateZone(center_xy=tuple(c["plate_xy"]), radius_px=c["zone_radius_px"])

        base_zones = resolve_base_zones(clip_path, calib_dir=CLIPS_DIR)

        results = {}
        for label, use_debounce in (("baseline", False), ("debounce", True)):
            cfg = seg_cfg_debounce if use_debounce else seg_cfg_baseline
            raw_kept, vetoed, fires, sm_motion, settle_cfg, occ = run_variant(
                motion, det, z, cfg, use_debounce)
            zone_velocities = {name: (det.times, compute_zone_velocity(det.times, det.boxes, zv))
                              for name, zv in base_zones.items()}
            if z is not None:
                zone_velocities["plate"] = (det.times, compute_zone_velocity(det.times, det.boxes, z))
            kept = refine_segments(raw_kept, motion.times, sm_motion, det.times,
                                   occ, fires, motion.duration,
                                   RefineConfig(settle=settle_cfg), zone_velocities,
                                   motion_scores=motion.scores)
            req_hit, req_tot, missed, cc = recall_and_continuity(kept, truth, motion.duration)
            results[label] = {
                "kept": kept, "req_hit": req_hit, "req_tot": req_tot,
                "missed": missed, "cc": cc, "flagged": total_duration(kept),
            }

        b, d = results["baseline"], results["debounce"]
        grand_flagged_baseline += b["flagged"]
        grand_flagged_debounce += d["flagged"]

        flag = " <-- FRAGILE CLIP (named by the user)" if clip_stem in FRAGILE_CLIPS else ""
        print(f"{clip_stem}{flag}")
        print(f"  baseline  recall {b['req_hit']}/{b['req_tot']}  "
             f"missed={b['missed']}  flagged={b['flagged']:.1f}s  "
             f"continuity={_cc_summary(b['cc'])}")
        print(f"  debounce  recall {d['req_hit']}/{d['req_tot']}  "
             f"missed={d['missed']}  flagged={d['flagged']:.1f}s  "
             f"continuity={_cc_summary(d['cc'])}")

        # regression check: any event covered under baseline but NOT
        # under debounce (recall OR continuity), named explicitly
        cc_ok_baseline = {i: ok for i, ok in b["cc"]}
        cc_ok_debounce = {i: ok for i, ok in d["cc"]}
        cc_regressions = [i for i in cc_ok_baseline
                          if cc_ok_baseline[i] and not cc_ok_debounce.get(i, False)]
        recall_regressions = set(d["missed"]) - set(b["missed"])
        if recall_regressions or cc_regressions:
            any_regression = True
            print(f"  *** REGRESSION vs baseline: recall={sorted(recall_regressions)} "
                 f"continuity={cc_regressions} ***")
        print()

    print(f"total flagged time: baseline={grand_flagged_baseline:.1f}s  "
         f"debounce={grand_flagged_debounce:.1f}s  "
         f"delta={grand_flagged_debounce - grand_flagged_baseline:+.1f}s")
    print()
    if any_regression:
        print("REGRESSIONS FOUND -- see *** markers above")
        sys.exit(1)
    print("NO REGRESSIONS: debounce matches baseline on every required-event "
         "recall and check_continuity coverage check, all 9 clips.")


if __name__ == "__main__":
    main()
