"""Re-verifies the 2026-08-13 embedding-level (X-CLIP + AST) audio+visual
fusion investigation against the corrected ground truth (clip_60#e6
relabel, commits 73f45a9/10ff7ff) -- confirmed via direct doc check (not
assumed) to be the last of the five items the 2026-08-16 consolidated
correction flagged as resting on the pre-correction 11-event set and NOT
yet rebuilt (the other four -- prompt-variant table, raw-embedding
nearest-centroid probe, HFC/spectral-flux, joint classifier -- were each
rechecked in earlier sessions tonight).

Reuses scripts/pose_audio_validation.py's load_real_events()/
load_ambient_samples() directly, same loaders every recheck tonight
used -- picks up the corrected clip_60#e6 label automatically (12 real,
up to 167-170 ambient depending on which embeddings succeed per sample).

Methodology, reconstructed from the original prose (2026-08-13 embedding
fusion writeup) -- no committed script existed for this investigation
before this one:
  - Video: X-CLIP `get_video_features(...).pooler_output`, L2-normalized,
    512-d, 2.0s window (same as every other X-CLIP recheck tonight).
  - Audio: `MIT/ast-finetuned-audioset-10-10-0.4593` (BSD-3-Clause,
    already license-verified in this project), pooled CLS output, 768-d,
    L2-normalized, matching X-CLIP's 2.0s window convention.
  - Fusion: concatenation (1280-d) -- the only method the original
    investigation used; cross-attention was considered and explicitly
    rejected as a strictly-worse overfitting risk at this sample size,
    a decision this recheck doesn't revisit.
  - Two scoring methods per feature set (video alone / audio alone /
    concatenated), matching the original 5-row table: LOO nearest-
    centroid (parameter-free) and LOO L2-regularized logistic regression
    at C in {0.001, 0.01, 0.1, 1.0}.

Real permutation test (2000 shuffles, seed 20260816, same standard as
every recheck tonight) on every one of the 15 resulting numbers, not
just the headline ones -- a fast vectorized AUC (rank-based, equivalent
to pose_audio_validation.auc's pairwise definition, verified against it
below) makes 15 x 2000 shuffles cheap.

Usage:
    venv/bin/python scripts/embedding_fusion_recheck.py
"""

import sys
from pathlib import Path

import numpy as np
from scipy.optimize import minimize
from scipy.stats import rankdata

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pose_audio_validation import auc, load_ambient_samples, load_real_events
from pipeline.xclip import DEFAULT_MODEL_NAME, XClipConfig, _extract_frames
from pipeline.audio import load_audio

CLIPS_DIR = ROOT / "reference_clips"
SEED = 20260816
N_PERM = 2000
AST_MODEL_NAME = "MIT/ast-finetuned-audioset-10-10-0.4593"
AUDIO_WINDOW_S = 2.0  # matches X-CLIP's window_s convention
AUDIO_SAMPLE_RATE = 16000


def fast_auc(real_scores, ambient_scores):
    """Rank-based AUC, ties averaged -- verified equivalent to
    pose_audio_validation.auc's pairwise 0.5-credit-on-ties definition."""
    real = np.asarray(real_scores, dtype=float)
    ambient = np.asarray(ambient_scores, dtype=float)
    n_r, n_a = len(real), len(ambient)
    ranks = rankdata(np.concatenate([real, ambient]))
    u = ranks[:n_r].sum() - n_r * (n_r + 1) / 2.0
    return u / (n_r * n_a)


def permutation_test(real_scores, ambient_scores, n_perm=N_PERM, seed=SEED):
    rng = np.random.default_rng(seed)
    observed = fast_auc(real_scores, ambient_scores)
    pooled = np.concatenate([np.asarray(real_scores), np.asarray(ambient_scores)])
    n_real = len(real_scores)
    count = 0
    for _ in range(n_perm):
        shuffled = pooled[rng.permutation(len(pooled))]
        if fast_auc(shuffled[:n_real], shuffled[n_real:]) >= observed:
            count += 1
    return observed, count / n_perm


