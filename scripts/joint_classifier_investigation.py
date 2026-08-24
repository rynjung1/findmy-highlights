"""Investigation, not implementation: a small joint classifier combining
motion, pose, and audio (temporal position deliberately excluded --
scripts/temporal_investigation.py found no consistent pattern to feed
in) on the same real 9-reference-clip dataset used for tonight's
pose+audio validation (scripts/pose_audio_validation.py, whose loaders
this script reuses directly rather than re-deriving the same real event/
ambient sample lists a second time).

Nothing deep, per the original Tier 3 scoping: L2-regularized logistic
regression over 3 standardized features (motion peak score, pose peak
wrist displacement, audio onset rise-time), evaluated with leave-one-out
cross-validation given how few real positive examples exist (n=10-11) --
a real train/test split would leave almost nothing to train or test on
either side. This is deliberately a "does a small model beat any single
feature alone, at real scale" check, not a deployable classifier -- see
README's Task 2 pose+audio writeup for the already-established Tier 3
bar (300-500 labeled events across 6-10 sessions) this is nowhere close
to clearing regardless of what number comes out below.

Usage:
    ./venv/bin/python scripts/joint_classifier_investigation.py
"""

import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from pose_audio_validation import (CACHE_DIR, CLIPS_DIR, auc,
                                   load_ambient_samples, load_real_events)

from pipeline.audio import onset_features
from pipeline.calibration import resolve_zone
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.motion import compute_motion
from pipeline.pose import build_landmarker, wrist_displacement


def motion_peak_near(motion_times, motion_scores, center_s, half_win=1.0):
    idx = (motion_times >= center_s - half_win) & (motion_times <= center_s + half_win)
    if not idx.any():
        return None
    return float(motion_scores[idx].max())


def build_dataset():
    """[(label, motion_peak, pose_px, audio_ms), ...] -- only samples
    where ALL THREE features were obtained (a real near-plate pose
    detection AND a real audio reading), same "paired subset" discipline
    tonight's earlier combined analysis already used."""
    real_events = load_real_events()
    ambient_samples = load_ambient_samples()

    by_clip = {}
    for clip, t, *_ in real_events:
        by_clip.setdefault(clip, {"real": [], "ambient": []})["real"].append(t)
    for clip, t in ambient_samples:
        by_clip.setdefault(clip, {"real": [], "ambient": []})["ambient"].append(t)

    landmarker = build_landmarker()
    audio_cache = {}
    rows = []
    try:
        for clip, groups in sorted(by_clip.items()):
            clip_path = CLIPS_DIR / f"{clip}.mkv"
            if not clip_path.exists():
                continue
            zone = resolve_zone(str(clip_path))
            if zone is None:
                continue
            det = detect_persons(str(clip_path), DetectionConfig(), cache_dir=str(CACHE_DIR))
            motion = compute_motion(str(clip_path))

            for label, times in (("real", groups["real"]), ("ambient", groups["ambient"])):
                for t in times:
                    m = motion_peak_near(motion.times, motion.scores, t)
                    p = wrist_displacement(str(clip_path), det.times, det.boxes, zone,
                                           t, landmarker=landmarker)
                    a = onset_features(str(clip_path), t, envelope_cache=audio_cache)
                    if m is None or p is None or a is None:
                        continue
                    rows.append((label, m, p["peak_displacement_px"],
                                a["rise_time_s"] * 1000))
    finally:
        landmarker.close()
    return rows


def zscore_fit(X):
    mean = X.mean(axis=0)
    std = X.std(axis=0)
    std[std == 0] = 1.0
    return mean, std


def logistic_fit(X, y, l2=1.0):
    """L2-regularized logistic regression via L-BFGS -- no external ML
    dependency needed for 3 features. Returns weights (bias last)."""
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])

    def nll(w):
        z = Xb @ w
        # numerically stable log(1+exp(-z)) / log(1+exp(z))
        loss = np.logaddexp(0, -z) * y + np.logaddexp(0, z) * (1 - y)
        reg = l2 * np.sum(w[:-1] ** 2)
        return loss.sum() + reg

    w0 = np.zeros(d + 1)
    result = minimize(nll, w0, method="L-BFGS-B")
    return result.x


def logistic_predict(w, X):
    Xb = np.hstack([X, np.ones((X.shape[0], 1))])
    z = Xb @ w
    return 1.0 / (1.0 + np.exp(-z))


