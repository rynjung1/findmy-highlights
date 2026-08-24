"""Re-verifies the HFC/spectral-flux onset-detection comparison
(originally documented AUC 0.605/p=0.128 and 0.646/p=0.056, both
nominally above the RMS-envelope rise-time baseline of 0.523 but neither
individually significant, and neither surviving a 6-test Bonferroni
correction across that night's flow+onset investigation) against the
corrected ground truth (clip_60#e6 relabel, commits 73f45a9/10ff7ff) --
another open debt item from the 2026-08-16 consolidated correction (no
committed script existed for either onset-detection variant before this
one).

Reuses scripts/pose_audio_validation.py's load_real_events()/
load_ambient_samples() directly, same loaders every other recheck
tonight used -- 12 real / 167 ambient, matching the main 0.690 -> 0.6527
X-CLIP correction's sample exactly (this is audio-only, so the same
ground-truth events, not the X-CLIP embeddings, but the identical
loader/sample construction).

Methodology, reconstructed from the original prose (2026-08-13 flow/
onset writeup): "HFC and spectral flux (scipy.signal.stft) in place of
the earlier ad-hoc RMS-envelope rise-time heuristic ... same real
event/ambient set, same +/-1.0s peak-search window as the rise-time
work". The exact STFT frame/hop size was never recorded (no committed
script) -- reconstructed here as nperseg=512, noverlap=384 (32ms frames,
8ms hop at 16kHz, deliberately fine-grained, comparable time resolution
to the rise-time work's 5ms envelope hop) and flagged plainly as a
reconstruction assumption, not a recovered original parameter.
HFC(t) = sum_k k*|X(t,k)|^2 (Masri 1996, frequency-bin-weighted energy,
used directly as the novelty function -- a burst of high-frequency
energy IS the onset signal). Spectral flux(t) = sum_k
max(0, |X(t,k)|-|X(t-1,k)|) (half-wave-rectified frame-to-frame
magnitude increase, the standard definition). Score per candidate =
the peak novelty value within the +/-1.0s window around the instant
(mirroring "peak-search window", higher = sharper/stronger transient =
more real-like -- unlike rise_time_s, no negation needed since novelty
height already points the right direction).

Also reproduces the RMS-envelope rise-time audio-alone baseline (0.508
on the corrected sample, already logged in the 2026-08-16 correction)
from pipeline.audio directly, as a sanity check before trusting the new
numbers -- same discipline every recheck tonight has used.

Usage:
    venv/bin/python scripts/onset_detection_recheck.py
"""

import sys
from pathlib import Path

import numpy as np
from scipy.signal import stft

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scripts.pose_audio_validation import auc, load_ambient_samples, load_real_events
from pipeline.audio import AudioConfig, load_audio, onset_features

CLIPS_DIR = ROOT / "reference_clips"
SEED = 20260816  # same seed as every other recheck tonight
N_PERM = 2000
WINDOW_S = 1.0  # matches the rise-time work's peak-search window
SAMPLE_RATE = 16000
NPERSEG = 512
NOVERLAP = 384


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


def compute_novelty_functions(audio, sample_rate=SAMPLE_RATE):
    """Returns (times, hfc, spectral_flux) -- one novelty value per STFT frame."""
    f, t, Zxx = stft(audio, fs=sample_rate, nperseg=NPERSEG, noverlap=NOVERLAP)
    mag = np.abs(Zxx)  # (n_freq, n_frames)
    hfc = (f[:, None] * (mag ** 2)).sum(axis=0)
    flux = np.zeros(mag.shape[1])
    diff = np.diff(mag, axis=1)
    flux[1:] = np.maximum(diff, 0).sum(axis=0)
    return t, hfc, flux


def peak_in_window(times, novelty, center_s, window_s=WINDOW_S):
    idx = (times >= center_s - window_s) & (times <= center_s + window_s)
    vals = novelty[idx]
    if len(vals) < 5:
        return None
    return float(np.max(vals))


