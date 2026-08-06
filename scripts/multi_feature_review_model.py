"""INVESTIGATION ONLY -- not wired into any real decision.

Real question: does a small, properly cross-validated multi-feature
model -- pose peak wrist displacement + audio onset amplitude/rise-time
+ X-CLIP p_swinging, the three independent signals this project has
already investigated individually and each found real-but-modest alone
-- do better at separating real_action from downtime than any of them
alone, on this project's own real review-queue labels?

Same discipline as every other model attempt this project has run:
  - LOO-CV (leave-one-out), not a single train/test split -- the only
    honest way to get real out-of-sample performance at this sample
    size (real n, see below, not enough for a held-out test split to
    mean anything on its own).
  - A real permutation test against the LOO-CV AUC -- "the number looks
    good" is not evidence on its own; the null distribution has to be
    beaten for real.
  - Explicit recall-risk verification: applied to the REQUIRED real
    events in clip_base1-4/clip_foul1/clip_whiff1 -- the six clips
    named, repeatedly, as the ones that have broken every prior
    enter-side/gating attempt in this project's history -- does this
    model ever classify one as "downtime"? A real miss here is
    disqualifying regardless of the CV numbers; reported honestly
    either way.

Dataset: every real labeled review-queue record
(training_data/reviews/*.json) EXCEPT the single "control" sample
(n=1, a deliberately-random calibration sample, not a borderline
pipeline decision -- not modelable and not real training signal).
Missing pose (no near-plate box found at that instant, a real ~31% of
records) is handled with an explicit missingness indicator feature and
a 0-fill for the displacement value itself, preserving the full real
sample rather than discarding a third of the real labeled data.

Usage:
    ./venv/bin/python scripts/multi_feature_review_model.py
"""

import json
import sys
import time
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sklearn.model_selection import LeaveOneOut
from sklearn.preprocessing import StandardScaler

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = ROOT / "training_data" / "reviews"
GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
CLIPS_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"

FRAGILE_CLIPS = ["clip_base1", "clip_base2", "clip_base3", "clip_base4",
                 "clip_foul1", "clip_whiff1"]

FEATURE_NAMES = ["pose_peak_displacement_px", "pose_missing",
                 "audio_peak_amplitude", "audio_rise_time_s",
                 "xclip_p_swinging"]

N_PERMUTATIONS = 1000


def load_dataset():
    records = []
    for f in sorted(REVIEWS_DIR.glob("*.json")):
        d = json.loads(f.read_text())
        if d["candidate_type"] == "control":
            continue  # n=1, not modelable -- see module docstring
        records.append(d)

    X, y, ids = [], [], []
    for d in records:
        feats = d["features_at_label_time"]
        pose = feats.get("pose")
        audio = feats["audio"]
        xclip = feats["xclip"]
        X.append([
            pose["peak_displacement_px"] if pose else 0.0,
            0.0 if pose else 1.0,
            audio["peak_amplitude"],
            audio["rise_time_s"],
            xclip["p_swinging"],
        ])
        y.append(1 if d["label"] == "real_action" else 0)
        ids.append(d["id"])
    return np.array(X, dtype=float), np.array(y, dtype=int), ids


def make_model():
    # sklearn's default L2 penalty, deliberate not incidental given the
    # modest events-per-feature ratio here (39 positives / 5 features);
    # inputs are standardized per-fold so the penalty applies comparably
    # across features of very different natural scales (pixel
    # displacement vs. a 0-1 probability).
    return LogisticRegression(max_iter=1000)


def loo_cv_auc(X, y):
    loo = LeaveOneOut()
    probs = np.zeros(len(y), dtype=float)
    for train_idx, test_idx in loo.split(X):
        scaler = StandardScaler().fit(X[train_idx])
        model = make_model().fit(scaler.transform(X[train_idx]), y[train_idx])
        probs[test_idx] = model.predict_proba(scaler.transform(X[test_idx]))[:, 1]
    return roc_auc_score(y, probs), probs


def permutation_test(X, y, n_perm=N_PERMUTATIONS, seed=0):
    real_auc, real_probs = loo_cv_auc(X, y)
    rng = np.random.default_rng(seed)
    null_aucs = np.zeros(n_perm)
    for i in range(n_perm):
        y_perm = rng.permutation(y)
        null_aucs[i], _ = loo_cv_auc(X, y_perm)
    p = (np.sum(null_aucs >= real_auc) + 1) / (n_perm + 1)
    return real_auc, real_probs, null_aucs, p


