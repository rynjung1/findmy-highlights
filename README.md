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

## Current status

**Phases 0-4 complete and approved.** Detection pipeline (motion → person
detection → play extension → padding), the manifest, and multi-file
handling all work end-to-end and are covered by 120 unit tests + 2 e2e
tests (`pytest tests/` and `pytest tests/ -m e2e`). Phase 5 (stitch kept
segments into one output video) is next and hasn't been started.

Open items:
- No full-length (30-60+ min) real video has been run through the
  pipeline yet — needed before Phase 5's checkpoint to catch
  performance/memory issues, per the project spec.
- No committed multi-file regression fixture. Multi-file logic was
  validated against real footage the user supplied directly (not
  committed — video files are gitignored), so a fresh clone can run the
  synthetic-metadata multi-file tests but can't re-run that exact
  end-to-end validation without the same source files.
- Manual "cut a kept segment" (the reverse of restore) is intentionally
  not built yet — see Known limitations.

Notes for whoever picks this up next:
- **The priority rule governs every threshold in this codebase**: never
  miss a real play; when a detection decision is borderline, err toward
  keeping it. If you're tuning anything, tune against recall first.
- `pipeline/run.py`'s `process_video()` is the one implementation of
  "process a single file" — both `scripts/detect.py` (one file) and
  `scripts/detect_multi.py` (many) call it. Don't duplicate pipeline
  logic in a script.
- A file boundary is a hard boundary by design (see Known limitations) —
  this is deliberate, not a bug to "fix" later.
