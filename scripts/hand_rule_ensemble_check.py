"""Tests simple, hand-specified AND/OR rules over already-characterized
weak signals (motion, mound-region windup pose, audio onset sharpness),
as a structurally different alternative to the collapsed trained
logistic-regression ensemble (0.100 AUC on the corrected sample, see
2026-08-23 joint-classifier recheck) -- a hand-specified rule has no
coefficients to fit or flip sign fold to fold, so it can't fail via that
SPECIFIC mechanism. It can still fail if the underlying signals
themselves don't carry real information, which is the real, live
possibility this script actually tests, not assumes away.

Real caveat carried in explicitly, not glossed over: every signal here
is already independently characterized as weak or actively
ANTI-correlated on this footage --
  - windup-pose (mound-region MediaPipe): succeeds MORE often during
    downtime (75.0%, was 66.7% pre-correction) than during real pitches
    (42.1%, was 44.4%) -- a static idle person gives MediaPipe an
    easier single-frame target than a pitcher's fast, blurring windup.
    See the 2026-08-14 windup-to-release investigation.
  - audio: no usable crowd/contact signal confirmed on this footage
    (AST crowd-reaction AUC 0.414, HFC/spectral-flux recall-risk
    fragile even where nominally significant -- see tonight's rechecks).
  - motion alone: not swing-selective (see zone-velocity gate closure).
A rule built from already-weak parts may still fail. This is a
structurally different test, not a guarantee the parts have improved.

Sample: the 27-instant hand-verified set from the 2026-08-14 windup-to-
release investigation (19 real / 8 negative, post-clip_60#e6-correction
counts). That investigation was explicitly "lived in an agent
scratchpad, not committed" -- no script or full instant list was ever
committed. Reconstructed here from the doc's own prose (exact clip/
event/instant citations) plus, for the 5 cases whose exact instant was
never individually recorded in prose (clip_540#e2/e3/e4, clip_60#e4/e5),
FRESH frame-level verification this session (ffmpeg frame pulls,
visually confirmed to show a real windup/pre-contact stance, or in one
case -- clip_60#e4 -- resolved a real discrepancy: the naive t=121.0
implied by a documented crop filename showed what looked like an
in-progress swing on inspection, contradicting that event's own
ground-truth note ("no swing"); t=120/123 both cleanly show a loaded,
non-swinging stance instead, so t=123.0 was used). Flagged plainly:
these 5 are this session's own verified picks, not a recovered
byte-identical match to the original investigation's own choices. The
10 training_data/reviews-sourced cases use each record's own exact
window.start_s/end_s from its JSON (source of truth, not re-derived).

Signal definitions, each reusing an already-established piece of this
codebase rather than inventing a new threshold to fit this test:
  - motion low: pipeline.motion peak score within +/-1.0s of the
    instant, compared against HardCutConfig.quiet_thresh (0.002) --
    the single strictest, most extensively safety-verified "quiet" bar
    already shipped in this project (see the 2026-08-23 threshold-sweep
    closure), not a new number chosen to make this test look good.
  - pose detected: single-frame MediaPipe PoseLandmarker check on the
    FIXED mound region (MOUND_X=(900,1150), MOUND_Y=(485,660), the same
    region independently re-derived in the original windup-to-release
    investigation), reusing pipeline.pose's own _padded_crop_bounds/
    build_landmarker construction. "Detected" = any pose_landmarks
    returned at all, matching the original single-frame-hit-rate
    methodology exactly.
  - audio transient: pipeline.audio.onset_features's rise_time_s <=
    0.030s (30ms) -- reasoned from pipeline/audio.py's own docstring
    ("typical bat-ball contact rises over single-digit to low-tens of
    ms"), not fit to this sample.

Three rules tested, chosen by reasoning about the known anti-
correlation, not a grid search over variants:
  1. motion low AND no pose detected -> predict downtime (the naive
     AND rule as specified)
  2. motion low AND no pose detected AND no audio transient -> predict
     downtime (adds the third already-weak signal)
  3. motion low alone -> predict downtime (baseline: isolates whether
     AND-ing in the anti-correlated pose signal helps or hurts relative
     to using motion by itself)

Usage:
    venv/bin/python scripts/hand_rule_ensemble_check.py
"""

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.audio import onset_features
from pipeline.motion import compute_motion
from pipeline.pose import _padded_crop_bounds, build_landmarker
from pipeline.segments import HardCutConfig

