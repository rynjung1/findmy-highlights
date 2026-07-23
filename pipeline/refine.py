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
raw motion drops below the exit threshold. The segment is held open until
pipeline.settle.settled_mask says motion has genuinely settled (the SAME
settle logic pipeline/atbat.py uses to decide a new at-bat has begun — they
must agree on this, or the extension could hold a segment open past the
exact point the at-bat detector already considers safe to close), closing
early if the at-bat-start signal fires, and never extending more than
trail_cap_s past the raw end. End-of-file closes everything — segments
cannot stay open to EOF by default; the cap fires first in any quiet
ending.

PADDING: pre_pad/post_pad seconds so cuts never clip into a play's run-up
or tail; clamped to the clip bounds. Padded segments that touch are merged.
"""

from dataclasses import dataclass, field

import numpy as np

from pipeline.segments import merge_segments
from pipeline.settle import SettleConfig, last_active_time, settled_mask


@dataclass
class RefineConfig:
    settle: SettleConfig = field(default_factory=SettleConfig)
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
        idx = (motion_times > b) & (motion_times <= cap_end)
        wt, ws = motion_times[idx], motion_smooth[idx]
        if len(wt) == 0:
            e = b
        else:
            settled = settled_mask(wt, ws, cfg.settle)
            settled_at = np.nonzero(settled)[0]
            # scan only up to (and including) the first settled sample —
            # mirrors the old loop's early break, so activity found AFTER
            # the play is already judged over can't re-extend it
            scan_end = int(settled_at[0]) + 1 if len(settled_at) else len(wt)
            e = last_active_time(wt[:scan_end], ws[:scan_end], cfg.settle)
            if e is None:
                e = b
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
