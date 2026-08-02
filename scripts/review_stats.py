"""Tier 1 review-queue usage: disagreement-rate reporting.

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

Usage:
    python scripts/review_stats.py [--training-data-dir training_data]
"""

import argparse
import json
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


if __name__ == "__main__":
    main()