def recall_risk_check(X, y):
    """Fits on the FULL real labeled dataset (no held-out fold needed --
    this is a downstream application check against real, separate
    reference clips, not a fresh CV estimate) and applies it to the
    REQUIRED real events of the six fragile clips."""
    from pipeline.detection import DetectionConfig, detect_persons
    from pipeline.fusion import PlateZone
    from pipeline.pose import build_landmarker, wrist_displacement
    from pipeline.audio import onset_features
    from pipeline.xclip import build_xclip, swing_probability

    scaler = StandardScaler().fit(X)
    model = make_model().fit(scaler.transform(X), y)

    xclip_model = build_xclip()
    landmarker = build_landmarker()

    calib_file = CLIPS_DIR / "calibration.json"
    default_zone = None
    if calib_file.exists():
        c = json.loads(calib_file.read_text())
        default_zone = PlateZone(center_xy=tuple(c["plate_xy"]), radius_px=c["zone_radius_px"])

    rows = []
    for clip_name in FRAGILE_CLIPS:
        truth = json.loads((GROUND_TRUTH_DIR / f"{clip_name}.json").read_text())
        clip_path = CLIPS_DIR / truth["clip"]
        if not clip_path.exists():
            print(f"[skip] {clip_name}: {clip_path} not found")
            continue

        zone = default_zone
        override = CLIPS_DIR / f"{clip_name}.calibration.json"
        if override.exists():
            c = json.loads(override.read_text())
            zone = PlateZone(center_xy=tuple(c["plate_xy"]), radius_px=c["zone_radius_px"])

        det = detect_persons(str(clip_path), DetectionConfig(), cache_dir=str(CACHE_DIR))

        for e in truth["events"]:
            if not e.get("required"):
                continue
            center_s = sum(e["window"]) / 2.0
            pose = wrist_displacement(clip_path, det.times, det.boxes, zone,
                                      center_s, landmarker=landmarker)
            audio = onset_features(str(clip_path), center_s)
            xclip = swing_probability(str(clip_path), center_s, xclip_model)
            x = np.array([[
                pose["peak_displacement_px"] if pose else 0.0,
                0.0 if pose else 1.0,
                audio["peak_amplitude"],
                audio["rise_time_s"],
                xclip["p_swinging"],
            ]])
            prob = model.predict_proba(scaler.transform(x))[0, 1]
            rows.append({
                "clip": clip_name, "event_id": e["id"], "type": e["type"],
                "window": e["window"], "center_s": center_s,
                "p_real_action": float(prob),
                "predicted": "real_action" if prob >= 0.5 else "downtime",
                "pose_available": pose is not None,
            })
    return rows


def main():
    X, y, ids = load_dataset()
    n = len(y)
    n_pos = int(y.sum())
    print(f"real dataset: n={n} (excluding the 1 control sample), "
         f"real_action={n_pos}, downtime={n - n_pos}")
    print(f"features: {FEATURE_NAMES}")
    print(f"pose available for {int((X[:, 1] == 0).sum())}/{n} records "
         f"({int((X[:, 1] == 1).sum())} missing)")
    print()

    t0 = time.time()
    real_auc, real_probs, null_aucs, p = permutation_test(X, y)
    print(f"LOO-CV AUC: {real_auc:.3f}")
    print(f"permutation null (n={N_PERMUTATIONS}): mean={null_aucs.mean():.3f} "
         f"sd={null_aucs.std():.3f} max={null_aucs.max():.3f}")
    print(f"permutation p-value: {p:.4f}")
    print(f"(took {time.time()-t0:.1f}s)")
    print()

    # majority-class baseline, same honest-calibration habit used
    # throughout this project -- a real AUC alone doesn't establish the
    # model beats the trivial "always guess the majority class" rule at
    # a real operating threshold.
    majority_acc = max(n_pos, n - n_pos) / n
    pred_labels = (real_probs >= 0.5).astype(int)
    model_acc = (pred_labels == y).mean()
    print(f"majority-class baseline accuracy: {majority_acc:.3f}")
    print(f"LOO-CV model accuracy @ 0.5 threshold: {model_acc:.3f}")
    print()

    print("=" * 70)
    print("RECALL-RISK CHECK: fragile clips' required real events")
    print("=" * 70)
    rows = recall_risk_check(X, y)
    any_miss = False
    for r in rows:
        flag = ""
        if r["predicted"] == "downtime":
            flag = "  *** WOULD MISCLASSIFY AS DOWNTIME ***"
            any_miss = True
        pose_note = "" if r["pose_available"] else " (no pose signal)"
        print(f"{r['clip']:<12} {r['event_id']:<4} {r['type']:<22} "
             f"window={r['window']} p(real_action)={r['p_real_action']:.3f} "
             f"-> {r['predicted']}{pose_note}{flag}")
    print()
    if any_miss:
        print("REAL RECALL RISK FOUND -- see *** markers above")
    else:
        print("No recall risk found: every required real event on the six "
             "fragile clips scores as real_action.")


if __name__ == "__main__":
    main()
