"""The core single-file detection pipeline: motion -> veto -> play
extension -> padding. Shared by scripts/detect.py (one file) and
scripts/detect_multi.py (many files) so there is exactly one
implementation of "process one video" — a multi-file run is this
function called once per file, nothing more. That's also what makes the
Stage 3/4 boundary decision (extension and at-bat state never cross a
file boundary) structurally true rather than a rule that has to be
remembered: each call gets its own fresh motion/detection/occupancy
computation and starts its at-bat detector unarmed-at-clip-start, with
no channel for state to leak from one file into the next.
"""

from pathlib import Path

from pipeline.atbat import AtBatConfig, atbat_start_times
from pipeline.calibration import resolve_base_zones
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import apply_veto, compute_occupancy, compute_zone_velocity, fuse
from pipeline.motion import compute_motion
from pipeline.refine import RefineConfig, refine_segments
from pipeline.segments import SegmentConfig, scores_to_segments, smooth_scores
from pipeline.settle import SettleConfig

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CACHE_DIR = ROOT / ".cache" / "detections"


def process_video(video: str, zone, motion_only: bool = False,
                  cache_dir=None, warn=None, on_stage=None):
    """Run the full pipeline on one video. `zone` is a PlateZone or None
    (already resolved by the caller — this function does no calibration
    lookup of its own, keeping single- and multi-file callers consistent).
    `warn`, if given, is called with a message when zone is None and
    motion_only is False (plate-occupancy signals disabled). `on_stage`,
    if given, is called with a human-readable stage name right before
    that stage of work starts (added for the backend's progress
    reporting — the CLI scripts don't pass one, so their behavior is
    unchanged).

    Returns (final_segments, vetoed, duration, motion_result).
    """
    cache_dir = cache_dir if cache_dir is not None else DEFAULT_CACHE_DIR
    if on_stage:
        on_stage("analyzing motion")
    motion = compute_motion(video)
    if motion_only:
        segs = scores_to_segments(motion.times, motion.scores, SegmentConfig())
        return segs, [], motion.duration, motion

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
    # motion alone owns segment open AND raw exit (Stage 3 replaced the
    # Stage 2 score-sustain with the explicit play-extension below)
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
                            zone_velocities)
    return final, vetoed, motion.duration, motion
