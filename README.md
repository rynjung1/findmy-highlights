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

**v1 is complete.** Every piece of work originally scoped for this version —
the detection pipeline, multi-file handling and stitching, the backend API,
the full Home/Upload and Edit Log frontend, multi-base calibration, and the
zone-velocity signal built on top of it — is done, tested, and has been
exercised end to end against real footage and a real running server, not
just in-process test clients. `pytest tests/` covers 247 unit tests,
`pytest tests/ -m e2e` covers 2 tests that run real model inference, and a
full ten-item pre-demo checklist (upload, calibrate, process, watch, Edit
Log restore/cut-again, multi-file, re-export) has been walked end to end in
an actual browser.

**Detection pipeline.** Motion detection, person detection, plate/base
occupancy, at-bat boundary detection, play extension, and padding work
together as described in Architecture overview below. Two honest findings
from early validation still hold: person detection alone doesn't improve
recall over motion-only on this footage — the discrimination that matters
comes from plate occupancy over time, not person presence — and the first
version of play extension cut real plays short by up to a few seconds
because it ended at the first quiet motion sample instead of requiring
quiet to hold continuously; both were caught and fixed against real
reference-clip footage, not synthetic cases. The full pipeline was also
run once against a real 67.5-minute, 4049.9s game recording
(`full_game.mkv`): 37.4 minutes wall-clock, ~969 MiB peak resident memory,
84% of the game (3391.7s) flagged kept, output fully decodable, every
sampled frame showing real live action. Re-run later for the pre-demo
checklist and reconfirmed clean (2237.02s = 37m17s, no regression) after an
earlier re-run had clocked 67.6 minutes — traced to a leftover
`uvicorn --reload` process eating background CPU during that specific run,
not a real slowdown.

**Multi-file handling & stitching.** Files are ordered by capture-time
metadata when it's unambiguous and trustworthy, and refuse to guess
otherwise — returning a suggested order for the caller to confirm instead.
A play split across a file boundary renders as two separate clips by
design: play extension and at-bat state never cross a file boundary,
verified with adversarial back-to-back test timelines and real split-clip
footage. Stitching stream-copies when every source shares
codec/resolution/fps (fast, lossless) and falls back to a normalizing
re-encode otherwise. A since-fixed bug capped a video's reported duration
at the frames actually decodable rather than what the container claimed —
trusting the nominal count had been overstating "dead time" by several
seconds on every clip.

**Backend API.** The FastAPI service wrapping the pipeline (upload,
calibration, trigger-processing, progress, manifest read/update,
re-export) is confirmed against a real running server via
`scripts/smoke_api.py` — real upload, real calibration, real detection,
real export, no fakes — and covered by 79 backend-specific tests. Two
issues were caught and fixed in review before sign-off: calibration
wasn't reachable through the API at all at first, so every API-triggered
job silently ran with the at-bat boundary system disabled; and processing
could start with no calibration set and no warning until a 37-minute job
finished. Both are fixed now — a dedicated calibration endpoint, and a 400
at trigger time unless the caller explicitly passes
`allow_uncalibrated: true`. Job state is written to disk at every stage
transition rather than kept in memory, so it survives the dev server's
frequent restarts, and a single-job-at-a-time lock stops two heavy
detection jobs from competing for the same CPU.

**Home/Upload frontend.** Upload → click-to-calibrate → (order
confirmation, if needed) → progress → player, all working end to end in a
real browser, with detection and export auto-chained so one "Process"
click ends in a playable, downloadable video. A manual pass surfaced and
fixed three real issues: an exported video that looked barely trimmed
(turned out to be already-documented keyframe-snap slack from stream-copy
seeking, not a bug); no progress indication during the long
person-detection stage (fixed by wiring an existing per-frame progress
callback that nothing was calling); and a small calibration-marker offset
from the actual click (investigated and judged ordinary click variance —
see Known limitations).

**Edit Log.** Lists every segment ever cut, using the manifest's permanent
`origin === "gap"` marker rather than its mutable `status`, so restored
entries still show up correctly. Each entry has a preview (seeking into
the original source file) and a restore/cut-again toggle. The
segment-preview endpoint checks requested filenames against a per-batch
allowlist rather than a traversal-character blocklist, specifically
because a blocklist can be bypassed by percent-encoding (`%2e%2e`) in a
way an allowlist can't. A real UI bug was caught and fixed during the
manual pass: a second toggle in quick succession sometimes didn't show its
result visually, even though the backend was already correct at every
step. Root-caused with a `jsdom`/`React.StrictMode` harness across seven
scenarios (none reproduced it), so the toggle handler was rewritten to
always re-fetch the manifest and fully replace local state from it, rather
than merge the `PATCH` response into local state — removing the entire
"local state can drift from the server" bug class regardless of the
original cause.