def main():
    real_events = load_real_events()
    ambient_samples = load_ambient_samples()
    print(f"Real swing-type ground-truth events: {len(real_events)}")
    print(f"Ambient (real gap) samples: {len(ambient_samples)}\n")

    real_items = [(c, t) for c, t, *_ in real_events]
    ambient_items = ambient_samples

    # --- sanity check: reproduce the RMS-envelope rise-time baseline
    # (documented corrected value: 0.508) before trusting anything new ---
    print("SANITY CHECK -- reproducing the rise-time audio-alone baseline...")
    rise_cache = {}
    rt_real, rt_ambient = [], []
    for clip_stem, t in real_items:
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        r = onset_features(str(clip_path), t, envelope_cache=rise_cache)
        if r is not None:
            rt_real.append(-r["rise_time_s"])
    for clip_stem, t in ambient_items:
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        r = onset_features(str(clip_path), t, envelope_cache=rise_cache)
        if r is not None:
            rt_ambient.append(-r["rise_time_s"])
    rt_auc = auc(rt_real, rt_ambient)
    print(f"rise-time AUC = {rt_auc:.4f} (documented corrected value: 0.508), "
          f"n={len(rt_real)}/{len(rt_ambient)}\n")

    # --- HFC / spectral flux novelty functions, computed once per clip ---
    print("Computing HFC / spectral-flux novelty functions per clip...")
    novelty_cache = {}

    def get_novelty(clip_stem):
        if clip_stem not in novelty_cache:
            clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
            audio = load_audio(str(clip_path), SAMPLE_RATE)
            novelty_cache[clip_stem] = compute_novelty_functions(audio)
        return novelty_cache[clip_stem]

    hfc_real, hfc_ambient, flux_real, flux_ambient = [], [], [], []
    for clip_stem, t in real_items:
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        times, hfc, flux = get_novelty(clip_stem)
        h = peak_in_window(times, hfc, t)
        s = peak_in_window(times, flux, t)
        if h is not None:
            hfc_real.append(h)
        if s is not None:
            flux_real.append(s)
    for clip_stem, t in ambient_items:
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        times, hfc, flux = get_novelty(clip_stem)
        h = peak_in_window(times, hfc, t)
        s = peak_in_window(times, flux, t)
        if h is not None:
            hfc_ambient.append(h)
        if s is not None:
            flux_ambient.append(s)

    print(f"HFC samples: {len(hfc_real)} real, {len(hfc_ambient)} ambient")
    print(f"Spectral flux samples: {len(flux_real)} real, {len(flux_ambient)} ambient\n")

    print(f"{'signal':<20} {'AUC':>8} {'perm p':>9} {'MWU p':>9}")
    print("-" * 50)
    results = {}
    for name, real_s, amb_s in [("HFC", hfc_real, hfc_ambient),
                                  ("spectral flux", flux_real, flux_ambient)]:
        obs, perm_p = permutation_test(real_s, amb_s)
        _, mwu_p = mannwhitney_p(real_s, amb_s)
        results[name] = (obs, perm_p, mwu_p)
        print(f"{name:<20} {obs:>8.4f} {perm_p:>9.4f} {mwu_p:>9.4f}")

    print("\nBonferroni over these 2 tests only: alpha = 0.05/2 = 0.025")
    for name, (obs, perm_p, mwu_p) in results.items():
        verdict = "SURVIVES" if perm_p < 0.025 else "does not survive"
        print(f"  {name:<20} perm p={perm_p:.4f}  {verdict}")

    print("\nOriginal (pre-correction, n=11/170): HFC AUC 0.605 (p=0.128), "
          "spectral flux AUC 0.646 (p=0.056) -- neither survived the original "
          "6-test Bonferroni correction (alpha=0.05/6~0.0083) either.")

    # --- the original investigation's own real-play recall-risk check,
    # independent of the aggregate AUC: clip_base3/e1 scored at the
    # 4th/5th/13th percentile of ambient on these exact best-looking
    # features -- a real, confirmed play reading as MORE ambient than
    # 87-96% of genuine ambient samples. Re-check whether that specific
    # red flag still holds on the corrected sample before trusting any
    # aggregate AUC number, same discipline as the prompt-variant recheck. ---
    print("\n=== clip_base1-4/foul1/whiff1 percentile-of-ambient (the original "
          "doc's real-play recall-risk check) ===")
    six_clips = {"clip_base1", "clip_base2", "clip_base3", "clip_base4",
                 "clip_foul1", "clip_whiff1"}
    six_events = [(c, t, eid, etype) for c, t, eid, etype in real_events if c in six_clips]

    def percentile(score, ambient_scores):
        below = sum(1 for a in ambient_scores if a < score)
        return 100.0 * below / len(ambient_scores)

    print(f"{'clip/event':<22}{'HFC pctile':>14}{'flux pctile':>14}")
    for clip_stem, t, eid, etype in six_events:
        clip_path = CLIPS_DIR / f"{clip_stem}.mkv"
        if not clip_path.exists():
            continue
        times, hfc, flux = get_novelty(clip_stem)
        h = peak_in_window(times, hfc, t)
        s = peak_in_window(times, flux, t)
        h_pct = percentile(h, hfc_ambient) if h is not None else None
        s_pct = percentile(s, flux_ambient) if s is not None else None
        h_str = f"{h_pct:.0f}%" if h_pct is not None else "n/a"
        s_str = f"{s_pct:.0f}%" if s_pct is not None else "n/a"
        print(f"{clip_stem}/{eid} (t={t}):{h_str:>14}{s_str:>14}")


if __name__ == "__main__":
    main()
