"""Re-verifies the 5-prompt-variant X-CLIP robustness table (originally
documented as an AUC range of 0.620-0.690, no per-variant numbers ever
recorded, no committed script) against the corrected ground truth
(clip_60#e6 relabel, commits 73f45a9/10ff7ff) -- the same open debt item
named in docs/INVESTIGATION_LOG.md's 2026-08-16 consolidated correction.

Reuses scripts/pose_audio_validation.py's load_real_events()/
load_ambient_samples() directly (same loaders the main X-CLIP zero-shot
0.690 -> 0.6527 correction used), so this automatically picks up the
corrected clip_60#e6 label with no manual n-adjustment: load_real_events()
reads tests/ground_truth/*.json live and filters on SWING_TYPES, which
now includes clip_60#e6 (type changed at_bat_activity -> hit_and_run by
73f45a9). Real sample: 12 real / 167 ambient -- NOT 18/9 (that pair of
numbers belongs to a different investigation entirely, the windup-pose
mound-occlusion script, where the corrected counts are 19 real / 8
negative, not "stays 9").

Reconstructed prompt wordings (exact original strings were never
committed -- same "no committed script" gap already flagged for this
table in the 2026-08-16 correction). Built from the only descriptions
that survive in docs/INVESTIGATION_LOG.md (2026-08-13 zero-shot X-CLIP
writeup): "a 3-way split adding 'fielders actively making a play'", "a
more specific 'idle' phrasing", "a defensive-negative pair", and "'swing
vs. game-in-progress' framing". Flagged as a reconstruction, not a
byte-identical rerun, same honesty standard as the "0.688 (this
session's faithful same-methodology reconstruction)" line already in the
log for the main baseline.

Video embeddings computed ONCE per window and cached; only the text side
changes per variant (same real cost-saving property the original table
exploited -- "reusing the already-computed video embeddings ... costs
seconds, not minutes").

Usage:
    venv/bin/python scripts/prompt_variant_recheck.py
"""

import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pose_audio_validation import auc, load_ambient_samples, load_real_events
from pipeline.xclip import DEFAULT_MODEL_NAME, XClipConfig, _extract_frames

CLIPS_DIR = ROOT / "reference_clips"
SEED = 20260816  # same seed as the main X-CLIP significance recheck
N_PERM = 2000

