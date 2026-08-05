"""The core single-file detection pipeline: motion -> veto -> play
extension -> padding -> real hard cuts of quiet stretches (see
pipeline/segments.py's HardCutConfig). Shared by scripts/detect.py
(one file) and
scripts/detect_multi.py (many files) so there is exactly one
implementation of "process one video" — a multi-file run is this
function called once per file, nothing more. That's also what makes the
Stage 3/4 boundary decision (extension and at-bat state never cross a
file boundary) structurally true rather than a rule that has to be
remembered: each call gets its own fresh motion/detection/occupancy
computation and starts its at-bat detector unarmed-at-clip-start, with
no channel for state to leak from one file into the next.
"""

import os
from pathlib import Path

from pipeline.atbat import AtBatConfig, atbat_start_times
from pipeline.calibration import resolve_base_zones
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import (FusionConfig, apply_veto, compute_occupancy,
                             compute_zone_velocity, fuse, occupancy_near_times,
                             scale_boost_factor)
from pipeline.motion import compute_motion
from pipeline.refine import RefineConfig, refine_segments
from pipeline.segments import (SegmentConfig, apply_hard_cuts, scores_to_segments,
                               smooth_scores)
from pipeline.settle import SettleConfig

ROOT = Path(__file__).resolve().parent.parent
# Overridable via FMH_DETECTION_CACHE_DIR so a deployment can point this
# at a mounted persistent volume instead of the repo-relative default —
# same env-var-with-a-repo-relative-fallback convention backend/app.py's
# FMH_TRAINING_DATA_DIR already established. A fresh container with no
# persistent volume still works fine (this is a performance cache, not
# required data), it just re-detects instead of hitting a warm cache.
DEFAULT_CACHE_DIR = Path(os.environ.get("FMH_DETECTION_CACHE_DIR", str(ROOT / ".cache" / "detections")))


