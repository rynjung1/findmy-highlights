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
  the plate zone. Segmentation uses dual hysteresis: only motion can OPEN
  a segment; the fused score can hold one open (protecting a play tail
  where fielders are still converging). A segment is vetoed only when
  detection is certain no person was ever near its motion — proven to
  fire correctly on constructed person-free footage (`tests/test_veto_e2e.py`),
  and the regression suite fails hard if a veto ever touches a known real
  play.

  **Honest Phase 2 finding:** on the reference footage, person detection
  did not improve the keep/cut decision. Motion-only already had full
  recall (every real play), the veto never fires on real clips (measured:
  no person-free motion run longer than 0.6s — essentially all motion on
  a rec field is human), and the fused signals *increase* flagged time
  (they hold segments open longer). The measured value of person/plate
  signals on this footage is (a) holding one defensive-play tail open
  that motion alone dropped (clip_60 146-148s), and (b) the
  plate-occupancy timeline, which is the input Phase 3's at-bat boundary
  logic needs. The original hypothesis — person detection discriminates
  real plays from milling — is false for this footage, because the
  milling is also people. The discrimination has to come from game
  context (plate occupancy over time), which is Phase 3.
- **CLI** (`scripts/detect.py`) — run detection on one video, print
  candidate segments as timestamps (or JSON with `--json`);
  `--motion-only` gives the Phase 1 baseline.
- **Manifest** — a JSON record of every candidate segment (timestamps, source
  file, detection score, kept/cut status). The single source of truth tying
  detection, editing, and export together.
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
# detect candidate action segments in one video
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv

# same, as JSON
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --json
```

## How the manifest works

*(To be filled in when manifest generation lands.)*

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
# unit tests (segment logic + synthetic-video edge cases; no real footage needed)
./venv/bin/python -m pytest tests/

# detection regression against the reference clips
./venv/bin/python scripts/regression.py
```

- **Reference clips** live in `reference_clips/` (gitignored — video files
  are never committed). Currently three 190-second softball clips.
- **Ground truth** lives in `tests/ground_truth/*.json`, one file per clip,
  hand-annotated by frame-level visual review. Each lists action *events*
  with a time window, a type, and a `required` flag: required events are
  confirmed real plays (swing, hit, run, pitch) that the detector must
  capture; non-required events are borderline moments (practice swings,
  warm-up throws) reported for information but not counted against recall.
- **Regression script** (`scripts/regression.py`) runs both the
  motion-only baseline and the fused pipeline on every reference clip and
  reports recall on required events, borderline capture, and flagged
  footage for each. It exits non-zero if: the fused pipeline misses any
  required event; fused recall drops below the motion-only baseline; a
  vetoed segment overlaps a required event (safety net — a distant or
  occluded fielder must never silently erase a real play); or a
  defensive-play window marked `check_continuity` in the ground truth is
  not covered by one contiguous kept segment.
- The edge cases covered so far: a video with no motion at all, a
  fraction-of-a-second video, a missing file, and a corrupt file (the last
  two fail with a clean error rather than crashing).
