"""Fuse the ~10 Hz motion signal with the ~1 Hz person-detection signal.

Pure numpy — no torch/cv2 — so every rule here is unit-testable.

Alignment: zero-order hold. Each motion sample uses the nearest detection
sample within `staleness_s`; boxes are held constant (players don't get
interpolated into positions they never occupied) and dilated to absorb
movement within the hold window. Motion samples with no detection inside
the staleness bound fall back to motion-only scoring and are treated
protectively everywhere else (they can never justify a veto).

Score (additive — a weak signal can only fail to add, never subtract):

    combined = motion + w_person * person_motion + w_occupancy * occupied

so combined >= motion everywhere, which guarantees Stage 2 can never flag
less than the Stage 1 motion baseline at the same thresholds.

Veto: a segment may be dropped only when person detection is certain there
was nobody near its motion — every sample in the segment had a valid
(non-stale) detection AND none of its moving blocks intersected any person
box. One person-adjacent sample, or one stale sample, keeps the segment.
The regression suite additionally fails hard if a vetoed segment overlaps
a ground-truth required event.
"""

from dataclasses import dataclass, field

import numpy as np


@dataclass
class PlateZone:
    center_xy: tuple      # (x, y) original pixels
    radius_px: float


@dataclass
class FusionConfig:
    # Both weights are deliberately small: each auxiliary signal must ASSIST
    # a borderline motion signal, not sustain a segment on its own. In
    # particular w_occupancy must stay below SegmentConfig.exit_thresh
    # (0.003), otherwise an occupied plate keeps a segment open through
    # arbitrary dead time (observed: 98% of a clip flagged).
    w_person: float = 0.3        # weight on person-covered motion mass
    w_occupancy: float = 0.002   # additive boost while the plate is occupied
    box_dilate: float = 0.30     # fractional box growth before overlap tests
    staleness_s: float = 0.75    # max age of a detection sample for ZOH
    block_active: float = 0.02   # grid block counts as "moving" above this
    stationary_v: float = 0.30   # max box-heights/sec to count as stationary


@dataclass
class FusedResult:
    times: np.ndarray
    combined: np.ndarray         # final additive score per motion sample
    motion: np.ndarray           # raw motion score (input, for comparison)
    person_motion: np.ndarray    # motion mass inside dilated person boxes
    occupied: np.ndarray         # bool: plate zone occupied (stationary person)
    covered: np.ndarray          # bool: any moving block overlaps a person box
    det_valid: np.ndarray        # bool: a non-stale detection sample existed


def dilate_box(box, frac):
    x1, y1, x2, y2 = box
    dw, dh = (x2 - x1) * frac / 2, (y2 - y1) * frac / 2
    return (x1 - dw, y1 - dh, x2 + dw, y2 + dh)


def boxes_to_grid_mask(boxes, frame_size, analysis_size, border_px,
                       grid_shape, dilate_frac=0.0):
    """Rasterize original-pixel boxes onto the motion grid (rows, cols)."""
    rows, cols = grid_shape
    mask = np.zeros((rows, cols), dtype=bool)
    fw, fh = frame_size
    aw, ah = analysis_size
    bw, bh = border_px
    core_w, core_h = aw - 2 * bw, ah - 2 * bh
    if core_w <= 0 or core_h <= 0:
        return mask
    sx, sy = aw / fw, ah / fh
    for box in boxes:
        x1, y1, x2, y2 = dilate_box(box, dilate_frac)
        # original px -> analysis px -> core-relative -> grid
        gx1 = (x1 * sx - bw) / core_w * cols
        gx2 = (x2 * sx - bw) / core_w * cols
        gy1 = (y1 * sy - bh) / core_h * rows
        gy2 = (y2 * sy - bh) / core_h * rows
        c1, c2 = max(0, int(np.floor(gx1))), min(cols, int(np.ceil(gx2)))
        r1, r2 = max(0, int(np.floor(gy1))), min(rows, int(np.ceil(gy2)))
        if c2 > c1 and r2 > r1:
            mask[r1:r2, c1:c2] = True
    return mask


