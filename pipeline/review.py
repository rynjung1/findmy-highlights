"""Tier 1 review/training queue: generates self-contained candidate clips
plus JSON records for the pipeline's own borderline decisions, so a human
can label each one Downtime vs. Real action after the fact. Purely
additive and opt-in -- pipeline.run.process_video only calls into this
module when the caller passes a real `training_data_dir`, which defaults
to None (off), specifically so scripts/regression.py (many repeated runs
against the same 9 reference clips) and any test-shaped run never
pollutes the label store with redundant records. See backend/app.py for
how a real deployment opts in.

Three candidate types (Tier 2/3 usage of the resulting labels --
threshold calibration, a learned classifier -- deliberately not built
here, see README):

  - hard-cut dips: one candidate per real hard-cut window this run
    actually shipped (pipeline.segments.HardCutConfig/apply_hard_cuts).
    margin combines how far the window's own peak motion score sits
    below quiet_thresh (score_margin -- can go negative, since the
    shipped window is buffered/merged and can include content whose
    peak legitimately exceeds quiet_thresh) and how far the window's own
    duration sits above the minimum raw dip length (duration_margin).
    The SMALLER of the two governs the overall margin -- the same
    "weakest margin wins" pattern this codebase already uses elsewhere
    (e.g. dynamic padding's own per-segment floor/buffer logic).
  - segment boundary crossings: one candidate per raw ENTER/EXIT
    hysteresis crossing from the pipeline's pre-extension segmentation
    (pipeline.segments.find_boundary_crossings). margin = |score -
    threshold| at that exact sample.
  - veto-boundary crossings: one candidate per segment
    pipeline.fusion.apply_veto discarded outright (no person detected
    near its motion for its whole duration). apply_veto is an all-or-
    nothing decision, not a threshold crossing, so there's no natural
    per-sample margin the way enter/exit have one -- instead this reuses
    enter_thresh, the same threshold that governs whether raw motion
    counts as "action" at all: margin = enter_thresh - peak_motion_in_window,
    same sign convention as hard-cut dips' own score_margin (a vetoed
    window whose peak motion sits FAR above enter_thresh despite the
    veto is the riskiest case -- real motion the pipeline discarded
    anyway -- so it gets a very negative margin and sorts first).

Every candidate, regardless of type, also gets real pose (wrist
displacement, pipeline.pose), audio (onset rise-time, pipeline.audio),
and xclip (zero-shot "swinging" probability, pipeline.xclip) features
attached to features_at_label_time when the inputs needed for them are
available (a real zone for pose; audio and xclip always attempted).
This is instrumentation, not a cutting signal -- see the README's Task 2
pose+audio validation writeup and the later transfer-learning/zero-shot
investigation writeups for whether any of the three earned real trust
before anything downstream ever reads these fields for a real decision.
xclip specifically cleared real statistical significance (AUC 0.690,
p=0.012) but was held back from cutting decisions over a real,
measured prompt-sensitivity risk on contact/hit-type events -- see
pipeline/xclip.py's own docstring for the full reasoning.

Selection: lowest margin first (most borderline, most useful to label),
capped at ReviewConfig.max_candidates_per_video, plus (with probability
control_sample_rate per video processed) one random "control" sample --
a window well inside a clearly-kept segment or a clearly-cut ordinary
gap, away from any real candidate -- so the label store isn't composed
entirely of edge cases and occasionally checks a labeler (or, later, a
calibrated threshold) against an unambiguous case.

Storage matches this project's existing per-file convention (see
.cache/detections/<hash>.json): one self-contained
training_data_dir/reviews/<id>.mp4 clip (full-frame, not cropped, window
padded ~1.5s each side) plus training_data_dir/reviews/<id>.json record.
training_data/*.mp4 is gitignored (bulky, personal footage);
training_data/*.json stays tracked.
"""

