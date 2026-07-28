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

**Phases 0-7 complete and approved.** Detection pipeline (motion → person
detection → play extension → padding), the manifest, multi-file handling,
stitching kept segments into one finished output video, a FastAPI
backend wrapping all of it (upload, calibration, trigger-processing,
progress, manifest read/update, re-export), and the Home/Upload frontend
all work end-to-end and are covered by 240 unit tests + 2 e2e tests
(`pytest tests/` and `pytest tests/ -m e2e`), including the Phase 7 manual
browser smoke test (upload → calibrate → process → watch, a real
end-to-end pass in an actual browser, not just the proxy-level HTTP
checks below).

The manual pass surfaced three real issues, since fixed and re-verified:

- **Exported video looked barely trimmed** (clip_300: 191s source down
  to only 181s, ~10s shorter). Investigated by pulling the actual test
  batch and re-running both detection and stitching directly: the
  manifest's real cut total (15.2s) matched `scripts/regression.py`'s
  currently-validated baseline for clip_300 almost exactly (170s kept,
  91%) and a fresh CLI run on the same file (16.0s cut) — **detection via
  the API behaves identically to CLI-validated behavior, not a bug.** The
  output looking barely-trimmed is the already-documented stream-copy
  keyframe-snap slack (see Stitching below): re-running `scripts/stitch.py`
  against the same manifest reproduced the exact 181.3s output and
  self-reported "requested output duration: 170.5s" vs. the 181.3s
  actually rendered, the same mechanism already measured and written up
  for `full_game.mkv`. Nothing changed here; it was a mismatch between
  expectation and the already-validated baseline, not a regression.
- **No progress indication during processing** — same static stage text
  for the whole run, no spinner. Root-caused, not just patched: a real
  (uncached) detect job was run while polling the job file every second,
  showing that ~92% of a real run's wall-clock time (80 of 87s on
  clip_300) is spent inside a single "running player detection" stage
  with zero intermediate updates — polling and re-rendering were already
  working correctly, the backend just never reported progress *within*
  that stage. `pipeline/detection.py` already had a per-sampled-frame
  `progress_cb` hook that nothing called; `pipeline/run.py` now wires it
  to `on_stage`, giving a live `(Ns/Ts)` count throughout what used to be
  one static string — re-verified with the same real-job probe, updating
  roughly once per second start to finish. `ProcessingStep.jsx` also
  gained a small CSS spinner so there's a visible "alive" indicator
  during any stage, not just this one.
- **Calibration marker offset from the click** — investigated and not
  pursued further; see Known limitations below.

Backend additions for Phase 7, all tested (59 backend tests total now):
- `GET /batches/{id}/preview.jpg` — a frame fixed at 20s into the video
  (not frame 0, which is often black/blurry/still-settling), for the
  calibration screen to click on. Guaranteed (and tested) to be encoded
  at the video's exact native resolution, never resized — the frontend's
  coordinate-scaling math depends on that being true.
- `GET /batches/{id}/output` — serves the exported video (range-request
  support via `FileResponse`, so the player can seek without downloading
  the whole file first).
- **Detect → export now auto-chains.** `POST /process` used to only run
  detection; now, once detection completes, export starts automatically
  in the same background thread — one "Process" action, one wait, then a
  playable video, matching the spec's Home-view description. If detect
  fails, export is never started. The manual `POST /export` endpoint
  stays separate for re-exporting after a future restore (Phase 8/9),
  and its busy-check was loosened to allow re-triggering once `completed`
  (unlike detect, which still blocks any re-trigger) — re-stitching is
  idempotent against whatever the manifest currently says, so it's the
  correct way to regenerate output after an edit, not a repeat of
  expensive work.
- **Upload validates file type, both sides.** Client-side (fast
  rejection before any bytes leave the browser) and server-side (the
  real enforcement — every filename in a batch is checked against an
  extension allowlist *before* any file is written to disk, so one bad
  file in a multi-file upload can't leave partial writes of the good
  ones). Deeper "does this actually decode" validation isn't duplicated
  here — the pipeline's existing corrupt-file handling already covers
  that at process time.

