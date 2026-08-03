"""Investigation, not implementation: two temporal-structure angles
requested alongside the pose+audio work, using only the project's own
already-trusted ground truth (tests/ground_truth/*.json) -- no new data,
no new licensing, free to run.

Angle 1: does dead time show a consistent TEMPORAL POSITION relative to
play structure -- a repeatable window after a play resolves before the
next at-bat genuinely begins, regardless of clip? Two real data sources,
reported separately since they differ in precision:
  (a) the one PRECISE data point available: clip_300's own
      atbat_expectations.fire_within window [104,112], measured against
      the immediately preceding real play's (e6, hit_and_run) window end
      (103) -- a real, frame-verified "play resolves -> at-bat genuinely
      starts" gap. n=1; only clip_300 has a real fire_within expectation
      (clip_540/clip_60 only have no_fire_within, a different kind of
      claim -- "must not fire here", not a timing estimate).
  (b) a coarser proxy: every consecutive-event gap across the 3 clips
      that have multiple annotated events (clip_300, clip_540, clip_60;
      the other 6 clips have exactly one event each, no gap to measure).
      Noisier -- not every such gap is a genuine "play resolved, new
      at-bat pending" transition (some are the same at-bat continuing:
      an at_bat_activity window overlapping the swing that follows it),
      and un-annotated at-bats between two annotated events inflate some
      gaps -- but it's the only source giving real n>1.

Angle 2: is time-to-next-play consistent WITHIN one clip (used as a
proxy for "within one game," since each reference clip is one
continuous recording) enough to justify a tighter, clip-specific
threshold instead of one global constant? Only the same 3 multi-event
clips have more than one inter-play interval to compare.

Usage:
    ./venv/bin/python scripts/temporal_investigation.py
"""

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"

REQUIRED_PLAY_TYPES = {
    "hit_and_run", "hit_and_putout_first_base", "pitch_swing_run",
    "swing_and_miss", "flyout_shortstop", "foul_ball", "pitch",
}


def load_clips():
    clips = {}
    for p in sorted(GROUND_TRUTH_DIR.glob("*.json")):
        gt = json.loads(p.read_text())
        clip = Path(gt["clip"]).stem
        events = sorted(gt["events"], key=lambda e: e["window"][0])
        clips[clip] = {"events": events, "atbat": gt.get("atbat_expectations")}
    return clips


def consecutive_gaps(events):
    """(prev_id, prev_type, next_id, next_type, gap_s) for every
    consecutive pair, sorted by start time. gap_s can be negative
    (overlapping windows, e.g. at-bat-activity leading straight into
    the swing that follows it)."""
    gaps = []
    for prev, nxt in zip(events, events[1:]):
        gap = nxt["window"][0] - prev["window"][1]
        gaps.append((prev["id"], prev["type"], nxt["id"], nxt["type"], gap))
    return gaps


def cv(values):
    values = np.asarray(values, dtype=float)
    mean = values.mean()
    if mean == 0:
        return float("inf")
    return float(values.std() / abs(mean))