CLIPS_DIR = ROOT / "reference_clips"
MOUND_BOX = (900, 485, 1150, 660)  # MOUND_X=(900,1150), MOUND_Y=(485,660)
MOTION_HALF_WIN_S = 1.0
AUDIO_TRANSIENT_THRESH_S = 0.030
QUIET_THRESH = HardCutConfig().quiet_thresh  # 0.002, the shipped bar

# (video_path relative to CLIPS_DIR's parent, instant_s, tag)
REAL_CASES = [
    ("clip_300.mkv", 59.0, "clip_300#e2a"),
    ("clip_300.mkv", 60.5, "clip_300#e2b"),
    ("clip_300.mkv", 120.5, "clip_300#e4a"),
    ("clip_300.mkv", 123.0, "clip_300#e4b"),
    ("clip_300.mkv", 96.0, "clip_300#e6"),
    ("clip_540.mkv", 45.0, "clip_540#e2 [session pick, frame-verified]"),
    ("clip_540.mkv", 81.0, "clip_540#e3"),
    ("clip_540.mkv", 180.0, "clip_540#e4 [session pick, frame-verified]"),
    ("clip_60.mkv", 123.0, "clip_60#e4 [session pick, frame-verified, resolves a documented-crop discrepancy]"),
    ("clip_60.mkv", 137.0, "clip_60#e5 [session pick, frame-verified]"),
    ("clip_60.mkv", 171.5, "clip_60#e6"),
    ("clip_foul1.mkv", 10.0, "clip_foul1#e1"),
    ("clip_whiff1.mkv", 10.0, "clip_whiff1#e1"),
    ("full_game.mkv", 3152.419654335095, "bc_2e636269472c"),
    ("full_game.mkv", 3646.6633338094557, "bc_38346e7bd32e"),
    ("full_game.mkv", 61.31124125124982, "bc_dc24666af1b5"),
    ("full_game.mkv", 452.6396229110127, "bc_ed7054ea5521"),
    ("full_game.mkv", (770.227181831167 + 770.5827024710755) / 2, "hc_09b6abe617f8"),
    ("full_game.mkv", (519.4558634480788 + 519.9156549064419) / 2, "hc_5f2905bcab76"),
]

NEG_CASES = [
    ("clip_300.mkv", 5.0, "clip_300 stretch a"),
    ("clip_300.mkv", 10.0, "clip_300 stretch b"),
    ("clip_60.mkv", 30.0, "clip_60#e2 warmup a"),
    ("clip_60.mkv", 40.0, "clip_60#e2 warmup b"),
    ("full_game.mkv", 261.5112126839023, "bc_121f61ed3d15"),
    ("full_game.mkv", 1485.7548921582631, "bc_374fa9fd1fa4"),
    ("full_game.mkv", (1218.3831595486358 + 1218.7386801885445) / 2, "hc_081647af24b4"),
    ("full_game.mkv", (1178.1346236251964 + 1178.281602628196) / 2, "hc_2677d77a90c4"),
]


def get_frame_at(cap, t, fps):
    cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
    ok, frame = cap.read()
    return frame if ok else None


def mound_pose_detected(video_path, t, landmarker):
    cap = cv2.VideoCapture(str(video_path))
    frame_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    frame_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame = get_frame_at(cap, t, cap.get(cv2.CAP_PROP_FPS) or 30.0)
    cap.release()
    if frame is None:
        return None
    x0, y0, x1, y1 = _padded_crop_bounds(MOUND_BOX, frame_w, frame_h, margin_frac=0.3)
    if x1 <= x0 or y1 <= y0:
        return None
    crop = frame[y0:y1, x0:x1]
    rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
    import mediapipe as mp
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))
    result = landmarker.detect(mp_image)
    return bool(result.pose_landmarks)