**Coordinate scaling (the click-to-calibrate design decision):** the
browser displays `preview.jpg` at whatever size fits the layout, not
necessarily its native resolution. `CalibrateStep.jsx` reads the loaded
`<img>` element's `naturalWidth`/`naturalHeight` (the browser's own
decoded pixel dimensions) and scales the click's on-screen offset by
`natural / displayed` before sending coordinates to the backend — no
extra API round-trip needed, since the JPEG already carries its own true
dimensions and the backend guarantees they equal the video's real frame
size. As defense in depth, `POST /calibration` still validates
coordinates fall within the video's actual frame bounds — real bounds
checking, not just documentation of the intent.

**What's verified:** the full build succeeds; every HTTP request shape
the React code sends was exercised through the Vite dev-server proxy
against the real backend (preview.jpg at native resolution, calibration
written correctly); and the manual browser pass covered the full
upload→calibrate→process→watch flow start to finish, clicking the
preview image and confirming the marker (small offset noted above and
in Known limitations, not a functional blocker), and processing showing
live stage progress. See "How to run it" for how to start both servers.

**Phase 10 (multi-base calibration): complete.** Additive-only, exactly as
scoped: `calibration.json` now supports an optional `bases` dict
(first/second/third, each independently optional) alongside the existing
home-plate fields; `pipeline/calibration.py` gained `resolve_base_zones()`
(same per-file-override-then-shared lookup as `resolve_zone()`, but reads
`bases`) and `build_calibration()` gained an optional `bases` parameter.
`resolve_zone()` itself, and every existing caller of it, is byte-for-byte
untouched — proven, not just claimed: `scripts/regression.py`'s output
across all seven reference clips was diffed before/after this phase's
pipeline changes (via `git stash`), and the diff is empty.

This phase's job was to compute and validate the raw base-occupancy
signal, not wire it into segment-closing logic (that's Phase 11's,
mirroring exactly how Phase 2 built and validated plate-occupancy but
Phase 3 was what actually used it). Validated against `clip_base1/3/4`
(real defensive plays at first base; `clip_base2`'s line-drive catch has
no base event, see its ground truth file) and found two real problems,
not one:

- **The plate's radius (280px) is wrong for bases, not just untuned.**
  Bases sit much farther from a backstop-mounted camera than the plate,
  so the same pixel radius that correctly bounds a batter covers roughly
  half the visible field at first base's distance — confirmed visually
  (a frame with both radii drawn) before touching any code. Occupancy
  at that radius read true for nearly the entire clip. Fixed with a
  much smaller, explicitly base-specific default
  (`DEFAULT_BASE_RADIUS_PX`, flagged in its own docstring as
  dataset-tuned against these three clips' framing, not derived from
  camera geometry — a real UI-driven calibration click will refine this
  further).