def compute_occupancy(det_times, det_boxes, zone: PlateZone,
                      stationary_v: float, require_stationary_entry: bool = True):
    """Per detection sample: is this zone occupied by a person?

    Occupancy has hysteresis, matching at-bat semantics:
    - ENTERING the occupied state requires a roughly-stationary person in
      the zone by default (someone jogging through the zone never
      triggers it) — this is deliberately tuned for the plate use case
      (Stage 2/3): a batter settling in to face a pitch. STAYING occupied
      only requires a person still in the zone — a batter striding into a
      load or swing moves fast but is still mid-at-bat, and dropping the
      boost at that exact moment is how a play gets cut.
    - `require_stationary_entry=False` (Stage 10, base zones): enter the
      occupied state the instant any person's box overlaps the zone, no
      stationarity check. Validated against real footage (clip_base1/3/4)
      that the stationary-entry requirement, reused as-is for bases,
      measurably lags or misses a defensive catch/tag — a fielder is
      often still moving fast at the exact instant they receive the
      ball, which is the opposite of the plate's "batter has settled in"
      assumption. Default stays True so every existing plate call site
      (fuse(), and therefore atbat_start_times() and refine_segments())
      is provably unaffected by this parameter's existence.
    The state clears the moment no person is in the zone, in both modes.

    Stationarity compares each in-zone box against the closest-center box in
    the PREVIOUS detection sample; a box with no plausible predecessor is not
    stationary (someone walking into frame must persist to count).
    """
    n = len(det_times)
    occupied = np.zeros(n, dtype=bool)
    cx0, cy0 = zone.center_xy
    prev_state = False
    for i in range(n):
        in_zone = False
        stationary = False
        if i > 0:
            dt = det_times[i] - det_times[i - 1]
            prev_centers = [(0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3]))
                            for b in det_boxes[i - 1]]
            for b in det_boxes[i]:
                cx, cy = 0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3])
                if (cx - cx0) ** 2 + (cy - cy0) ** 2 > zone.radius_px ** 2:
                    continue
                in_zone = True
                if dt <= 0 or not prev_centers:
                    continue
                h = max(1.0, b[3] - b[1])
                d = min(np.hypot(cx - px, cy - py) for px, py in prev_centers)
                if d / dt <= stationary_v * h:
                    stationary = True
                    break
        elif i == 0:
            # no previous sample to compare against for stationarity, but
            # a person can still simply BE in the zone in the first sample
            for b in det_boxes[i]:
                cx, cy = 0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3])
                if (cx - cx0) ** 2 + (cy - cy0) ** 2 <= zone.radius_px ** 2:
                    in_zone = True
                    break
        entry_condition = stationary if require_stationary_entry else in_zone
        occupied[i] = entry_condition or (prev_state and in_zone)
        prev_state = occupied[i]
    return occupied


def compute_zone_velocity(det_times, det_boxes, zone: PlateZone):
    """Per detection sample: box-height-normalized speed (box-heights/sec)
    of the fastest in-zone box, relative to the nearest box in the
    previous sample -- the SAME normalization compute_occupancy's
    stationarity check already uses (see its docstring), so a reading
    from this function is directly comparable to that one's stationary_v.

    0.0 when nothing is in the zone, or at the first sample (no previous
    frame to compare against). A box with no plausible predecessor
    contributes 0.0 for that box -- same "not yet moving" convention as
    compute_occupancy's stationarity check (someone freshly appearing in
    the zone isn't a measured velocity yet, just a position).

    Stage 11: this is the signal that separates a resting fielder
    (near-zero, sustained -- Stage 10's "resting fielder confound",
    where raw occupancy alone can't tell a standing fielder from a live
    play) from a genuine transient arrival (a clear speed spike).
    Validated directly against clip_base1/3/4: resting-period samples
    measured 0.00-0.12 bh/s, the real throw/catch arrival spiking to
    0.32-1.01 bh/s, consistently in the few seconds BEFORE the catch/tag
    instant (the fielder moving to the bag, then typically stationary AT
    the catch itself, not after it) -- see pipeline/refine.py's
    zone_close_candidate() and RefineConfig.zone_arrival_thresh."""
    det_times = np.asarray(det_times, dtype=float)
    n = len(det_times)
    out = np.zeros(n)
    cx0, cy0 = zone.center_xy
    r2 = zone.radius_px ** 2
    prev_boxes = []
    for i in range(n):
        boxes = det_boxes[i]
        in_zone = [b for b in boxes
                  if (0.5 * (b[0] + b[2]) - cx0) ** 2
                  + (0.5 * (b[1] + b[3]) - cy0) ** 2 <= r2]
        if in_zone and prev_boxes and i > 0:
            dt = det_times[i] - det_times[i - 1]
            if dt > 0:
                prev_centers = [(0.5 * (pb[0] + pb[2]), 0.5 * (pb[1] + pb[3]))
                                for pb in prev_boxes]
                speeds = []
                for b in in_zone:
                    cx, cy = 0.5 * (b[0] + b[2]), 0.5 * (b[1] + b[3])
                    h = max(1.0, b[3] - b[1])
                    d = min(np.hypot(cx - px, cy - py) for px, py in prev_centers)
                    speeds.append((d / dt) / h)
                out[i] = max(speeds)
        prev_boxes = boxes
    return out