def process_video(video: str, zone, motion_only: bool = False,
                  cache_dir=None, warn=None, on_stage=None,
                  training_data_dir=None, training_data_source_info=None,
                  review_cfg=None):
    """Run the full pipeline on one video. `zone` is a PlateZone or None
    (already resolved by the caller — this function does no calibration
    lookup of its own, keeping single- and multi-file callers consistent).
    `warn`, if given, is called with a message when zone is None and
    motion_only is False (plate-occupancy signals disabled). `on_stage`,
    if given, is called with a human-readable stage name right before
    that stage of work starts (added for the backend's progress
    reporting — the CLI scripts don't pass one, so their behavior is
    unchanged).

    `training_data_dir`, if given, opts this run into the Tier 1 review
    queue (pipeline.review.generate_review_candidates): a handful of the
    run's own most-borderline hard-cut and segment-boundary decisions get
    a self-contained clip + JSON record written under
    training_data_dir/reviews/ for later human labeling. Defaults to
    None (off) deliberately — scripts/regression.py and any test-shaped
    run must never pass this, since running the same 9 reference clips
    repeatedly would flood the label store with redundant records; see
    backend/app.py for how a real deployment opts in. `training_data_source_info`,
    if given, is merged into each written record's `source` field
    (e.g. a real batch id) -- ignored unless `training_data_dir` is also set.
    `review_cfg`, if given, overrides the default `ReviewConfig()` (5
    candidates/video, see pipeline.review) -- ignored unless
    `training_data_dir` is also set. The real backend and every CLI
    script leave this None (unchanged default behavior);
    scripts/mine_review_candidates.py is the one caller that passes a
    real override, deliberately bypassing the per-run cap to build up a
    real labeling batch.

    Returns (final_segments, vetoed, duration, motion_result,
    hard_cut_windows). hard_cut_windows is always [] for the
    motion-only baseline (no real cutting happens there, it's a raw
    diagnostic).
    """
    cache_dir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
    if on_stage:
        on_stage("analyzing motion")
    motion = compute_motion(video)
    if motion_only:
        segs = scores_to_segments(motion.times, motion.scores, SegmentConfig())
        return segs, [], motion.duration, motion, []

    if zone is None and warn is not None:
        warn(f"no calibration for {video}; plate-occupancy signals disabled")

    if on_stage:
        on_stage("running player detection")

    def _detection_progress(t, duration):
        # detect_persons runs one real model inference call per sampled
        # frame (~1/sec) and is, in practice, the overwhelming majority
        # of a real run's wall-clock time (measured: ~80s of an ~87s
        # clip_300 run) -- without this, on_stage only fires once at the
        # start of this stage and once at the end, so a poller watching
        # job["stage"] sees the exact same string for the entire run.
        on_stage(f"running player detection ({t:.0f}s/{duration:.0f}s)")

    det = detect_persons(video, DetectionConfig(), cache_dir=str(cache_dir),
                         progress_cb=_detection_progress if on_stage else None)
    if on_stage:
        on_stage("extending and padding segments")
    fused = fuse(motion.times, motion.scores, motion.grids,
                 motion.frame_size, motion.analysis_size, motion.border_px,
                 det.times, det.boxes, zone)
    # Enter-side scale boost (camera-distance investigation): a batch
    # filmed more distantly than the reference clips reads a smaller
    # near-plate box, so its raw motion score is boosted before the
    # ENTER comparison only (never the exit/sustain side -- raw
    # motion.scores is passed separately as sustain_scores below,
    # completely unaffected). See SegmentConfig.reference_plate_box_width_px
    # for the full derivation and safety proof; scale_boost_factor()
    # itself returns exactly 1.0 (no-op) when zone is None or too few
    # reliable near-plate detections exist to trust a reading.
    seg_cfg = SegmentConfig()
    boost = 1.0
    if zone is not None:
        boost = scale_boost_factor(det.times, det.boxes, motion.frame_size,
                                   zone, seg_cfg.reference_plate_box_width_px)
    enter_scores = motion.scores * (boost ** 2)
    # Enter-side occupancy debounce (see SegmentConfig.enter_debounce_s):
    # a candidate enter crossing with no real occupancy evidence within
    # enter_occupancy_window_s must sustain briefly before being trusted,
    # rather than opening on its first sample. A debounce, not a veto --
    # it only ever delays the DECISION to trust a crossing, backdating to
    # the crossing's own true first instant once confirmed, so no real
    # content is ever lost (see scripts/enter_occupancy_gate_investigation.py
    # for the real 9-clip zero-regression validation and the real
    # full_game.mkv effectiveness numbers this shipped against). None
    # whenever zone is None, identical to every prior behavior (no
    # occupancy signal available to debounce against).
    occupancy_near = None
    if zone is not None:
        occ_det_enter = compute_occupancy(det.times, det.boxes, zone,
                                          FusionConfig().stationary_v)
        occupancy_near = occupancy_near_times(
            motion.times, det.times, occ_det_enter, seg_cfg.enter_occupancy_window_s)
    # motion alone owns segment open AND raw exit (Stage 3 replaced the
    # Stage 2 score-sustain with the explicit play-extension below)
    raw = scores_to_segments(motion.times, enter_scores, seg_cfg,
                             sustain_scores=motion.scores,
                             occupancy_near=occupancy_near)
    kept, vetoed = apply_veto(raw, fused)

    sm = smooth_scores(motion.times, motion.scores, seg_cfg.smooth_window_s)
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
    # Stage 11 tier 1: base zones are resolved from the same calibration
    # file the plate zone comes from (per-file override, else shared) --
    # purely additive, a file with no "bases" key yields {} and
    # refine_segments behaves exactly as before (see pipeline/refine.py's
    # ZONE-LOCAL CLOSE docstring section).
    base_zones = resolve_base_zones(video)
    zone_velocities = {name: (det.times, compute_zone_velocity(det.times, det.boxes, z))
                       for name, z in base_zones.items()}
    # Stage 11 tier 1 (walk-up gap, extension-layer piece): the SAME
    # mechanism, applied to the plate zone instead of a base -- an
    # incoming batter arriving then settling into the box is the
    # identical spike-then-quiet shape a fielder receiving a throw has,
    # just for "the next at-bat has begun" instead of "this play is
    # over". Reuses the same generic zone_velocities dict
    # extend_segments() already consumes; no new mechanism, no new
    # safety property beyond what's already validated. (Tier 2 -- the
    # bigger raw-segment-level fix -- is deliberately not started here;
    # see pipeline/refine.py's WALK-UP GAP note.)
    if zone is not None:
        zone_velocities["plate"] = (det.times, compute_zone_velocity(det.times, det.boxes, zone))
    final = refine_segments(kept, motion.times, sm, det.times, occ, fires,
                            motion.duration, RefineConfig(settle=settle_cfg),
                            zone_velocities, motion_scores=motion.scores)
    # Real, destructive hard cuts of quiet stretches inside otherwise-kept
    # segments (see pipeline/segments.py's HardCutConfig). Deliberately
    # the LAST step, consuming refine_segments' own final boundaries, so
    # a hard cut can never fight extension/padding over where a segment
    # really ends. No protected_windows here: real user uploads have no
    # ground truth to protect anything with, so this is unconditional --
    # accepted and recoverable via the Edit Log, not a gap to design
    # around (see README's hard-cut writeup for the reasoning and the
    # real numbers this shipped against).
    final, hard_cut_windows = apply_hard_cuts(final, motion.times, motion.scores)

    if training_data_dir is not None:
        from pipeline.review import generate_review_candidates
        generate_review_candidates(
            final_segments=final, hard_cut_windows=hard_cut_windows,
            motion_times=motion.times, motion_scores=motion.scores,
            enter_scores=enter_scores, video_path=video,
            source_file=Path(video).name, training_data_dir=training_data_dir,
            duration=motion.duration, vetoed_segments=vetoed,
            det_times=det.times, det_boxes=det.boxes, zone=zone,
            seg_cfg=seg_cfg, warn=warn,
            extra_source_info=training_data_source_info,
            review_cfg=review_cfg)

    return final, vetoed, motion.duration, motion, hard_cut_windows
