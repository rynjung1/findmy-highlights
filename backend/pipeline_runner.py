"""The functions actually run in a background thread once a job is
dispatched. Calls into the existing pipeline exactly the way
scripts/detect.py, scripts/detect_multi.py, and scripts/stitch.py do —
process_video() and run_stitch() stay the single implementations of
"process one file" / "stitch a manifest"; this module only wires their
on_stage callbacks to job-file updates, matching the project rule that
pipeline logic lives in pipeline/, never duplicated in a caller.
"""

from pathlib import Path

from pipeline.calibration import resolve_zone
from pipeline.manifest import (apply_output_offsets, build_manifest,
                               build_multi_file_manifest, load_manifest,
                               save_manifest)
from pipeline.run import DEFAULT_CACHE_DIR, process_video
from pipeline.segments import SegmentConfig, find_skip_suggestions, smooth_scores
from pipeline.stitch import run_stitch

from backend.jobs import create_job, save_job


def run_detect_job(batch_dir, job: dict, ordered_paths: list,
                   training_data_dir=None) -> None:
    """Runs detection across ordered_paths (already order-resolved by
    the caller — this function does no ordering itself, same division of
    responsibility as scripts/detect_multi.py) and writes the manifest.
    Mutates and persists `job` at each stage transition.

    `training_data_dir`, if given, opts every file in this real batch
    into the Tier 1 review queue (pipeline.review, via
    pipeline.run.process_video) -- see backend/app.py for where this
    comes from (an explicit opt-in, off by default, specifically so a
    real deployment has to choose to collect this rather than it
    happening silently)."""
    def on_stage(stage, path=None):
        job["stage"] = f"{stage} ({Path(path).name})" if path else stage
        save_job(batch_dir, job)

    try:
        job["status"] = "in_progress"
        save_job(batch_dir, job)

        files_for_manifest = []
        for path in ordered_paths:
            zone = resolve_zone(path)
            segments, vetoed, duration, motion, hard_cut_windows = process_video(
                path, zone, motion_only=False, cache_dir=DEFAULT_CACHE_DIR,
                warn=lambda msg: job["warnings"].append(msg),
                on_stage=lambda s, p=path: on_stage(s, p),
                training_data_dir=training_data_dir,
                training_data_source_info={"batch_id": job["batch_id"]})

            sm = smooth_scores(motion.times, motion.scores,
                               SegmentConfig().smooth_window_s)

            def peak_score(a, b, sm=sm, mt=motion.times):
                idx = (mt >= a) & (mt <= b)
                return float(sm[idx].max()) if idx.any() else 0.0

            # Manual, non-destructive skip-ahead suggestions (see
            # pipeline/manifest.py's skip_suggestions field): uses RAW
            # motion.scores, not the smoothed `sm` peak_score reads above --
            # this is a UI affordance the viewer can simply not click, not
            # a real cutting decision, so it's deliberately more permissive
            # than anything used for actual segment boundaries or padding.
            def skip_fn(a, b, mt=motion.times, sc=motion.scores):
                return find_skip_suggestions(a, b, mt, sc)

            files_for_manifest.append({
                "source_file": Path(path).name, "duration": duration,
                "kept_segments": segments, "score_fn": peak_score,
                "skip_fn": skip_fn, "hard_cut_windows": hard_cut_windows,
            })

        on_stage("building manifest")
        if len(files_for_manifest) == 1:
            f = files_for_manifest[0]
            manifest = build_manifest(f["source_file"], f["duration"],
                                      f["kept_segments"],
                                      score_fn=f["score_fn"],
                                      skip_fn=f["skip_fn"],
                                      hard_cut_windows=f["hard_cut_windows"])
        else:
            manifest = build_multi_file_manifest(files_for_manifest)

        manifest_path = Path(batch_dir) / "manifest.json"
        save_manifest(manifest, manifest_path)

        job["status"] = "completed"
        job["stage"] = None
        job["manifest_path"] = str(manifest_path)
        save_job(batch_dir, job)
    except Exception as e:
        job["status"] = "failed"
        job["stage"] = None
        job["error"] = str(e)
        save_job(batch_dir, job)
        raise


def run_detect_then_export_job(batch_dir, detect_job: dict,
                               ordered_paths: list,
                               training_data_dir=None) -> None:
    """Auto-chains detect -> export so one trigger-processing call ends
    with a playable output, matching the Home view's "one Process
    action, one wait, then a video" flow (Stage 7) — the manual
    POST /export endpoint (run_export_job called directly) stays
    separate and is what a future restore-then-re-export (Stage 8/9)
    will call again on its own. If detect fails, its own except block
    already persisted the failure to detect_job before re-raising; this
    just stops the chain there rather than starting an export against a
    manifest that was never written."""
    try:
        run_detect_job(batch_dir, detect_job, ordered_paths, training_data_dir)
    except Exception:
        return
    export_job = create_job(batch_dir, detect_job["batch_id"], "export",
                            status="pending")
    run_export_job(batch_dir, export_job)


def run_export_job(batch_dir, job: dict) -> None:
    """Stitches the batch's current manifest into a final output video
    (the Edit Log's re-export action). Also persists each kept segment's
    REAL rendered position in that output (output_start_s/output_end_s,
    see pipeline.manifest.apply_output_offsets) back into manifest.json
    -- runs on every export, including every restore/cut-again-triggered
    re-export, so these never go stale for whatever output.mp4 this job
    just produced."""
    def on_stage(stage):
        job["stage"] = stage
        save_job(batch_dir, job)

    try:
        job["status"] = "in_progress"
        save_job(batch_dir, job)

        manifest_path = Path(batch_dir) / "manifest.json"
        manifest = load_manifest(manifest_path)
        output_path = Path(batch_dir) / "output.mp4"
        result = run_stitch(manifest, batch_dir, output_path, on_stage=on_stage)

        apply_output_offsets(manifest, result.segment_output_offsets)
        save_manifest(manifest, manifest_path)

        job["status"] = "completed"
        job["stage"] = None
        job["output_path"] = str(output_path)
        save_job(batch_dir, job)
    except Exception as e:
        job["status"] = "failed"
        job["stage"] = None
        job["error"] = str(e)
        save_job(batch_dir, job)
        raise
