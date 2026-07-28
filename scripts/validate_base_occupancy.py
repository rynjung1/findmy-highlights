"""Stage 10 validation: does the raw base-occupancy signal actually match
real footage, and specifically how close does its onset land to the real
catch/tag instant?

Deliberately standalone, not part of scripts/regression.py: this validates
compute_all_occupancy() directly against the base_occupancy ground truth in
tests/ground_truth/clip_base{1,3,4}.json (clip_base2 has no base event, see
its ground truth file), using already-cached person detections. Nothing
here is wired into the detection pipeline -- see pipeline/fusion.py and
pipeline/calibration.py docstrings for why that's Stage 11's job.
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.calibration import resolve_base_zones
from pipeline.detection import DetectionConfig, detect_persons
from pipeline.fusion import compute_all_occupancy

GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
REFERENCE_DIR = ROOT / "reference_clips"
CACHE_DIR = ROOT / ".cache" / "detections"
STATIONARY_V = 0.30  # same default pipeline/fusion.py's FusionConfig uses


def onset_time(occupied, det_times, search_from):
    """First time occupied goes True at or after search_from -- None if
    it never does. search_from guards against an unrelated earlier
    occupancy (e.g. someone else briefly crossing the zone) being
    mistaken for this play's onset."""
    for t, occ in zip(det_times, occupied):
        if t >= search_from and occ:
            return float(t)
    return None


def validate_clip(stem: str, mode_label: str, require_stationary_entry: bool):
    truth = json.loads((GROUND_TRUTH_DIR / f"{stem}.json").read_text())
    bo = truth.get("base_occupancy")
    if not bo:
        return None

    video = REFERENCE_DIR / f"{stem}.mkv"
    zones = resolve_base_zones(video, calib_dir=REFERENCE_DIR)
    zone = zones.get(bo["base"])
    if zone is None:
        print(f"  {stem}: no '{bo['base']}' zone calibrated, skipping")
        return None

    det = detect_persons(str(video), DetectionConfig(), cache_dir=str(CACHE_DIR))
    occ = compute_all_occupancy(det.times, det.boxes, {bo["base"]: zone},
                                STATIONARY_V, require_stationary_entry)[bo["base"]]

    window_start, window_end = bo["occupied_window_s"]
    catch_instant = bo["catch_tag_instant_s"]
    onset = onset_time(occ, det.times, search_from=window_start - 3)
    any_occupied_in_window = any(
        o for t, o in zip(det.times, occ) if window_start <= t <= window_end
    )

    if onset is None:
        lag_str = "NEVER occupied"
        lag = None
    else:
        lag = onset - catch_instant
        lag_str = f"onset={onset:.2f}s  lag={lag:+.2f}s vs catch~{catch_instant}s"

    # Resting-fielder confound check: a first baseman stands near first as
    # normal positioning, not just during an active play (unlike the
    # plate, which is genuinely empty between at-bats) -- this measures
    # how much of the time BEFORE the play even develops already reads
    # occupied. Found to vary a lot (clip_base1/3: 100%, clip_base4: 15%)
    # rather than being universal; whether that's a first-vs-second/third
    # base difference is untested (all three calibrated clips are first
    # base) and is flagged for Stage 11, not resolved here.
    pre_play_idx = [i for i, t in enumerate(det.times) if t < catch_instant - 3]
    pre_play_frac = (sum(occ[i] for i in pre_play_idx) / len(pre_play_idx)
                     if pre_play_idx else float("nan"))

    print(f"  {stem} [{mode_label}]: {lag_str}  "
         f"(any occupied in [{window_start},{window_end}]: {any_occupied_in_window})  "
         f"pre-play occupied: {pre_play_frac:.0%}")
    return lag


def main():
    clips = ["clip_base1", "clip_base3", "clip_base4"]
    for label, require_stationary in [
        ("plate-style: require_stationary_entry=True (current default)", True),
        ("relaxed: require_stationary_entry=False (Stage 10 proposal)", False),
    ]:
        print(f"\n=== {label} ===")
        for stem in clips:
            validate_clip(stem, label.split(":")[0], require_stationary)


if __name__ == "__main__":
    main()
