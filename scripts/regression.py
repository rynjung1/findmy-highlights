"""Detection regression: run the pipeline on every reference clip and score
it against the hand-written ground truth.

For each clip this reports:
  - recall on REQUIRED events (real plays: swings, hits, runs) — the number
    that any detected segment overlaps. Per the project priority rule this
    is the metric that must stay perfect.
  - capture of borderline (non-required) events — informational.
  - over-inclusion: how much footage was flagged in total vs. how much the
    ground-truth event windows cover. Expected to be well above 1.0 for the
    permissive Phase 1 detector; reported so changes can be compared.

Usage:
    python scripts/regression.py [--clips-dir reference_clips]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.motion import compute_motion
from pipeline.segments import (SegmentConfig, scores_to_segments,
                               segment_covers, total_duration)

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"


def run_clip(clip_path: Path, truth: dict) -> dict:
    result = compute_motion(str(clip_path))
    segments = scores_to_segments(result.times, result.scores, SegmentConfig())

    required = [e for e in truth["events"] if e["required"]]
    borderline = [e for e in truth["events"] if not e["required"]]
    req_hit = [e for e in required if segment_covers(segments, e["window"])]
    req_miss = [e for e in required if not segment_covers(segments, e["window"])]
    bord_hit = [e for e in borderline if segment_covers(segments, e["window"])]

    truth_cover = sum(b - a for a, b in (e["window"] for e in truth["events"]))
    flagged = total_duration(segments)
    return {
        "clip": clip_path.name,
        "duration": result.duration,
        "segments": segments,
        "required_total": len(required),
        "required_captured": len(req_hit),
        "required_missed": req_miss,
        "borderline_total": len(borderline),
        "borderline_captured": len(bord_hit),
        "flagged_s": flagged,
        "flagged_frac": flagged / result.duration if result.duration else 0.0,
        "truth_window_s": truth_cover,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips-dir", default=str(ROOT / "reference_clips"))
    args = ap.parse_args()
    clips_dir = Path(args.clips_dir)

    truths = sorted(GROUND_TRUTH_DIR.glob("*.json"))
    if not truths:
        sys.exit("no ground truth files found")

    all_ok = True
    for tf in truths:
        truth = json.loads(tf.read_text())
        clip_path = clips_dir / truth["clip"]
        if not clip_path.exists():
            print(f"[skip] {truth['clip']}: not found in {clips_dir}")
            all_ok = False
            continue
        r = run_clip(clip_path, truth)
        ok = r["required_captured"] == r["required_total"]
        all_ok &= ok
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {r['clip']}: recall {r['required_captured']}/"
              f"{r['required_total']} required events"
              f" | borderline {r['borderline_captured']}/{r['borderline_total']}"
              f" | flagged {r['flagged_s']:.0f}s of {r['duration']:.0f}s"
              f" ({100 * r['flagged_frac']:.0f}%)"
              f" | truth windows {r['truth_window_s']:.0f}s")
        for e in r["required_missed"]:
            print(f"       MISSED {e['id']} {e['type']} window={e['window']}"
                  f" — {e['note']}")
        for a, b in r["segments"]:
            print(f"       seg {a:7.1f} - {b:7.1f}  ({b - a:5.1f}s)")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
