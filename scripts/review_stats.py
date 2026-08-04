"""Tier 1 review-queue usage: disagreement-rate reporting, plus real
feature-vs-label pattern analysis.

Scans training_data/reviews/*.json (see pipeline/review.py for the
schema) and reports, per candidate_type (and per pipeline_decision within
each type), how often a human's label disagreed with the pipeline's own
decision at that instant. This is the cheap, first real use of the
labels collected by the Review Queue UI -- Tier 2 (threshold calibration
via a precision/recall sweep) and Tier 3 (a learned classifier) are
future work, deliberately not built here; per the project's own honest-
threshold rule, Tier 3 in particular needs 300-500 labeled events across
6-10 distinct recording sessions before a learned classifier is a better
bet than the current hand-tuned thresholds -- nowhere close to what a
handful of review sessions produces.

Agreement is defined by what each pipeline_decision claims about that
instant: "cut"/"exit" both claim "this is downtime"; "kept"/"enter" both
claim "this is real action". A label that matches the claim is agreement;
anything else is a disagreement worth looking at.

Also reports real feature-vs-label patterns: for each of the four
instrumentation features already attached to every candidate
(pipeline.pose/pipeline.audio/pipeline.xclip, plus the raw motion score
every candidate type but "control" carries), splits every labeled
record's real value by the real label and reports the real distribution
per group plus a real AUC (P[a random real_action-labeled sample reads
more real-action-like than a random downtime-labeled sample] --
threshold-free separation, same formula used throughout this project's
own signal validations) -- an actual correlation number, not just an
eyeballed distribution. Records missing a given feature (e.g. pose with
no calibrated zone) are excluded from that feature's own analysis and
the skip count is reported, not hidden.

Usage:
    python scripts/review_stats.py [--training-data-dir training_data]
"""

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DECISION_EXPECTS_LABEL = {
    "cut": "downtime",
    "exit": "downtime",
    "kept": "real_action",
    "enter": "real_action",
}


def _motion_score(record: dict):
    """peak_score (hard_cut_dip, veto_boundary) or score (boundary_crossing)
    -- two field names for the same underlying quantity across candidate
    types. None for control samples, which never get a motion score
    computed for their own instant."""
    f = record["features_at_label_time"]
    if "peak_score" in f:
        return f["peak_score"]
    if "score" in f:
        return f["score"]
    return None


# (display name, extractor, higher_is_more_real_action_like). Audio is the
# one inverted feature -- a sharp, fast attack (SHORT rise time) is what
# real contact looks like, not a long one (see pipeline/audio.py).
FEATURES = [
    ("motion score", _motion_score, True),
    ("pose peak_displacement_px",
     lambda r: (r["features_at_label_time"].get("pose") or {}).get("peak_displacement_px"),
     True),
    ("audio rise_time_s",
     lambda r: (r["features_at_label_time"].get("audio") or {}).get("rise_time_s"),
     False),
    ("xclip p_swinging",
     lambda r: (r["features_at_label_time"].get("xclip") or {}).get("p_swinging"),
     True),
]


def auc(pos_scores: list, neg_scores: list):
    """P(a random pos score > a random neg score), ties counted half --
    the same threshold-free separation statistic this project has used
    for every real signal validation tonight. None if either side is
    empty (nothing to compare)."""
    if not pos_scores or not neg_scores:
        return None
    wins = 0.0
    for p in pos_scores:
        for n in neg_scores:
            if p > n:
                wins += 1.0
            elif p == n:
                wins += 0.5
    return wins / (len(pos_scores) * len(neg_scores))


def _group_stats(values: list) -> dict:
    return {
        "n": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.mean(values),
        "max": max(values),
    }


def feature_label_pattern(records: list, extractor, higher_is_real_action: bool) -> dict:
    """{"real": group_stats|None, "downtime": group_stats|None,
    "auc": float|None, "n_skipped": int} -- real_action/downtime split of
    every labeled record's real value for this one feature. AUC is always
    reported in the same "higher = more real-action-like" direction
    regardless of the feature's own raw polarity, per FEATURES above."""
    real_vals, downtime_vals, skipped = [], [], 0
    for r in records:
        v = extractor(r)
        if v is None:
            skipped += 1
            continue
        if r["label"] == "real_action":
            real_vals.append(v)
        elif r["label"] == "downtime":
            downtime_vals.append(v)
    pos, neg = (real_vals, downtime_vals) if higher_is_real_action else (downtime_vals, real_vals)
    return {
        "real": _group_stats(real_vals) if real_vals else None,
        "downtime": _group_stats(downtime_vals) if downtime_vals else None,
        "auc": auc(pos, neg),
        "n_skipped": skipped,
    }