def robust_box_width(det_times, det_boxes, frame_size, zone: PlateZone | None = None,
                     edge_margin_frac: float = 0.02, min_samples: int = 5):
    """A robust, per-CLIP (not per-frame) person-box-width estimate: the
    median width of detections that (a) fall inside `zone` if given (e.g.
    the plate zone, to compare like-for-like against how far away the
    batter/catcher/pitcher normally are), and (b) are NOT clipped by the
    LEFT/RIGHT frame edge.

    This exists to measure camera-to-subject scale as a defense against
    the failure this project's enter_thresh margin investigation found:
    `pipeline.motion`'s score is NOT scale-invariant (it scales with
    roughly the square of the subject's linear size in frame), so a more
    distant camera setup than the reference clips share could plausibly
    push a real play's score below `enter_thresh` outright. A per-frame
    "nearest box" reading was tried and rejected as a fix for that same
    investigation: box height swung ~59px to ~429px across consecutive
    ~1s samples of the SAME real subject (an RF-DETR identity-tracking
    artifact, not real depth change), so only a robust, clip-wide
    aggregate is trustworthy here, never a single frame's box.

    Height is deliberately NOT used, unlike compute_zone_velocity's own
    normalization -- confirmed directly with two controlled clips filmed
    at two different real distances (distance_test_close/far.mov, same
    camera, same subject, same motion): the close clip frames the subject
    so tightly that their feet are clipped by the frame's BOTTOM edge in
    every single sample (box bottom sits within a few px of the frame
    edge throughout), which silently caps box HEIGHT at close range and
    erases the real distance difference a naive height comparison would
    show (median height came out ~666px for both clips, despite the
    subject being dramatically closer in one). Width isn't vulnerable to
    THAT failure mode for a person standing upright in a landscape frame
    -- vertical cropping doesn't touch a box's horizontal extent -- which
    is exactly why this function must NOT also reject vertically-clipped
    boxes (an earlier version of this function did, by checking top/bottom
    margins here too, and it silently discarded every single sample from
    distance_test_close.mov as a result, since all of them are bottom-
    clipped; caught by tests/test_distance_scaling.py, not a fresh check
    added after the fact). Only LEFT/RIGHT clipping (someone exiting frame
    sideways) can corrupt a width reading, so only that is excluded below.

    Returns None if fewer than `min_samples` boxes survive filtering --
    an honest "not enough signal to trust a scale reading" rather than a
    noisy guess from one or two samples.
    """
    fw, fh = frame_size
    margin = edge_margin_frac * fw
    widths = []
    for boxes in det_boxes:
        for b in boxes:
            x1, y1, x2, y2 = b
            if x1 <= margin or x2 >= fw - margin:
                continue
            if zone is not None:
                cx, cy = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                cx0, cy0 = zone.center_xy
                if (cx - cx0) ** 2 + (cy - cy0) ** 2 > zone.radius_px ** 2:
                    continue
            widths.append(x2 - x1)
    if len(widths) < min_samples:
        return None
    return float(np.median(widths))


def scale_boost_factor(det_times, det_boxes, frame_size, zone: PlateZone,
                       reference_width_px: float) -> float:
    """max(1.0, reference_width_px / w_batch) -- the LINEAR scale-boost
    factor. Callers must SQUARE this before multiplying a motion score,
    since pipeline.motion's score scales with subject AREA in frame
    (roughly the square of linear size), not linear size directly.

    Returns exactly 1.0 (no boost) whenever `robust_box_width` can't
    measure a reliable width (too few non-edge-clipped, in-zone
    detections) -- no signal means behave exactly as before this
    existed, never guess. Also 1.0 whenever the batch's subject reads
    AS BIG OR BIGGER than the reference (closer than or equal to the
    distance enter_thresh was tuned against): this function only ever
    boosts for a MORE distant camera than the reference clips, it never
    reduces a score for a closer one. That asymmetry is the entire
    safety property this exists for -- see
    pipeline.segments.SegmentConfig.reference_plate_box_width_px for the
    full derivation and the enter_thresh camera-distance investigation
    this closes out.
    """
    w_batch = robust_box_width(det_times, det_boxes, frame_size, zone=zone)
    if w_batch is None or w_batch <= 0:
        return 1.0
    return max(1.0, reference_width_px / w_batch)


