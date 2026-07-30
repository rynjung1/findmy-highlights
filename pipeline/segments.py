"""Turn per-frame motion scores into candidate action segments.

Pure logic, no video I/O — kept separate so it can be unit-tested.

Flagging uses hysteresis: a segment opens when the smoothed score crosses
`enter_thresh` and stays open until it falls below `exit_thresh` (lower),
so a play whose motion briefly dips (batter connects, ball in the air,
runner mid-stride) isn't chopped into fragments. Nearby segments are then
merged and very short blips dropped. All defaults err permissive, per the
project rule that missing a real play is worse than keeping dead time.
Proper padding and final merge policy come in Stage 3; the small merge here
only exists to avoid absurdly fragmented output.
"""

from dataclasses import dataclass

import numpy as np


@dataclass
class SegmentConfig:
    # Rolling-mean window (seconds) applied to raw scores before thresholding.
    smooth_window_s: float = 1.0
    # Hysteresis thresholds on the smoothed moving-pixel fraction. Set
    # permissively: on the reference clips the quiet-field baseline sits
    # around 0.001-0.005 and confirmed plays peak at 0.014-0.05, so 0.006
    # catches every known play at the cost of also flagging busy milling.
    #
    # enter_thresh has essentially NO real margin (v2 segments.py retune,
    # measured across all 9 current reference clips): clip_foul1's required
    # event peaks at just 0.00665, a 1.11x margin, clip_300's e4 at 1.17x.
    # NOT raised for exactly this reason -- see README's Known Limitations
    # section for this flagged as its own standalone safety concern, not
    # just a note here.
    enter_thresh: float = 0.006
    # Reference near-plate person-box width (px), median of
    # pipeline.fusion.robust_box_width() across clip_60/300/540 -- the
    # SAME clips enter_thresh's own 0.006 was tuned against. Used by
    # pipeline.run.process_video (via pipeline.fusion.scale_boost_factor)
    # to boost a batch's motion score before the enter-side comparison
    # when its own near-plate box width reads smaller than this -- i.e.
    # when the camera is more distant than the one every reference clip
    # shares. Ships as a real default following the enter_thresh
    # camera-distance investigation: motion score is not scale-invariant
    # (confirmed directly against distance_test_close/far.mov, two real
    # clips of the same subject/motion filmed at two distances -- see
    # tests/test_distance_scaling.py), so a real play at a more distant
    # setup than these 9 clips could otherwise fail to cross enter_thresh
    # at all, a silent total miss. The boost is structurally one-directional
    # (never reduces a score, only ever raises it for a more-distant
    # batch than this reference) -- confirmed zero regression on required-
    # event recall across all 9 reference clips before shipping, and a
    # concrete rescue of a synthetically distance-degraded clip_foul1
    # (its own thinnest-margin required event: 0.43x margin at simulated
    # 1.6x distance -> segment fails to open at all -> 1.35x margin and
    # opens correctly with this boost applied). A companion ambient-
    # motion discount (to also cut dead time on the enter side, not just
    # protect recall) was investigated alongside this and explicitly
    # NOT shipped -- see README's Known Limitations for why that's a
    # structural dead end, not a tuning gap.
    reference_plate_box_width_px: float = 121.4
    # exit_thresh: re-measured and raised twice now.
    #
    # First pass (0.003 -> 0.0045, v2 segments.py retune): several required
    # check_continuity events legitimately dip far below the old 0.003
    # inside their own window (e.g. clip_60's e5 down to 0.00004), meaning
    # raw hysteresis already fragments some real plays today and
    # continuity survives via pipeline.refine's separate, settle-based
    # extension logic, not via this threshold -- so there was real, unused
    # room here, unlike enter_thresh above. Swept safely up to 0.01 (AT/
    # above enter_thresh itself, a degenerate configuration where the
    # hysteresis band collapses to nothing or inverts) with zero
    # recall/continuity failures found anywhere in that swept range on all
    # 9 clips, but first landed on 0.0045 deliberately short of the
    # ceiling: keeps a comfortable ~25% hysteresis gap below enter_thresh
    # rather than compounding enter_thresh's own thin margin on the same
    # underlying mechanism.
    #
    # Second pass, PRIORITY RULE CHANGE (post enter-side scale boost): the
    # ~25% gap above was that same kind of deliberate, spendable cushion,
    # not a recall floor -- the sweep already showed zero recall cost
    # anywhere in range. The owner has since explicitly decided to spend
    # it for more aggressive dead-time cutting (README's "Priority rule:
    # v1 default vs. current live setting"). Raised to 0.0058: as close to
    # enter_thresh (0.006) as this ships without landing ON the literal
    # degenerate boundary the sweep flagged above, on purpose -- not
    # 0.01, since equaling or exceeding enter_thresh isn't "more
    # aggressive," it's a different, nonsensical mechanism (no hysteresis
    # band left at all). Confirmed fresh: still 9/9 recall and continuity
    # on all 9 known clips at this value (scripts/regression.py). See
    # README's Current Status for the real full_game.mkv cut-time delta.
    exit_thresh: float = 0.0058
    # Merge segments separated by less than this (seconds).
    merge_gap_s: float = 3.0
    # Drop segments shorter than this (seconds) AFTER merging.
    min_len_s: float = 1.0


