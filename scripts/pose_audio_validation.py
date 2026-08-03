"""Real, scaled-up validation of pose (wrist displacement) and audio
(onset rise-time) as candidate cutting signals -- Task 2's pose+audio
investigation, finished for real this time (the prior investigation's
own numbers were never saved anywhere and couldn't be trusted or
reused; see README's Task 2 pose+audio writeup for the full provenance
story). Not itself a cutting signal: this only reports real, measured
separation. Nothing downstream reads these numbers as a decision yet.

REAL events: every ground-truth event in tests/ground_truth/*.json whose
type plausibly involves a bat swing (hit_and_run,
hit_and_putout_first_base, pitch_swing_run, swing_and_miss,
flyout_shortstop, foul_ball -- 11 real events across the 9 reference
clips, not a round number chosen in advance, just what the project's
own already-trusted annotations actually contain). The instant used per
event is parsed from that event's own note field (e.g. "contact ~97",
"swings through the zone ~14.5-14.8" -> 14.5) -- these are the project's
own frame-verified estimates, not re-derived here. Falls back to the
event window's midpoint only if no number is parseable.

AMBIENT samples: real gaps between annotated events within each clip
(time no ground-truth event covers at all), sampled every 3s, staying
>=1.5s from any window edge. This is deliberately a stronger negative
class than "some other event type" -- genuinely nothing annotated is
happening.

Reports, for both features and a naive combined score: real vs ambient
distributions and ROC-AUC (probability a random real event scores higher
than a random ambient sample) -- a threshold-free separation statistic,
since picking an actual operating threshold is Tier 2 work, not this
script's job.

Usage:
    ./venv/bin/python scripts/pose_audio_validation.py
"""

import json
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.audio import onset_features
from pipeline.calibration import resolve_zone
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.pose import build_landmarker, wrist_displacement

GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
CLIPS_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"

SWING_TYPES = {
    "hit_and_run", "hit_and_putout_first_base", "pitch_swing_run",
    "swing_and_miss", "flyout_shortstop", "foul_ball",
}

_NUMBER_RE = re.compile(r"~(\d+(?:\.\d+)?)")


def _parse_instant(note: str, window) -> float:
    m = _NUMBER_RE.search(note)
    if m:
        return float(m.group(1))
    return 0.5 * (window[0] + window[1])


def load_real_events():
    """[(clip_stem, instant_s), ...] -- one per swing-type ground-truth event."""
    events = []
    for gt_path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        gt = json.loads(gt_path.read_text())
        clip = Path(gt["clip"]).stem
        for e in gt["events"]:
            if e["type"] in SWING_TYPES:
                instant = _parse_instant(e.get("note", ""), e["window"])
                events.append((clip, instant, e["id"], e["type"]))
    return events


def load_ambient_samples(hop_s=3.0, edge_margin_s=1.5):
    """[(clip_stem, instant_s), ...] -- real gaps between ground-truth
    event windows, sampled every hop_s, staying edge_margin_s clear of
    any window boundary."""
    samples = []
    for gt_path in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        gt = json.loads(gt_path.read_text())
        clip = Path(gt["clip"]).stem
        duration = gt["duration"]
        windows = sorted(e["window"] for e in gt["events"])
        cursor = 0.0
        gaps = []
        for a, b in windows:
            if a > cursor:
                gaps.append((cursor, a))
            cursor = max(cursor, b)
        if cursor < duration:
            gaps.append((cursor, duration))
        for a, b in gaps:
            a2, b2 = a + edge_margin_s, b - edge_margin_s
            t = a2
            while t <= b2:
                samples.append((clip, t))
                t += hop_s
    return samples


def auc(real_scores, ambient_scores) -> float:
    """P(a random real score > a random ambient score), ties counted
    half -- the standard threshold-free separation statistic."""
    wins = 0.0
    for r in real_scores:
        for a in ambient_scores:
            if r > a:
                wins += 1.0
            elif r == a:
                wins += 0.5
    return wins / (len(real_scores) * len(ambient_scores))


def zscore(values, all_values):
    mean, std = float(np.mean(all_values)), float(np.std(all_values))
    if std == 0:
        return [0.0 for _ in values]
    return [(v - mean) / std for v in values]