def load_labeled_records(reviews_dir: Path) -> list:
    records = []
    if not reviews_dir.exists():
        return records
    for p in sorted(reviews_dir.glob("*.json")):
        try:
            record = json.loads(p.read_text())
        except json.JSONDecodeError:
            print(f"  (skipping unreadable record: {p.name})")
            continue
        if record.get("label") is not None:
            records.append(record)
    return records


def disagreement_stats(records: list) -> dict:
    """{(candidate_type, pipeline_decision): {"n": int, "disagree": int}}"""
    buckets = defaultdict(lambda: {"n": 0, "disagree": 0})
    for r in records:
        key = (r["candidate_type"], r["pipeline_decision"])
        expected = DECISION_EXPECTS_LABEL.get(r["pipeline_decision"])
        buckets[key]["n"] += 1
        if expected is not None and r["label"] != expected:
            buckets[key]["disagree"] += 1
    return dict(buckets)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--training-data-dir", default=str(ROOT / "training_data"))
    args = ap.parse_args()

    reviews_dir = Path(args.training_data_dir) / "reviews"
    records = load_labeled_records(reviews_dir)

    print(f"Reading labeled review records from {reviews_dir}")
    if not records:
        print("No labeled records found yet -- nothing to report.")
        print("(Label some in the Review Queue UI, then re-run this script.)")
        return

    print(f"{len(records)} labeled record(s) total\n")

    by_type = defaultdict(lambda: {"n": 0, "disagree": 0})
    stats = disagreement_stats(records)
    for (ctype, decision), s in sorted(stats.items()):
        by_type[ctype]["n"] += s["n"]
        by_type[ctype]["disagree"] += s["disagree"]

    for ctype in sorted(by_type):
        totals = by_type[ctype]
        rate = totals["disagree"] / totals["n"] if totals["n"] else 0.0
        low_n_note = " (fewer than 20 labels -- treat as noisy, not a real signal yet)" \
            if totals["n"] < 20 else ""
        print(f"{ctype}: {totals['n']} labeled, "
              f"{totals['disagree']} disagreement(s) "
              f"({rate:.1%}){low_n_note}")
        for (t, decision), s in sorted(stats.items()):
            if t != ctype or s["n"] == 0:
                continue
            sub_rate = s["disagree"] / s["n"]
            print(f"    decision={decision}: {s['n']} labeled, "
                 f"{s['disagree']} disagreement(s) ({sub_rate:.1%})")
        print()

    total_n = sum(s["n"] for s in stats.values())
    total_disagree = sum(s["disagree"] for s in stats.values())
    overall_rate = total_disagree / total_n if total_n else 0.0
    print(f"Overall: {total_n} labeled, {total_disagree} disagreement(s) "
         f"({overall_rate:.1%})")

    print(f"\n=== Feature vs. label patterns ({len(records)} labeled record(s)) ===")
    for name, extractor, higher_is_real_action in FEATURES:
        p = feature_label_pattern(records, extractor, higher_is_real_action)
        print(f"\n{name}:")
        if p["real"] is None or p["downtime"] is None:
            print("  not enough data yet (need at least one labeled example on "
                 "both sides)")
            if p["n_skipped"]:
                print(f"  ({p['n_skipped']} record(s) skipped: feature not present)")
            continue
        r, d = p["real"], p["downtime"]
        print(f"  real_action (n={r['n']}): min={r['min']:.4f} median={r['median']:.4f} "
             f"mean={r['mean']:.4f} max={r['max']:.4f}")
        print(f"  downtime    (n={d['n']}): min={d['min']:.4f} median={d['median']:.4f} "
             f"mean={d['mean']:.4f} max={d['max']:.4f}")
        low_n_note = " (fewer than 20 labels on the smaller side -- treat as noisy, " \
            "not a real signal yet)" if min(r["n"], d["n"]) < 20 else ""
        direction = "" if higher_is_real_action else " (lower reads more real-action-like for this feature)"
        print(f"  AUC (P[real_action reads more real-action-like than downtime]): "
             f"{p['auc']:.3f}{direction}{low_n_note}")
        if p["n_skipped"]:
            print(f"  ({p['n_skipped']} record(s) skipped: feature not present)")


if __name__ == "__main__":
    main()