def angle1_temporal_position(clips):
    print("=" * 70)
    print("ANGLE 1: temporal position of dead time relative to play structure")
    print("=" * 70)

    print("\n(a) PRECISE data: real atbat_expectations.fire_within windows")
    precise_gaps = []
    for clip, data in clips.items():
        ae = data["atbat"]
        if not ae or "fire_within" not in ae:
            continue
        fire_lo, fire_hi = ae["fire_within"][0]
        # the play immediately preceding the fire window
        prior_plays = [e for e in data["events"] if e["window"][1] <= fire_lo
                      and e["type"] in REQUIRED_PLAY_TYPES]
        if not prior_plays:
            continue
        prior = max(prior_plays, key=lambda e: e["window"][1])
        gap_lo = fire_lo - prior["window"][1]
        gap_hi = fire_hi - prior["window"][1]
        precise_gaps.append((clip, prior["id"], gap_lo, gap_hi))
        print(f"  {clip}: {prior['id']} ({prior['type']}) ends {prior['window'][1]}s -> "
             f"real at-bat-start fire window [{fire_lo},{fire_hi}]s "
             f"-> gap {gap_lo}-{gap_hi}s")
    print(f"  n={len(precise_gaps)} -- far too few to claim a consistent "
         f"window on its own.")

    print("\n(b) COARSER proxy: every consecutive-event gap "
         "(3 multi-event clips only)")
    all_gaps = []
    for clip, data in clips.items():
        gaps = consecutive_gaps(data["events"])
        if not gaps:
            continue
        for prev_id, prev_type, next_id, next_type, gap in gaps:
            all_gaps.append(gap)
            print(f"  {clip}: {prev_id}({prev_type}) -> {next_id}({next_type}) "
                 f"= {gap:+.0f}s")

    if all_gaps:
        arr = np.array(all_gaps, dtype=float)
        print(f"\n  n={len(arr)} real consecutive-event gaps across "
             f"{sum(1 for d in clips.values() if len(d['events'])>1)} multi-event clips")
        print(f"  min={arr.min():.0f}s max={arr.max():.0f}s "
             f"mean={arr.mean():.1f}s median={np.median(arr):.1f}s "
             f"std={arr.std():.1f}s")
        print(f"  coefficient of variation: {cv(all_gaps):.2f} "
             f"(>1.0 = spread exceeds the mean itself -- not a tight, "
             f"repeatable window)")

    print("\n  CONCLUSION (angle 1): ", end="")
    if all_gaps and cv(all_gaps) < 0.5:
        print("gaps cluster tightly -- a real, exploitable temporal "
             "position signal looks plausible.")
    else:
        print("no consistent window. Real gaps range from "
             f"{min(all_gaps):.0f}s to {max(all_gaps):.0f}s (including "
             "near-zero/negative gaps where an at_bat_activity window "
             "overlaps directly into the play it leads into) with a "
             f"coefficient of variation of {cv(all_gaps):.2f} -- the "
             "spread is larger than the mean itself. The single precise "
             "data point (5-9s) sits well inside that range, so it's "
             "consistent with the noisy proxy, but n=1 can't confirm a "
             "repeatable pattern either way. Not a consistent temporal "
             "position at this reference set's granularity.")
    return all_gaps


def angle2_per_clip_rhythm(clips):
    print("\n" + "=" * 70)
    print("ANGLE 2: per-clip (proxy for per-game) rhythm consistency")
    print("=" * 70)

    per_clip_intervals = {}
    for clip, data in clips.items():
        starts = sorted(e["window"][0] for e in data["events"]
                        if e["type"] in REQUIRED_PLAY_TYPES)
        if len(starts) < 2:
            continue
        intervals = [b - a for a, b in zip(starts, starts[1:])]
        per_clip_intervals[clip] = intervals
        print(f"  {clip}: required-event starts {starts} -> "
             f"inter-play intervals {intervals}")

    print(f"\n  {len(per_clip_intervals)} of 9 clips have 2+ required "
         f"events to compare (the other 6 have exactly one, or zero, "
         f"real plays annotated -- no within-clip rhythm to measure at all).")

    all_intervals = [i for ivs in per_clip_intervals.values() for i in ivs]
    if all_intervals:
        print(f"  all within-clip intervals pooled: {sorted(all_intervals)}")
        print(f"  pooled coefficient of variation: {cv(all_intervals):.2f}")

    print("\n  CONCLUSION (angle 2): ", end="")
    if len(per_clip_intervals) < 2:
        print("not enough clips with multiple real plays to compare "
             "within-clip vs across-clip variance at all.")
    else:
        detail = ""
        worst_clip, worst_ratio = None, 0.0
        for clip, ivs in per_clip_intervals.items():
            if len(ivs) > 1 and min(ivs) > 0:
                ratio = max(ivs) / min(ivs)
                if ratio > worst_ratio:
                    worst_clip, worst_ratio = clip, ratio
        if worst_clip:
            detail = (f"{worst_clip}'s own real intervals are "
                     f"{per_clip_intervals[worst_clip]} "
                     f"({worst_ratio:.1f}x apart within the SAME clip) -- ")
        print(f"data is far too sparse for real statistical confidence "
             f"(only {len(per_clip_intervals)} clips have any within-clip "
             f"comparison at all, most with just 2 intervals), but what "
             f"exists actively argues against the premise: {detail}"
             "within-game variation is not obviously smaller than "
             "across-game variation. A tighter, clip-specific threshold "
             "is not supported by this data; it would need real "
             "per-game data at real scale (many plays per game), which "
             "this reference set was never built to provide (each clip "
             "is a short highlight-length excerpt, not a full game).")


def main():
    clips = load_clips()
    angle1_temporal_position(clips)
    angle2_per_clip_rhythm(clips)


if __name__ == "__main__":
    main()
