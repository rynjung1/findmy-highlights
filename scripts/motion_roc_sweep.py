"""Full ROC/AUC sweep of raw motion score as a CONTINUOUS signal, on the
27-instant hand-verified sample (19 real/8 negative, see
scripts/hand_rule_ensemble_check.py for provenance) -- completely
unconstrained by any threshold, safety-related or otherwise. Answers a
narrower, more fundamental question than the 2026-08-24 hand-rule check
did: does raw motion carry ANY separating information between real and
downtime instants on this footage, independent of where any operating
threshold gets placed? The hand-rule check found the shipped
`HardCutConfig.quiet_thresh` (0.002) fires on almost nothing in this
sample (1/27) -- this script asks whether that's a threshold-placement
problem (some other cutpoint would work) or an information problem (no
cutpoint would).

Reuses the exact same 27-instant sample (REAL_CASES/NEG_CASES) and
motion-peak extraction (+/-1.0s window, pipeline.motion.compute_motion)
from scripts/hand_rule_ensemble_check.py directly -- not re-derived, so
the two scripts' numbers are guaranteed comparable.

Usage:
    venv/bin/python scripts/motion_roc_sweep.py
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.hand_rule_ensemble_check import CLIPS_DIR, MOTION_HALF_WIN_S, NEG_CASES, REAL_CASES
from scripts.pose_audio_validation import auc
from pipeline.motion import compute_motion


def motion_peak(motion_times, motion_scores, t, half_win=MOTION_HALF_WIN_S):
    idx = (motion_times >= t - half_win) & (motion_times <= t + half_win)
    if not idx.any():
        return None
    return float(motion_scores[idx].max())


def roc_curve(real_scores, ambient_scores):
    """Full ROC curve swept across every distinct observed score value
    as a candidate threshold (the complete, exact curve for this finite
    sample -- no binning, no interpolation). Positive class = "downtime"
    (higher score = more downtime-like, i.e. score = -motion_peak).
    Returns [(threshold, tpr, fpr, balanced_acc), ...] sorted by
    threshold ascending, plus the AUC via trapezoidal integration."""
    all_scores = sorted(set(real_scores) | set(ambient_scores))
    # candidate thresholds: just below min to just above max, stepping
    # through every real cutpoint between distinct values
    cutpoints = [all_scores[0] - 1e-9] + \
                [(a + b) / 2 for a, b in zip(all_scores, all_scores[1:])] + \
                [all_scores[-1] + 1e-9]
    points = []
    for c in cutpoints:
        tp = sum(1 for s in real_scores if s > c)
        fn = len(real_scores) - tp
        fp = sum(1 for s in ambient_scores if s > c)
        tn = len(ambient_scores) - fp
        tpr = tp / len(real_scores) if real_scores else float("nan")
        fpr = fp / len(ambient_scores) if ambient_scores else float("nan")
        recall_real_side = tn / len(ambient_scores) if ambient_scores else float("nan")
        bal_acc = 0.5 * (tpr + recall_real_side)
        points.append((c, tpr, fpr, bal_acc, tp, fp, tn, fn))
    return points


def main():
    print(f"Sample: {len(REAL_CASES)} real / {len(NEG_CASES)} negative "
         f"(same 27-instant set as scripts/hand_rule_ensemble_check.py)\n")

    motion_cache = {}

    def get_motion_peak(fname, t):
        if fname not in motion_cache:
            m = compute_motion(str(CLIPS_DIR / fname))
            motion_cache[fname] = (m.times, m.scores)
        mt, ms = motion_cache[fname]
        return motion_peak(mt, ms, t)

    real_raw = []
    for fname, t, tag in REAL_CASES:
        p = get_motion_peak(fname, t)
        real_raw.append(p)
        print(f"[real    ] {tag:<55} motion_peak={p:.6f}")

    neg_raw = []
    for fname, t, tag in NEG_CASES:
        p = get_motion_peak(fname, t)
        neg_raw.append(p)
        print(f"[negative] {tag:<55} motion_peak={p:.6f}")

    print(f"\nreal:     n={len(real_raw)}  min={min(real_raw):.5f}  max={max(real_raw):.5f}  "
         f"mean={np.mean(real_raw):.5f}  median={np.median(real_raw):.5f}")
    print(f"negative: n={len(neg_raw)}  min={min(neg_raw):.5f}  max={max(neg_raw):.5f}  "
         f"mean={np.mean(neg_raw):.5f}  median={np.median(neg_raw):.5f}")

    # score oriented so higher = more downtime-like (the hypothesis
    # direction: motion LOW -> downtime), matching the negated-rise-time
    # convention already used elsewhere in this project for "lower is
    # more real"-type features.
    real_scores = [-p for p in real_raw]
    neg_scores = [-p for p in neg_raw]

    full_auc = auc(real_scores, neg_scores)
    # AUC here answers: P(a random downtime score > a random real score)
    # i.e. P(a random downtime instant has LOWER raw motion than a
    # random real instant) -- the hypothesis's own direction.
    print(f"\n=== Full AUC (motion-low-as-downtime-predictor direction) ===")
    print(f"AUC = {full_auc:.4f}  (0.5 = pure chance, no threshold assumed)")

    print(f"\n=== Full ROC sweep -- EVERY possible threshold on this sample, "
         f"not just quiet_thresh (0.002) ===")
    points = roc_curve(real_scores, neg_scores)
    best = max(points, key=lambda p: p[3])
    print(f"{'raw motion cutoff':>20} {'TPR(downtime)':>15} {'FPR(real lost)':>16} "
         f"{'balanced acc':>13} {'confusion (tp/fp/tn/fn)':>26}")
    for c, tpr, fpr, bal_acc, tp, fp, tn, fn in points:
        raw_cutoff = -c  # convert back to raw motion units for readability
        marker = "  <-- BEST" if (c, tpr, fpr, bal_acc, tp, fp, tn, fn) == best else ""
        print(f"{raw_cutoff:>20.6f} {tpr:>15.3f} {fpr:>16.3f} {bal_acc:>13.3f} "
             f"{f'{tp}/{fp}/{tn}/{fn}':>26}{marker}")

    c, tpr, fpr, bal_acc, tp, fp, tn, fn = best
    print(f"\n=== Best possible operating point on this data, unconstrained "
         f"(purely diagnostic -- NOT a proposed threshold) ===")
    print(f"raw motion cutoff (predict downtime if peak motion < this): {-c:.6f}")
    print(f"balanced accuracy at this single best point: {bal_acc:.4f}")
    print(f"confusion: TP(downtime correct)={tp} FP(real play lost)={fp} "
         f"TN(real correct)={tn} FN(downtime missed)={fn}")
    print(f"(for reference: shipped HardCutConfig.quiet_thresh = 0.002, "
         f"far from this best-point cutoff)")

    print(f"\n=== Comparison points ===")
    print(f"This sweep's full AUC: {full_auc:.4f}")
    print(f"X-CLIP zero-shot baseline: 0.653 (its own native sample) / "
         f"0.428 (this SAME independent 27-instant sample)")
    print(f"Zone-velocity investigation's own real numbers (different, "
         f"zone-specific signal, not this whole-frame one): real contact "
         f"0.33-1.11, taken-pitch (no swing, required) 0.08, practice-swing "
         f"0.61, false-positive walk-away 0.65 -- zone-velocity DOES "
         f"separate high-motion from low-motion cleanly, but isn't "
         f"swing-selective (practice/walk-away score as high as real contact).")


if __name__ == "__main__":
    main()
