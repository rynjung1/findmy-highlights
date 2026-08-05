"""INVESTIGATION ONLY -- not wired into any real decision.

The enter-side occupancy debounce (now shipped, see
scripts/enter_occupancy_gate_investigation.py and pipeline.run.
process_video) fixes 2 of 11 real enter-type boundary_crossing
disagreements against full_game.mkv's real labeled records, by design:
it only ever helps a BRIEF no-occupancy gap around a candidate crossing.
The remaining disagreements were characterized (not yet verified
directly against real data) as "sustained ambient motion, no batter
visible" -- a structurally different failure mode, motion/occupancy
signals have now failed on it three separate ways this session (the
closed ambient-motion-discount investigation, the dead/unused fuse()
combined score, and now this debounce by design).

This script:
  1. Loads the 16 real labeled enter-type boundary_crossing records
     (training_data/reviews/bc_*.json, pipeline_decision == "enter",
     label is not None) -- the exact real dataset the debounce was
     checked against.
  2. Re-derives each record's post-ship pipeline decision (does a real
     KEPT segment, under the now-shipped default config, cover the
     record's timestamp) directly from the real pipeline, not memory.
  3. For records still disagreeing with their real label, checks
     real occupancy over a WIDE window (+/- 10s, well beyond
     enter_occupancy_window_s=2.5s) to confirm "sustained ambient
     motion, no batter visible" rather than merely "occupancy_near
     window was too narrow" -- a different, real check from what the
     debounce itself already tests.
  4. Reports each confirmed record's stored X-CLIP p_swinging (already
     computed at candidate-mining time, in
     features_at_label_time.xclip, same window_s/prompt config
     documented in pipeline/xclip.py) -- a genuinely different,
     semantic (not motion-based) signal, to see whether it can
     separate real_action from downtime where motion/occupancy
     structurally cannot.

Usage:
    python scripts/sustained_ambient_xclip_check.py
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
from pipeline.segments import SegmentConfig, scores_to_segments, smooth_scores
from pipeline.settle import SettleConfig

ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = ROOT / "training_data" / "reviews"
CLIPS_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"
VIDEO = CLIPS_DIR / "full_game.mkv"

WIDE_WINDOW_S = 10.0  # well beyond enter_occupancy_window_s (2.5s)


def load_enter_labeled_records():
    records = []
    for f in sorted(REVIEWS_DIR.glob("bc_*.json")):
        d = json.loads(f.read_text())
        if d.get("label") is not None and d.get("pipeline_decision") == "enter":
            records.append(d)
    return records


def main():
    calib_file = CLIPS_DIR / "calibration.json"
    zone = None
    if calib_file.exists():
        c = json.loads(calib_file.read_text())
        zone = PlateZone(center_xy=tuple(c["plate_xy"]), radius_px=c["zone_radius_px"])

    print("Running the real shipped pipeline on full_game.mkv...")
    motion = compute_motion(str(VIDEO))
    det = detect_persons(str(VIDEO), DetectionConfig(), cache_dir=str(CACHE_DIR))
    seg_cfg = SegmentConfig()

    fused = fuse(motion.times, motion.scores, motion.grids,
                motion.frame_size, motion.analysis_size, motion.border_px,
                det.times, det.boxes, zone)
    boost = 1.0
    if zone is not None:
        boost = scale_boost_factor(det.times, det.boxes, motion.frame_size,
                                   zone, seg_cfg.reference_plate_box_width_px)
    enter_scores = motion.scores * (boost ** 2)

    occupancy_near = None
    occ_det = None
    if zone is not None:
        occ_det = compute_occupancy(det.times, det.boxes, zone, FusionConfig().stationary_v)
        occupancy_near = occupancy_near_times(
            motion.times, det.times, occ_det, seg_cfg.enter_occupancy_window_s)

    raw = scores_to_segments(motion.times, enter_scores, seg_cfg,
                             sustain_scores=motion.scores,
                             occupancy_near=occupancy_near)
    raw_kept, vetoed = apply_veto(raw, fused)

    sm = smooth_scores(motion.times, motion.scores, seg_cfg.smooth_window_s)
    settle_cfg = SettleConfig()
    if zone is not None:
        occ = compute_occupancy(det.times, det.boxes, zone, 0.30)
        fires = atbat_start_times(det.times, occ, motion.times, sm, AtBatConfig(settle=settle_cfg))
    else:
        occ = [False] * len(det.times)
        fires = []
    base_zones = resolve_base_zones(VIDEO, calib_dir=CLIPS_DIR)
    zone_velocities = {name: (det.times, compute_zone_velocity(det.times, det.boxes, z))
                       for name, z in base_zones.items()}
    if zone is not None:
        zone_velocities["plate"] = (det.times, compute_zone_velocity(det.times, det.boxes, zone))
    final = refine_segments(raw_kept, motion.times, sm, det.times, occ, fires,
                            motion.duration, RefineConfig(settle=settle_cfg),
                            zone_velocities, motion_scores=motion.scores)

    def covered(t):
        return any(a <= t <= b for a, b in final)

    records = load_enter_labeled_records()
    print(f"\n{len(records)} real labeled enter-type boundary_crossing records\n")

    disagreements = []
    for d in records:
        t = d["window"]["start_s"]
        label = d["label"]
        is_open = covered(t)
        should_be_open = (label == "real_action")
        agree = is_open == should_be_open
        status = "AGREE" if agree else "DISAGREE"
        print(f"{d['id']}: t={t:8.2f}s label={label:<12} kept_open={is_open!s:<5}  {status}")
        if not agree:
            disagreements.append((d, t))

    print(f"\n{len(disagreements)} real disagreements under the now-shipped pipeline\n")

    if zone is None or occ_det is None:
        print("no zone/occupancy available; cannot characterize sustained-ambient-ness")
        return

    print(f"Characterizing each disagreement: occupancy fraction in a "
         f"+/-{WIDE_WINDOW_S:.0f}s window (well beyond the "
         f"{seg_cfg.enter_occupancy_window_s:.1f}s debounce window)\n")

    det_times_arr = np.asarray(det.times, dtype=float)
    confirmed_sustained = []
    for d, t in disagreements:
        idx = (det_times_arr >= t - WIDE_WINDOW_S) & (det_times_arr <= t + WIDE_WINDOW_S)
        n = int(idx.sum())
        occ_frac = float(occ_det[idx].mean()) if n else 0.0
        xc = d["features_at_label_time"].get("xclip")
        p_swing = xc["p_swinging"] if xc else None
        sustained = occ_frac < 0.05  # near-zero occupancy across the whole wide window
        tag = "SUSTAINED, no batter visible" if sustained else "occupancy present somewhere in wide window"
        print(f"{d['id']}: t={t:8.2f}s label={d['label']:<12} "
             f"occ_frac(+/-{WIDE_WINDOW_S:.0f}s)={occ_frac:.3f} n={n:<3} "
             f"p_swinging={p_swing}  -- {tag}")
        if sustained:
            confirmed_sustained.append((d, t, p_swing))

    print(f"\n{len(confirmed_sustained)} confirmed sustained-ambient-motion, "
         f"no-batter-visible disagreements\n")

    print("X-CLIP p_swinging on the confirmed set:")
    real_action_p = [p for d, t, p in confirmed_sustained if d["label"] == "real_action" and p is not None]
    downtime_p = [p for d, t, p in confirmed_sustained if d["label"] == "downtime" and p is not None]
    print(f"  real_action (n={len(real_action_p)}): {sorted(real_action_p)}")
    print(f"  downtime    (n={len(downtime_p)}): {sorted(downtime_p)}")
    if real_action_p and downtime_p:
        print(f"  mean real_action p_swinging: {np.mean(real_action_p):.3f}")
        print(f"  mean downtime    p_swinging: {np.mean(downtime_p):.3f}")


if __name__ == "__main__":
    main()