- **A fielder arriving at speed lags the plate-style stationary-entry
  requirement by a measured 3 seconds** (`clip_base4`: relaxed-entry
  onset at t=11 vs. strict-entry onset at t=14) — confirmed with real
  per-sample occupancy timelines, not assumed. `compute_occupancy()`
  gained a `require_stationary_entry` parameter (default `True`, so
  `fuse()`'s plate call site is provably unaffected); the new
  `compute_all_occupancy()` (base-only, no prior callers) defaults it
  to `False`, encoding this finding directly.
- **A third, more fundamental finding, correctly radius-tuned or not:**
  a first baseman stands near first continuously as normal defensive
  positioning, not just during an active play — unlike the plate, which
  is genuinely empty between at-bats. Quantified, not just observed:
  `clip_base1` and `clip_base3` read occupied for 100% of pre-play
  samples (the zone was "occupied" from t=0, long before any play
  developed); `clip_base4` did not (15% pre-play occupied — empty until
  the play actually approached). All three calibrated clips are
  first-base plays, so whether this varies by base (first vs.
  second/third) is untested and flagged for Phase 11 to check against
  real second/third-base footage, not assumed either way from this
  data. Raw occupancy alone cannot reliably distinguish "fielder
  standing at their position" from "a catch/tag resolving right now" —
  this is the load-bearing finding Phase 11 needs to design around
  (likely pairing occupancy with motion/velocity at the zone, or a
  sustained-vs-transient distinction), not something this phase
  attempts to solve.

15 new unit tests (backward-compat fixtures, partial base sets,
per-file override resolution for bases, both entry modes,
`compute_all_occupancy`) — 240 total pass. `scripts/validate_base_occupancy.py`
is the standalone validation harness this phase's numbers came from,
reusing already-cached person detections rather than re-running model
inference.

**Phase 9 (re-export flow): complete.** The spec left the choice
between an explicit "Re-export" button and automatic background
re-render open; automatic was chosen — every restore/cut-again toggle
in the Edit Log now triggers a real `POST /export` itself, waits for it
to complete, and refreshes the video shown in the Edit Log's own
"Current output" section (no navigating to Home to see the result, per
the spec). Chosen over a button because the manifest update and the
regenerated video should never be allowed to drift apart — an explicit
button adds a state where they can (a restore that's "saved" but not
yet reflected in anything watchable), and re-export is fast (stitching
only, not re-detection: ~1s measured against clip_300) so there's no
real cost to doing it every time.

All toggle buttons disable while a re-export from any toggle is in
flight, which also serializes rapid successive toggles into one
export-at-a-time rather than racing `POST /export` calls (the backend's
existing single-job-at-a-time lock, Phase 6, would 409 an overlapping
one anyway — this avoids ever hitting that path from normal use). The
video's URL carries a cache-bust token that changes on every completed
export, so the browser can't serve back the previous export's response
for what's otherwise the same URL. On mount, the Edit Log also checks
for an already-completed export (e.g., Phase 7's original auto-chained
one) and shows it immediately, rather than the player only appearing
after the first restore of the session.

**Verified two ways.** First, the exact request sequence the component
makes (`PATCH` segment → `GET` manifest → `POST /export` → poll
`GET /jobs/export`) was replicated by hand against the real running
backend and the real clip_300 batch: restoring `seg_002` correctly
dropped `output.mp4`'s duration to 178.009s, and reverting it correctly
restored 181.299s — the exact numbers from the Phase 8 frame-level
verification, now reached by triggering through the toggle path instead
of a manual export call. Second, the actual component source (bundled,
unmodified) was driven through the same `jsdom` + `React.StrictMode`
harness as the Phase 8 investigation: confirmed exactly one export
triggers per toggle (not zero, not a duplicate), the "Re-exporting..."
indicator and disabled buttons appear for the duration of the job, and
the video's `src` carries a fresh cache-bust token afterward.

**Confirmed by a real manual pass**: preview seek, Download, both toggle
directions, and rapid restore→cut-again-before-export-finishes (handled
cleanly, no errors or inconsistent state) all work correctly.

**Full pre-demo checklist walked end to end, all ten items pass** — see
the Phase 5 full-length checkpoint above for the one real scare along
the way (an initial re-run came in at 67.6 minutes instead of ~37,
traced to environmental contention during that specific run and
conclusively re-confirmed clean in isolation, not a regression) and
Setup/How to run it below for the multi-file and Edit Log checks run
fresh against this exact codebase rather than resting on older phases'
validation. **v1 is complete.**

**Phase 8 (Edit Log UI): complete and approved.**

New for this phase:
- `GET /batches/{id}/source/{filename}` — serves one of the batch's own
  original uploaded files (range-request support, same as `/output`),
  for the Edit Log's "preview a cut segment by seeking into the source
  file" requirement rather than exporting a physical clip per candidate.
  `{filename}` is the first endpoint here where the trailing path
  component is a client-supplied URL segment rather than a hardcoded
  literal, so it's checked against `files.json` (the batch's own
  recorded file list) as an allowlist, not a traversal-character
  blocklist — a bare `..` needs no slash to climb one directory, and a
  regex/character-based check would also miss encoded bypasses
  (`%2e%2e`), which an exact-match allowlist can't be fooled by
  regardless of encoding. A resolved-parent-path check backs that up.
  Five tests cover it, including one that confirms `%2e%2e` actually
  reaches the handler (a literal bare `..` doesn't — httpx, like a real
  browser, collapses it during URL construction before the request is
  even sent, so that alone would've been a test that passes for the
  wrong reason) and one confirming the allowlist is scoped per-batch,
  not just "does a same-named file exist somewhere under the resolved
  path."
- `EditLogView.jsx` — lists every segment detection ever cut. Filters on
  `origin === "gap"` rather than current `status`: `origin` is set once
  at manifest build time and never changes (see `pipeline/manifest.py`),
  so it's the permanent "was this ever a cut candidate" marker,
  independent of whether it's since been restored — which is exactly
  what lets restored and still-cut entries both show up, visually
  distinguished (green left-border + "Restored" badge vs. plain), per
  the spec. Each entry has a Preview toggle (an inline `<video>` seeked
  to the segment's `start_s` via `loadedmetadata`, auto-pausing at
  `end_s` via `timeupdate` — bounded playback of just that span, not the
  whole source file) and a Restore/Cut-again toggle (`PATCH
  .../manifest/segments/{id}`, already built in Phase 6). Handles no
  batch yet and no manifest yet as distinct non-error states, not a
  crash.
