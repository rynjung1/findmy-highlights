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
    # exit_thresh WAS re-measured and raised (0.003 -> 0.0045, v2 segments.py
    # retune): several required check_continuity events legitimately dip far
    # below the old 0.003 inside their own window (e.g. clip_60's e5 down to
    # 0.00004), meaning raw hysteresis already fragments some real plays
    # today and continuity survives via pipeline.refine's separate,
    # settle-based extension logic, not via this threshold -- so there was
    # real, unused room here, unlike enter_thresh above. Swept safely up to
    # 0.01 (above enter_thresh itself, a degenerate configuration) with zero
    # recall/continuity failures on all 9 clips, but landed on 0.0045
    # deliberately short of that: keeps a comfortable ~25% hysteresis gap
    # below enter_thresh rather than compounding enter_thresh's own thin
    # margin on the same underlying mechanism. Confirmed on full_game.mkv
    # (the one real 67.5-minute recording): cuts 8.8 real minutes of dead
    # time vs. 7.6 at the old default, a genuine improvement, not just a
    # short-reference-clip artifact -- see scripts/regression.py output and
    # README's Current Status for the full before/after.
    exit_thresh: float = 0.0045
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