**Automatic re-export.** Every restore/cut-again toggle in the Edit Log
triggers a real export and refreshes the video shown, instead of requiring
an explicit "Re-export" button — chosen so the manifest and the video it
describes can never drift apart, and because re-export is fast (stitching
only, ~1s measured). Verified two ways: the real HTTP request sequence
replayed by hand against a live backend and a real clip (restoring a
segment correctly dropped output duration from 181.299s to 178.009s;
reverting correctly restored it), and the actual bundled component driven
through a `jsdom` harness confirming exactly one export fires per toggle.
A frame-level check — not just a duration number — confirmed the restored
footage is genuinely present in the output's pixels, not just relabeled in
the JSON.

**Multi-base calibration.** `calibration.json` now optionally covers
first/second/third base alongside home plate, entirely additive —
`scripts/regression.py`'s output across every reference clip is
byte-identical before and after. Validating it against real defensive
plays at first base surfaced two real problems, both fixed: the plate's
pixel radius is far too large for a base at a backstop camera's typical
distance (fixed with a separate, smaller base-specific default), and a
fielder arriving at speed doesn't satisfy the plate's "stationary entry"
requirement the way a settling batter does (fixed with an opt-out
parameter). It also surfaced the finding the next piece of work needed to
design around: raw occupancy alone can't tell "a fielder standing at their
normal position" from "a catch or tag resolving right now" — a first
baseman can read as continuously occupied for an entire clip, play or no
play.

**Zone-velocity tightening.** Answers the problem multi-base calibration
surfaced: `compute_zone_velocity()` measures how fast the nearest tracked
person in a zone is moving (box-heights/sec) and distinguishes a resting
fielder (0.00-0.12 bh/s, sustained) from a genuine throw/catch arrival
(0.32-1.01 bh/s, a clear spike) — validated directly against real
first-base footage. It's wired into play extension for both base zones
and, since the same spike-then-settle shape shows up there too, the plate
zone: an incoming batter walking up and settling into the box tightens the
same "walk-up gap" left over from the previous play. The mechanism only
ever shortens a segment's close time, never lengthens it, and only when no
other whole-frame motion is still happening elsewhere — a continued play
on the field (a relay throw, a runner still advancing) still wins and
blocks the tightening, so this can trim dead time faster but can't cut off
real action. Two known limitations are flagged rather than shipped
silently: a double play or continued relay at the *same* base within one
play could look identical to "the play is over" from that zone's velocity
alone (no reference clip currently exercises this case); and only the
smaller, extension-layer piece of the walk-up gap is addressed this way —
roughly half of it is already present in the raw motion segment before
extension logic ever runs, which would need a larger, separately-scoped
change to fix.

**Frontend redesign.** Replaced the original flat MVP styling with a
persistent sidebar (nav plus a live step tracker), a self-hosted Inter
font and consistent design tokens in place of generic gradients and pill
buttons, a custom-styled file picker, and an intro panel next to the
upload dropzone so the initial screen isn't just an empty card.

Open items:
- No committed multi-file regression fixture. Multi-file logic was
  validated against real footage the user supplied directly (not
  committed — video files are gitignored), so a fresh clone can run the
  synthetic-metadata multi-file tests but can't re-run that exact
  end-to-end validation without the same source files.
- Manual "cut a kept segment" (the reverse of restore) is intentionally
  not built yet — see Known limitations.
- Zone-velocity tightening's same-zone re-arm gap (a double play or
  continued relay at one base within a single play) has no reference clip
  to test against yet — see Zone-velocity tightening above.
