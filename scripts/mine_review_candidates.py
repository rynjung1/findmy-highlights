"""Bulk review-candidate mining: generates many review-queue candidates
at once from real footage, deliberately bypassing the per-run cap
(pipeline.review.ReviewConfig.max_candidates_per_video, which stays 5
for every normal process_video call -- see that module's docstring, and
pipeline/run.py's process_video for the review_cfg passthrough this
script is the one real caller of). Built for building up a real
labeling batch in one sitting; scripts/detect.py, scripts/detect_multi.py,
and the real backend are all untouched and keep the small per-run cap.

Two modes:
  - a single video path: resolves calibration the same way
    scripts/detect.py does (calibration.json next to the video, shared
    or per-file), runs the real pipeline (motion + person detection,
    both cached under .cache/detections/, so a re-run against an
    already-detected video is cheap), and mines up to --limit candidates
    from it.
  - --all-batches: scans uploads/*/ for real batches that have already
    been processed through the app -- a real manifest.json present is
    the signal that a real detect+export cycle already happened, per
    batch's own files.json (source file names) and calibration. Any
    batch without a manifest.json is skipped and reported, not silently
    processed as a side effect of running this script.

--limit is a TOTAL budget across the whole invocation, not per video --
mining stops once it's spent, even mid-batch in --all-batches mode, so
"pull roughly N candidates" means N however many videos that takes, not
N per video.

Usage:
    python scripts/mine_review_candidates.py reference_clips/full_game.mkv --limit 50
    python scripts/mine_review_candidates.py --all-batches --limit 50
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.calibration import resolve_zone
from pipeline.review import ReviewConfig
from pipeline.run import DEFAULT_CACHE_DIR, process_video

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_UPLOADS_ROOT = ROOT / "uploads"
DEFAULT_TRAINING_DATA_DIR = ROOT / "training_data"


def _existing_windows_for_video(reviews_dir: Path, video_path) -> set:
    """(start_s, end_s) of every real record already on disk whose
    source.video_path matches this exact video -- real candidates are
    re-derived deterministically from a video's own motion/hard-cut
    computation every time (same video, same config -> the same ranked
    list), so re-mining the same video without this would just
    regenerate the same lowest-margin windows already mined (and
    probably already labeled) last time, not reach anything new."""
    windows = set()
    if not reviews_dir.exists():
        return windows
    for f in reviews_dir.glob("*.json"):
        try:
            record = json.loads(f.read_text())
        except json.JSONDecodeError:
            continue
        if record.get("source", {}).get("video_path") == str(video_path):
            w = record.get("window", {})
            windows.add((w.get("start_s"), w.get("end_s")))
    return windows


def mine_one(video_path, training_data_dir, budget, extra_source_info=None):
    """Runs the real pipeline against one video and mines up to `budget`
    GENUINELY NEW candidates from it -- possibly fewer, if the video
    doesn't have that many real borderline candidates left or a clip
    extraction fails. Real candidates already covered by an earlier
    mining pass over this same video are requested again internally
    (there's no cheaper way to ask the pipeline's own deterministic
    ranking for "the next N beyond what I already have" -- see
    _existing_windows_for_video above) but then deleted rather than left
    as literal duplicate records; the real, extra ffmpeg/pose/audio/xclip
    cost of re-extracting already-known windows is the honest price of
    not touching pipeline.review's core selection API for what's a
    bulk-mining-only concern.

    process_video() doesn't return the written review records itself
    (changing that return shape would touch every real caller: the
    backend, both detect scripts, every test), so this counts what
    landed in reviews_dir before/after instead -- correct and simple for
    a single-process CLI tool that owns the run, not worth a bigger
    signature change across the whole pipeline for."""
    zone = resolve_zone(str(video_path))

    def warn(msg):
        print(f"  warning: {msg}", file=sys.stderr)

    source_info = {"mined_by": "scripts/mine_review_candidates.py"}
    if extra_source_info:
        source_info.update(extra_source_info)

    reviews_dir = Path(training_data_dir) / "reviews"
    existing_windows = _existing_windows_for_video(reviews_dir, video_path)
    total_request = len(existing_windows) + budget

    before = set(reviews_dir.glob("*.json")) if reviews_dir.exists() else set()
    process_video(str(video_path), zone, cache_dir=DEFAULT_CACHE_DIR, warn=warn,
                  training_data_dir=str(training_data_dir),
                  training_data_source_info=source_info,
                  review_cfg=ReviewConfig(max_candidates_per_video=total_request))
    after = set(reviews_dir.glob("*.json")) if reviews_dir.exists() else set()
    new_files = sorted(after - before)

    kept = []
    n_deduped = 0
    for f in new_files:
        record = json.loads(f.read_text())
        w = (record["window"]["start_s"], record["window"]["end_s"])
        if w in existing_windows:
            f.unlink()
            f.with_suffix(".mp4").unlink(missing_ok=True)
            n_deduped += 1
            continue
        existing_windows.add(w)
        kept.append(f)
    if n_deduped:
        print(f"  ({n_deduped} candidate(s) matched an already-mined window for "
             f"this video, discarded rather than duplicated)")
    return kept


def discover_batches(uploads_root: Path):
    """[(batch_id, bdir, [source_file_names])] for every real batch under
    uploads_root that has a manifest.json -- "already processed", per
    this script's own docstring. Batches without one are reported and
    skipped, never triggered into processing as a side effect."""
    found, skipped = [], []
    if not uploads_root.exists():
        return found, skipped
    for bdir in sorted(uploads_root.iterdir()):
        if not bdir.is_dir():
            continue
        if not (bdir / "manifest.json").exists():
            skipped.append(bdir.name)
            continue
        files_json = bdir / "files.json"
        if not files_json.exists():
            skipped.append(bdir.name)
            continue
        names = json.loads(files_json.read_text())["files"]
        found.append((bdir.name, bdir, names))
    return found, skipped


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", nargs="?", help="a single video file to mine")
    ap.add_argument("--all-batches", action="store_true",
                    help="scan uploads/*/ for already-processed batches instead")
    ap.add_argument("--uploads-root", default=str(DEFAULT_UPLOADS_ROOT))
    ap.add_argument("--limit", type=int, default=50,
                    help="total candidates to mine across the whole run (default 50)")
    ap.add_argument("--training-data-dir", default=str(DEFAULT_TRAINING_DATA_DIR))
    args = ap.parse_args()

    if bool(args.video) == bool(args.all_batches):
        ap.error("pass exactly one of: a video path, or --all-batches")

    training_data_dir = Path(args.training_data_dir)
    remaining = args.limit
    total_written = 0

    if args.video:
        video_path = Path(args.video)
        if not video_path.exists():
            ap.error(f"no such file: {video_path}")
        print(f"mining {video_path} (budget {remaining})...")
        written = mine_one(video_path, training_data_dir, remaining)
        total_written += len(written)
        print(f"  wrote {len(written)} candidate(s)")
    else:
        uploads_root = Path(args.uploads_root)
        found, skipped = discover_batches(uploads_root)
        print(f"{len(found)} already-processed batch(es) found under {uploads_root}"
             f" ({len(skipped)} skipped: no manifest.json yet)")
        for batch_id, bdir, names in found:
            if remaining <= 0:
                print(f"budget spent -- stopping before batch {batch_id}")
                break
            for name in names:
                if remaining <= 0:
                    break
                video_path = bdir / name
                if not video_path.exists():
                    print(f"  {batch_id}/{name}: file missing, skipping")
                    continue
                print(f"mining {batch_id}/{name} (budget {remaining})...")
                written = mine_one(video_path, training_data_dir, remaining,
                                   extra_source_info={"batch_id": batch_id})
                total_written += len(written)
                remaining -= len(written)
                print(f"  wrote {len(written)} candidate(s), {max(remaining, 0)} left in budget")

    print(f"\ntotal mined: {total_written} candidate(s) -> "
         f"{training_data_dir / 'reviews'}")
    print("Review them in the app's Review Queue tab, or via GET /review/next.")


if __name__ == "__main__":
    main()