import hashlib
import json
import random
import subprocess
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from pipeline.segments import HardCutConfig, SegmentConfig, find_boundary_crossings
from pipeline.stitch import probe_video_params

CANDIDATE_KIND_TO_ID_PREFIX = {
    "hard_cut_dip": "hc",
    "boundary_crossing": "bc",
    "veto_boundary": "vb",
    "control": "ctl",
}


@dataclass
class ReviewConfig:
    max_candidates_per_video: int = 5
    # probability, per process_video run, of adding one extra random
    # control sample from a clearly-not-borderline window -- "~1-in-10",
    # a per-run coin flip rather than a per-candidate one, since a
    # control sample's purpose is occasional calibration/sanity-check,
    # not scaling with how many real borderline candidates a given video
    # happened to produce.
    control_sample_rate: float = 0.1
    clip_padding_s: float = 1.5


def _config_hash(seg_cfg: SegmentConfig, hard_cut_cfg: HardCutConfig) -> str:
    """Short, stable hash of the exact threshold/config values these
    candidates were generated under -- stored on every record so labels
    from before and after a threshold-tuning pass can be told apart
    honestly (see README's Task 2 design) rather than silently pooled."""
    payload = json.dumps(
        {"segment": asdict(seg_cfg), "hard_cut": asdict(hard_cut_cfg)},
        sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def hard_cut_dip_candidates(hard_cut_windows, motion_times, motion_scores,
                            hard_cut_cfg: HardCutConfig | None = None):
    """One candidate dict per real hard-cut window this run shipped.
    Unsorted, margin-annotated -- see module docstring for the margin
    definition."""
    cfg = hard_cut_cfg or HardCutConfig()
    motion_times = np.asarray(motion_times, dtype=float)
    motion_scores = np.asarray(motion_scores, dtype=float)
    candidates = []
    for (a, b) in hard_cut_windows:
        idx = (motion_times >= a) & (motion_times <= b)
        peak = float(motion_scores[idx].max()) if idx.any() else 0.0
        score_margin = cfg.quiet_thresh - peak
        duration_margin = (b - a) - cfg.min_raw_dip_s
        margin = min(score_margin, duration_margin)
        candidates.append({
            "candidate_type": "hard_cut_dip",
            "window": {"start_s": float(a), "end_s": float(b)},
            "margin": margin,
            "pipeline_decision": "cut",
            "features_at_label_time": {
                "peak_score": peak, "quiet_thresh": cfg.quiet_thresh,
                "score_margin": score_margin,
                "duration_s": float(b - a),
                "min_raw_dip_s": cfg.min_raw_dip_s,
                "duration_margin": duration_margin,
            },
        })
    return candidates


def boundary_crossing_candidates(crossings):
    """One candidate dict per raw ENTER/EXIT crossing (see
    pipeline.segments.find_boundary_crossings). window is a zero-width
    instant (start_s == end_s) -- clip extraction pads symmetrically
    around it, same as any other candidate's window."""
    candidates = []
    for c in crossings:
        candidates.append({
            "candidate_type": "boundary_crossing",
            "window": {"start_s": c["time"], "end_s": c["time"]},
            "margin": c["margin"],
            "pipeline_decision": c["kind"],  # "enter" (segment opening) or "exit" (closing)
            "features_at_label_time": {
                "score": c["score"], "threshold": c["threshold"],
                "kind": c["kind"],
            },
        })
    return candidates


def veto_boundary_candidates(vetoed_segments, motion_times, motion_scores,
                             seg_cfg: SegmentConfig | None = None):
    """One candidate per segment pipeline.fusion.apply_veto discarded
    (no person detected near its motion for its whole duration). See
    module docstring for the margin definition -- reuses enter_thresh
    since apply_veto has no threshold-crossing sample of its own to
    measure a margin against."""
    cfg = seg_cfg or SegmentConfig()
    motion_times = np.asarray(motion_times, dtype=float)
    motion_scores = np.asarray(motion_scores, dtype=float)
    candidates = []
    for (a, b) in vetoed_segments:
        idx = (motion_times >= a) & (motion_times <= b)
        peak = float(motion_scores[idx].max()) if idx.any() else 0.0
        margin = cfg.enter_thresh - peak
        candidates.append({
            "candidate_type": "veto_boundary",
            "window": {"start_s": float(a), "end_s": float(b)},
            "margin": margin,
            "pipeline_decision": "cut",
            "features_at_label_time": {
                "peak_score": peak, "enter_thresh": cfg.enter_thresh,
                "score_margin": margin, "duration_s": float(b - a),
            },
        })
    return candidates


def _control_candidate(final_segments, hard_cut_windows, duration, rng):
    """One random window well inside either a clearly-kept segment or a
    clearly-cut ordinary gap (never inside a hard-cut window -- that's
    already its own candidate type, not a control case). Returns None if
    no window on this video is long enough to safely sample one from
    (e.g. a very short clip with no real margin around its edges)."""
    margin_s = 2.0  # stay this far from either edge of the chosen span
    kept = [(a, b) for a, b in final_segments if (b - a) > 2 * margin_s]
    gaps = []
    cursor = 0.0
    for a, b in sorted(final_segments):
        if a > cursor:
            gaps.append((cursor, a))
        cursor = b
    if cursor < duration:
        gaps.append((cursor, duration))
    ordinary_gaps = [(a, b) for a, b in gaps if (b - a) > 2 * margin_s
                     and not any(a <= wb and b >= wa for wa, wb in hard_cut_windows)]

    pools = [("kept", span) for span in kept] + [("cut", span) for span in ordinary_gaps]
    if not pools:
        return None
    decision, (a, b) = rng.choice(pools)
    t = rng.uniform(a + margin_s, b - margin_s)
    return {
        "candidate_type": "control",
        "window": {"start_s": float(t), "end_s": float(t)},
        "margin": None,
        "pipeline_decision": decision,
        "features_at_label_time": {},
    }


def select_candidates(hard_cut_candidates, boundary_candidates, final_segments,
                      hard_cut_windows, duration, config: ReviewConfig | None = None,
                      rng=None, veto_candidates=None):
    """Ranks hard-cut-dip, boundary-crossing, and veto-boundary
    candidates together by margin (lowest/most-borderline first), keeps
    the top max_candidates_per_video, and (probabilistically) adds one
    control sample. Pure logic -- no clip extraction, no I/O.
    `veto_candidates` defaults to None/[] so every existing caller that
    predates veto-boundary candidates is unaffected."""
    cfg = config or ReviewConfig()
    rng = rng or random.Random()
    ranked = sorted(hard_cut_candidates + boundary_candidates + (veto_candidates or []),
                    key=lambda c: c["margin"])
    chosen = list(ranked[:cfg.max_candidates_per_video])
    if rng.random() < cfg.control_sample_rate:
        control = _control_candidate(final_segments, hard_cut_windows, duration, rng)
        if control is not None:
            chosen.append(control)
    return chosen


def _extract_review_clip(video_path, start_s, end_s, start_offset, pad_s,
                          out_path, runner=None) -> None:
    """Self-contained clip: window padded pad_s each side, full-frame
    (never cropped), decoded and re-encoded (not stream-copied) so a
    short arbitrary window is frame-accurate regardless of the source's
    own keyframe placement -- these clips are for a human to watch out of
    context, correctness matters far more than extraction speed for a
    ~3-4s clip. -ss/-to are shifted by start_offset for the same reason
    pipeline.stitch.build_extract_cmd already needs to (the source's own
    video-stream start_time is not guaranteed to be 0 -- see that
    module's docstring)."""
    a = max(0.0, start_s - pad_s) + start_offset
    b = end_s + pad_s + start_offset
    cmd = ["ffmpeg", "-y", "-ss", f"{a}", "-to", f"{b}", "-i", str(video_path),
          "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
          "-c:a", "aac", str(out_path)]
    run = runner or (lambda c: subprocess.run(c, check=True, capture_output=True))
    run(cmd)


def _default_pose_feature_fn(det_times, det_boxes, zone, landmarker):
    from pipeline.pose import wrist_displacement

    def fn(video_path, center_s):
        return wrist_displacement(video_path, det_times, det_boxes, zone,
                                  center_s, landmarker=landmarker)
    return fn


def _default_audio_feature_fn():
    from pipeline.audio import onset_features
    cache = {}

    def fn(video_path, center_s):
        return onset_features(video_path, center_s, envelope_cache=cache)
    return fn


def _default_xclip_feature_fn(xclip):
    from pipeline.xclip import swing_probability

    def fn(video_path, center_s):
        return swing_probability(video_path, center_s, xclip)
    return fn


def generate_review_candidates(final_segments, hard_cut_windows, motion_times,
                               motion_scores, enter_scores, video_path,
                               source_file, training_data_dir, duration,
                               vetoed_segments=None, det_times=None,
                               det_boxes=None, zone=None,
                               seg_cfg: SegmentConfig | None = None,
                               hard_cut_cfg: HardCutConfig | None = None,
                               review_cfg: ReviewConfig | None = None,
                               extra_source_info=None, rng=None,
                               prober=probe_video_params, clip_runner=None,
                               pose_feature_fn=None, audio_feature_fn=None,
                               xclip_feature_fn=None,
                               warn=None) -> list:
    """Entry point called from pipeline.run.process_video when the caller
    opts in with a real training_data_dir. Selects candidates, extracts a
    self-contained clip per candidate, and writes one
    training_data_dir/reviews/<id>.json (+ matching <id>.mp4) per
    candidate. Returns the list of record dicts written (mainly for
    tests). Never raises on a single candidate's clip-extraction failure
    -- one bad ffmpeg run shouldn't cost the rest of a real detect job's
    output; failures go through `warn` if given, matching
    pipeline.run.process_video's existing non-fatal warning convention.

    `vetoed_segments`/`det_times`/`det_boxes`/`zone` are all optional
    (default None/[]) so every existing caller that predates
    veto-boundary candidates and pose/audio/xclip features is unaffected
    -- veto candidates need `vetoed_segments`, pose features need real
    `det_times`/`det_boxes`/`zone` (skipped, not a crash, when zone is
    None -- an uncalibrated batch has no plate zone to crop a batter
    from), audio and xclip features only need `video_path`.
    `pose_feature_fn`/`audio_feature_fn`/`xclip_feature_fn`, if given,
    override the real pipeline.pose/pipeline.audio/pipeline.xclip calls
    -- tests inject cheap fakes here the same way `clip_runner` lets them
    fake ffmpeg. Building the real X-CLIP model (xclip_feature_fn not
    given) is itself wrapped and non-fatal, same as a single candidate's
    own feature-extraction failure -- a missing network connection on
    first download, or any other real-world model-load failure, costs
    only this one instrumentation feature, never the rest of a real
    detect job."""
    seg_cfg = seg_cfg or SegmentConfig()
    hard_cut_cfg = hard_cut_cfg or HardCutConfig()
    review_cfg = review_cfg or ReviewConfig()
    rng = rng or random.Random()
    vetoed_segments = vetoed_segments or []

    crossings = find_boundary_crossings(motion_times, enter_scores, seg_cfg,
                                        sustain_scores=motion_scores)
    hc_candidates = hard_cut_dip_candidates(hard_cut_windows, motion_times,
                                            motion_scores, hard_cut_cfg)
    bc_candidates = boundary_crossing_candidates(crossings)
    vb_candidates = veto_boundary_candidates(vetoed_segments, motion_times,
                                             motion_scores, seg_cfg)
    chosen = select_candidates(hc_candidates, bc_candidates, final_segments,
                               hard_cut_windows, duration, review_cfg, rng,
                               veto_candidates=vb_candidates)
    if not chosen:
        return []

    reviews_dir = Path(training_data_dir) / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    info = prober(str(video_path))
    config_hash = _config_hash(seg_cfg, hard_cut_cfg)
    now = datetime.now(timezone.utc).isoformat()

    # Real pose detection loads a model once and reuses it across every
    # candidate in this call (matching pipeline.pose.build_landmarker's
    # own docstring) rather than paying model-load cost per candidate --
    # only built when actually needed (a real zone + real detections).
    own_landmarker = None
    if pose_feature_fn is not None:
        pose_fn = pose_feature_fn
    elif zone is not None and det_times is not None and det_boxes is not None:
        from pipeline.pose import build_landmarker
        own_landmarker = build_landmarker()
        pose_fn = _default_pose_feature_fn(det_times, det_boxes, zone, own_landmarker)
    else:
        pose_fn = None
    audio_fn = audio_feature_fn or _default_audio_feature_fn()

    # Real X-CLIP model load (~786MB, network-dependent on first download)
    # is wrapped and non-fatal, unlike pose's -- a review-queue run should
    # never fail outright because this one instrumentation feature's model
    # couldn't load. Built once, reused across every candidate (same
    # reasoning as own_landmarker above).
    if xclip_feature_fn is not None:
        xclip_fn = xclip_feature_fn
    else:
        try:
            from pipeline.xclip import build_xclip
            xclip_fn = _default_xclip_feature_fn(build_xclip())
        except Exception as e:
            if warn:
                warn(f"xclip model load failed, xclip feature disabled for this run: {e}")
            xclip_fn = None

    try:
        written = []
        for c in chosen:
            prefix = CANDIDATE_KIND_TO_ID_PREFIX[c["candidate_type"]]
            record_id = f"{prefix}_{uuid.uuid4().hex[:12]}"
            clip_path = reviews_dir / f"{record_id}.mp4"
            try:
                _extract_review_clip(video_path, c["window"]["start_s"],
                                     c["window"]["end_s"], info.start_offset,
                                     review_cfg.clip_padding_s, clip_path,
                                     runner=clip_runner)
            except Exception as e:
                if warn:
                    warn(f"review clip extraction failed for {record_id}: {e}")
                continue

            source = {"video_path": str(video_path), "source_file": source_file}
            if extra_source_info:
                source.update(extra_source_info)

            features = dict(c["features_at_label_time"])
            center_s = 0.5 * (c["window"]["start_s"] + c["window"]["end_s"])
            if pose_fn is not None:
                try:
                    pose_result = pose_fn(video_path, center_s)
                except Exception as e:
                    if warn:
                        warn(f"pose feature extraction failed for {record_id}: {e}")
                    pose_result = None
                if pose_result is not None:
                    features["pose"] = pose_result
            try:
                audio_result = audio_fn(video_path, center_s)
            except Exception as e:
                if warn:
                    warn(f"audio feature extraction failed for {record_id}: {e}")
                audio_result = None
            if audio_result is not None:
                features["audio"] = audio_result
            if xclip_fn is not None:
                try:
                    xclip_result = xclip_fn(video_path, center_s)
                except Exception as e:
                    if warn:
                        warn(f"xclip feature extraction failed for {record_id}: {e}")
                    xclip_result = None
                if xclip_result is not None:
                    features["xclip"] = xclip_result

            record = {
                "id": record_id,
                "created_at": now,
                "source": source,
                "window": c["window"],
                "candidate_type": c["candidate_type"],
                "pipeline_decision": c["pipeline_decision"],
                "margin": c["margin"],
                "features_at_label_time": features,
                "config_hash": config_hash,
                "label": None,
                "labeled_at": None,
                "note": None,
            }
            (reviews_dir / f"{record_id}.json").write_text(json.dumps(record, indent=2))
            written.append(record)
    finally:
        if own_landmarker is not None:
            own_landmarker.close()

    return written
