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
from pipeline.manifest import (build_manifest, build_multi_file_manifest,
                               load_manifest, save_manifest)
from pipeline.run import DEFAULT_CACHE_DIR, process_video
from pipeline.segments import SegmentConfig, smooth_scores
from pipeline.stitch import run_stitch

from backend.jobs import save_job


def run_detect_job(batch_dir, job: dict, ordered_paths: list) -> None:
    """Runs detection across ordered_paths (already order-resolved by
    the caller — this function does no ordering itself, same division of
    responsibility as scripts/detect_multi.py) and writes the manifest.
    Mutates and persists `job` at each stage transition."""
    def on_stage(stage, path=None):
        job["stage"] = f"{stage} ({Path(path).name})" if path else stage
        save_job(batch_dir, job)

    try:
        job["status"] = "in_progress"
        save_job(batch_dir, job)

        files_for_manifest = []
        for path in ordered_paths:
            zone = resolve_zone(path)
            segments, vetoed, duration, motion = process_video(
                path, zone, motion_only=False, cache_dir=DEFAULT_CACHE_DIR,
                warn=lambda msg: job["warnings"].append(msg),
                on_stage=lambda s, p=path: on_stage(s, p))

            sm = smooth_scores(motion.times, motion.scores,
                               SegmentConfig().smooth_window_s)

            def peak_score(a, b, sm=sm, mt=motion.times):
                idx = (mt >= a) & (mt <= b)
                return float(sm[idx].max()) if idx.any() else 0.0

            files_for_manifest.append({
                "source_file": Path(path).name, "duration": duration,
                "kept_segments": segments, "score_fn": peak_score,
            })

        on_stage("building manifest")
        if len(files_for_manifest) == 1:
            f = files_for_manifest[0]
            manifest = build_manifest(f["source_file"], f["duration"],
                                      f["kept_segments"],
                                      score_fn=f["score_fn"])
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


def run_export_job(batch_dir, job: dict) -> None:
    """Stitches the batch's current manifest into a final output video
    (the Edit Log's re-export action)."""
    def on_stage(stage):
        job["stage"] = stage
        save_job(batch_dir, job)

    try:
        job["status"] = "in_progress"
        save_job(batch_dir, job)

        manifest = load_manifest(Path(batch_dir) / "manifest.json")
        output_path = Path(batch_dir) / "output.mp4"
        run_stitch(manifest, batch_dir, output_path, on_stage=on_stage)

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