def main():
    real_events = load_real_events()
    ambient_samples = load_ambient_samples()
    print(f"Real swing/contact-type ground-truth events: {len(real_events)}")
    print(f"Ambient (real gap) samples: {len(ambient_samples)}")

    by_clip = {}
    for clip, t, *_ in real_events:
        by_clip.setdefault(clip, {"real": [], "ambient": []})["real"].append(t)
    for clip, t in ambient_samples:
        by_clip.setdefault(clip, {"real": [], "ambient": []})["ambient"].append(t)

    landmarker = build_landmarker()
    audio_cache = {}

    # One real pass per sample, computing both features together --
    # (pose_px_or_None, audio_rise_ms_or_None) per (label, clip, t).
    raw = {"real": [], "ambient": []}
    skipped_pose_no_zone = 0
    skipped_pose_none = 0

    for clip, groups in sorted(by_clip.items()):
        clip_path = CLIPS_DIR / f"{clip}.mkv"
        if not clip_path.exists():
            print(f"  (skipping {clip}: not available locally)")
            continue
        zone = resolve_zone(str(clip_path))
        det = detect_persons(str(clip_path), DetectionConfig(), cache_dir=str(CACHE_DIR))
        print(f"{clip}: {len(groups['real'])} real, {len(groups['ambient'])} ambient"
             f"{' (no calibration -- pose skipped)' if zone is None else ''}")

        for label, times in (("real", groups["real"]), ("ambient", groups["ambient"])):
            for t in times:
                pose_val = None
                if zone is None:
                    skipped_pose_no_zone += 1
                else:
                    p = wrist_displacement(str(clip_path), det.times, det.boxes, zone,
                                           t, landmarker=landmarker)
                    if p is None:
                        skipped_pose_none += 1
                    else:
                        pose_val = p["peak_displacement_px"]
                a = onset_features(str(clip_path), t, envelope_cache=audio_cache)
                audio_val = a["rise_time_s"] * 1000 if a is not None else None
                raw[label].append((pose_val, audio_val))

    landmarker.close()

    pose_real = [p for p, _ in raw["real"] if p is not None]
    pose_ambient = [p for p, _ in raw["ambient"] if p is not None]
    audio_real = [a / 1000.0 for _, a in raw["real"] if a is not None]
    audio_ambient = [a / 1000.0 for _, a in raw["ambient"] if a is not None]

    print(f"\nPose samples obtained: {len(pose_real)} real, {len(pose_ambient)} ambient "
         f"({skipped_pose_no_zone} skipped: no calibration, "
         f"{skipped_pose_none} skipped: no near-plate detection)")
    print(f"Audio samples obtained: {len(audio_real)} real, {len(audio_ambient)} ambient")

    print("\n=== POSE ALONE (peak wrist displacement, px) ===")
    if pose_real and pose_ambient:
        print(f"real:    n={len(pose_real)} min={min(pose_real):.1f} "
             f"max={max(pose_real):.1f} mean={np.mean(pose_real):.1f} "
             f"median={np.median(pose_real):.1f}")
        print(f"ambient: n={len(pose_ambient)} min={min(pose_ambient):.1f} "
             f"max={max(pose_ambient):.1f} mean={np.mean(pose_ambient):.1f} "
             f"median={np.median(pose_ambient):.1f}")
        print(f"AUC (P[real > ambient]): {auc(pose_real, pose_ambient):.3f}")
    else:
        print("not enough real samples to report")

    print("\n=== AUDIO ALONE (onset rise time, ms; LOWER = sharper) ===")
    if audio_real and audio_ambient:
        rr = [x * 1000 for x in audio_real]
        ra = [x * 1000 for x in audio_ambient]
        print(f"real:    n={len(rr)} min={min(rr):.0f} max={max(rr):.0f} "
             f"mean={np.mean(rr):.0f} median={np.median(rr):.0f}")
        print(f"ambient: n={len(ra)} min={min(ra):.0f} max={max(ra):.0f} "
             f"mean={np.mean(ra):.0f} median={np.median(ra):.0f}")
        # lower rise time = more real -> AUC of the NEGATED value
        print(f"AUC (P[real sharper than ambient]): "
             f"{auc([-x for x in rr], [-x for x in ra]):.3f}")
    else:
        print("not enough real samples to report")

    print("\n=== POSE + AUDIO COMBINED (naive z-score sum, only where BOTH exist) ===")
    # only meaningful where the SAME (clip, instant) has both a pose and
    # an audio reading -- reuse the single real pass above, no re-detection
    paired_real = [(p, -a) for p, a in raw["real"] if p is not None and a is not None]
    paired_ambient = [(p, -a) for p, a in raw["ambient"] if p is not None and a is not None]

    if paired_real and paired_ambient:
        all_pose = [p for p, _ in paired_real + paired_ambient]
        all_audio = [a for _, a in paired_real + paired_ambient]
        pose_z_real = zscore([p for p, _ in paired_real], all_pose)
        audio_z_real = zscore([a for _, a in paired_real], all_audio)
        pose_z_amb = zscore([p for p, _ in paired_ambient], all_pose)
        audio_z_amb = zscore([a for _, a in paired_ambient], all_audio)
        combined_real = [p + a for p, a in zip(pose_z_real, audio_z_real)]
        combined_ambient = [p + a for p, a in zip(pose_z_amb, audio_z_amb)]
        print(f"paired n: {len(paired_real)} real, {len(paired_ambient)} ambient")
        print(f"AUC (pose alone, paired subset): "
             f"{auc([p for p, _ in paired_real], [p for p, _ in paired_ambient]):.3f}")
        print(f"AUC (audio alone, paired subset): "
             f"{auc([a for _, a in paired_real], [a for _, a in paired_ambient]):.3f}")
        print(f"AUC (pose+audio combined z-score sum): "
             f"{auc(combined_real, combined_ambient):.3f}")
    else:
        print("not enough paired real+ambient samples to report")


if __name__ == "__main__":
    main()
