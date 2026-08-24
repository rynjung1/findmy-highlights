"""Re-verifies the X-CLIP raw-embedding nearest-centroid probe (originally
documented AUC 0.587, not significant at p=0.17 even on its own
pre-correction sample) against the corrected ground truth (clip_60#e6
relabel, commits 73f45a9/10ff7ff) -- another open debt item named in
docs/INVESTIGATION_LOG.md's 2026-08-16 consolidated correction (no
committed script existed for it before this one).

Reuses scripts/pose_audio_validation.py's load_real_events()/
load_ambient_samples() directly (same loaders every other X-CLIP recheck
tonight used), so this automatically picks up the corrected clip_60#e6
label -- 12 real / 167 ambient, matching the main 0.690 -> 0.6527
correction's sample exactly.

Methodology, reconstructed from the original prose (2026-08-13 zero-shot
X-CLIP writeup): "Leave-one-out nearest-centroid cosine similarity on
the 512-dim pooled video embedding" -- X-CLIP's raw get_video_features()
pooler_output (no text prompts involved at all, unlike the zero-shot
probe or the prompt-variant table), scored per point as
cos_sim(embedding, LOO real-centroid) - cos_sim(embedding, LOO
ambient-centroid). LOO means each point's own class centroid excludes
itself; the OTHER class's centroid uses every member of that class
(the point isn't in it, nothing to exclude). This is the standard
nearest-centroid construction and matches the "different LOO discipline
applied this time" note the 2026-08-13 fusion investigation's own
0.579 video-alone number already flagged as a real, not identical,
consistency check against this exact 0.587 figure.

Also reproduces the zero-shot text-prompt baseline (0.6527) from the
same cached video embeddings as a sanity check before trusting the new
number -- same discipline scripts/prompt_variant_recheck.py used.

Usage:
    venv/bin/python scripts/embedding_centroid_recheck.py
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pose_audio_validation import auc, load_ambient_samples, load_real_events
from pipeline.xclip import DEFAULT_MODEL_NAME, XClipConfig, POS_PROMPT, NEG_PROMPT, _extract_frames

CLIPS_DIR = ROOT / "reference_clips"
SEED = 20260816  # same seed as every other X-CLIP recheck tonight
N_PERM = 2000


def permutation_test(real_scores, ambient_scores, n_perm=N_PERM, seed=SEED):
    rng = np.random.default_rng(seed)
    observed = auc(real_scores, ambient_scores)
    pooled = np.array(real_scores + ambient_scores)
    n_real = len(real_scores)
    count = 0
    for _ in range(n_perm):
        idx = rng.permutation(len(pooled))
        shuffled = pooled[idx]
        r, a = shuffled[:n_real].tolist(), shuffled[n_real:].tolist()
        if auc(r, a) >= observed:
            count += 1
    return observed, count / n_perm


def mannwhitney_p(real_scores, ambient_scores):
    from scipy.stats import mannwhitneyu
    stat, p = mannwhitneyu(real_scores, ambient_scores, alternative="greater")
    return stat, p


def loo_nearest_centroid_scores(real_embeds, ambient_embeds):
    """score_i = cos_sim(x_i, LOO real-centroid) - cos_sim(x_i, LOO ambient-centroid).
    real_embeds/ambient_embeds: lists of unit-normalized 1-D numpy arrays."""
    real_arr = np.stack(real_embeds)
    ambient_arr = np.stack(ambient_embeds)
    real_sum = real_arr.sum(axis=0)
    ambient_sum = ambient_arr.sum(axis=0)

    def unit(v):
        n = np.linalg.norm(v)
        return v / n if n > 0 else v

    real_scores = []
    for i, x in enumerate(real_embeds):
        real_centroid = unit((real_sum - x) / (len(real_embeds) - 1))
        ambient_centroid = unit(ambient_sum / len(ambient_embeds))
        real_scores.append(float(x @ real_centroid - x @ ambient_centroid))

    ambient_scores = []
    for i, x in enumerate(ambient_embeds):
        ambient_centroid = unit((ambient_sum - x) / (len(ambient_embeds) - 1))
        real_centroid = unit(real_sum / len(real_embeds))
        ambient_scores.append(float(x @ real_centroid - x @ ambient_centroid))

    return real_scores, ambient_scores


def main():
    real_events = load_real_events()
    ambient_samples = load_ambient_samples()
    print(f"Real swing-type ground-truth events: {len(real_events)}")
    print(f"Ambient (real gap) samples: {len(ambient_samples)}\n")

    import torch
    from transformers import AutoModel, AutoProcessor

    model = AutoModel.from_pretrained(DEFAULT_MODEL_NAME).eval()
    processor = AutoProcessor.from_pretrained(DEFAULT_MODEL_NAME)
    logit_scale = model.logit_scale.exp().item()
    cfg = XClipConfig()

    real_items = [(c, t) for c, t, *_ in real_events]
    ambient_items = ambient_samples

    print("Computing video embeddings (once per window)...")
    video_embeds = {}
    all_items = [(c, t) for c, t in real_items] + [(c, t) for c, t in ambient_items]
    for i, (clip_stem, t) in enumerate(all_items):
        if (clip_stem, t) in video_embeds:
            continue
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        frames = _extract_frames(clip_path, t, cfg.n_frames, cfg.window_s)
        if len(frames) < 3:
            continue
        inputs = processor(videos=frames, return_tensors="pt")
        with torch.no_grad():
            v = model.get_video_features(pixel_values=inputs["pixel_values"]).pooler_output
            v = v / v.norm(dim=-1, keepdim=True)
        video_embeds[(clip_stem, t)] = v[0].numpy()
        if (i + 1) % 30 == 0:
            print(f"  {i + 1}/{len(all_items)} windows embedded")
    print(f"Done. {len(video_embeds)} unique windows embedded.\n")

    real_embeds = [video_embeds[k] for k in real_items if k in video_embeds]
    ambient_embeds = [video_embeds[k] for k in ambient_items if k in video_embeds]
    print(f"Real embeddings: {len(real_embeds)}, ambient embeddings: {len(ambient_embeds)}\n")

    # --- sanity check: reproduce the zero-shot text-prompt baseline (0.6527)
    # from these SAME cached video embeddings, before trusting anything new ---
    with torch.no_grad():
        text_inputs = processor(text=[POS_PROMPT, NEG_PROMPT], return_tensors="pt", padding=True)
        text_embeds = model.get_text_features(**text_inputs).pooler_output
        text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)
        text_embeds = text_embeds.numpy()

    def zeroshot_score(x):
        logits = logit_scale * (x @ text_embeds.T)
        e = np.exp(logits - logits.max())
        probs = e / e.sum()
        return float(probs[0])

    zs_real = [zeroshot_score(x) for x in real_embeds]
    zs_ambient = [zeroshot_score(x) for x in ambient_embeds]
    zs_auc = auc(zs_real, zs_ambient)
    print(f"SANITY CHECK -- zero-shot text-prompt baseline reproduced from these "
          f"embeddings: AUC = {zs_auc:.4f} (documented corrected value: 0.6527)\n")

    # --- the actual probe: LOO nearest-centroid cosine similarity ---
    nc_real, nc_ambient = loo_nearest_centroid_scores(real_embeds, ambient_embeds)
    nc_auc, nc_perm_p = permutation_test(nc_real, nc_ambient)
    _, nc_mwu_p = mannwhitney_p(nc_real, nc_ambient)

    print("=== LOO nearest-centroid cosine similarity, 512-dim pooled video embedding ===")
    print(f"AUC: {nc_auc:.4f}")
    print(f"Permutation p (2000 shuffles, seed {SEED}): {nc_perm_p:.4f}")
    print(f"Mann-Whitney p: {nc_mwu_p:.4f}")
    print(f"n: {len(nc_real)} real / {len(nc_ambient)} ambient")
    print(f"\nOriginal (pre-correction, n=11/170): AUC 0.587, permutation p~0.17 (not significant)")


if __name__ == "__main__":
    main()
