"""Segment refinement: play extension, padding, and final merge (Stage 3).

This replaces Stage 2's score-level sustain as the mechanism that decides
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

ZONE-LOCAL CLOSE (Stage 11): whole-frame settle protects against closing
mid-play, but it's a blunt instrument for anything localized to one part
of the frame -- it waits for the ENTIRE frame to quiet down, not just the
specific thing that's resolving. pipeline.fusion.compute_zone_velocity
gives a tighter, zone-local signal: box-height-normalized speed of
whoever's in a calibrated zone, base OR plate. Validated against
clip_base1/3/4 for bases: a resting fielder (standard positioning, no
play happening) reads 0.00-0.12 bh/s for many seconds; a real throw/catch
arrival spikes to 0.32-1.01 bh/s in the few seconds before the catch, then
drops back down once the fielder is set to receive it. Raw base OCCUPANCY
can't tell these apart (Stage 10's resting-fielder confound: a fielder can
sit in the zone at 92-100% occupancy the whole clip) -- velocity can,
because the confound is about presence, not motion. The identical
spike-then-quiet shape shows up at the PLATE too (Stage 11 tier 1): an
incoming batter walking up then settling into the box, closing out the
walk-up gap left over from the previous play -- same function, same
guard, just a different physical event producing the same signature.
Plate-zone data is noisier than the base clips (a much larger calibrated
radius catches the catcher/umpire/on-deck batter too, not just one
player), so not every at-bat transition produces a usable candidate --
that's fine, the mechanism only ever tightens when it's confident, and
falls back to whole-frame behavior otherwise.

zone_close_candidate() finds the first time, after a genuine velocity
spike, that a zone has gone quiet again -- but extend_segments() only
ever uses that time to SHORTEN the whole-frame close time, never to
lengthen it, and only when there is no whole-frame-detected motion
between the zone candidate and the whole-frame close time. That guard
matters: a continued play elsewhere on the field (a relay throw, a runner
still advancing, a second batter's activity) almost always still shows up
in whole-frame motion even after this one zone has quieted down, and
deferring to whole-frame in that case is what keeps this addition from
ever cutting off real, still-detected action -- it can tighten dead time,
it can never override a real detected signal.

KNOWN LIMITATION (no reference clip currently exercises this): a double
play or a continued relay -- the SAME base receiving a second throw after
a brief lull -- looks identical to "the play is over" from that one
zone's velocity alone, since zone_close_candidate() returns the first
settle after the first arrival and does not re-arm for a second spike
within the same call. The whole-frame guard above catches the common
case, because a real second throw/relay typically also produces broader
whole-frame motion (a runner, a second fielder) that blocks the
tightening. But a hypothetical continued play that stayed entirely
within one base's own zone -- with no detectable whole-frame signature
elsewhere -- would still be missed. Flagging this now rather than
discovering it silently later; it needs a real reference clip with a
double play or relay before it can be tested, let alone fixed.

WALK-UP GAP note: tier 1 above (plate zone reusing this same mechanism)
only tightens the EXTENSION layer -- it's floored at the raw segment's
own end, same as the base case. Measured directly (v2 Step 1, all five
walk-up gaps across clip_60/300/540, not just one): the RAW motion
segment (pipeline.segments, Stage 1/2) already covers 30-64% of a given
gap before extension logic ever runs -- clip_540's ~86s stretch measures
~48.7% of that, right in the middle of the range, not a special case.
Extension itself (this module, excluding padding) adds only ~0-9
percentage points on top in every gap measured. PADDING (pad_and_merge,
NOT extension) is what accounts for most of the rest that reaches the
final output -- 12-32 percentage points, more than extension everywhere
and more than the raw segment itself on the two shortest gaps measured.
An earlier version of this note credited padding's contribution to "the
extension layer"; that was wrong -- keeping the two separate matters,
since they're different fixes with different costs (see PADDING
VALIDATION below). Tier 1 cannot touch the raw-segment piece by
construction. A fix that could would mean changing what counts as
segment-worthy motion in pipeline.segments itself (e.g. weighting by
proximity to the plate), which is a materially bigger, riskier change
than anything in Stage 10/11 so far -- under active feasibility
investigation (v2), not attempted here.

PADDING VALIDATION (re-measured, v2 padding track): the original
pre_pad_s=3.0/post_pad_s=1.5 were set once, when this module was first
written, against only clip_60/300/540 (largest observed pre-open need:
1.2s, hence 3.0s = a 2.5x margin) -- six more reference clips
(clip_base1-4, clip_foul1, clip_whiff1) were added after that and the
number was never re-checked against them, despite padding being the
single biggest contributor to kept dead time in most walk-up gaps.
Re-measured directly against all nine current clips (binary-searching the
real minimum pre_pad_s/post_pad_s that still produces full, contiguous
coverage of every required check_continuity event via this exact
pad_and_merge()): the real current max need is 2.8s pre / 1.3s post
(clip_540's e4), meaning the original 2.5x margin had quietly eroded to
~1.1x as harder clips were added -- not the comfortable cushion it was
meant to be.

Restoring the LITERAL 2.5x margin on that new max (7.0s/3.25s) passes
recall and continuity cleanly on all nine clips, but tested against
full_game.mkv (the one real 67.5-minute recording) it nearly halves the
pipeline's actual dead-time reduction: 11.4 minutes cut at the old
defaults vs. only 4.7 minutes cut at literal 2.5x, because padding that
large merges most of the game's distinct plays into a handful of huge
segments (123 -> 32 final segments). The short reference clips can't
surface this cost since each brackets one isolated play with nothing
nearby to over-merge -- only real, long-form footage exposes it. A sweep
between the two showed a smooth, monotonic cost curve with no free
margin anywhere on it: every bit of safety costs real cut time.

Landed on pre_pad_s=4.5/post_pad_s=2.2 (~1.6x over the real 2.8s/1.3s
max) rather than either extreme: a real, meaningful improvement over the
eroded ~1.1x margin, while still keeping ~7.6 of the original 11.4
minutes of real dead-time reduction on full_game.mkv. Landed above the
bare 1.6s/0.7s "just enough" minimum specifically because of an Edit Log
asymmetry: it can restore a segment that padding cut ENTIRELY, but there
is no way (v1 has no manual re-cut/extend feature) to fix a segment padding
cut INTO -- a mid-play clip is unrecoverable in the UI, while an
over-cautious full cut is a one-click fix. That asymmetry is why this
lands with real headroom rather than the tightest value that merely
passes today's nine clips. Confirmed clean (9/9 recall, 9/9 continuity)
against all nine clips at this exact value, run fresh, not inferred from
a nearby candidate.

PRIORITY RULE CHANGE (post enter-side scale boost): the 4.5s/2.2s
headroom above was a deliberate choice, not a forced one -- the owner
has since explicitly decided to spend it, accepting real recall risk in
exchange for more aggressive dead-time cutting (README's "Priority rule:
v1 default vs. current live setting"). Attempting to ship the literal
2.8s/1.3s bare minimum from this section's own binary search FAILED --
that number is stale, measured before the enter-side boost existed, and
re-running scripts/regression.py at exactly that value regresses
continuity on 2 of the 9 known clips (clip_540's e4, clip_base3's e1)
today. Re-binary-searched fresh against the current pipeline instead:
RefineConfig now ships pre_pad_s=2.8/post_pad_s=1.85 (only post needed to
grow), confirmed 9/9 recall and continuity fresh, zero margin beyond
that. See README's Current Status for the real full_game.mkv cut-time
delta this bought, and for why "bare minimum" needed re-deriving instead
of reusing the old number.
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
    # Padding defaults: PRIORITY RULE CHANGE (post enter-side scale boost) --
    # the project's owner explicitly, deliberately loosened the standing
    # never-miss-a-play bias, accepting real recall risk in exchange for
    # more aggressive dead-time cutting (see README's "Priority rule: v1
    # default vs. current live setting" for the full decision). The
    # previous default (4.5s/2.2s, ~1.6x over the then-measured 2.8s/1.3s
    # bare minimum) was a deliberate safety cushion against the Edit Log's
    # cut-into-a-play asymmetry; that cushion is what's being spent here.
    #
    # IMPORTANT, found while making this exact change: the OLD bare
    # minimum (2.8s/1.3s, from this module's PADDING VALIDATION section)
    # is stale and NO LONGER SUFFICIENT -- it was measured before the
    # enter-side scale boost shipped. Re-running scripts/regression.py at
    # the literal old 2.8s/1.3s value FAILS continuity today, on 2 of the
    # 9 known clips (clip_540's e4 and clip_base3's e1), not hypothetically
    # on unseen footage -- a real regression on the very clips this number
    # was calibrated against. Isolated before shipping anything (per
    # standing process): the exit_thresh raise alone is NOT the cause
    # (confirmed clean by itself); the padding reduction alone reproduces
    # both failures even with exit_thresh unchanged. Re-binary-searched
    # fresh against TODAY's actual pipeline (boost included): pre_pad_s=2.8
    # holds, post_pad_s needed to grow from 1.3 to 1.85 (1.82 still fails
    # clip_base3's e1 by a hair; 1.85 passes clean). This value carries
    # deliberately ZERO margin beyond what's needed for today's 9 clips --
    # the takeaway isn't "2.8/1.85 is now safe forever," it's that a
    # literal bare-minimum number has no headroom against ANY other
    # pipeline change, including ones (like the enter boost) that don't
    # touch padding at all. Confirmed via scripts/regression.py, ALL PASS,
    # both previously-failing continuity checks explicitly re-verified.
    pre_pad_s: float = 2.8
    post_pad_s: float = 1.85
    final_merge_gap_s: float = 0.5
    # Stage 11: box-heights/sec (pipeline.fusion.compute_zone_velocity's
    # units). 0.20 sits above every resting-fielder sample measured across
    # clip_base1/3/4 (max 0.12) and below the real arrival spikes (min
    # 0.32) -- see this module's ZONE-LOCAL CLOSE docstring section. Reused
    # as-is for the plate zone (tier 1) rather than separately re-derived --
    # not proven optimal there, just not proven wrong either; the guard
    # means a bad threshold costs missed tightening opportunities, not
    # correctness, so this wasn't worth blocking tier 1 on.
    zone_arrival_thresh: float = 0.20
    zone_settle: SettleConfig = field(
        default_factory=lambda: SettleConfig(threshold=0.20, min_quiet_s=1.5))


def departure_times(det_times, occupied):
    """Times at which the plate went occupied -> vacant."""
    det_times = np.asarray(det_times, dtype=float)
    occupied = np.asarray(occupied, dtype=bool)
    out = []
    for i in range(1, len(det_times)):
        if occupied[i - 1] and not occupied[i]:
            out.append(float(det_times[i]))
    return out


def zone_close_candidate(zone_times, zone_velocity, search_start, cap_end,
                         arrival_thresh, zone_settle_cfg):
    """Earliest time it's safe to consider THIS base's play resolved,
    using zone-local velocity alone. Returns None if no genuine arrival
    was ever seen in [search_start, cap_end] -- a resting fielder with no
    real play must never produce a candidate (this function never looks
    at occupancy, only velocity, precisely because occupancy alone can't
    tell the two apart -- see this module's ZONE-LOCAL CLOSE docstring).

    Also returns None if an arrival was seen but the zone never actually
    settled again within the window -- an unresolved (or still-ongoing)
    play must not produce a fabricated close time.

    No whole-frame cross-check happens here -- that's extend_segments()'s
    job, the only caller allowed to act on this candidate."""
    zone_times = np.asarray(zone_times, dtype=float)
    zone_velocity = np.asarray(zone_velocity, dtype=float)
    idx = (zone_times >= search_start) & (zone_times <= cap_end)
    zt, zv = zone_times[idx], zone_velocity[idx]
    active = np.nonzero(zv >= arrival_thresh)[0]
    if len(active) == 0:
        return None
    arrival_time = float(zt[active[0]])
    after = zt >= arrival_time
    settled = settled_mask(zt[after], zv[after], zone_settle_cfg)
    settled_at = np.nonzero(settled)[0]
    if len(settled_at) == 0:
        return None
    return float(zt[after][settled_at[0]])


def extend_segments(segments, motion_times, motion_smooth, departures,
                    atbat_fires, duration, config: RefineConfig | None = None,
                    zone_velocities: dict | None = None):
    """Apply the play-extension rule to each eligible segment's end.

    `zone_velocities`, if given, is {base_name: (times, velocity)} from
    pipeline.fusion.compute_zone_velocity -- see this module's ZONE-LOCAL
    CLOSE docstring section for what it does and its known limitation.
    Omitting it (the default) is byte-for-byte identical to Stage 10
    behavior: every existing caller that doesn't pass it is unaffected."""
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

        if zone_velocities and e > b:
            best_zone_e = None
            for zt, zv in zone_velocities.values():
                cand = zone_close_candidate(zt, zv, a, cap_end,
                                            cfg.zone_arrival_thresh,
                                            cfg.zone_settle)
                if cand is not None and cand < e:
                    if best_zone_e is None or cand < best_zone_e:
                        best_zone_e = cand
            if best_zone_e is not None:
                # `e` is BY DEFINITION the last whole-frame-active sample,
                # so (best_zone_e, e] always contains an "active" reading
                # whenever best_zone_e < e -- a naive "any activity in
                # between" check would therefore always block, making this
                # whole mechanism a no-op. What actually matters is
                # whether that trailing activity is a genuine REACTIVATION
                # (quiet, then active again -- a real second event, e.g. a
                # relay throw) versus a monotonic tail-off of the SAME
                # settling process the zone already tracked (never dips
                # and rises again). Only the former should block. Includes
                # the sample AT best_zone_e itself (>=, not >) -- the
                # quiet half of a reactivation can land exactly there.
                between = (motion_times >= best_zone_e) & (motion_times <= e)
                ws_between = motion_smooth[between]
                was_quiet = False
                reactivated = False
                for val in ws_between:
                    if val < cfg.settle.threshold:
                        was_quiet = True
                    elif was_quiet:
                        reactivated = True
                        break
                if not reactivated:
                    e = max(b, best_zone_e)

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
                    config: RefineConfig | None = None,
                    zone_velocities: dict | None = None):
    """Full refinement: extension then padding/merge. `segments` should
    already be veto-filtered. `zone_velocities` -- see extend_segments()."""
    cfg = config or RefineConfig()
    deps = departure_times(det_times, occupied)
    extended = extend_segments(segments, motion_times, motion_smooth, deps,
                               atbat_fires, duration, cfg, zone_velocities)
    return pad_and_merge(extended, duration, cfg)
