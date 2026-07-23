"""Shared 'has motion settled' logic.

Both play extension (pipeline/refine.py, deciding when to stop holding a
segment open) and at-bat boundary detection (pipeline/atbat.py, deciding
when a new batter has genuinely arrived) need the same underlying
question answered: has the field been quiet for a sustained period? They
must agree on the answer, using the identical threshold and duration, or
the extension could hold a segment open past the exact point the at-bat
detector already considers safe to close (or vice versa) — a previous
version had each computing this independently with different thresholds
(0.002 vs 0.003) and different mechanics, which this module replaces with
one implementation both consume.

'Settled' tolerates a brief above-threshold blip without permanently
disqualifying the interval: any sample >= threshold resets the quiet
clock, but the clock re-accumulates from there, so a short blip followed
by a real quiet stretch of min_quiet_s still settles. This is what fixed
the extension bug (real defensive-play motion dips ~1s then resumes) and
is deliberately applied to the at-bat check too, for the same reason: a
single noisy sample (shadow flicker, detection jitter) inside an
otherwise-quiet re-occupancy window should not block a genuine at-bat
start from being recognized.
"""

from dataclasses import dataclass

import numpy as np

# Empirically set against the reference clips (see pipeline/refine.py):
# 0.002 sits below the segment exit_thresh (0.003) so brief real dips in
# a defensive play don't read as settled, and 1.5s bridges the longest
# such dip observed (clip_60, ~0.9s) without number bridging genuinely
# separate bursts of activity.
DEFAULT_THRESHOLD = 0.002
DEFAULT_MIN_QUIET_S = 1.5


@dataclass
class SettleConfig:
    threshold: float = DEFAULT_THRESHOLD
    min_quiet_s: float = DEFAULT_MIN_QUIET_S


def settled_mask(times, scores, config: SettleConfig | None = None):
    """Per-sample boolean: has `scores` been continuously below
    `threshold` for at least `min_quiet_s` as of this sample? A sample
    at/above threshold resets the quiet clock."""
    cfg = config or SettleConfig()
    times = np.asarray(times, dtype=float)
    scores = np.asarray(scores, dtype=float)
    n = len(times)
    out = np.zeros(n, dtype=bool)
    quiet_since = None
    for i in range(n):
        if scores[i] >= cfg.threshold:
            quiet_since = None
        elif quiet_since is None:
            quiet_since = times[i]
        out[i] = quiet_since is not None and \
            (times[i] - quiet_since) >= cfg.min_quiet_s
    return out


def last_active_time(times, scores, config: SettleConfig | None = None):
    """The latest time in the series with score >= threshold; None if the
    series never had an active sample."""
    cfg = config or SettleConfig()
    times = np.asarray(times, dtype=float)
    scores = np.asarray(scores, dtype=float)
    active = np.nonzero(scores >= cfg.threshold)[0]
    return float(times[active[-1]]) if len(active) else None


def is_settled_at_end(times, scores, config: SettleConfig | None = None):
    """Convenience: is the series settled by its last sample? Empty input
    counts as settled (nothing to wait out)."""
    if len(times) == 0:
        return True
    return bool(settled_mask(times, scores, config)[-1])