- `reference_clips/*.mkv` and any other test video files are gitignored
  and not in the repo. If you need them, ask the user; several were
  provided over the course of Phases 1-4 and then intentionally not
  committed.

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
  status toggling for the future Edit Log. Extended for multi-file:
  `build_multi_file_manifest()` builds across several files at once;
  every segment's `start_s`/`end_s` stay LOCAL to its own `source_file`
  (matching the spec's example), and an explicit `source_file_index`
  locks in processing order — nothing has to re-derive order by sorting
  filenames or re-probing timestamps later. `kept_spans_by_file()` reads
  spans back grouped by file, merging only *within* a file; spans from
  different files are never merged into one, even when their local
  timestamps would look numerically adjacent, since they're different
  pieces of video that need independent seeking. `build_manifest()`'s
  original single-file signature is untouched — the existing 11 tests
  pass unmodified.
- **Multi-file ordering** (`pipeline/multifile.py`) — orders input files
  by capture-time metadata (ffprobe's `creation_time` tag) and reports
  real gaps between them. That signal is trusted only when it's
  unambiguous: present on every file, and far enough apart (default
  5s minimum) that measurement noise couldn't flip two files' order, and
  physically consistent (a file can't plausibly start before the previous
  one's own duration would have finished). Otherwise ordering is **not
  guessed**, but this is explicitly not a dead end: `resolve_order()`
  raises `AmbiguousOrderError` carrying a suggested order, and
  `scripts/detect_multi.py` turns that into an error message with the
  exact `--order file1,file2,...` command to re-run — the "ask the user
  to confirm/reorder" fallback the project spec calls for, unit-tested
  (`test_resolve_order_*` in `tests/test_multifile.py`) rather than only
  checked by hand. Also flags mismatched resolution or frame rate across
  files (Phase 5's problem to resolve at stitch time).
- **Calibration resolution** (`pipeline/calibration.py`) — one
  `calibration.json` covers every file in a batch by default (the
  zero-friction path: click the plate once). A per-file override
  (`<stem>.calibration.json`, produced by re-running `scripts/calibrate.py`
  with `--output` against just the file whose camera moved) takes
  priority for that file specifically — no new tooling needed.
- **Shared per-file pipeline core** (`pipeline/run.py`) — `process_video()`
  is the exact same motion → veto → extension → padding pipeline whether
  called once (`scripts/detect.py`) or once per file in a batch
  (`scripts/detect_multi.py`). This is also what makes the file-boundary
  rule structural rather than a convention to remember: each call gets
  its own fresh motion/detection/occupancy arrays and an at-bat detector
  that arms fresh at that call's own t=0, with no parameter or shared
  state through which one file's trailing condition could reach the
  next. Proven, not just argued: `tests/test_multifile_boundary.py` runs
  two adversarially-constructed per-file timelines back to back (file A
  ending mid-play with motion still hot; file B opening in a state that
  would reveal any leaked "still extending" or "already fired" flag) and
  confirms neither call's result depends on having seen the other, or on
  which one ran first.
- **CLI** (`scripts/detect.py` for one file, `scripts/detect_multi.py` for
  several) — print candidate segments as timestamps (or JSON with
  `--json` for the single-file CLI), and `--manifest PATH` to write a
  manifest; `--motion-only` gives the Phase 1 baseline.

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

# multiple files as one game timeline (order auto-detected from file
# metadata; a calibration.json in the same folder covers all of them)
./venv/bin/python scripts/detect_multi.py game_part1.mkv game_part2.mkv --manifest out/game_manifest.json

# if ordering is ambiguous, detect_multi.py refuses and asks for this:
./venv/bin/python scripts/detect_multi.py game_part1.mkv game_part2.mkv --order game_part1.mkv,game_part2.mkv

# per-file calibration override, e.g. if the camera moved between files
./venv/bin/python scripts/calibrate.py game_part2.mkv --set 900,700 --output game_part2.calibration.json
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

For multiple files, `build_multi_file_manifest()` builds one manifest
across all of them: every segment stays timestamped LOCAL to its own
source file (never a global concatenated clock), plus an explicit
`source_file_index` that locks in processing order. `kept_spans_by_file()`
reads spans back grouped by file — never merging across a file boundary,
since two different files are never one continuous span even if their
local timestamps happen to look adjacent.

## Known limitations / non-goals for this version

- **A real play split across a file boundary becomes two separate
  segments, not one continuous one.** Per the Phase 3/4 design decision,
  play extension and at-bat state never cross a file boundary — so if
  recording genuinely stops mid-play (not just between innings) and
  resumes in a new file, each half is detected and kept independently:
  the first file's segment truncates at its own end (correctly, not
  dropping any footage — see the fix below), and the second file opens
  its own fresh segment picking up the rest. This fragmentation is
  visible and reviewable in the manifest as two distinct `kept` entries
  (confirmed against a real test case: a hit-with-runners-advancing play
  from `clip_540`'s ground truth, split at t=85, appears as one segment
  ending at `boundary_test_part1.mkv`'s own true end and a second segment
  opening at `boundary_test_part2.mkv`'s start) — nothing is silently
  lost — but the two halves are not automatically stitched back into one
  continuous highlight clip. That's accepted for v1.
- **Fixed: a video's `duration` was sometimes reported longer than what
  was actually decodable**, because it was read from the container's
  `CAP_PROP_FRAME_COUNT` unconditionally. Found via the boundary-split
  test case above (a file produced by splitting another at a
  non-keyframe point can end up with a frame count the container claims
  but never actually decodes: observed, 4080 frames/85.08s claimed, only
  3861 frames/80.52s retrievable) — trusting the nominal value made the
  manifest report several seconds of confirmed "cut" dead time that had
  in fact never been analyzed at all. `pipeline/motion.py` now caps
  `duration` at the last frame genuinely retrieved. This turned out to
  affect the three original reference clips too, by a smaller margin
  (~4.1-4.7s each) — likely the same container/frame-rate quirk at a
  smaller scale — so every clip's reported duration is now a few seconds
  shorter and more accurate than in the Phase 1-3 checkpoints. Recall and
  continuity on all reference clips are unaffected (`scripts/regression.py`
  still passes in full); only the never-real "dead time" past each file's
  true end changed.
- **Multi-file testing uses real files, not committed ones.** Four
  multi-file test sets (a clean two-file pair with a real ~4-minute
  offset, an ambiguous pair 4 seconds apart, and the boundary-split pair
  above) were provided and used to validate ordering, the manifest, and
  boundary behavior end-to-end — see `pipeline/multifile.py`,
  `pipeline/calibration.py`, and the `test_multifile*`/`test_calibration.py`
  suites for what's unit-tested from this. The actual video files aren't
  committed (gitignored, like all reference clips), so a fresh checkout
  can run the synthetic-metadata unit tests but not re-run this exact
  end-to-end validation without the same source files.
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
- **Multi-file tests** (no committed multi-file reference set yet — see
  Known limitations): `tests/test_multifile.py` covers file ordering
  against synthetic metadata (clean ordering, missing metadata, two files
  whose timestamps are too close to trust, physically-implausible
  overlapping timestamps, mismatched resolution/fps) plus a real-ffprobe
  smoke test against tiny ffmpeg-generated clips; `tests/test_calibration.py`
  covers shared-vs-per-file-override resolution;
  `tests/test_manifest_multifile.py` covers file-order locking and —
  specifically requested — that a segment sitting right at a file
  boundary can be individually restored/toggled without disturbing its
  neighbor in the other file, through an actual save/load round trip;
  `tests/test_multifile_boundary.py` proves no extension/at-bat state
  leaks between two files processed back to back. All of this was also
  manually verified end-to-end against a real two-file set (split from a
  reference clip with `ffmpeg`, real `creation_time` gaps, shuffled input
  order, an ambiguous-timestamp variant, and a per-file calibration
  override) before being trusted — not committed, since it was scratch
  validation data, but the same checks are why these tests exist.