- Nav bar (`App.jsx`) — Home / Edit Log, independent of the existing
  upload→calibrate→...→done stage machine, which is now nested under the
  Home tab unchanged. Switching tabs doesn't reset in-progress upload
  state.

**What's verified:** the production build succeeds. Every endpoint the
Edit Log depends on was exercised for real against the actual clip_300
batch from the Phase 7 manual pass (not a fake) — `GET manifest`,
`PATCH .../segments/{id}`, and `GET .../source/{filename}` (206 Partial
Content) all confirmed working directly. The manual browser pass then
covered the rest: restore toggle visual state, preview playback, and
list rendering, plus one real bug it caught (below).

**Caught during the manual pass, root-caused and fixed:** toggling a
segment restore → cut-again showed the *first* toggle's visual result
correctly but not the second — the backend was confirmed correct at
every step (`PATCH` responses and the manifest itself both showed the
right final status), so this was specifically a client-side rendering
question. Investigated by bundling the actual `EditLogView.jsx` source
and driving it through `jsdom` + `@testing-library/react` under
`React.StrictMode` (matching `main.jsx` exactly) across seven scenarios
— sequential toggles, multi-row, a rapid no-wait-between double-click,
interleaved different-row toggles, and toggling with the preview player
open — none reproduced it. Rather than keep guessing at an
unreproducible cause, `handleToggle` was rewritten to eliminate the
entire bug class regardless of root cause: it now does a fresh
`GET .../manifest` after every successful `PATCH` and fully replaces
local state from that response, instead of merging the `PATCH`'s own
response into local state. A `console.log` was added at the exact
moment a toggle is clicked, before any request goes out, specifically
so a real click could be told apart from a request just being hard to
spot in a busy Network tab. Confirmed resolved on a clean retest:
Console showed both clicks firing, Network showed both `PATCH`
requests returning 200 with a manifest refetch after each, and the page
now visually shows the correct final state — the original symptom was
stale visual state in an already-open tab from before the refetch fix
landed, not a live bug in the current code.

