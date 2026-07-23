# Find My Highlights

Takes one or more long, single-angle recordings of a sports game (phone or
GoPro footage of rec-league softball or basketball) and produces one finished
highlight video containing just the live-action segments, with dead time
removed. Multiple input files are treated as one continuous game timeline and
stitched into a single output.

Alongside the finished video, an Edit Log shows every segment that was cut,
lets you preview each one, and lets you restore any segment the detection
pipeline shouldn't have trimmed.

**Current scope (v1):** detect *action vs. dead time* — a missed swing and a
hit swing both count as "action" and both get kept. Distinguishing outcomes
(whiff vs. hit vs. called strike) is explicitly out of scope for now; see
[Known limitations](#known-limitations--non-goals-for-this-version).

## Architecture overview

Built so far:

- **Motion detection** (`pipeline/motion.py`) — scans a video at ~10
  samples/sec, downscales each frame, estimates the global camera
  translation between consecutive frames with phase correlation and removes
  it, then measures what fraction of pixels still changed. That residual is
  localized motion: players moving, a swing, a run. A camera pan therefore
  does not read as action (with one known limitation: if the frame is
  otherwise featureless, a single large moving object can be mistaken for
  camera motion — not a realistic condition for game footage).
- **Segment extraction** (`pipeline/segments.py`) — smooths the motion
  scores, applies hysteresis thresholding (a segment opens at a higher
  threshold than it closes at, so brief dips mid-play don't split it),
  merges near-adjacent segments, and drops sub-second blips. Thresholds are
  deliberately permissive: over-flagging is acceptable, missing a play is
  not. Pure logic, fully unit-tested without video.
- **Person detection** (`pipeline/detection.py`) — RF-DETR Base
  (Apache-2.0, code and weights) detects players on frames sampled at
  ~1 fps, run at 1120px input resolution (validated to catch distant
  fielders down to ~19px tall). Results are cached in `.cache/` keyed by
  file + config, so re-runs are free.
- **Plate calibration** (`scripts/calibrate.py`) — one-time manual step per
  camera setup marking where home plate is (see Setup below). Enables the
  plate-occupancy signal.
- **Signal fusion** (`pipeline/fusion.py`) — aligns the ~10 Hz motion
  signal with the ~1 Hz detections (zero-order hold with a staleness
  bound), then scores additively: motion + a boost for motion inside
  person boxes + a small boost while a roughly-stationary person occupies
  the plate zone. A segment is vetoed only when detection is certain no
  person was ever near its motion — proven to fire correctly on
  constructed person-free footage (`tests/test_veto_e2e.py`), and the
  regression suite fails hard if a veto ever touches a known real play.

  **Honest Phase 2 finding:** on the reference footage, person detection
  did not improve the keep/cut decision. Motion-only already had full
  recall (every real play), the veto never fires on real clips (measured:
  no person-free motion run longer than 0.6s — essentially all motion on
  a rec field is human), and the fused signals *increase* flagged time if
  used to hold segments open indiscriminately. The measured value of
  person/plate signals on this footage is the **plate-occupancy
  timeline**: the input Phase 3's at-bat boundary logic needs. The
  original hypothesis — person detection discriminates real plays from
  milling — is false for this footage, because the milling is also
  people. The discrimination has to come from game context (plate
  occupancy over time), which is Phase 3.
- **Shared settle logic** (`pipeline/settle.py`) — both play extension and
  at-bat detection need to answer the same question, "has the field been
  quiet for a sustained period?", and must agree on the answer: a segment
  extension and the at-bat detector independently deciding "settled" means
  different things is exactly how one mechanism could hold a segment open
  past the point the other already considers it safe to close. This is the
  single implementation both consume — one threshold, one duration, one
  debounce that tolerates a brief above-threshold blip without resetting a
  real quiet stretch (the fix for the extension bug below). `RefineConfig`
  and `AtBatConfig` each hold a `SettleConfig`; the pipeline wiring
  constructs exactly one and passes it to both, so the numbers can't drift
  apart between the two call sites.
- **At-bat boundary detection** (`pipeline/atbat.py`) — decides when a new
  batter has genuinely started, so a live play can be safely closed.
  Fires only when: the plate was vacant for a while (arms the detector),
  then re-occupied in a *sustained* way (≥80% of a 4s window) *and* motion
  has settled (by `pipeline/settle.py`, as of the window's end). A pending
  re-arm keeps evaluating through a busy re-occupancy instant rather than
  giving up (a genuine at-bat start during a still-busy transition would
  otherwise be missed). Validated against real footage: correctly ignores
  mid-at-bat step-outs (a batter leaving and returning to the box) and a
  constructed multi-step-out sequence (`tests/test_atbat.py`), and
  correctly identifies a real batter change in clip_300 found during this
  analysis (see ground truth `e6`).
- **Segment refinement** (`pipeline/refine.py`) — replaces Phase 2's
  score-level sustain with an explicit play-extension step: a segment that
  closes on raw motion is held open until `pipeline/settle.py` says motion
  has genuinely settled, up to a capped trail duration, closing early if
  the at-bat detector fires. Padding (pre/post) and a final merge follow.
  One mechanism now owns the segment boundary — the old dual-hysteresis
  sustain path still exists in `pipeline/segments.py` (parameterized,
  unit-tested) but nothing feeds it in the live pipeline.

  **A real bug caught by testing against real footage, not synthetic
  cases:** the first version of extension ended at the *first* motion
  sample below the settle floor. Real defensive-play motion isn't
  monotonic — it dips (ball in the air, a fielder set) and resumes (a
  throw, a relay) — so this cut plays short by as little as 0.1s after the
  raw segment end. The fix (a sustained-quiet debounce: only end the
  extension after quiet holds continuously for `min_quiet_s`) was
  validated three ways before being trusted: a targeted unit test
  reproducing the exact dip-then-resume shape; a diff against the old
  buggy logic on all three reference clips, confirming the same pattern
  (and the fix's effect) existed in clip_300 and clip_540 too, not only
  where it was first noticed; and a frame-by-frame visual check of every
  newly-included time range, confirming each one is real field activity
  (a relay throw, a bat pickup, a batter's warm-up swings) rather than
  noise the debounce was fooled by.
- **Manifest** (`pipeline/manifest.py`) — builds the spec's manifest
  structure from final kept segments (gaps become `cut` entries covering
  the whole timeline exactly once), with save/load and `kept`/`cut`
  status toggling for the future Edit Log.
- **CLI** (`scripts/detect.py`) — run detection on one video, print
  candidate segments as timestamps (or JSON with `--json`), and
  `--manifest PATH` to write a manifest;
  `--motion-only` gives the Phase 1 baseline.
Planned pieces:

- **Backend API** — a small local service wrapping the pipeline: upload,
  process, progress, manifest read/update, re-export.
- **Frontend Home view** — upload files, watch progress, play/download the
  finished output.
- **Frontend Edit Log view** — review cut segments, preview them, restore any
  that were wrongly trimmed, and re-export.

## Setup

One-time setup on a fresh machine:

1. **Install ffmpeg** (system-level dependency, not a pip package):

   ```sh
   # macOS
   brew install ffmpeg
   # verify it's on PATH
   ffmpeg -version
   ```

   Developed against ffmpeg 8.0.1.

2. **Install Python 3.11** (e.g. `brew install python@3.11`).

3. **Create and activate the virtual environment** (from the project root):

   ```sh
   python3.11 -m venv venv
   source venv/bin/activate
   ```

4. **Install pinned dependencies:**

   ```sh
   pip install -r requirements.txt
   ```

All dependency versions are pinned exactly in `requirements.txt`. Do not
install packages into system/global Python.

5. **Calibrate the plate zone** (one-time, per camera setup — clips shot
   from the same mounted camera share one calibration):

   ```sh
   # interactive: opens a window, click home plate, press 's' to save
   ./venv/bin/python scripts/calibrate.py path/to/any_clip_from_that_camera.mkv

   # or non-interactive with known pixel coordinates
   ./venv/bin/python scripts/calibrate.py path/to/clip.mkv --set 1147,840
   ```

   This writes `calibration.json` next to the video. Without it the
   pipeline still runs, but the plate-occupancy signal is disabled (a
   warning is printed). The first pretrained-model download (~355 MB)
   happens automatically on first detection run and is cached under
   `~/.roboflow/`.

## How to run it

No UI or backend yet — the pipeline runs from the command line:

```sh
# detect final (extended, padded) action segments in one video
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv

# same, as JSON
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --json

# also write a manifest (see "How the manifest works" below)
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --manifest out/clip_60_manifest.json

# Phase 1 baseline (motion only, no person detection or play extension)
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --motion-only
```

## How the manifest works

`pipeline/manifest.py` builds the manifest described in the project spec
from the pipeline's final kept segments: every span of the source video is
listed exactly once, either as a detected `kept` segment or a `cut` gap
between them — so the manifest's segments always cover the whole timeline
with no overlaps or holes. Each entry has an id, a source file, a
timestamp range (both as an `"HH:MM:SS.mmm"` string and as float seconds),
a detection score, and a `status` of `kept` or `cut`.

`set_status()` flips a segment's status (the future Edit Log's restore
action) and `kept_spans()` returns the current kept spans with adjacent
ones merged — that's what a re-export would render. Nothing here mutates
video files; the manifest is the only thing that changes on restore.

Every processed video (or multi-file group) gets a manifest listing every
candidate segment found during detection, with its timestamp range, source
file, detection score, and a `kept`/`cut` status. The final output is always
regenerated from the manifest's current `kept` segments — video files are
never mutated directly, which is what makes restoring a cut segment lossless.

## Known limitations / non-goals for this version

- **No outcome classification (Tier 2).** v1 keeps every action segment,
  including missed swings. Telling a whiff from a hit is a later, harder
  problem.
- v1 targets **one sport at a time** (softball first); basketball comes later
  as a separate modular ruleset.
- No login, accounts, or user profiles.
- No team features, sharing, or recruiting/scouting layer.
- No personalization or per-user learning; no model training or fine-tuning.
- No voice commands or natural-language editing.
- No native mobile app — v1 is a local web app.
- No manual "cut a kept segment" feature yet (the data model is designed to
  allow it later).
- Detection is tuned to **never miss a real play**: borderline segments are
  kept, so some dead time will survive into the output by design.

## Testing

```sh
# unit tests (pure logic + synthetic-video edge cases; no real footage needed)
./venv/bin/python -m pytest tests/

# end-to-end tests that run real model inference (slower; excluded by default)
./venv/bin/python -m pytest tests/ -m e2e

# full detection regression against the reference clips
./venv/bin/python scripts/regression.py
```

- **Reference clips** live in `reference_clips/` (gitignored — video files
  are never committed). Currently three 190-second softball clips, with a
  shared `calibration.json` for the plate zone.
- **Ground truth** lives in `tests/ground_truth/*.json`, one file per clip,
  hand-annotated by frame-level visual review. Each lists action *events*
  with a time window, a type, and a `required` flag: required events are
  confirmed real plays (swing, hit, run, pitch) that the detector must
  capture; non-required events are borderline moments (practice swings,
  warm-up throws) reported for information but not counted against recall.
  An optional `atbat_expectations` block pins frame-verified windows where
  the at-bat-start detector must (`fire_within`) or must not
  (`no_fire_within`) fire — e.g. a real batter change vs. a mid-at-bat
  step-out that looks similar in the raw occupancy signal.
- **Regression script** (`scripts/regression.py`) runs both the
  motion-only baseline and the full refined pipeline (motion → veto →
  play extension → padding) on every reference clip and reports recall,
  borderline capture, and flagged footage for each. It exits non-zero if:
  the refined pipeline misses any required event; refined recall drops
  below the motion-only baseline; a vetoed segment overlaps a required
  event (safety net — a distant or occluded fielder must never silently
  erase a real play); a defensive-play window marked `check_continuity`
  is not covered by one contiguous kept segment; or an at-bat fire/no-fire
  expectation is violated.
- The edge cases covered so far: a video with no motion at all, a
  fraction-of-a-second video, a missing file, and a corrupt file (the last
  two fail with a clean error rather than crashing).
- `tests/test_veto_e2e.py` runs real model inference against constructed
  footage (a wind-blown-branch analog with no person, and a real-person
  photo crop riding the same motion) to give the veto positive-case
  evidence beyond "it never misfired" — the reference clips alone never
  exercise it (longest person-free motion run measured: 0.6s).
