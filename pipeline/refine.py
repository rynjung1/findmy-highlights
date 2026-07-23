"""Segment refinement: play extension, padding, and final merge (Phase 3).

This replaces Phase 2's score-level sustain as the mechanism that decides
where a segment ENDS (the dual-hysteresis sustain path remains in
segments.py but is no longer fed by the fused score — one mechanism owns
the exit boundary, so extensions are attributable and can't compound).

Pipeline order: raw motion segments -> veto -> EXTENSION -> PADDING ->
final merge. All steps pure logic, unit-tested without video.

EXTENSION (the keep-through-the-defensive-play rule): if a batter departed
the plate around a segment (occupancy ended inside it, or shortly before
it opened — a hit often vacates the plate in the quiet instant before the
running/fielding burst opens the segment), the play may still be live when
raw motion drops below the exit threshold. The segment is held open while
smoothed motion stays above a lower settle floor, closing early if the
at-bat-start signal fires (next batter settled in => play over), and never
extending more than trail_cap_s past the raw end. End-of-file closes
everything — segments cannot stay open to EOF by default; the cap fires
first in any quiet ending.

PADDING: pre_pad/post_pad seconds so cuts never clip into a play's run-up
or tail; clamped to the clip bounds. Padded segments that touch are merged.
"""

from dataclasses import dataclass

import numpy as np

from pipeline.segments import merge_segments


@dataclass
class RefineConfig:
    settle_low: float = 0.002       # extension holds while smoothed motion >= this
    # Real defensive-play motion is not monotonic — it dips (ball in the
    # air, a fielder set) and resumes (throw, tag, relay). Measured on
    # clip_60's trouble spot: a 0.9s dip at 146.1-147.0 is followed by
    # real throwing motion at 147.0-148+. Ending extension at the FIRST
    # low sample cuts the play; requiring quiet to be SUSTAINED for this
    # long before ending bridges real dips without running forever on
    # background noise (which never stays this quiet for this long).
    min_quiet_s: float = 1.5
    trail_cap_s: float = 12.0       # hard cap on extension past the raw end
    occupancy_lookback_s: float = 5.0  # departure this long before open still counts
    # Occupancy is a lagging signal (the zone reads occupied while anyone —
    # catcher, next batter — is inside it), so the recorded departure can
    # land just AFTER the raw segment end; allow that slack for eligibility.
    departure_slack_s: float = 2.0
    # Padding defaults: measured against the reference clips, the largest
    # pre-open coverage need is 1.2s (clip_300's swing at ~97 vs raw open
    # at 98.2), so 3.0s is a 2.5x margin. Padding dominates over-inclusion
    # (+29-44s/clip vs +1-12s for extension), so these stay as tight as the
    # priority rule allows: widen them before ever risking a clipped play.
    pre_pad_s: float = 3.0
    post_pad_s: float = 1.5
    final_merge_gap_s: float = 0.5


def departure_times(det_times, occupied):
    """Times at which the plate went occupied -> vacant."""
    det_times = np.asarray(det_times, dtype=float)
    occupied = np.asarray(occupied, dtype=bool)
    out = []
    for i in range(1, len(det_times)):
        if occupied[i - 1] and not occupied[i]:
            out.append(float(det_times[i]))
    return out


def extend_segments(segments, motion_times, motion_smooth, departures,
                    atbat_fires, duration, config: RefineConfig | None = None):
    """Apply the play-extension rule to each eligible segment's end."""
    cfg = config or RefineConfig()
    motion_times = np.asarray(motion_times, dtype=float)
    motion_smooth = np.asarray(motion_smooth, dtype=float)
    out = []
    for a, b in segments:
        eligible = any(a - cfg.occupancy_lookback_s <= d
                       <= b + cfg.departure_slack_s
                       for d in departures)
        if not eligible:
            out.append((a, b))
            continue
        cap_end = min(b + cfg.trail_cap_s, duration)
        e = b
        quiet_start = None
        idx = (motion_times > b) & (motion_times <= cap_end)
        for t, s in zip(motion_times[idx], motion_smooth[idx]):
            if s >= cfg.settle_low:
                e = float(t)
                quiet_start = None
            elif quiet_start is None:
                quiet_start = t
            elif t - quiet_start >= cfg.min_quiet_s:
                break  # sustained quiet: play is genuinely over
        fire = next((f for f in sorted(atbat_fires) if b < f <= cap_end), None)
        if fire is not None:
            e = min(e, fire) if e > b else e
            e = min(max(e, b), fire)
        out.append((a, e))
    return out


def pad_and_merge(segments, duration, config: RefineConfig | None = None):
    cfg = config or RefineConfig()
    padded = [(max(0.0, a - cfg.pre_pad_s), min(duration, b + cfg.post_pad_s))
              for a, b in segments]
    return merge_segments(padded, cfg.final_merge_gap_s)


def refine_segments(segments, motion_times, motion_smooth, det_times,
                    occupied, atbat_fires, duration,
                    config: RefineConfig | None = None):
    """Full refinement: extension then padding/merge. `segments` should
    already be veto-filtered."""
    cfg = config or RefineConfig()
    deps = departure_times(det_times, occupied)
    extended = extend_segments(segments, motion_times, motion_smooth, deps,
                               atbat_fires, duration, cfg)
    return pad_and_merge(extended, duration, cfg)