def leave_one_out_auc(X, y, l2=1.0):
    """Real classifiers' scores on held-out samples, one at a time --
    every prediction comes from a model that never saw that sample
    during training, the honest way to evaluate with this little data.
    Also returns the per-fold fitted weight vector (bias last) for every
    fold that held out a REAL sample -- the original investigation's own
    "the fitted boundary swings enough between folds" claim was always
    qualitative; this is what actually quantifies it (how much the
    coefficients themselves move fold to fold, not just the resulting
    score)."""
    n = len(y)
    scores = np.zeros(n)
    real_fold_weights = []
    for i in range(n):
        train_idx = np.array([j for j in range(n) if j != i])
        mean, std = zscore_fit(X[train_idx])
        Xtr = (X[train_idx] - mean) / std
        w = logistic_fit(Xtr, y[train_idx], l2=l2)
        Xte = (X[i:i + 1] - mean) / std
        scores[i] = logistic_predict(w, Xte)[0]
        if y[i] == 1:
            real_fold_weights.append(w)
    real_scores = scores[y == 1]
    ambient_scores = scores[y == 0]
    return auc(list(real_scores), list(ambient_scores)), scores, np.array(real_fold_weights)


def main():
    print("Building paired (motion, pose, audio) dataset -- real model "
         "inference, will take a few minutes...")
    rows = build_dataset()
    n_real = sum(1 for r in rows if r[0] == "real")
    n_ambient = sum(1 for r in rows if r[0] == "ambient")
    print(f"\nFully-paired samples (all 3 features present): "
         f"{n_real} real, {n_ambient} ambient")

    if n_real < 3:
        print("Too few real examples with all 3 features to fit anything "
             "meaningful.")
        return

    y = np.array([1 if r[0] == "real" else 0 for r in rows])
    X = np.array([[r[1], r[2], r[3]] for r in rows])
    feature_names = ["motion_peak", "pose_peak_px", "audio_rise_ms"]

    print("\n=== single-feature AUCs on this exact paired subset "
         "(comparison baseline) ===")
    for i, name in enumerate(feature_names):
        real_vals = list(X[y == 1, i])
        ambient_vals = list(X[y == 0, i])
        # audio: lower is "more real" -- negate for the AUC convention
        # used everywhere else tonight (higher score = more real)
        if name == "audio_rise_ms":
            real_vals = [-v for v in real_vals]
            ambient_vals = [-v for v in ambient_vals]
        print(f"  {name}: AUC = {auc(real_vals, ambient_vals):.3f}")

    print("\n=== joint logistic regression, leave-one-out cross-validated ===")
    cv_auc, scores, real_fold_weights = leave_one_out_auc(X, y, l2=1.0)
    print(f"  AUC (leave-one-out): {cv_auc:.3f}")
    print(f"  n={len(y)} ({n_real} real / {n_ambient} ambient) -- with "
         f"this few real positives, treat this number as indicative, "
         f"not a real performance estimate.")

    real_held_out = scores[y == 1]
    ambient_held_out = scores[y == 0]
    print("\n=== per-fold variance, not just the single AUC number ===")
    print(f"  real-side held-out scores: {np.array2string(real_held_out, precision=3)}")
    print(f"    mean={real_held_out.mean():.3f} std={real_held_out.std():.3f} "
         f"min={real_held_out.min():.3f} max={real_held_out.max():.3f}")
    print(f"  ambient-side held-out scores: mean={ambient_held_out.mean():.3f} "
         f"std={ambient_held_out.std():.3f} min={ambient_held_out.min():.3f} "
         f"max={ambient_held_out.max():.3f}")
    print(f"  (real median {np.median(real_held_out):.3f} vs. ambient median "
         f"{np.median(ambient_held_out):.3f} -- {'LOWER' if np.median(real_held_out) < np.median(ambient_held_out) else 'higher'}, "
         f"the below-chance AUC's direct cause)")

    print("\n  per-fold fitted coefficients (one row per fold that held out a "
         "REAL sample -- quantifies 'the boundary swings between folds'):")
    for name, col in zip(feature_names, real_fold_weights.T):
        print(f"    {name}: {np.array2string(col, precision=2)}  "
             f"std={col.std():.3f}  sign flips={ (np.sign(col[:-1]) != np.sign(col[1:])).sum() if len(col) > 1 else 0 }")

    # weights on the FULL dataset, for a look at what the model actually
    # leaned on -- not used for the CV number above, diagnostic only
    mean, std = zscore_fit(X)
    Xz = (X - mean) / std
    w_full = logistic_fit(Xz, y, l2=1.0)
    print("\n  full-data fit coefficients (standardized features, "
         "diagnostic only, not cross-validated):")
    for name, coef in zip(feature_names, w_full[:-1]):
        print(f"    {name}: {coef:+.3f}")

    print(f"\n  For real context: this project's own established Tier 3 "
         f"bar is 300-500 labeled events across 6-10 distinct recording "
         f"sessions, evaluated against these 9 reference clips as a "
         f"fixed held-out check. This run used {n_real} real events from "
         f"ONE session's worth of reference footage -- roughly 2% of "
         f"the low end of that bar.")


if __name__ == "__main__":
    main()