VARIANTS = [
    ("baseline (original pair, sanity-check reproduction)",
     ["a baseball player swinging a bat", "baseball players standing idle"]),
    ("3-way split (+ fielders actively making a play)",
     ["a baseball player swinging a bat", "baseball players standing idle",
      "fielders actively making a play"]),
    ("more specific idle phrasing",
     ["a baseball player swinging a bat",
      "baseball players standing around doing nothing, no action happening"]),
    ("defensive-negative pair",
     ["a baseball player swinging a bat",
      "baseball fielders defending a play in the field"]),
    ("swing vs. game-in-progress framing",
     ["a baseball player swinging a bat", "a baseball game in progress"]),
]


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

    # --- compute + cache video embeddings once per window ---
    def compute_video_embed(clip_stem, t):
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        frames = _extract_frames(clip_path, t, cfg.n_frames, cfg.window_s)
        if len(frames) < 3:
            return None
        inputs = processor(videos=frames, return_tensors="pt")
        with torch.no_grad():
            v = model.get_video_features(pixel_values=inputs["pixel_values"]).pooler_output
            v = v / v.norm(dim=-1, keepdim=True)
        return v

    real_items = [(c, t) for c, t, *_ in real_events]
    ambient_items = ambient_samples

    print("Computing video embeddings (once per window, cached across all variants)...")
    video_embeds = {}
    skipped = []
    all_items = [("real", c, t) for c, t in real_items] + [("ambient", c, t) for c, t in ambient_items]
    for i, (label, clip_stem, t) in enumerate(all_items):
        if (clip_stem, t) in video_embeds:
            continue
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            skipped.append((clip_stem, t))
            continue
        v = compute_video_embed(clip_stem, t)
        video_embeds[(clip_stem, t)] = v
        if (i + 1) % 30 == 0:
            print(f"  {i + 1}/{len(all_items)} windows embedded")
    print(f"Done. {len(video_embeds)} unique windows embedded, {len(skipped)} skipped (clip missing).\n")

    def scores_for(items, text_embeds):
        out = []
        for clip_stem, t in items:
            v = video_embeds.get((clip_stem, t))
            if v is None:
                continue
            logits = logit_scale * v @ text_embeds.T
            probs = logits.softmax(dim=-1)[0]
            out.append(float(probs[0].item()))
        return out

    print(f"{'variant':<55} {'AUC':>7} {'perm p':>9} {'MWU p':>9}  n(real/amb)")
    print("-" * 100)
    results = []
    for name, prompts in VARIANTS:
        text_inputs = processor(text=prompts, return_tensors="pt", padding=True)
        with torch.no_grad():
            text_embeds = model.get_text_features(**text_inputs).pooler_output
            text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)

        real_scores = scores_for(real_items, text_embeds)
        ambient_scores = scores_for(ambient_items, text_embeds)

        observed, perm_p = permutation_test(real_scores, ambient_scores)
        _, mwu_p = mannwhitney_p(real_scores, ambient_scores)

        results.append((name, observed, perm_p, mwu_p, len(real_scores), len(ambient_scores)))
        print(f"{name:<55} {observed:>7.4f} {perm_p:>9.4f} {mwu_p:>9.4f}  "
              f"{len(real_scores)}/{len(ambient_scores)}")

    print("\nBonferroni correction over the 4 NEW alternates (excludes baseline "
          "reproduction, which isn't a new hypothesis): alpha = 0.05/4 = 0.0125")
    alpha_corrected = 0.05 / 4
    for name, observed, perm_p, mwu_p, nr, na in results[1:]:
        verdict = "SURVIVES" if perm_p < alpha_corrected else "does not survive"
        print(f"  {name:<55} perm p={perm_p:.4f}  {verdict}")

    # --- re-check the original doc's structural finding: does the
    # defensive-activity-vs-clean-whiff fragility pattern still hold on
    # the corrected sample, across these reconstructed variants? ---
    print("\n=== clip_base1-4/foul1/whiff1 percentile-of-ambient, per variant "
          "(the original doc's 'defensive activity destabilizes the score' check) ===")
    six_clips = {"clip_base1", "clip_base2", "clip_base3", "clip_base4",
                 "clip_foul1", "clip_whiff1"}
    six_events = [(c, t, eid, etype) for c, t, eid, etype in real_events if c in six_clips]

    def percentile(score, ambient_scores):
        below = sum(1 for a in ambient_scores if a < score)
        return 100.0 * below / len(ambient_scores)

    header = f"{'clip/event':<20}" + "".join(f"{n[:18]:>20}" for n, *_ in VARIANTS)
    print(header)
    for clip_stem, t, eid, etype in six_events:
        row = f"{clip_stem}/{eid} (t={t}):"
        row = f"{row:<20}"
        for name, prompts in VARIANTS:
            text_inputs = processor(text=prompts, return_tensors="pt", padding=True)
            with torch.no_grad():
                text_embeds = model.get_text_features(**text_inputs).pooler_output
                text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)
            v = video_embeds.get((clip_stem, t))
            if v is None:
                row += f"{'n/a':>20}"
                continue
            logits = logit_scale * v @ text_embeds.T
            probs = logits.softmax(dim=-1)[0]
            score = float(probs[0].item())
            ambient_scores = scores_for(ambient_items, text_embeds)
            pct = percentile(score, ambient_scores)
            row += f"{pct:>19.0f}%"
        print(row)


if __name__ == "__main__":
    main()
