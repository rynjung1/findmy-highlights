"""At-bat-start detection (the 'safe to end the previous play' signal).

Fires when, after the plate has been vacant for a while, someone re-occupies
it in a sustained way AND the field's motion has settled. Used by segment
refinement to close a play-extension: the fire is *consumed* by a pending
extension and ignored otherwise, so a spurious fire during dead time is a
no-op by construction.

Design safeguards (validated against real footage):
- ARMING: the detector only arms after the plate has been continuously
  vacant for >= vacancy_arm_s. Detection flicker (sub-threshold vacancies)
  cannot re-arm it.
- PENDING RE-ARM: once armed, every occupied sample is evaluated until the
  detector fires or the plate empties again — so a genuine at-bat start is
  not missed just because the re-occupancy instant was still busy
  (measured failure of the single-instant variant: clip_540's at-bat at
  t=32, clip_300's batter change at t=73).
- SUSTAIN + SETTLE: a fire needs occupancy across >= occ_frac of the next
  sustain_s seconds AND motion settled (pipeline.settle.settled_mask) by
  the end of that window — the SAME settle logic and threshold that
  pipeline/refine.py uses to decide a play-extension may end, so the two
  mechanisms can't disagree about what "settled" means. A batter stepping
  out and back mid-at-bat while anything is still happening cannot fire;
  when everything has settled, firing is harmless (nothing pending to
  consume it).
"""

from dataclasses import dataclass, field

import numpy as np

from pipeline.settle import SettleConfig, is_settled_at_end


@dataclass
class AtBatConfig:
    vacancy_arm_s: float = 2.0    # continuous vacancy needed to arm
    sustain_s: float = 4.0        # re-occupancy must hold this long
    occ_frac: float = 0.8         # fraction of sustain window occupied
    settle: SettleConfig = field(default_factory=SettleConfig)


def atbat_start_times(det_times, occupied, motion_times, motion_smooth,
                      config: AtBatConfig | None = None):
    """Return times (s) at which a new at-bat is judged to have started."""
    cfg = config or AtBatConfig()
    det_times = np.asarray(det_times, dtype=float)
    occupied = np.asarray(occupied, dtype=bool)
    motion_times = np.asarray(motion_times, dtype=float)
    motion_smooth = np.asarray(motion_smooth, dtype=float)

    fires = []
    armed = True          # armed at clip start: no prior at-bat is known
    vacancy = 0.0
    prev_t = None
    for i, (t, occ) in enumerate(zip(det_times, occupied)):
        dt = (t - prev_t) if prev_t is not None else 0.0
        prev_t = t
        if not occ:
            vacancy += dt
            if vacancy >= cfg.vacancy_arm_s:
                armed = True
            continue
        vacancy = 0.0
        if not armed:
            continue
        w_end = t + cfg.sustain_s
        widx = (det_times >= t) & (det_times <= w_end)
        frac = float(np.mean(occupied[widx])) if widx.any() else 0.0
        midx = (motion_times >= t) & (motion_times <= w_end)
        # require actual motion data in the window — is_settled_at_end
        # treats empty input as settled (nothing to wait out), which is
        # the right default for extension but wrong here: no data should
        # never be read as "confirmed quiet, safe to fire"
        settled = bool(midx.any()) and \
            is_settled_at_end(motion_times[midx], motion_smooth[midx], cfg.settle)
        if frac >= cfg.occ_frac and settled:
            fires.append(float(t))
            armed = False
    return fires