def loo_nearest_centroid(X, y):
    real_idx = np.where(y == 1)[0]
    amb_idx = np.where(y == 0)[0]
    real_arr, amb_arr = X[real_idx], X[amb_idx]
    real_sum, amb_sum = real_arr.sum(axis=0), amb_arr.sum(axis=0)

    def unit(v):
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    real_scores = [float(x @ unit((real_sum - x) / (len(real_arr) - 1)) -
                        x @ unit(amb_sum / len(amb_arr))) for x in real_arr]
    amb_scores = [float(x @ unit(real_sum / len(real_arr)) -
                       x @ unit((amb_sum - x) / (len(amb_arr) - 1))) for x in amb_arr]
    return real_scores, amb_scores


def zscore_fit(X):
    mean, std = X.mean(axis=0), X.std(axis=0)
    std[std == 0] = 1.0
    return mean, std


def logistic_fit(X, y, l2):
    n, d = X.shape
    Xb = np.hstack([X, np.ones((n, 1))])

    def nll(w):
        z = Xb @ w
        loss = np.logaddexp(0, -z) * y + np.logaddexp(0, z) * (1 - y)
        return loss.sum() + l2 * np.sum(w[:-1] ** 2)

    return minimize(nll, np.zeros(d + 1), method="L-BFGS-B").x


def loo_logistic(X, y, l2):
    n = len(y)
    scores = np.zeros(n)
    for i in range(n):
        train = np.array([j for j in range(n) if j != i])
        mean, std = zscore_fit(X[train])
        w = logistic_fit((X[train] - mean) / std, y[train], l2)
        xb = np.concatenate([(X[i] - mean) / std, [1.0]])
        scores[i] = 1.0 / (1.0 + np.exp(-(xb @ w)))
    return list(scores[y == 1]), list(scores[y == 0])