def motion_peak(motion_times, motion_scores, t):
    idx = (motion_times >= t - MOTION_HALF_WIN_S) & (motion_times <= t + MOTION_HALF_WIN_S)
    if not idx.any():
        return None
    return float(motion_scores[idx].max())


def audio_transient(video_path, t, audio_cache):
    r = onset_features(str(video_path), t, envelope_cache=audio_cache)
    if r is None:
        return None
    return r["rise_time_s"] <= AUDIO_TRANSIENT_THRESH_S


def main():
    print(f"Sample: {len(REAL_CASES)} real / {len(NEG_CASES)} negative "
          f"(27-instant hand-verified set)")
    print(f"quiet_thresh (motion low bar, from shipped HardCutConfig): {QUIET_THRESH}")
    print(f"audio transient bar: rise_time_s <= {AUDIO_TRANSIENT_THRESH_S}s\n")

    landmarker = build_landmarker()
    motion_cache = {}
    audio_cache = {}
    rows = []

    try:
        for label, cases in (("real", REAL_CASES), ("negative", NEG_CASES)):
            for fname, t, tag in cases:
                video_path = CLIPS_DIR / fname
                if fname not in motion_cache:
                    m = compute_motion(str(video_path))
                    motion_cache[fname] = (m.times, m.scores)
                mt, ms = motion_cache[fname]

                mpeak = motion_peak(mt, ms, t)
                mlow = (mpeak < QUIET_THRESH) if mpeak is not None else None
                pose = mound_pose_detected(video_path, t, landmarker)
                audio = audio_transient(video_path, t, audio_cache)
                rows.append({"label": label, "tag": tag, "video": fname, "t": t,
                            "motion_peak": mpeak, "motion_low": mlow,
                            "pose_detected": pose, "audio_transient": audio})
                print(f"[{label:<8}] {tag:<55} t={t:>10.2f}  motion_peak={mpeak}  "
                     f"motion_low={mlow}  pose_detected={pose}  audio_transient={audio}")
    finally:
        landmarker.close()

    # drop any row missing a needed signal (treat as unusable, don't guess)
    complete = [r for r in rows if r["motion_low"] is not None and r["pose_detected"] is not None]
    print(f"\n{len(complete)}/{len(rows)} rows have usable motion+pose signals "
          f"(audio dropped separately per-rule if missing)")

    def eval_rule(name, predict_downtime_fn, need_audio=False):
        usable = [r for r in complete if (not need_audio or r["audio_transient"] is not None)]
        tp = fp = tn = fn = 0  # positive class = "downtime"
        for r in usable:
            pred_downtime = predict_downtime_fn(r)
            actual_downtime = r["label"] == "negative"
            if pred_downtime and actual_downtime:
                tp += 1
            elif pred_downtime and not actual_downtime:
                fp += 1  # REAL PLAY WRONGLY CALLED DOWNTIME -- the dangerous case
            elif not pred_downtime and actual_downtime:
                fn += 1
            else:
                tn += 1
        n = tp + fp + tn + fn
        accuracy = (tp + tn) / n if n else float("nan")
        precision_downtime = tp / (tp + fp) if (tp + fp) else float("nan")
        recall_downtime = tp / (tp + fn) if (tp + fn) else float("nan")
        recall_real = tn / (tn + fp) if (tn + fp) else float("nan")  # TNR
        balanced_acc = (recall_downtime + recall_real) / 2
        print(f"\n=== {name} ===")
        print(f"  n={n} (usable subset)")
        print(f"  confusion: TP(correct downtime)={tp} FP(REAL PLAY LOST)={fp} "
             f"TN(correct real)={tn} FN(missed downtime)={fn}")
        print(f"  accuracy={accuracy:.3f}  precision(downtime)={precision_downtime:.3f}  "
             f"recall(downtime)={recall_downtime:.3f}")
        print(f"  recall(real, i.e. real-play-safety rate)={recall_real:.3f}")
        print(f"  balanced-accuracy-equivalent AUC (comparable scale to X-CLIP's "
             f"continuous AUC numbers)={balanced_acc:.3f}")
        return balanced_acc, fp

    results = {}
    results["Rule 1: motion_low AND NOT pose_detected"] = eval_rule(
        "Rule 1: motion_low AND NOT pose_detected",
        lambda r: r["motion_low"] and not r["pose_detected"])

    results["Rule 2: motion_low AND NOT pose_detected AND NOT audio_transient"] = eval_rule(
        "Rule 2: motion_low AND NOT pose_detected AND NOT audio_transient",
        lambda r: r["motion_low"] and not r["pose_detected"] and not r["audio_transient"],
        need_audio=True)

    results["Rule 3 (baseline): motion_low alone"] = eval_rule(
        "Rule 3 (baseline): motion_low alone",
        lambda r: r["motion_low"])

    print("\n=== summary ===")
    for name, (bal_acc, fp) in results.items():
        print(f"  {name}: balanced-acc-AUC={bal_acc:.3f}, real-plays-lost={fp}")

    # --- diagnostic only, NOT a proposed rule: the shipped 0.002 quiet_thresh
    # fires on almost nothing in this +/-1.0s point-instant sample (1/27),
    # making rules 1/2 degenerate ("never predict downtime" regardless of
    # what pose says) -- which trivially avoids real-play loss but can't
    # actually answer whether AND-ing in the anti-correlated pose signal
    # helps or hurts, the specific question this task asked. Median-split
    # this SAME sample's own motion scores purely to unstick that and get a
    # real answer -- flagged explicitly as diagnostic, not a candidate
    # threshold, since fitting a cutoff to this exact 27-point sample and
    # then evaluating on the same sample would be the same small-sample
    # overclaim risk this project has flagged everywhere else tonight. ---
    print("\n=== diagnostic only (not a candidate rule): does AND-ing in pose "
          "help or hurt once the motion gate isn't saturating everything to "
          "False? median-split on this sample's OWN motion scores ===")
    peaks = [r["motion_peak"] for r in complete if r["motion_peak"] is not None]
    median_peak = float(np.median(peaks))
    print(f"  median motion_peak across the 27-instant sample: {median_peak:.5f} "
         f"(for comparison, shipped quiet_thresh: {QUIET_THRESH})")
    for r in complete:
        r["motion_low_relaxed"] = r["motion_peak"] < median_peak

    def eval_relaxed(name, fn):
        tp = fp = tn = fn_ = 0
        for r in complete:
            pred = fn(r)
            actual = r["label"] == "negative"
            if pred and actual:
                tp += 1
            elif pred and not actual:
                fp += 1
            elif not pred and actual:
                fn_ += 1
            else:
                tn += 1
        n = tp + fp + tn + fn_
        bal_acc = 0.5 * (tp / (tp + fn_) if (tp + fn_) else 0) + 0.5 * (tn / (tn + fp) if (tn + fp) else 0)
        print(f"  {name}: TP={tp} FP(real lost)={fp} TN={tn} FN={fn_}  "
             f"balanced-acc-AUC={bal_acc:.3f}")
        return bal_acc

    bal_motion_only = eval_relaxed("motion_low_relaxed alone",
                                   lambda r: r["motion_low_relaxed"])
    bal_motion_and_pose = eval_relaxed("motion_low_relaxed AND NOT pose_detected",
                                       lambda r: r["motion_low_relaxed"] and not r["pose_detected"])
    delta = bal_motion_and_pose - bal_motion_only
    direction = "HELPS" if delta > 0.005 else ("HURTS" if delta < -0.005 else "no meaningful change")
    print(f"  AND-ing in NOT pose_detected vs. motion alone: delta={delta:+.3f} ({direction})")

    print(f"\nComparison: corrected X-CLIP zero-shot baseline AUC 0.653 (its own "
          f"original sample) / 0.428 (this SAME independent 27-instant sample, "
          f"already established, below chance).")


if __name__ == "__main__":
    main()