**End-to-end proof the mechanism affects real video, not just JSON**
(requested specifically, since Phase 9 wasn't built yet to demonstrate
this through the UI): against the real clip_300 batch, restored
`seg_002` via `PATCH`, triggered a real `POST /export`, and confirmed
two ways — not just a duration number. First, a real finding worth
recording: total output duration went *down* (181.3s → 178.0s) despite
restoring content, because merging the restored span with its two
kept neighbors removed a keyframe-snap boundary's worth of the slack
documented under Stitching below; `scripts/stitch.py`'s own "requested
output duration" line confirmed the manifest-level accounting was
still correct (170.5s → 172.3s, exactly the restored span's length),
which is why duration alone isn't proof and a frame check was needed.
Second, extracted a real frame at the corresponding output timestamp
and compared it to the same timestamp in the original source: same
field, same players, same batter mid-swing, same clouds — the restored
segment's actual pixels are in the output. Cut it again, re-exported:
duration returned to exactly 181.299s (byte-identical to the original
baseline), and the same frame position now showed different, later
content — the restored footage was genuinely gone, not just relabeled.

**Phase 6 (backend API):** confirmed against a real running server (not
just FastAPI's in-process test client) — `scripts/smoke_api.py` uploads a
real reference clip, uploads its calibration, triggers real detection
(real model inference, no fakes), polls progress through real stage
transitions, restores a real cut segment via the manifest-update
endpoint, triggers real export, and confirms the stitched output file
exists and plays. All passed (`SMOKE TEST PASSED`).

**Caught during review, fixed before this phase was signed off (not
deferred):** the first version of this phase let a batch be processed via
the API with no way to ever provide a plate calibration — `resolve_zone()`
looks in the video's own directory, but uploaded files live in
`uploads/<batch_id>/`, so every API-triggered job silently ran with
plate-occupancy disabled, which means the Phase 3 at-bat boundary system
(the mechanism that extends a segment through a defensive play and tells
a real batter change apart from a step-out) never ran at all — a
materially weaker pipeline than everything Phases 2-5 validated, not a
cosmetic gap. Confirmed directly: the first smoke-test run *did* execute
this degraded path (`clip_60.mkv` produced 10 kept/10 cut segments,
visible in the job's `warnings` field) — a different, weaker result than
the fully-calibrated CLI run. Fixed by adding `POST /batches/{id}/calibration`
(accepts either an existing `calibration.json` to reuse, or plate pixel
coordinates to compute a fresh one — the API equivalent of `scripts/
calibrate.py --set x,y`) and `GET` to read it back; `pipeline/calibration.py`
gained `build_calibration()`/`save_calibration()`, refactored out of
`scripts/calibrate.py` so the CLI and the API write the identical schema,
not two copies that could drift apart. Re-ran the smoke test with
calibration uploaded first: `clip_60.mkv` now produces **7 kept/7 cut
segments — an exact match for the original CLI run**, with no warnings.
`tests/test_backend_api.py` includes a pair of tests that check this
directly by inspecting the actual `zone` object `process_video` was
called with (not inferred from the warnings list being empty, which a
careless fake could satisfy by accident either way).

**Second review round, also fixed before sign-off:** the calibration fix
above still let a batch start processing uncalibrated by default —
finding out only from a completed job's `warnings` field is expensive
when a real run takes 37 minutes. `POST /batches/{id}/process` now
checks for `calibration.json` before doing anything else and returns a
400 immediately if it's missing, unless the caller explicitly passes
`allow_uncalibrated: true` — a deliberate, visible opt-in instead of a
silent default, while still preserving the CLI's existing behavior that
calibration is not force-required (some camera setups genuinely can't
frame the plate). Verified on the real running server, not just in
tests: `curl`ing `/process` on an uncalibrated batch returns the 400
immediately; the same call with `allow_uncalibrated: true` returns 200
and a real job starts. Setting calibration is itself now rejected
(409) once the batch's detect job is pending, in_progress, or completed
— changing it at that point would either race an already-running job or
silently do nothing for one that's already finished.

Endpoint-level tests (`tests/test_backend_api.py`, `tests/test_jobs.py`,
`tests/test_storage.py`) use a fast fake pipeline and cover: every
malformed-request case the spec calls out (updating a manifest that
doesn't exist yet, updating an unknown segment id, triggering processing
twice on the same batch, triggering export twice), the
single-job-at-a-time lock (a second batch is correctly rejected while one
is active, but a batch stuck on `needs_order_confirmation` correctly does
NOT block others), the ambiguous-order HTTP flow against real
ffmpeg-generated multi-file clips, the calibration endpoint (both input
modes; malformed files; out-of-bounds coordinates; negative radius;
unknown batch; setting calibration after the detect job is in_progress
or completed, both rejected), the trigger-processing calibration gate
(blocked by default, the `allow_uncalibrated` opt-in, and proceeding
normally once calibration is set), and the startup interrupt sweep.

**Phase 5 full-length checkpoint (per the project spec's requirement to
test a real 30-60+ min video before this phase is considered done):** ran
the complete pipeline — detection through stitching — against a real
67.5-minute (4049.9s) game recording (`reference_clips/full_game.mkv`,
1920x1080).
- Detection: 37.4 minutes wall-clock (0.55x realtime), peak memory ~969
  MiB resident (2.15 GiB peak footprint per macOS's accounting) — stayed
  bounded throughout, no runaway growth. 120 candidate segments, 84% of
  the game (3391.7s) flagged kept.
- Stitching: 8.2s wall-clock for all 120 spans (stream-copy path, no
  re-encode needed — single source file). Output: 1.42 GB, fully
  decodable (180,690 frames, matching the expected frame count for its
  duration almost exactly — no corruption). Rendered duration (3769.5s)
  came in 377.8s longer than the manifest's requested 3391.7s (3.15s
  average extra per span, 6.17s max) — verified as stream-copy
  keyframe-snap slack, never lost or duplicated content (see the
  Stitching section below for the root cause, checked per-span against
  this file's real 6.006s keyframe interval, not just asserted).
- Visual spot-check (since there's no ground truth for this file): sampled
  frames across the output — 5 spread through the full timeline plus the
  start/middle/end of the 3 longest segments (144s, 135s, 106s). All show
  real live action (batters mid-stance, fielders positioned and moving, one
  frame catches a live pitch mid-flight) — this is genuinely catching real
  plays, not just avoiding a crash. One honest caveat: the tail end of the
  144s segment shows a batter walking toward the plate with the bat
  lowered, not swinging — a borderline dead-time moment kept in per the
  priority rule's bias toward not cutting early, not a detection failure.

**Re-confirmed during the Phase 9 pre-demo checklist**, with a real scare
along the way worth recording honestly: a first re-run of this same file
(via the API, concurrently with other checklist testing) took 67.6
minutes — nearly double. Rather than accept "the machine was probably
busy" as an unverified excuse for a hard numeric bar this project set,
a second run was done in genuine isolation: the one other thing found
running was a leftover `uvicorn --reload` process that had silently
accumulated an enormous amount of background CPU time (killed before
re-running), the CLI was used directly instead of the API to remove the
FastAPI/threading layer as a variable, and the stale detection cache
entry for this exact file was deleted first (it otherwise would have
produced a meaningless near-instant cache-hit "measurement" instead of
a real one). Result: **2237.02s = 37 min 17s wall-clock**, peak RSS 931
MiB, peak footprint 2.14 GiB — matching the original checkpoint within
1-4%, i.e. no regression at all. Detection output was identical again
too (120 segments, 84% flagged). The 67.6-minute figure was genuinely
an artifact of that specific run's environment, not the pipeline.

Open items:
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
- **Stitching** (`pipeline/stitch.py`, `scripts/stitch.py`) — renders a
  manifest's `kept` spans into one finished output video. Reads
  `kept_spans_by_file()` so spans are pulled per file, in
  `source_file_index` order, each file's own spans in their own local
  order — never merged across a file boundary, matching the Phase 3/4
  boundary decision: a play split across two files is rendered as two
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
  - `boundary_test_part1/2.mkv` (the Phase 4 file-boundary pair): confirms
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
  callbacks (added in this phase) to durable progress updates.

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
  that point. `GET /batches/{id}/preview.jpg` (Phase 7) is what the
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

  **Preview frame + auto-chained export** (Phase 7) —
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
  per the Phase 0 rule to keep Node and Python dependencies separate) —
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
  view (Phase 9) — see Current Status above for why automatic was
  chosen over an explicit button.

  **The toggle re-fetches the manifest fresh after every `PATCH` rather
  than merging the response into local state.** Caught during the Phase
  8 manual pass: a restore → cut-again sequence sometimes didn't show
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
  job state (Phase 6) is what makes this possible at all.

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

# Phase 1 baseline (motion only, no person detection or play extension)
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
lets the Edit Log (Phase 8) list "every segment that was ever cut"
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
- **Fixed during Phase 6 review: calibration wasn't reachable through the
  backend API at all**, so every API-triggered job silently ran with the
  Phase 3 at-bat boundary system disabled — see the Current Status
  writeup above for the full account (what was actually lost, how it was
  confirmed, and the fix). Also fixed in the same review round:
  `POST /process` now actively blocks on missing calibration (400) unless
  the caller explicitly passes `allow_uncalibrated: true` — no longer
  possible to trigger a batch uncalibrated by accident, only on purpose.
- **Fixed: upload accepted path-traversal filenames.** `POST /batches`
  wrote each uploaded file to `<batch_dir>/<client-supplied filename>`
  with only an extension check — a filename like `../../evil.mp4` (or
  a bare `..`, which needs no slash at all) would have written outside
  the batch's own directory. Found while designing the Phase 8 source-file
  endpoint (which reads by filename and needed the same class of check),
  fixed immediately since this was a live write-side issue in already-committed
  code, not deferred to Phase 8. `upload_batch` now rejects any filename
  containing `/`, `\`, `..`, or that doesn't equal its own
  `os.path.basename()`, before anything is written to disk. Verified with
  a test that reverts the fix and confirms the rejection tests actually
  fail without it, not just that they pass with it.
- **Calibration accuracy depends on click precision.** The Phase 7
  manual browser pass found the saved plate coordinates for one clip
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
  three reference clips got.
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
- **Source-file endpoint tests** (in `tests/test_backend_api.py`, Phase 8)
  cover the allowlist specifically: serving a real upload, an unknown
  batch, a filename the batch doesn't have, a percent-encoded `%2e%2e`
  (the actual adversarial case — a literal bare `..` never reaches the
  server for real, since httpx/browsers collapse it during URL
  construction before the request is sent, so a test using the bare
  form would pass for the wrong reason), and that two batches with an
  identically-named file don't leak into each other.