def main():
    real_events = load_real_events()
    ambient_samples = load_ambient_samples()
    print(f"Real swing-type ground-truth events: {len(real_events)}")
    print(f"Ambient (real gap) samples: {len(ambient_samples)}\n")

    # sanity: fast_auc matches the canonical pairwise auc()
    r_test, a_test = [0.9, 0.3, 0.6], [0.5, 0.2, 0.6, 0.1]
    assert abs(fast_auc(r_test, a_test) - auc(r_test, a_test)) < 1e-9, "fast_auc mismatch"

    import torch
    from transformers import AutoModel as AutoModelXC, AutoProcessor as AutoProcessorXC
    from transformers import AutoFeatureExtractor, ASTModel

    xc_model = AutoModelXC.from_pretrained(DEFAULT_MODEL_NAME).eval()
    xc_processor = AutoProcessorXC.from_pretrained(DEFAULT_MODEL_NAME)
    cfg = XClipConfig()

    ast_extractor = AutoFeatureExtractor.from_pretrained(AST_MODEL_NAME)
    ast_model = ASTModel.from_pretrained(AST_MODEL_NAME).eval()

    real_items = [(c, t) for c, t, *_ in real_events]
    ambient_items = ambient_samples
    all_items = real_items + ambient_items

    print("Computing X-CLIP video + AST audio embeddings (once per window)...")
    video_embeds, audio_embeds, audio_cache = {}, {}, {}
    for i, (clip_stem, t) in enumerate(all_items):
        if (clip_stem, t) in video_embeds:
            continue
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue

        frames = _extract_frames(clip_path, t, cfg.n_frames, cfg.window_s)
        if len(frames) >= 3:
            inputs = xc_processor(videos=frames, return_tensors="pt")
            with torch.no_grad():
                v = xc_model.get_video_features(pixel_values=inputs["pixel_values"]).pooler_output
                v = v / v.norm(dim=-1, keepdim=True)
            video_embeds[(clip_stem, t)] = v[0].numpy()

        if clip_stem not in audio_cache:
            audio_cache[clip_stem] = load_audio(str(clip_path), AUDIO_SAMPLE_RATE)
        audio = audio_cache[clip_stem]
        half = AUDIO_WINDOW_S / 2
        start = max(0, int((t - half) * AUDIO_SAMPLE_RATE))
        end = min(len(audio), int((t + half) * AUDIO_SAMPLE_RATE))
        window = audio[start:end]
        if len(window) >= int(0.5 * AUDIO_SAMPLE_RATE):  # need at least 0.5s of real audio
            inputs = ast_extractor(window, sampling_rate=AUDIO_SAMPLE_RATE, return_tensors="pt")
            with torch.no_grad():
                out = ast_model(**inputs)
                a = out.pooler_output[0].numpy()
                a = a / np.linalg.norm(a)
            audio_embeds[(clip_stem, t)] = a

        if (i + 1) % 30 == 0:
            print(f"  {i + 1}/{len(all_items)} windows processed")

    paired = [k for k in all_items if k in video_embeds and k in audio_embeds]
    real_paired = [k for k in real_items if k in video_embeds and k in audio_embeds]
    amb_paired = [k for k in ambient_items if k in video_embeds and k in audio_embeds]
    print(f"\nFully-paired (both embeddings): {len(real_paired)} real, {len(amb_paired)} ambient\n")

    y = np.array([1] * len(real_paired) + [0] * len(amb_paired))
    video_X = np.stack([video_embeds[k] for k in real_paired + amb_paired])
    audio_X = np.stack([audio_embeds[k] for k in real_paired + amb_paired])
    concat_X = np.hstack([video_X, audio_X])

    feature_sets = {"video alone": video_X, "audio (AST) alone": audio_X, "concatenated": concat_X}
    rows = ["nearest-centroid (LOO)", "logistic C=0.001", "logistic C=0.01",
            "logistic C=0.1", "logistic C=1.0"]

    print(f"{'method':<24}" + "".join(f"{name:>20}" for name in feature_sets))
    print("-" * (24 + 20 * len(feature_sets)))

    all_results = {}
    for row in rows:
        line = f"{row:<24}"
        for fname, X in feature_sets.items():
            if row.startswith("nearest"):
                r, a = loo_nearest_centroid(X, y)
            else:
                l2 = float(row.split("C=")[1])
                r, a = loo_logistic(X, y, l2)
            obs, p = permutation_test(r, a)
            all_results[(row, fname)] = (obs, p, len(r), len(a))
            line += f"{obs:>14.4f}(p={p:.3f})"
        print(line)

    print(f"\nOriginal (pre-correction, n=11/167 paired):")
    print("  nearest-centroid: video 0.579, audio 0.285, concat 0.327")
    print("  logistic C=0.001: video 0.557, audio 0.194, concat 0.371")
    print("  logistic C=0.01:  video 0.698, audio 0.269, concat 0.453")
    print("  logistic C=0.1:   video 0.753, audio 0.292, concat 0.474")
    print("  logistic C=1.0:   video 0.762, audio 0.311, concat 0.491")
    print("  (flagged then as a near-certain overfitting artifact at C=1.0, "
          "512-1280 dims against 11 positives)")

    print(f"\nZero-shot text-prompt baseline for comparison: 0.6527 "
          f"(documented, corrected, from the main 2026-08-16 recheck).")

    # --- unlike the pre-correction table, EVERY C value for video-alone
    # logistic regression now scores 0.74-0.78, not just the weak-reg
    # (C=1.0) extreme flagged as an overfitting artifact before. Before
    # trusting that as a real finding, apply the same recall-risk check
    # every other X-CLIP-family result tonight has been held to. ---
    print("\n=== video-alone logistic (C=0.01) recall-risk check: "
          "clip_base1-4/foul1/whiff1 percentile-of-ambient held-out score ===")
    video_only_X = video_X
    r_scores, a_scores = loo_logistic(video_only_X, y, 0.01)
    real_score_by_key = dict(zip(real_paired, r_scores))
    amb_score_list = a_scores

    def percentile(score, pool):
        below = sum(1 for x in pool if x < score)
        return 100.0 * below / len(pool)

    six_clips = {"clip_base1", "clip_base2", "clip_base3", "clip_base4",
                 "clip_foul1", "clip_whiff1"}
    for clip_stem, t, eid, etype in real_events:
        if clip_stem not in six_clips:
            continue
        key = (clip_stem, t)
        if key not in real_score_by_key:
            print(f"  {clip_stem}/{eid} (t={t}): not in paired set (missing an embedding)")
            continue
        score = real_score_by_key[key]
        pct = percentile(score, amb_score_list)
        print(f"  {clip_stem}/{eid} (t={t}): held-out score={score:.4f}  "
             f"percentile-of-ambient={pct:.0f}%")


if __name__ == "__main__":
    main()