def smooth_scores(times: np.ndarray, scores: np.ndarray,
                  window_s: float) -> np.ndarray:
    """Centered rolling mean over a time window."""
    if len(times) < 2 or window_s <= 0:
        return scores.astype(float)
    dt = float(np.median(np.diff(times)))
    n = max(1, int(round(window_s / dt)))
    if n % 2 == 0:
        n += 1
    kernel = np.ones(n) / n
    return np.convolve(scores, kernel, mode="same")


def scores_to_segments(times, scores, config: SegmentConfig | None = None,
                       sustain_scores=None):
    """Return a list of (start_s, end_s) candidate action segments.

    If `sustain_scores` is given, hysteresis becomes two-signal: a segment
    OPENS only when `scores` crosses enter_thresh, but stays open until
    `sustain_scores` falls below exit_thresh. Used by fusion so auxiliary
    signals (person boxes, plate occupancy) can hold a live play open past
    a motion lull, but can never open a segment on their own (measured on
    the reference clips: letting them open segments only inflated flagged
    time, it never added a play that motion hadn't already found).
    """
    cfg = config or SegmentConfig()
    times = np.asarray(times, dtype=float)
    scores = np.asarray(scores, dtype=float)
    if len(times) == 0:
        return []
    sm_open = smooth_scores(times, scores, cfg.smooth_window_s)
    if sustain_scores is None:
        sm_sustain = sm_open
    else:
        sustain_scores = np.asarray(sustain_scores, dtype=float)
        sm_sustain = smooth_scores(times, sustain_scores, cfg.smooth_window_s)

    segments = []
    open_start = None
    for t, so, ss in zip(times, sm_open, sm_sustain):
        if open_start is None:
            if so >= cfg.enter_thresh:
                open_start = t
        else:
            if ss < cfg.exit_thresh:
                segments.append((open_start, t))
                open_start = None
    if open_start is not None:
        segments.append((open_start, float(times[-1])))

    segments = merge_segments(segments, cfg.merge_gap_s)
    return [(a, b) for a, b in segments if (b - a) >= cfg.min_len_s]


def merge_segments(segments, max_gap_s: float):
    """Merge overlapping or near-adjacent (start, end) pairs. Input need not
    be sorted; output is sorted and non-overlapping."""
    if not segments:
        return []
    segs = sorted(segments)
    merged = [list(segs[0])]
    for a, b in segs[1:]:
        if a - merged[-1][1] <= max_gap_s:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(s) for s in merged]


def segment_covers(segments, window) -> bool:
    """True if any (start, end) segment overlaps the (start, end) window."""
    ws, we = window
    return any(a <= we and b >= ws for a, b in segments)


def total_duration(segments) -> float:
    return float(sum(b - a for a, b in segments))


@dataclass
class SkipSuggestionConfig:
    """Skip-ahead is a purely manual, non-destructive UI affordance (see
    pipeline/manifest.py's skip_suggestions field): nothing is ever
    deleted, the viewer chooses whether to use it, and the worst case of
    a wrong suggestion is a slightly-off button, not a missed play. That's
    a fundamentally different risk than real cutting, so this is
    deliberately MORE permissive than anything used for actual segment
    boundaries:
      - quiet_thresh reuses SegmentConfig.exit_thresh (0.0058) directly,
        not the much stricter pad_quiet_thresh (0.002) dynamic padding
        uses for real, safety-relevant shrink decisions -- "quiet enough
        that a live segment wouldn't even stay open on this motion alone"
        is a fine bar for a dismissable suggestion, tighter than that
        would just mean fewer, over-cautious suggestions for no reason.
      - min_dip_s is UX-driven, not safety-driven: 2.0s is long enough
        that skipping it is actually worth the viewer's action, short
        enough to catch a real in-play lull (a batter settling, a pause
        before the next pitch) without needing the multi-part
        floor/buffer machinery real padding shrink requires.
    No floor, no safety buffer, no contiguity-from-a-specific-edge scan --
    those exist for padding because padding protects against unknown real
    events; a skip suggestion the viewer can simply not click needs none
    of that.
    """
    quiet_thresh: float = 0.0058   # == SegmentConfig.exit_thresh
    min_dip_s: float = 2.0


def find_skip_suggestions(seg_start, seg_end, motion_times, motion_scores,
                          config: SkipSuggestionConfig | None = None):
    """Contiguous quiet stretches within [seg_start, seg_end] long enough
    to be worth a manual skip-ahead suggestion. Returns a list of
    (dip_start, dip_end) spans, in the SAME timeline as seg_start/seg_end
    (source-file-local seconds, matching the rest of the manifest)."""
    cfg = config or SkipSuggestionConfig()
    motion_times = np.asarray(motion_times, dtype=float)
    motion_scores = np.asarray(motion_scores, dtype=float)
    idx = (motion_times >= seg_start) & (motion_times <= seg_end)
    t, s = motion_times[idx], motion_scores[idx]
    order = np.argsort(t)
    t, s = t[order], s[order]

    suggestions = []
    dip_start = None
    for i in range(len(t)):
        quiet = s[i] < cfg.quiet_thresh
        if quiet and dip_start is None:
            dip_start = t[i]
        elif not quiet and dip_start is not None:
            if t[i] - dip_start >= cfg.min_dip_s:
                suggestions.append((float(dip_start), float(t[i])))
            dip_start = None
    if dip_start is not None and len(t) and t[-1] - dip_start >= cfg.min_dip_s:
        suggestions.append((float(dip_start), float(t[-1])))
    return suggestions