def compute_all_occupancy(det_times, det_boxes, zones: dict,
                          stationary_v: float,
                          require_stationary_entry: bool = False) -> dict:
    """compute_occupancy() for each named zone (Stage 10: base_name ->
    PlateZone from pipeline.calibration.resolve_base_zones()). A thin
    convenience loop, not new logic -- kept separate from fuse()/refine.py
    on purpose, since Stage 10 computes and validates this signal without
    wiring it into segment-closing decisions (Stage 11's job).

    Defaults to require_stationary_entry=False, unlike compute_occupancy()
    itself -- this function has no existing callers (it's new, base-only),
    so its own default can safely encode what validation against real
    footage found: clip_base4 showed the stationary-entry requirement,
    reused as-is from the plate, lags detecting a fielder arriving at a
    base by 3 full seconds (onset t=14 vs t=11) because a fielder is
    often still moving when they receive the ball, unlike a batter
    settling in at the plate. See scripts/validate_base_occupancy.py."""
    return {name: compute_occupancy(det_times, det_boxes, zone,
                                    stationary_v, require_stationary_entry)
           for name, zone in zones.items()}


def fuse(motion_times, motion_scores, motion_grids,
         frame_size, analysis_size, border_px,
         det_times, det_boxes, zone: PlateZone | None,
         config: FusionConfig | None = None) -> FusedResult:
    cfg = config or FusionConfig()
    motion_times = np.asarray(motion_times, dtype=float)
    motion_scores = np.asarray(motion_scores, dtype=float)
    motion_grids = np.asarray(motion_grids, dtype=float)
    det_times_arr = np.asarray(det_times, dtype=float)

    n = len(motion_times)
    person_motion = np.zeros(n)
    covered = np.ones(n, dtype=bool)      # protective default
    det_valid = np.zeros(n, dtype=bool)
    occupied = np.zeros(n, dtype=bool)

    occ_det = (compute_occupancy(det_times, det_boxes, zone, cfg.stationary_v)
               if zone is not None and len(det_times) else
               np.zeros(len(det_times), dtype=bool))

    grid_shape = motion_grids.shape[1:] if motion_grids.ndim == 3 else (0, 0)
    # cell area fraction: grid cells are equal-area tiles of the core region
    n_cells = grid_shape[0] * grid_shape[1] if grid_shape[0] else 1
    box_masks = {}

    for i, t in enumerate(motion_times):
        if len(det_times_arr) == 0:
            continue
        j = int(np.argmin(np.abs(det_times_arr - t)))
        if abs(det_times_arr[j] - t) > cfg.staleness_s:
            continue  # stale: motion-only, covered stays True protectively
        det_valid[i] = True
        occupied[i] = bool(occ_det[j])
        if j not in box_masks:
            box_masks[j] = boxes_to_grid_mask(
                det_boxes[j], frame_size, analysis_size, border_px,
                grid_shape, cfg.box_dilate)
        mask = box_masks[j]
        grid = motion_grids[i]
        person_motion[i] = float(np.sum(grid[mask]) / n_cells)
        active = grid > cfg.block_active
        covered[i] = bool(np.any(active & mask))

    combined = (motion_scores + cfg.w_person * person_motion
                + cfg.w_occupancy * occupied.astype(float))
    return FusedResult(times=motion_times, combined=combined,
                       motion=motion_scores, person_motion=person_motion,
                       occupied=occupied, covered=covered, det_valid=det_valid)


def apply_veto(segments, fused: FusedResult):
    """Split segments into (kept, vetoed).

    Vetoed only if EVERY motion sample inside the segment both (a) had a
    valid detection sample and (b) had no moving block overlapping any
    person box. Any stale/undetected sample, or any person-adjacent motion,
    keeps the segment.
    """
    kept, vetoed = [], []
    for a, b in segments:
        idx = (fused.times >= a) & (fused.times <= b)
        if idx.any() and fused.det_valid[idx].all() and not fused.covered[idx].any():
            vetoed.append((a, b))
        else:
            kept.append((a, b))
    return kept, vetoed


def vetoed_overlapping_required(vetoed_segments, required_events):
    """Safety net: return vetoed segments that overlap any required
    ground-truth event window. Regression FAILS if this is non-empty."""
    bad = []
    for a, b in vetoed_segments:
        for e in required_events:
            ws, we = e["window"]
            if a <= we and b >= ws:
                bad.append(((a, b), e["id"]))
    return bad