- The walk-up gap's larger raw-segment-level piece is deliberately not
  started — see Zone-velocity tightening above.

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
  provided over the course of this project and then intentionally not
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

  **Honest early finding:** on the reference footage, person detection
  did not improve the keep/cut decision. Motion-only already had full
  recall (every real play), the veto never fires on real clips (measured:
  no person-free motion run longer than 0.6s — essentially all motion on
  a rec field is human), and the fused signals *increase* flagged time if
  used to hold segments open indiscriminately. The measured value of
  person/plate signals on this footage is the **plate-occupancy
  timeline**: the input the at-bat boundary logic below needs. The
  original hypothesis — person detection discriminates real plays from
  milling — is false for this footage, because the milling is also
  people. The discrimination has to come from game context (plate
  occupancy over time), which is what at-bat boundary detection does.
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
- **Segment refinement** (`pipeline/refine.py`) — replaces the earlier
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
  files (stitching's problem to resolve, below).
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
  manifest; `--motion-only` gives the motion-only baseline (no person
  detection or play extension).
- **Stitching** (`pipeline/stitch.py`, `scripts/stitch.py`) — renders a
  manifest's `kept` spans into one finished output video. Reads
  `kept_spans_by_file()` so spans are pulled per file, in
  `source_file_index` order, each file's own spans in their own local
  order — never merged across a file boundary, matching the file-boundary
  design decision: a play split across two files is rendered as two
  clips back to back, not stitched into one continuous shot (confirmed
  against the `boundary_test_part1/2` reference pair — the split hit-play
  survives the join intact, just as two consecutive clips).

  Every kept span is extracted with ffmpeg first, then all spans are
  joined with the concat demuxer. Two paths: **stream copy** (`-c copy`,
  no re-encode) whenever every contributing source file shares the same
  codec/resolution/fps/orientation — fast and lossless, which matters on
  a 30-60+ minute video; or a **re-encode fallback**, normalizing every
  span to the largest resolution and highest fps among the inputs (so
  nothing is downscaled) when they disagree. `pipeline.stitch` probes
  the contributing source files itself at stitch time (rather than
  reusing `pipeline.multifile`'s ordering-time flags), since a manifest
  only stores filenames, not the probed params, and stitching can run as
  its own step later against just the manifest. Which path ran, and why,
  is always reported — never silent. A stream-copy extraction can't cut
  mid-GOP, so ffmpeg's input-level seek starts a copied span at a
  keyframe at or before the requested time (extra footage, never less) —
  the same err-toward-keeping direction as the priority rule, applied to
  trimming itself.

  How much extra depends on the source file's own keyframe interval
  (GOP), not on anything this module controls, so a small-GOP test clip
  doesn't bound what a real file will show. Verified directly against the
  full-length checkpoint below (67.5-minute MKV, constant 6.006s GOP
  measured from its own packet keyframe flags): 120 real spans averaged
  3.15s of slack, max 6.17s. Root-caused, not just measured — for 7 of
  the 120 spans, ffmpeg's seek landed on the keyframe *before* the
  nearest preceding one (confirmed by comparing each span's actual
  rendered start against the source's full keyframe timeline), not the
  mathematically nearest keyframe; a known ffmpeg/MKV cue-index seeking
  characteristic on this file, not a bug in this module's command
  construction. Still strictly extra content, never lost — safe per the
  priority rule — but the honest bound is "roughly one to a bit over one
  *real* source GOP," not a fixed number.

  Validated three ways: unit tests for the pure planning/command-building
  logic (no video needed); real-ffmpeg tests against tiny synthetic clips
  for both paths, including one that deliberately mismatches resolution
  *and* fps to exercise the re-encode fallback end-to-end (previously the
  untested path); and the two real end-to-end runs below. Building the
  real-ffmpeg tests caught an actual bug before either real run: a
  relative `work_dir` produced a concat list whose relative entries the
  concat demuxer re-resolved against the list file's own directory
  (not the process's cwd), doubling the path and failing outright —
  `run_stitch` now resolves `work_dir` to absolute first; regression test
  added and confirmed to fail without the fix.
  - `clip_60.mkv` (single file, 7 non-contiguous kept spans): stream-copy
    path, output plays and decodes cleanly.
  - `boundary_test_part1/2.mkv` (the multi-file boundary pair): confirms
    a play split across a file boundary renders as two clips back to
    back with no corruption — the specific defensive play that was cut
    mid-play by the file split is intact across the join.

- **Backend API** (`backend/app.py`, `backend/jobs.py`,
  `backend/storage.py`, `backend/pipeline_runner.py`) — a local FastAPI
  service wrapping the pipeline: upload, calibration, trigger-processing,
  progress, manifest read/update, re-export. No pipeline logic lives here — every
  endpoint is a thin wrapper around `pipeline.run.process_video()` and
  `pipeline.stitch.run_stitch()`, which stay the single implementations;
  `backend/pipeline_runner.py`'s job is only to wire their `on_stage`
  callbacks (added in this stage) to durable progress updates.

  **Durable job state, not in-memory.** A real detect job takes tens of
  minutes and the backend restarts constantly during frontend
  development — in-memory-only progress would silently vanish on every
  restart. Instead, `backend/jobs.py` writes minimal progress (job id,
  type, status, stage, timestamps) to `<batch>/detect_job.json` /
  `<batch>/export_job.json` at every stage transition, the same
  JSON-file-as-source-of-truth approach as the manifest. On startup, any
  job left `pending`/`in_progress` from before the process started is
  swept to `interrupted` with an explanatory error, rather than being
  left to silently report "in progress" forever for a job that isn't
  actually running.

  **Single-job-at-a-time, deliberately (v1).** This is a local
  single-user app and detection is CPU/memory-heavy (~1GB RSS, near-full
  CPU on the full-length checkpoint) — running two jobs at once would
  only make both slower, not add real throughput. A second
  trigger-processing or re-export call while anything else is
  `pending`/`in_progress` anywhere gets a clean 409 naming the job
  that's already running, checked by scanning the job files directly
  (`find_active_job()`) rather than a separate lock file that could
  drift out of sync. A batch sitting in `needs_order_confirmation`
  (waiting on a human decision, not consuming resources) deliberately
  does **not** count as active and never blocks a different batch.

  **Ambiguous multi-file order, as data instead of a CLI exit code.**
  `POST /batches/{id}/process` mirrors `scripts/detect_multi.py`'s
  `AmbiguousOrderError` fallback, but shaped for an HTTP client: if
  ordering can't be trusted, the job is written with
  `status="needs_order_confirmation"` plus `suggested_order` and
  `order_reason` (not an error — an expected, actionable state) and
  processing does not start. `POST /batches/{id}/order` submits the
  confirmed order and starts it; a file-list mismatch is a 400, and
  confirming when nothing is awaiting confirmation is a 409.

  **Upload never buffers a whole file in memory.** Real recordings run
  1GB+; `backend/storage.py` streams each upload to disk in fixed 1 MiB
  chunks (verified directly: a test asserts every read call requests a
  bounded size, not a whole-body `.read()`), never through FastAPI's
  default in-memory path.

  **Calibration** (`POST`/`GET /batches/{id}/calibration`) writes
  `<batch>/calibration.json`, which `resolve_zone()` then finds
  automatically since it looks in the video's own directory — no changes
  needed anywhere else for this to take effect. Two ways to set it,
  mirroring `scripts/calibrate.py`'s two non-interactive paths: upload an
  existing `calibration.json` verbatim (the "same camera setup, already
  calibrated" case — a shared one covers every file in the batch, same
  as the CLI), or give plate pixel coordinates to compute a fresh one
  against the batch's own first video. `pipeline.calibration.build_calibration()`/
  `save_calibration()` are shared by both `scripts/calibrate.py` and this
  endpoint specifically so the schema can't drift between the CLI and API
  paths. Coordinates are validated against the video's actual frame size
  (rejecting anything outside it) and radius must be positive; setting
  calibration is rejected once the batch's detect job is
  pending/in_progress/completed, since it can no longer take effect at
  that point. `GET /batches/{id}/preview.jpg` (below) is what the
  in-browser click-to-calibrate flow reads a frame from — see
  `frontend/src/components/CalibrateStep.jsx`.

  **`POST /batches/{id}/process` requires calibration to have been set**
  (400 if not) **unless the caller explicitly passes
  `allow_uncalibrated: true`.** This exists specifically so a missing
  calibration is caught in milliseconds at trigger time, not discovered
  37 minutes later in a completed job's `warnings` field — the CLI's
  softer "warn and continue" behavior is preserved, but only as an
  explicit, visible choice rather than a silent default. On success,
  detection and export now auto-chain (see below) — one trigger, one
  playable output at the end.

  **Preview frame + auto-chained export** —
  `GET /batches/{id}/preview.jpg` grabs a frame fixed 20 seconds into the
  batch's first video (`pipeline.calibration.grab_preview_frame()`),
  encoded at the video's exact native resolution so the frontend's
  coordinate-scaling math (see Frontend, below) has something reliable
  to scale against. `POST /process` no longer stops after detection: once
  it completes, `backend/pipeline_runner.run_detect_then_export_job()`
  immediately creates and runs the export job in the same background
  thread, so a single trigger ends with a playable video — matching the
  spec's "a Process action kicks off the pipeline... on completion, an
  embedded player" description as one flow, not two. If detection fails,
  export is never started. `GET /batches/{id}/output` then serves the
  result with range-request support (`FileResponse`), so a `<video>`
  player can seek without downloading the whole file first.

- **Frontend** (`frontend/`, React + Vite, own `package.json`/lockfile
  per the project's own rule to keep Node and Python dependencies separate) —
  a top-level Home / Edit Log nav (`App.jsx`), independent of Home's own
  linear stage machine: upload → click to calibrate → (order
  confirmation, only if needed) → progress → player + download.
  Switching to Edit Log and back doesn't reset in-progress upload state,
  since it's a separate `view` toggle, not a stage. No pipeline or
  business logic here either — `src/api.js` is a thin fetch wrapper, one
  function per backend endpoint.

  **Edit Log** (`EditLogView.jsx`) — lists every segment detection ever
  cut (filtered on the manifest's `origin === "gap"`, which is set once
  at build time and never changes, independent of `status` — see How
  the manifest works below), each with a Preview toggle (an inline
  `<video>` reading the original source file via `GET .../source/{name}`,
  seeked to the segment's span) and a Restore/Cut-again toggle, restored
  entries visually marked. Every toggle auto-triggers a real re-export
  and refreshes the "Current output" player shown at the top of the
  view — see Current Status above for why automatic was
  chosen over an explicit button.

  **The toggle re-fetches the manifest fresh after every `PATCH` rather
  than merging the response into local state.** Caught during the Edit
  Log manual pass: a restore → cut-again sequence sometimes didn't show
  the second toggle's result visually, even though the backend was
  confirmed correct at every step. Root-caused via a `jsdom` +
  `React.StrictMode` harness against the actual component source across
  seven scenarios (multi-row, rapid double-click, interleaved toggles,
  preview open) — none reproduced it, so rather than leave an
  unreproducible local-merge path in place, `handleToggle` was rewritten
  to always re-`GET .../manifest` and fully replace state from that
  response, removing the "local state can drift from server truth" bug
  class outright regardless of the exact original cause (later confirmed
  on retest to be stale visual state in an already-open tab, not a live
  bug). A `console.log` fires the instant a toggle is clicked, before
  any request goes out, to make a real click unambiguous from a request
  just being hard to spot in a busy Network tab.

  **Mid-processing browser close/reopen is handled by design, not as an
  afterthought.** The only client state kept is the batch id
  (`localStorage`); on load, `App.jsx` always re-fetches the batch's
  actual job status from the server and resumes whatever stage that
  implies, rather than assuming a fresh session — durable server-side
  job state (see Backend API, above) is what makes this possible at all.

  **Coordinate scaling for click-to-calibrate**, since this is the one
  most likely to fail silently: the browser displays `preview.jpg` at
  whatever size fits the layout, not its native resolution.
  `CalibrateStep.jsx` reads the loaded `<img>`'s `naturalWidth`/
  `naturalHeight` (the browser's own decoded pixel dimensions) and
  scales the click's on-screen offset by `natural / displayed` before
  sending coordinates — no extra backend round trip needed, since the
  JPEG already carries its true dimensions and the backend guarantees
  (tested) they equal the video's real frame size. `POST /calibration`'s
  existing frame-bounds check is a real, tested safety net underneath
  this, not just documentation of intent.

  This scaling is only correct as long as the `<img>`'s rendered box has
  the same aspect ratio as its actual content — true today because the
  component deliberately sets only `max-width`, never `object-fit` or a
  fixed `height`, so there's no letterboxing/padding for
  `getBoundingClientRect()` to include. That's an assumption a future
  style change could break without anyone noticing, so it isn't just
  trusted: `handleClick` checks the box's aspect ratio against
  `naturalWidth/naturalHeight` on every click and refuses to record a
  coordinate (with a visible error, not a silent bad value) if they
  don't match within 1%.

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

6. **Frontend (optional — only needed to use the Home view UI, not the
   CLI or bare API):** install Node.js (developed against Node 18.20;
   note the very latest `create-vite`/tooling needs Node 20+, but this
   project's own `frontend/package.json` was pinned to work with 18),
   then:

   ```sh
   cd frontend
   npm install
   ```

   Kept in its own `package.json`/lockfile, separate from the Python venv
   — see Version control workflow.

## How to run it

**Full app (backend + frontend), two terminals:**

```sh
# terminal 1: backend, must be on port 8420 -- the frontend dev server's
# proxy (frontend/vite.config.js) is hardcoded to that port
./venv/bin/uvicorn backend.app:app --reload --port 8420

# terminal 2: frontend dev server
cd frontend && npm run dev
```

Then open `http://localhost:5173` in a browser (not `127.0.0.1` — see the
note in Known limitations about Vite's dev-server binding). Upload a
video, click home plate on the preview frame, and it'll walk through
processing to a finished, downloadable highlight video.

**Backend alone**, for poking at the API directly or before the frontend
existed:

```sh
./venv/bin/uvicorn backend.app:app --reload --port 8420

# in another terminal: exercise it end-to-end against a real clip
# (uploads, uploads calibration if a calibration.json sits next to the
# video, triggers real detection which now auto-chains into export,
# restores a segment, re-exports, downloads the final output)
./venv/bin/python scripts/smoke_api.py reference_clips/clip_60.mkv --base-url http://127.0.0.1:8420
```

Interactive API docs are served at `http://127.0.0.1:8420/docs` (FastAPI's
built-in Swagger UI) once the server is running.

Or run the pipeline directly from the command line, same as before:

```sh
# detect final (extended, padded) action segments in one video
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv

# same, as JSON
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --json

# also write a manifest (see "How the manifest works" below)
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --manifest out/clip_60_manifest.json

# motion-only baseline (no person detection or play extension)
./venv/bin/python scripts/detect.py reference_clips/clip_60.mkv --motion-only

# multiple files as one game timeline (order auto-detected from file
# metadata; a calibration.json in the same folder covers all of them)
./venv/bin/python scripts/detect_multi.py game_part1.mkv game_part2.mkv --manifest out/game_manifest.json

# if ordering is ambiguous, detect_multi.py refuses and asks for this:
./venv/bin/python scripts/detect_multi.py game_part1.mkv game_part2.mkv --order game_part1.mkv,game_part2.mkv

# per-file calibration override, e.g. if the camera moved between files
./venv/bin/python scripts/calibrate.py game_part2.mkv --set 900,700 --output game_part2.calibration.json

# stitch a manifest's kept segments into one finished output video
# (--input-dir is where the manifest's named source file(s) actually live,
# since the manifest itself only stores filenames, not full paths)
./venv/bin/python scripts/stitch.py out/clip_60_manifest.json --input-dir reference_clips --output out/clip_60_highlights.mp4
```

## How the manifest works

`pipeline/manifest.py` builds the manifest described in the project spec
from the pipeline's final kept segments: every span of the source video is
listed exactly once, either as a detected `kept` segment or a `cut` gap
between them — so the manifest's segments always cover the whole timeline
with no overlaps or holes. Each entry has an id, a source file, a
timestamp range (both as an `"HH:MM:SS.mmm"` string and as float seconds),
a detection score, a `status` of `kept` or `cut`, and an `origin` of
`"detected"` or `"gap"` recording how the entry was created — set once
at build time and never changed afterward, unlike `status`. This is what
lets the Edit Log list "every segment that was ever cut"
correctly even after some have been restored: `origin === "gap"` is a
permanent marker, `status` is the current (possibly restored) state.

`set_status()` flips a segment's status (the Edit Log's restore action)
and `kept_spans()` returns the current kept spans with adjacent ones
merged — that's what a re-export would render. Nothing here mutates
video files; the manifest is the only thing that changes on restore.

For multiple files, `build_multi_file_manifest()` builds one manifest
across all of them: every segment stays timestamped LOCAL to its own
source file (never a global concatenated clock), plus an explicit
`source_file_index` that locks in processing order. `kept_spans_by_file()`
reads spans back grouped by file — never merging across a file boundary,
since two different files are never one continuous span even if their
local timestamps happen to look adjacent.

## Known limitations / non-goals for this version

- **Fixed: `ProcessingStep` could poll for a detect job before it
  existed, logging a real (if harmless) 404.** `App.jsx`'s
  `handleCalibrated` called `setStage('processing')` — which mounts
  `ProcessingStep` and starts it polling `GET .../jobs/detect`
  immediately — *before* even sending the `POST /process` request that
  creates that job, not after. Not a rare race; guaranteed on every run
  until the stage flip was moved to after `triggerProcess` resolves.
  `CalibrateStep`'s own "Saving..." button state already covers the
  brief extra wait, so there's no UX gap.
- **The Edit Log's cut-segment preview serves the original uploaded file
  as-is (e.g. `.mkv`), not a re-encoded `.mp4`.** Not tested against
  every browser: Chromium-based browsers generally play H.264-in-MKV
  fine, but Safari does not support the MKV container natively
  regardless of the codecs inside it. If a browser fails to play a
  preview, that's a container-compatibility gap, not the endpoint
  serving the wrong bytes — flag it if it comes up and it can be
  addressed (e.g. transcoding on the fly) then, not preemptively.
- **Fixed during backend API review: calibration wasn't reachable through
  the backend API at all**, so every API-triggered job silently ran with the
  at-bat boundary system disabled — see the Current Status
  writeup above for the full account (what was actually lost, how it was
  confirmed, and the fix). Also fixed in the same review round:
  `POST /process` now actively blocks on missing calibration (400) unless
  the caller explicitly passes `allow_uncalibrated: true` — no longer
  possible to trigger a batch uncalibrated by accident, only on purpose.
- **Fixed: upload accepted path-traversal filenames.** `POST /batches`
  wrote each uploaded file to `<batch_dir>/<client-supplied filename>`
  with only an extension check — a filename like `../../evil.mp4` (or
  a bare `..`, which needs no slash at all) would have written outside
  the batch's own directory. Found while designing the Edit Log's source-file
  endpoint (which reads by filename and needed the same class of check),
  fixed immediately since this was a live write-side issue in already-committed
  code, not deferred. `upload_batch` now rejects any filename
  containing `/`, `\`, `..`, or that doesn't equal its own
  `os.path.basename()`, before anything is written to disk. Verified with
  a test that reverts the fix and confirms the rejection tests actually
  fail without it, not just that they pass with it.
- **Calibration accuracy depends on click precision.** A manual browser
  pass found the saved plate coordinates for one clip
  offset from the reference calibration by about 14px on a 1080px-tall
  frame (checked: `CalibrateStep.jsx`'s marker is drawn from the raw
  on-screen click position, not run back through the scaling math, so
  this isn't a rendering bug — it reads as ordinary click variance
  between two separately-calibrated clips off the same camera). Users
  should visually confirm the red marker lands on the intended spot
  before confirming a calibration; small offsets are expected and
  acceptable and won't meaningfully affect plate-occupancy signals, but
  a marker that's clearly off (not just a few pixels) should be re-clicked.
- **Vite's dev server binds in a way `curl 127.0.0.1:5173` can't reach —
  use `localhost:5173`.** Confirmed while testing the proxy setup:
  Node's default `localhost` resolution in this environment prefers
  `::1` over `127.0.0.1`, and Vite listens accordingly. Not a bug in
  this project's config, just worth knowing before assuming the dev
  server isn't running when `127.0.0.1` refuses the connection.
- **Nothing in `uploads/` is ever auto-deleted in v1.** Every batch's
  source files, job state, manifest, and exported output accumulate on
  disk indefinitely. Deliberate scope cut for v1, not an oversight — a
  retention/cleanup policy is a later concern once real usage patterns
  are known.
- **The `full_game.mkv` full-length checkpoint is not a recall
  measurement — don't read it as equivalent rigor to the reference-clip
  regression.** It validates performance (37.4 min wall-clock, bounded
  memory), stitching correctness (output fully decodable, duration
  accounted for), and plausibility via a 13-frame visual spot-check
  (5 spread across the timeline, plus start/middle/end of the 3 longest
  segments) — all of that is real and confirmed, not hand-waved. What it
  does *not* have is ground truth: unlike the reference clips, there's no
  hand-annotated list of every real play in `full_game.mkv` for
  `scripts/regression.py` to check recall against, so there's no measured
  answer to "did detection miss anything in this file." Thirteen frames
  looking like real action is evidence the pipeline is doing something
  sensible on unseen real footage, not proof of recall on it. If this
  file (or one like it) is going to be relied on for tuning decisions
  later, it needs real ground truth annotation first, the same as the
  other reference clips got.
- **A real play split across a file boundary becomes two separate
  segments, not one continuous one.** Per the file-boundary design decision,
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
  shorter and more accurate than in the earliest checkpoints. Recall and
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
- **`enter_thresh` (0.006, `pipeline/segments.py`) has almost no real
  margin — a live safety concern, not just an implementation detail.**
  Measured directly while investigating `segments.py`'s raw thresholds
  (v2): across all 9 current reference clips, the required event with the
  least motion-score headroom is `clip_foul1`'s foul ball, peaking at just
  0.00665 — a bare **1.11x** margin over the threshold that decides
  whether motion counts as "action" at all. `clip_300`'s `e4` pitch event
  is similarly thin (1.17x). This isn't something padding or extension can
  paper over: recall depends on a segment ever OPENING, which happens
  strictly before either of those mechanisms gets a chance to help. On
  real footage even a little quieter than what's in the reference set, a
  foul-ball-type event is a plausible candidate to be missed outright, not
  just trimmed. Deliberately *not* raised as part of the `exit_thresh`
  retune below (raising `enter_thresh` was explicitly ruled out for
  exactly this reason). The two obvious ways to build real margin here —
  more reference footage covering quiet-contact events, or lowering
  `enter_thresh` and accepting the resulting over-inclusion cost — haven't
  been scoped yet; flagging this now so it doesn't get rediscovered later.
- **`enter_thresh`'s margin above has only ever been measured at one fixed
  camera distance/zoom — there is a plausible, physically-reasoned path by
  which a real play fails to open a segment at all on a more distant camera
  setup, independent of anything else in this codebase.** Found while
  scoping the v2 enter-side sub-track's margin table (above): all 9
  reference clips — not just clip_60/300/540, but clip_foul1, clip_whiff1,
  and all four clip_base clips too — share the *literally identical*
  calibration coordinate (`plate_xy = [1147.0, 840.0]`, same
  `zone_radius_px`), confirmed directly by inspecting every calibration
  file in `reference_clips/`. That means the entire reference set, and
  every margin number ever measured against it (including the 1.11x/1.17x
  figures directly above), comes from one single, unvaried camera
  position and zoom. `pipeline/motion.py`'s score is a *fraction of frame
  pixels moving* — not scale-invariant — so for a fixed real action, it
  scales down roughly with the **square** of the subject's linear size in
  frame. A camera positioned twice as far back (an entirely ordinary
  variation: a different field, a stands angle instead of behind the
  backstop, a different phone/GoPro zoom) would shrink the same physical
  play's score to roughly a **quarter** of its current value. Applied to
  `clip_foul1`'s already-thin 1.11x margin, that lands comfortably
  *below* `enter_thresh` — a segment that never opens at all for a real
  play, on footage no more unusual than what this project already expects
  to handle. Raw per-frame inspection also showed the two thinnest events
  share a short (~1-1.5s) real-motion burst barely as wide as the 1-second
  smoothing window itself, meaning burst duration is a second, independent
  risk axis on top of camera distance, also unexercised by the current set.
  A follow-up feasibility check (using existing person-detection box
  heights as a camera-distance proxy, so a scale-normalized score could
  keep a roughly consistent margin regardless of distance) found the raw
  ingredient is free — boxes are already computed and cached — but a naive
  per-frame implementation is measurably unreliable (nearest-box height
  swings ~59px to ~429px across consecutive ~1s samples on the same real
  subject, an identity-tracking artifact, not real depth change) and, more
  fundamentally, **cannot be validated against this reference set at all**:
  since every clip shares one camera distance, any correction tested only
  here would look like a no-op regardless of whether it actually
  generalizes. This blocks the v2 "touching the enter/open-side decision"
  sub-track specifically — not resuming that work until real (or
  deliberately simulated) footage from a genuinely different camera
  distance exists to validate against. Flagging this now, at length, so
  the reasoning and the exact numbers don't need re-deriving later.
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
  are never committed). Currently nine softball clips: three ~190-second
  general clips (`clip_60`, `clip_300`, `clip_540`), four shorter clips
  for base-occupancy validation (`clip_base1`-`clip_base4`, three with a
  per-file calibration override for bases), and two short clips added for
  a foul ball and a missed swing (`clip_foul1`, `clip_whiff1`) — plus a
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
- **Backend API tests** (`tests/test_backend_api.py`, `tests/test_jobs.py`,
  `tests/test_storage.py`) run against FastAPI's test client with a fast
  fake standing in for the real pipeline (a real detect job takes tens of
  minutes and needs model weights — covered instead by the real-server
  smoke test below), except the ambiguous-order tests, which use real
  ffmpeg-generated clips since backend/app.py deliberately reuses
  `pipeline.multifile`'s real ordering logic rather than reimplementing
  it. Covers every malformed-request case called out in the spec
  (updating a manifest before one exists, an unknown segment id,
  triggering processing or export twice on the same batch), the
  single-job-at-a-time lock and its `needs_order_confirmation` exception,
  the calibration endpoint (verified by inspecting the actual `zone`
  object `process_video` receives, not just whether a warning was
  absent), and the startup interrupt sweep. `scripts/smoke_api.py` is the
  "hit the API directly, not the UI yet" real-server check the spec
  calls for: run against a live `uvicorn` process with a real reference
  clip, it uploads, uploads calibration, triggers real detection, polls
  real stage transitions, restores a real cut segment, re-exports, and
  confirms the stitched output plays — see "How to run it" above.
- **Source-file endpoint tests** (in `tests/test_backend_api.py`)
  cover the allowlist specifically: serving a real upload, an unknown
  batch, a filename the batch doesn't have, a percent-encoded `%2e%2e`
  (the actual adversarial case — a literal bare `..` never reaches the
  server for real, since httpx/browsers collapse it during URL
  construction before the request is sent, so a test using the bare
  form would pass for the wrong reason), and that two batches with an
  identically-named file don't leak into each other.
