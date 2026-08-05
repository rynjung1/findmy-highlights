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

## Priority rule: v1 default vs. current live setting

This rule changed partway through the project, on purpose, by explicit
decision. Both parts apply, to different things.

**v1 default** (everything through the enter-side scale boost) was built
under a strict bias-toward-inclusion rule: missing a real play was treated
as worse than keeping extra dead time, always err toward keeping a
borderline segment. That rule is why several designs got built, tested,
and explicitly rejected for introducing real (if small) recall risk: the
plate-distance and person-proximity segments.py designs, more aggressive
zone-velocity and bat-anchored discount factors, and the ambient-discount
enter-side idea. All of those were safe-but-rejected, not broken.

**As of this point forward**, the project's owner has explicitly decided
to accept real risk of occasionally trimming into or missing a real play,
in exchange for more aggressive dead-time cutting — a deliberate, informed
choice made after seeing the actual tradeoff data on every rejected
design, not a default to assume lightly or revert to quietly. Going
forward: previously-rejected designs are back on the table for real
implementation, not just simulation; thresholds (`enter_thresh`,
`exit_thresh`, padding, discount factors) can be pushed more aggressively
than the margins established during the v1 safety work; but the real
tradeoff — recall/continuity impact alongside dead-time gained — still
gets reported honestly every time, and a change that clips into or fully
misses a real, unambiguous play on a reference clip still gets flagged
plainly, not silently absorbed as "expected now." `enter_thresh` itself
was explicitly considered and explicitly left alone (see Known
Limitations) — this rule change doesn't mean every dial gets turned to
its riskiest setting by default, each one still gets proposed and reviewed
on its own.

## Current status

**v1 is complete.** Every piece of work originally scoped for this version —
the detection pipeline, multi-file handling and stitching, the backend API,
the full Home/Upload and Edit Log frontend, multi-base calibration, and the
zone-velocity signal built on top of it — is done, tested, and has been
exercised end to end against real footage and a real running server, not
just in-process test clients. `pytest tests/` covers 411 unit tests,
`pytest tests/ -m e2e` covers 6 tests that run real model inference, and a
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

**Timing drift investigated to a real, confirmed conclusion across four
`full_game.mkv` runs — not left as a guess.** A fresh, cache-cleared run
(every currently-shipped mechanism active: dynamic padding, the tuned
`exit_thresh`, the enter-side scale boost, hard-cut-with-exclusion)
clocked **62.20 minutes wall-clock**, ~66% slower than the 37.4-minute
figure above, with memory and cut/kept behavior both confirmed identical
to history in the same run (956 MiB peak RSS vs. ~969 MiB documented;
50.65 min kept, an exact match to the padding-only 53.19 min baseline
minus hard-cut's own already-documented +2.54 min estimate) — real,
isolated to wall-clock time, not a correctness regression, and X-CLIP was
not a factor (not invoked in this call at all).

Three follow-up runs, each ruling out one real candidate cause rather than
assuming it:

1. **Contention**, checked first (same isolation this project already used
  once to root-cause a leftover `uvicorn --reload` process): confirmed no
  extraneous `claude-code` process, no dev servers, load average low. A
  fresh isolated re-run landed at **62.11 minutes** — 0.09 min from the
  first, and every behavioral number (348 segments, 204 hard-cut windows,
  75.03% kept) bit-for-bit identical. Contention ruled out: isolating the
  environment changed nothing.
2. **Thermal, attempt one**: real `sudo powermetrics` logging alongside a
  fresh run surfaced something bigger than throttling — the run took
  **18.8 hours** (`elapsed_min=1126.41`) against only ~24 minutes of actual
  CPU time (2% utilization). macOS's own `powermetrics` output literally
  reported `Current pressure level: Sleeping` at one point, and the log
  showed 109 recurring ~900-1075s gaps all night — the system was
  Power-Nap sleep-cycling for hours, unprotected by `caffeinate`, both the
  detection process and the logger themselves getting suspended and
  resumed dozens of times. Every non-Nominal thermal reading that DID occur
  (39 samples, Moderate→Heavy) fell in one 110-second burst right as real
  computation started, then settled to Nominal and never recurred —
  suggestive, but not a valid continuous-window measurement, since the
  real active work was itself fragmented by sleep after only ~19 minutes.
3. **Thermal, attempt two, done right**: the whole run wrapped in
  `caffeinate -i`, `powermetrics` also `caffeinate`-protected, confirmed
  zero gaps >5s across the full 37.75-minute capture. Real result: **35.66
  minutes wall-clock** — faster than the original 37.4-minute baseline,
  and roughly half the two uncaffeinated isolated runs above — at 60.8%
  CPU utilization (vs. 32-36% uncaffeinated) doing the identical amount of
  real work (~1226-1362 CPU-seconds every single time, all four runs).
  Thermal pressure was real and sustained this time — **Heavy for 83.44%
  of the run** (1778 of 2131 samples), mean CPU package power 2214.9 mW
  against a ~100-200 mW idle baseline — but P-cluster frequency, whenever
  actually engaged, reached 3386-4037 MHz, near this chip's top boost
  range, not suppressed. **The real, load-bearing finding: this was
  simultaneously the run with the most sustained thermal pressure and the
  fastest completion time of all four attempts** — the opposite of what
  throttling would predict. Thermal throttling is ruled out on its own
  terms, not assumed away.

**Conclusion: `caffeinate` itself is the confirmed fix, independent of
whatever the exact OS mechanism is.** It nearly doubled CPU utilization
(32-36% → 60.8%) on identical total work across every run measured
tonight, which is the real explanation for the 62-minute figures — some
non-thermal, non-contention OS-level scheduling deprioritization of an
unprotected long-running background process, name not fully pinned down,
but the practical fix is real, measured, and reproducible regardless. See
How to run it below for the resulting default recommendation.

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

**Review/training queue, Tier 1 (data collection + UI only — Tiers 2/3
deliberately not built).** Surfaces the pipeline's own most-borderline
real decisions for a human to label Downtime vs. Real action after the
fact, so future threshold-tuning and (eventually, if the label count
ever justifies it) a learned classifier have real ground truth to check
against instead of just the 9 hand-annotated reference clips.

Two candidate types, generated at the end of a real `process_video()`
run: **hard-cut dips** (a real window `apply_hard_cuts` shipped — margin
combines how far its peak motion score sits below `quiet_thresh` and how
far its own duration sits above the minimum raw dip length, the smaller
of the two governing, same "weakest margin wins" pattern dynamic padding
already uses) and **segment boundary crossings** (a raw ENTER/EXIT
hysteresis crossing from the pre-extension segmentation — margin =
`|score - threshold|` at that exact sample, via a new
`pipeline.segments.find_boundary_crossings`, kept as a separate function
rather than changing `scores_to_segments`' own return shape, which every
existing caller already relies on). Selection keeps the
`max_candidates_per_video` (default 5) lowest-margin candidates across
both types, plus — with probability `control_sample_rate` (default 0.1)
per video, not per candidate, since a control sample's job is occasional
calibration, not scaling with how borderline a given video happened to
be — one random window well inside a clearly-kept segment or a clearly-
cut ordinary gap (never inside a hard-cut window), so the label store
isn't composed entirely of edge cases.

**Opt-in by design, off unless explicitly turned on twice over.**
`pipeline.run.process_video`'s `training_data_dir` param defaults to
`None`; `scripts/regression.py` never passes it, so its many repeated
runs against the same 9 reference clips can't flood the label store.
The real product path (`backend/app.py`) reads it from an environment
variable, `FMH_TRAINING_DATA_DIR` — not a hardcoded real path — for a
reason specific to this project: `scripts/smoke_api.py` drives the real
backend over HTTP, indistinguishable at the API layer from a genuine
user upload, so a hardcoded default would have meant every dev running
the smoke test against their own local server silently contributed
synthetic clip_60.mkv records to the label store. An env-var opt-in
means nobody, including a developer poking at their own dev server,
collects training data by accident — a real deployment that wants this
has to choose it explicitly.

**Storage matches this project's own established per-file convention**
(same pattern as `.cache/detections/<hash>.json`): one self-contained
`training_data/reviews/<id>.mp4` clip (full-frame, never cropped, window
padded ~1.5s each side, decoded and re-encoded rather than stream-copied
so a short arbitrary window is frame-accurate regardless of the source's
own keyframe placement) plus a `training_data/reviews/<id>.json` record
(id, created_at, source, window, candidate_type, pipeline_decision,
`features_at_label_time`, a `config_hash` of the exact threshold values
these candidates were generated under — so labels from before and after
a future threshold-tuning pass can be told apart honestly instead of
silently pooled — and `label`/`labeled_at`/`note`, all null until
answered). `*.mp4` was already globally gitignored (bulky, personal
footage); `*.json` metadata stays tracked, no `.gitignore` change
needed.

**Backend** adds three endpoints guarded by an id allowlist regex
(`^[a-z0-9_]+$`, matching every id this project ever actually generates)
rather than a blocklist, the same reasoning the source-file endpoint's
own filename check already documents: `GET /review/next` (lowest-margin
unlabeled record first, control samples — `margin: null` — sorting
last), `GET /review/{id}/clip` (serves the self-contained clip), and
`POST /review/{id}/label` (writes the answer, returns the next pending
item so the frontend can label one after another without an extra round
trip). An empty queue is a normal `200 {"done": true}` on both read
endpoints, not an error — the review queue being empty isn't a failure
state. `backend/pipeline_runner.py` threads `training_data_dir` through
to every real detect job and tags each record's `source` with the real
`batch_id`, so a labeled record can always be traced back to the batch
it came from.

**Frontend**: `ReviewQueueView.jsx`, a new top-level nav entry alongside
Home/Edit Log (global, not scoped to any batch). Plays the clip, shows
candidate type / pipeline decision / margin as badges, two buttons
(Downtime / Real action) that post the label and load whatever the
server returns next. Verified with a real, driven browser session (not
just unit tests): seeded a scratch `training_data/reviews/` with three
real ffmpeg-generated clips + matching JSON records (one of each
candidate type), ran the real backend with `FMH_TRAINING_DATA_DIR` set
and the real Vite dev server, and drove it with Playwright — confirmed
correct lowest-margin-first ordering, the control sample correctly
sorting last, each label click advancing to the next item, the final
click correctly reaching the empty state, and zero browser console
errors. **This caught a real bug before it shipped**: `frontend/vite.config.js`'s
dev-server proxy only forwarded `/batches/*` to the backend — `/review/*`
requests were silently served the SPA's own `index.html` instead (visible
in the browser as `"Unexpected token '<'... is not valid JSON"`), exactly
the kind of failure `npm run dev` plus a real click-through catches and
a unit test suite structurally cannot, since every backend test talks to
the FastAPI app directly, never through Vite's proxy. Fixed by adding
`/review` to the proxy map alongside `/batches`.

**Usage (Tier 1 only): `scripts/review_stats.py`** reports disagreement
rate — how often a human's label disagreed with the pipeline's own
claim at that instant (`cut`/`exit` claim "downtime", `kept`/`enter`
claim "real action") — per candidate type and per decision within each
type, with an explicit low-sample-size warning under 20 labels. Verified
against the real records the Playwright session above produced and
labeled: correctly reported 2 of 3 disagreements (the `enter` and `cut`
labels were deliberately mislabeled during that click-through to prove
the arithmetic, the `kept` one deliberately agreed), matching hand
verification exactly. **Tiers 2 (threshold calibration via a
precision/recall sweep) and 3 (a learned classifier) are deliberately
not built** — per this project's own honest-threshold standard, Tier 3
specifically needs 300-500 labeled events across 6-10 distinct recording
sessions, evaluated against the 9 reference clips as a fixed held-out
check, before a learned classifier is a better bet than the current
hand-tuned thresholds; nowhere close to what a first review session
produces.

**Bulk candidate mining, real backlog handling, and real feature-vs-label
pattern analysis — the label-accumulation infrastructure the Tier 3 bar
above actually needs, built once real usage made "5 candidates per run"
too slow a trickle to ever reach it.** Three real, separate pieces, all
built on the existing Tier 1 machinery, none of it duplicated.

*`scripts/mine_review_candidates.py` (new).* The per-run cap
(`ReviewConfig.max_candidates_per_video`, still 5 for every normal
`process_video` call) turned out to just be a config value —
`select_candidates()`'s own `ranked[:cfg.max_candidates_per_video]`
already returns everything when the cap is `None` (a real Python slicing
property, not new logic), so widening the field to `int | None` and
adding a `review_cfg` passthrough to `pipeline.run.process_video()`
(defaults to `None`, zero behavior change for the backend or either CLI
script) was the whole change needed to make "uncapped" a first-class
option. The script itself reuses `process_video()` directly — real
detection is already cached, so mining against an already-processed
video is cheap — in two modes: a single video path (calibration resolved
the same way `scripts/detect.py` does), or `--all-batches` (scans
`uploads/*/` for batches with a real `manifest.json` — "already
processed" — using each one's own `files.json` and calibration; a batch
without a manifest is reported and skipped, never triggered into
processing as a side effect of running this script). `--limit` is a
total budget across the whole invocation, not per video. Mined records
get `source.mined_by` set for provenance, same pattern as the existing
`batch_id` tag. **Real run**: mined 40 real candidates from
`reference_clips/full_game.mkv` in one invocation (detection cache hit,
motion computed fresh) — all real hard-cut-dip candidates, real margins,
real audio/xclip features attached (pose absent on the specific ones
checked by hand, correctly, since no near-plate detection existed at
those exact instants) — sitting in `training_data/reviews/` now, real
and committed, unlabeled, ready.

*Review-queue backlog handling.* The real gap wasn't queue-scan
performance (`_pending_reviews` already re-reads and re-parses every
record file, labeled and unlabeled, on every request — real, honest,
O(n) with no cache or index, but trivial at the scale that matters here)
— it was that nothing reported queue size at all;
`ReviewQueueView.jsx`'s own `reviewedCount` was session-only, no
denominator. Fixed with a `remaining` field added to `GET /review/next`
and `POST /review/{id}/label`'s responses (both already compute the full
pending list; this is just its length) and shown prominently in the UI.
**Verified against the real 40-candidate batch above, not a synthetic
one**: copied it to a scratch `training_data_dir` (never the real one —
a script clicking through and labeling all 40 for real would have
destroyed the actual batch before real labeling started), pointed a
second real backend instance at the copy, and drove all 40 real
`GET .../clip` + `POST .../label` calls end to end. `remaining` counted
40 → 0 with zero gaps or off-by-ones. Real timing:
`POST /review/{id}/label` mean 1.5ms/max 2.3ms, `GET /review/{id}/clip`
mean 2.5ms/max 3.7ms — both effectively instant at this scale, confirming
"smooth through 30-50+ in one sitting" empirically rather than assuming
it from the O(n) design alone.

*`scripts/review_stats.py` extended*: a new "Feature vs. label patterns"
section, real distribution split by real label (not just an aggregate
disagreement rate) for all four instrumentation features — motion score
(`peak_score`/`score`, unified with a fallback since the two candidate
types name it differently), pose `peak_displacement_px`, audio
`rise_time_s`, xclip `p_swinging` — each reporting real n/min/median/
mean/max per label group plus a real AUC (same `P[real_action >
downtime]` formula used for every signal validation tonight), correctly
oriented per feature (audio's own polarity is inverted — a short rise
time is the real-action-like direction, not a long one — so its AUC is
computed against the reversed pair, not naively). Records missing a
feature are excluded from that feature's own analysis and the skip count
reported, not hidden. Kept dependency-light (stdlib `statistics`, no new
import), matching the script's existing style. 12 new unit tests cover
the pure `auc()`/`_motion_score()`/`feature_label_pattern()` logic
directly, including the audio-inversion case specifically (a real_action
group with genuinely shorter rise times than downtime must still read as
a *high*, correctly-oriented AUC).

Nothing here changes what the pipeline cuts or how a real detect job
behaves by default — `mine_review_candidates.py` is the one real caller
that ever passes a non-default `review_cfg`, and the `remaining` field
is purely additive to the existing response shape. Full suite (429
passed) confirmed clean before and after.

**First real single-feature model against real human labels: xclip
p_swinging alone, n=40, real LOO-CV, closed as a genuine-but-fragile
replication — not stable enough for a threshold, but real enough to
become a review-queue ranking signal.** The 40 mined candidates above
got labeled for real (14 real_action, 26 downtime) — the first real test
of whether xclip's own AUC finding (0.690/p=0.012 against the clean
reference-clip swing/ambient set) replicates on genuinely different,
harder data: real human labels on `hard_cut_dip` candidates specifically
(already-borderline-by-construction, not clean swing-vs-ambient-gap
instants).

Same LOO-CV + permutation-test discipline as the joint classifier
investigation earlier tonight. **Raw AUC: 0.662 (permutation p=0.0455).
LOO-CV AUC (midpoint-of-group-means threshold, refit per fold): 0.662
(p=0.0410)** — a real, single, pre-specified test (not "best of several
tried," so no multiple-comparisons discount applies), and a real
replication of the earlier finding's direction and rough magnitude on
independent data.

**But a fitted threshold demonstrably doesn't survive at this n, checked
directly rather than assumed from the AUC alone.** LOO accuracy at the
naive midpoint threshold: 62.5% (25/40) — *worse* than just always
guessing "downtime" (65%, the majority class at this 26/14 split). Then,
letting each LOO fold fit its own best-separating threshold on the other
39 points (not just the midpoint) to see if a better cutoff existed:
accuracy **collapsed to 37.5%**, with the fitted threshold swinging from
0.618 to 0.941 across folds — the same "textbook overfitting instability"
signature (a boundary that swings too much fold-to-fold to generalize)
the joint classifier hit earlier tonight, demonstrated directly here on
a single feature, not assumed. AUC is threshold-free and doesn't feel
class imbalance the way a fitted cutoff does — that gap is the real
finding, not a contradiction.

**Verdict: real, replicated ranking signal; not a safe threshold.**
Grounded sample-size projection (not a repeat of the general 300-500
Tier 3 figure): using the actual observed effect size
(|AUC−0.5|=0.162) and the actual permutation-null standard deviation
measured at n=40 (0.097, scales ~1/√n), **~57 total labels** would be
needed for this specific effect to clear ~2 standard errors from chance
as a ranking signal — not a promise that a safe threshold exists even
then, given the demonstrated overfitting risk. Approved for one real use:
review-queue **priority ranking**, not cutting.

**Implemented**: `pipeline.review.xclip_disagreement()` computes, per
candidate, how strongly xclip's `p_swinging` disagrees with what that
candidate's own `pipeline_decision` claims (reusing
`DECISION_EXPECTS_LABEL`, now defined once in `pipeline.review` and
imported by `scripts/review_stats.py` instead of duplicated).
`review_priority_key()` reorders the pending queue: real disagreement
first (highest first), falling back to the original lowest-margin-first
order for anything xclip has no opinion on — a candidate with a real,
even mild, xclip disagreement now outranks a candidate with no xclip
data regardless of margin, but every existing margin-only ordering is
completely unchanged for records xclip has no opinion on.
`backend/app.py`'s `_pending_reviews()` calls this directly (no pipeline
logic reimplemented in the backend, matching this project's own
architecture rule). **Nothing is hidden, skipped, or auto-decided** — a
human still sees and labels every real candidate eventually, just in a
different order; `ReviewQueueView.jsx` shows the raw `p_swinging` value
and a "signal disagrees" badge whenever present, so the reordering is
never invisible. Verified against a real, fresh, unlabeled batch (see
below): the API's real top-of-queue item matched the independently
computed expected order exactly, on a real case where the pipeline
confidently said "cut" (margin -0.353) but xclip read 0.917 — precisely
the kind of case this is meant to surface first.

**`scripts/mine_review_candidates.py` gained real dedup logic** the
first mining pass didn't need: real candidate ranking is deterministic
(same video, same config → the same ranked list), so a second mining
pass over the same video would otherwise just regenerate the same
lowest-margin windows already mined (and likely already labeled) instead
of reaching new ones. `_existing_windows_for_video()` scans existing
records for the same `source.video_path`, and `mine_one()` requests
enough candidates to cover what's already mined plus the new budget,
then deletes (not skips-silently) any real duplicate window before
writing anything permanent. **Real run**: 18 more real candidates mined
from `full_game.mkv` in one pass, correctly reporting "40 candidate(s)
matched an already-mined window... discarded rather than duplicated" —
58 total records, 58 distinct windows, confirmed zero duplicates.

Full suite: 442 passed.

**LOO-CV re-checked at n=58 (40 original + 18 more real labels): the
ranking signal got stronger, the threshold instability didn't.** Raw and
LOO-CV AUC both moved from 0.662 (p≈0.045) to **0.700 (p=0.0050 raw,
p=0.0045 LOO-CV)** — a real, meaningful improvement, confirming the
ranking-signal decision was the right call, not a lucky small-sample
read. But the threshold-safety question specifically did **not**
improve: naive-midpoint LOO accuracy (65.5%) still trails the majority-
class baseline (69.0%, now higher since the class split drifted to
40/18), and letting each fold fit its own best-separating threshold
still collapses accuracy (43.1%, up only slightly from 37.5% at n=40),
with the fitted threshold now clustering near the *maximum* observed
value rather than settling into a stable middle — a different, still-
unstable failure mode, not a resolved one. Why these move in opposite
directions: AUC measures ranking quality across the whole distribution,
which more data makes more reliable; a specific threshold's accuracy
depends on how much the two distributions overlap in the boundary
region, and more data of the *same kind* doesn't shrink that overlap, it
just gives a more confident read that a real difference exists on
average. Verdict unchanged and now better-supported: ranking only, no
threshold.

**That last point raised a real, checkable question: is the overlap
specific to `hard_cut_dip` candidates, since every one of the 83
candidates mined so far was that one type?** `pipeline.review.ReviewConfig`
gained a `candidate_types` field (a real frozenset filter applied to the
ranked pool before capping, default `None` = unrestricted, unchanged
behavior) and `scripts/mine_review_candidates.py` a `--candidate-types`
flag, so a mining pass can deliberately target `boundary_crossing`/
`veto_boundary` instead of letting `hard_cut_dip`'s systematically more
negative margins dominate every unrestricted ranking.

**A real bug, found and fixed the same session it shipped, not
discovered later.** The first type-restricted run (`--limit 30
--candidate-types boundary_crossing,veto_boundary`) wrote **114**
candidates — the dedup budget math (`existing + budget`) counted every
existing record for this video regardless of type (83, all
`hard_cut_dip`), even though none of those windows could ever match a
`boundary_crossing` one; the run asked for 83+30=113 and every single
one came back "genuinely new" since nothing overlapped, overshooting the
requested 30 by 83. Root cause and fix: `_existing_windows_for_video()`
now takes the same `candidate_types` filter and only counts existing
records of those same types — a real regression test
(`test_existing_windows_for_video_scoped_to_requested_types`) locks in
the specific before/after behavior. The 83 excess records were trimmed
back to the real 30 lowest-margin ones (matching exactly what a
correctly-sized run would have produced, since both draws come from the
identical deterministic ranked pool) and deleted, not left sitting in
the queue past what was actually asked for.

**Real, honest result once fixed: 30 real `boundary_crossing` candidates,
zero `veto_boundary`.** Not a bug — consistent with this project's own
already-documented finding that the veto mechanism essentially never
fires on real footage ("no person-free motion run longer than 0.6s,"
see Signal fusion above). `boundary_crossing` margins are real,
genuinely distinct floats in the 1e-8-to-1e-5 range (not literal ties,
just naturally tiny near a real hysteresis crossing) — the ranking is
meaningful even though it looks like "0.0000" at 4 decimal places.
Whether this genuinely different, non-`hard_cut_dip` sample shows the
same threshold-overlap pattern is still open — these 30 are freshly
mined and unlabeled, ready for whenever real labeling picks back up.

**`scripts/mine_review_candidates.py` gained real dedup logic** the
first mining pass didn't need: real candidate ranking is deterministic
(same video, same config → the same ranked list), so a second mining
pass over the same video would otherwise just regenerate the same
lowest-margin windows already mined (and likely already labeled) instead
of reaching new ones. `_existing_windows_for_video()` scans existing
records for the same `source.video_path`, and `mine_one()` requests
enough candidates to cover what's already mined plus the new budget,
then deletes (not skips-silently) any real duplicate window before
writing anything permanent. **Real run**: 18 more real candidates mined
from `full_game.mkv` in one pass, correctly reporting "40 candidate(s)
matched an already-mined window... discarded rather than duplicated" —
58 total records, 58 distinct windows, confirmed zero duplicates. A
later, dedup-protected pass added 25 more real `hard_cut_dip` candidates
the same way (83 total at that point, 58 labeled).

Full suite: 446 passed.

**X-CLIP's AUC confirmed on a genuinely mixed, harder set — not overfit
to one candidate type.** Once real labels existed across `hard_cut_dip`,
`boundary_crossing`, and a control sample together (114 labeled records:
34 real_action, 80 downtime), raw AUC = **0.691** (permutation p=0.0005,
2000 shuffles) — real_action median p_swinging 0.809 vs. downtime's
0.669. This is the same signal already validated on the clean reference-
clip swing/ambient set (0.690) and on `hard_cut_dip`-only real labels
(0.700 at n=58) holding up on a structurally different, harder mix it
was never tuned against — real evidence this is a genuine property of
the signal, not a fit to one narrow candidate type.

**A real, structural pattern found in `boundary_crossing`'s own
disagreement rate, investigated down to the code, not left as "needs
more labels."** Per-type disagreement rates on the 114 labeled records:
`hard_cut_dip` 30.1% (25/83), `boundary_crossing` exit 28.6% (4/14), but
**`boundary_crossing` enter 68.8% (11/16)** — far worse than everything
else measured tonight. Pulled all 16 real `enter` records and looked for
what actually separates the 11 disagreements from the 5 agreements.

Every one of the 16 sits within 0.00001 of `enter_thresh` (0.006) — not
a bug, just what "lowest margin first" mechanically selects — so raw
score itself doesn't separate the groups (both groups run
0.00600-0.00601). What does: **whether a person was detected near the
plate at that exact instant** (`pose` present/absent in
`features_at_label_time`, meaning a real near-plate RF-DETR box existed
at all, not a missing-calibration artifact — this video has real
calibration and other candidates in the same set have real pose data).
`enter` crossings with no one detected near the plate: **wrong 100% of
the time (6/6)**. With someone detected: wrong 50% of the time (5/10).
Fisher's exact test on that split: p=0.093 — real and suggestive, not
fully significant at this n (the "no pose" cell is only 6 records).

**Checked whether this is just "pose correlates with correctness in
general" by pulling the 14 real `exit` records the same way — it isn't;
the direction flips, and that flip is the more informative result.**
`exit` crossings with no one detected near the plate: correct 100% of
the time (0/5 disagree). With someone detected: wrong 44.4% of the time
(4/9). This is the same underlying signal read in both directions,
mechanistically coherent: a real person still at the plate means the
play is more likely still live (making a same-instant `enter` more
likely right and an `exit` more likely wrong); nobody at the plate means
it's more likely genuinely dead (the reverse). Confirmed directly against
the code, not just inferred from the correlation:
`pipeline.segments.scores_to_segments`/`find_boundary_crossings` open
and close a segment purely from `smoothed_score >= enter_thresh` — the
scale-boosted raw motion score alone, with **zero plate-occupancy or
person-detection input** at the crossing decision itself (occupancy is
used elsewhere in this pipeline — at-bat detection, zone velocity — but
never gates the raw hysteresis crossing). The real data is showing
exactly the gap the architecture predicts.

**Honest calibration check before claiming this is "fixable" outright:
neither the pipeline's own decision nor a naive pose-presence rule beats
just guessing the majority class on this real 30-record boundary_crossing
set** (21 downtime / 9 real_action, 70.0% majority baseline). The
pipeline's own enter/exit decision: 50.0% (15/30) — *worse* than the
baseline, a real, concerning number on its own regardless of what
explains it. A naive "predict real_action iff a person is detected near
the plate" rule: 66.7% (20/30) — a real, meaningful improvement over the
pipeline's own decision, but still short of the trivial baseline, so
this is a real structural insight about *where* the enter-side gap comes
from, not yet a validated, ready-to-ship replacement rule. Investigation
only — no pipeline logic changed. A real next step, if pursued, would
need to weigh the same priority-rule tradeoff this project has navigated
before with occupancy-based gating (see the enter-side ambient-discount
investigation in Known Limitations): gating `enter` on plate occupancy
could itself miss a real, fast, partially-occluded swing occupancy
detection fails to catch — a real risk to design around carefully, not
a free win just because the correlation is real.

**Enter-side occupancy gating: designed, simulated, and safety-validated
against all 9 reference clips (the same non-negotiable bar every
segments.py-adjacent change gets) — real, safe, but modest.** A real
discovery made while designing this: `pipeline.fusion.fuse()` already
computes an occupancy-boosted `combined` score
(`motion + w_person*person_motion + w_occupancy*occupied`, provably
`>= motion` everywhere) but it's **never used anywhere** in the shipped
pipeline — a pure additive boost like that structurally can't fix a
false-positive problem anyway, since it can only ever help a segment
open, never suppress one. The real finding needed something that could
genuinely suppress a false open, done safely.

*Design, informed specifically by the partial-occlusion risk.* A hard
veto (never open without occupancy evidence) was rejected outright — the
exact failure mode the ambient-motion-discount investigation already
closed as a structural dead end: a real, fast swing that occupancy
detection itself misses (RF-DETR samples at ~1fps; a real contact
instant can land between samples) would be silently discarded. Built as
a **debounce** instead: when no occupancy evidence exists within
`enter_occupancy_window_s` (2.5s, matching the scale of `pre_pad_s`, this
project's own existing "how much real lead-time a play needs" constant)
of a candidate enter crossing, the crossing must sustain for
`enter_debounce_s` before being trusted, instead of opening on the very
first sample — filtering brief, single-sample spikes that don't look
like a real swing burst (the shortest known real bursts, clip_foul1/
clip_300's e4, run ~1-1.5s, comfortably longer than any debounce tested).
**Critical safety property, real not assumed:** once trusted, the
segment is backdated to its own TRUE first-crossing instant, not the
later confirmation instant — the debounce only ever delays the DECISION
to trust a crossing, it never loses real content, by construction.
Occupancy evidence, when present, is completely unaffected (opens
immediately, zero change from today) — the mechanism only ever touches
the "nobody visible nearby" case.

Implemented as `pipeline.fusion.occupancy_near_times()` (windowed
occupancy lookup, O(n log n) via searchsorted) and two new
`SegmentConfig` fields (`enter_debounce_s`, `enter_occupancy_window_s`)
plus an `occupancy_near` parameter on `scores_to_segments` (`None` by
default, so `find_boundary_crossings` — deliberately left untouched, a
separate, review-queue-only concern — and any future caller that omits
it stays byte-for-byte unaffected). 10 new unit tests, including one
proving the "zero content lost" backdating property directly.

*Real simulation, `scripts/enter_occupancy_gate_investigation.py`,
mirroring `scripts/regression.py`'s exact real recall/continuity checks
(not a lighter bar) against the FULL real pipeline (motion → scale
boost → scores_to_segments → veto → refine_segments) run twice per
clip, baseline vs. debounce, identical in every other respect.* **9/9
clean, zero regressions, at both `enter_debounce_s=0.3` and `0.5`** —
recall, continuity, and total flagged time (600.9s, to the decimal)
identical to baseline on all 9 clips, explicitly confirmed on
`clip_base1`-`4`/`clip_foul1`/`clip_whiff1`. Checked this wasn't a
vacuous pass: 728 of 3465 real above-enter_thresh samples across the 9
clips (21.0%) genuinely had no occupancy nearby, so the debounce
genuinely activated — it just never mattered for these clips' own real
events, because every one of them has a real, visible batter near the
plate (that's what the reference clips are *of*), so the "no occupancy"
condition never coincides with a real required event here.

*Real effectiveness check against the actual motivating data
(`full_game.mkv`'s 16 labeled `enter` records) — honest, not dramatic.*
Ran the debounce against the real full_game.mkv motion/detection data
and checked whether any of the 16 real labeled instants changed
open/closed status. At `enter_debounce_s=0.3`: 1 of 16 changed, and it
was a real, correct fix — a `downtime`-labeled crossing that was wrongly
open under baseline correctly stopped opening. Swept `enter_debounce_s`
up to 2.0s (still well under any known real event's burst duration): the
fix count plateaus at **2 of 11 real disagreements**, zero broken, from
0.5s onward — the remaining ~9 false positives are apparently *sustained*
motion without a visible batter (ambient milling that lasts real
seconds, not a brief blip), a genuinely different failure mode a
debounce can't touch by design, not a tuning gap to push further on.

**Shipped: wired into `pipeline.run.process_video` (the enter-side raw
`scores_to_segments` call now passes `occupancy_near`, computed the same
way the investigation script validated — a fresh `compute_occupancy` at
`FusionConfig().stationary_v`, windowed via `occupancy_near_times` at
`enter_occupancy_window_s`) and into `scripts/regression.py`, kept in
sync with `process_video` for the same reason its scale-boost mirror
already is: this script must measure the pipeline that actually ships,
not a stale reimplementation.** Re-ran the full test suite (456 passed)
and the full `scripts/regression.py` gate — the real ship gate, not the
lighter investigation-script bar — against the now-integrated code:
`ALL PASS`, kept-before-hard-cut total 600.90s, byte-identical to the
investigation script's own pre-ship number, confirming the real
integration introduces no drift from what was validated.

A real, safe, zero-cost mechanism that fixes a real but modest slice of
the problem: fixing 2 of 11 real disagreements doesn't meaningfully move
the 68.8% enter-side disagreement rate on its own.

**The 9 remaining post-ship disagreements: a real hypothesis, checked
directly, and corrected.** Re-ran the shipped pipeline against the same
16 real labeled `enter` records and confirmed exactly 9 disagreements
survive (`scripts/sustained_ambient_xclip_check.py`). The working
hypothesis going in was "sustained ambient motion, no batter visible" —
checked directly against real occupancy over a wide (±10s, well beyond
the 2.5s debounce window) window around each: **wrong for 8 of the 9**.
Only one (`bc_121f61ed3d15`) is genuinely no-occupancy-anywhere-nearby;
the other 8 have real occupancy present somewhere in the wide window.
The actual failure mode is **presence without action** — a person
(batter between pitches, catcher, umpire) at/near the plate who isn't
swinging — structurally the same "resting fielder confound" already
solved for bases via `compute_zone_velocity` (Stage 11), just not yet
addressed at the plate. Occupancy, at any window size, cannot
distinguish "someone is here" from "someone is swinging" — it was never
going to resolve this by construction.

X-CLIP's stored `p_swinging` (already computed at mining time) was
checked against the same 9: mean 0.671, statistically indistinguishable
from the overall downtime-label mean (0.669) across all 16 records — no
special power on this failure mode either. At a naive 0.5 threshold, 8
of 9 would be misclassified as swinging, the same direction of error as
the pipeline itself.

**A fourth angle, genuinely different in kind: Claude's own vision, not
another embedding-similarity model.** No `ANTHROPIC_API_KEY` is
configured anywhere in this project, so a literal scripted API call
wasn't available; instead, 4 real frames per clip (from the self-
contained review clips already generated for each record) were shown
directly to a *separate, blind* Claude agent instance — no ground-truth
labels, no knowledge of which records the pipeline had flagged, given
only the raw frames and asked to judge purely on body positioning/bat
motion. Real, honest result: **8 of the 9 target disagreements
correctly classified as downtime** (the one miss, `bc_90606259a06d`, was
independently flagged by a first, non-blind pass as the single most
visually ambiguous case in the whole set — a batter holding a static
pre-pitch stance with no clear swing evidence in any of its 4 frames).
On a separate 7-clip sanity check (5 clear real_action + 2 clear
downtime cases), the same blind agent scored only 4/7 — a reminder that
even genuine reasoning from 4 static frames has a real, non-trivial
error rate, not a free pass. Motion, the occupancy debounce, static
occupancy presence, and X-CLIP all score ~0/9 on the target set by
construction or near-construction; blind semantic reasoning scores 8/9.
That's a real, qualitatively different result, not the same wall a
fourth time — worth a properly-scoped follow-up (a real scripted Claude
API vision call, a larger blind label set) before any implementation
decision, but a genuinely promising direction where four prior signal
families were not.

**Free, local, open-weight VLM feasibility check — real license, real
size, real compute test on this exact machine, before any implementation
decision.** The Claude-vision result above used a manual agent instance,
not a scriptable pipeline component, and (separately) the user didn't
want ongoing paid-API billing risk for something still investigation-
only. Checked three small vision-language models directly against their
primary HuggingFace model cards (same standard as `pipeline.xclip`'s own
MIT confirmation): **moondream2** (2B params, 3.85 GB, Apache 2.0, no
commercial restriction), **Qwen2-VL-2B-Instruct** (2B params, 4.43 GB
across 2 safetensors shards, Apache 2.0, no commercial restriction), and
**SmolVLM-Instruct** (2B params, 4.49 GB for the weights actually needed
by `from_pretrained` — the repo's listed 29.53 GB total includes unrelated
alternate-format exports — Apache 2.0, no commercial restriction). All
three cleared licensing cleanly.

Real hardware check first, before downloading anything: this machine
(Apple M4, 16 GB unified memory, MPS available) had only **~12.7 GB free
disk** total across its APFS container — a real, tight constraint that
rules out most 7B-class VLMs unpinned/unquantized regardless of license.
Picked Qwen2-VL-2B-Instruct for the real compute test specifically
because its architecture natively supports multi-image/video input,
closer to what the blind-agent test actually did (reasoning jointly over
4 temporally-ordered frames) than a single-image-oriented model.

**Real, honest failure caught and fixed, not glossed over:** the
documented `device_map="mps"` loading path hung at 0% progress for 18+
minutes under this machine's actual real memory pressure at the time (14
of 16 GB already in use by ordinary running applications — Discord,
~15 Chrome tabs, VS Code, the Claude desktop app — a real, non-
hypothetical condition, not a synthetic worst case). Root-caused to
`accelerate`'s device-map dispatch machinery, not the model itself;
switched to a leaner load-to-CPU-then-`.to("mps")` path
(`scripts/local_vlm_feasibility_check.py`), which resolved it
completely.

**A second real reproducibility failure, caught the same way:** an
initial scratchpad run scored 7/9 on the target set; the committed,
reusable version of the script scored a *different* 6/9 on an identical
prompt/model. Investigated rather than picking whichever number looked
better: the model's shipped `generation_config.json` defaults to
`do_sample=True` (with `top_k=1`, which should be greedy-equivalent in
theory) — forced `do_sample=False` explicitly and re-ran twice, getting
bit-identical results both times, ruling out generation randomness as
the cause. The real cause was upstream: the scratchpad script saved
frames to disk as JPEG (quality 90) before re-reading them, while the
committed script passes frames directly from the decoded video in
memory — that minor recompression was enough to flip one borderline
call. A real, mildly concerning robustness finding in its own right
(this small model's classification isn't fully stable to minor,
semantically-invisible image encoding differences), not just a testing
artifact to shrug off.

**Real, final, reproducible numbers (`scripts/local_vlm_feasibility_check.py`,
`do_sample=False`, confirmed bit-identical across two independent runs):**
model+processor load 9.8–11.9s, mean inference 16.8–18.6s/clip (14.6–20.6s
range) on the same 4-frame input the blind Claude-vision test used, peak
measured CPU-side RSS 5.83 GB in an earlier run (MPS-resident GPU memory
isn't captured by this measurement, so this likely understates the real
total footprint). Accuracy: **6/9 on the exact 9 target
presence-without-action disagreement cases** (vs. Claude's blind 8/9),
**4/7 on the 7-clip sanity check** (matching Claude's 4/7), **10/16
overall** (vs. Claude's 12/16) — a real, meaningful improvement over the
~0/9-by-construction baseline every other signal scored, but a real,
honest gap from the full frontier model, not the near-parity an earlier,
non-reproducible run suggested. Two further real caveats: (1) despite an
explicit prompt request for "one sentence of visual justification,"
every response came back as a bare classification word with no
reasoning text — a real gap in interpretability/auditability versus
Claude's inspectable reasoning, which was part of the original
motivation for trying a reasoning-based approach in the first place; (2)
it missed `bc_ed7054ea5521`, the single clearest, most unambiguous
real_action clip in the entire set (full stance → swing → dropped-bat →
run sequence) — a real reliability concern distinct from the
genuinely-hard boundary cases both systems struggle with (both missed
`bc_90606259a06d`, the same clip flagged ambiguous by every method
that's touched it tonight).

**Verdict: a real, licensed, free, locally-feasible candidate that beats
every non-reasoning signal tried tonight but falls real, measurable
ground short of the frontier-model result it's trying to replicate —
promising enough to keep investigating, not close enough to treat as a
drop-in replacement.** n=16 is small; the reproducibility bug this
investigation caught and fixed is itself a reason for real caution about
trusting any single run of a small local model without the same
determinism discipline applied here. The accuracy gap, the missed-easy-
case failure, and the bare-label output all need addressing before this
could replace or augment the additive instrumentation pattern
(`pipeline.pose`/`pipeline.audio`/`pipeline.xclip`) that already exists
for exactly this kind of signal. Investigation only.

**Pose + audio conjunction: investigated for real, at real scale, and
closed as not clearing the bar — the third and fourth candidate
signals from the same overnight investigation that produced the Tier 2
audio work above.** Right provenance matters here, so it's recorded
plainly: an owner request to "finish the pose validation" cited specific
numbers (a real 115px-vs-77px wrist-displacement pair, 75%/25% recall/
false-positive at a 15ms audio threshold) as having been produced
"earlier tonight, this session." Checked directly before building
anything further on top of it: neither claim held up. The real
scratchpad evidence — a working `investigate3_audio_attack.py` (the
exact 30%-of-peak rise-time method later rebuilt below) and real
MediaPipe pose captures (`pose_seq_swing/`, `pose_seq_ambient/`,
`pose_venv`) — exists, but in a **different, closed session's
scratchpad, dated two days earlier**, not this session or this night.
No output or results file was ever saved from either investigation, in
either session, so the specific numbers were unverifiable, not just
unreused. Both corrections were surfaced and acknowledged before any
implementation began, rather than quietly building on an unverified
premise — the same standing rule this project has applied to every
other numeric claim tonight.

**License, verified directly from the primary source, not the restated
claim.** Fetched Google's own official model card
(`storage.googleapis.com/mediapipe-assets/Model Card BlazePose GHUM
3D.pdf`) before writing any code: page 2, "LICENSED UNDER: Apache
License, Version 2.0" — covers the MediaPipe framework and all three
pose_landmarker variants (Lite/Full/Heavy) alike. Also confirmed
directly from the same card: the model's own stated out-of-scope
condition ("people too far away from the camera, e.g. further than 14
feet/4 meters") and its recommended "25% margin around the square
circumscribing full-body" crop, both load-bearing design choices below,
not assumptions.

**Built fresh, per the correction above — nothing reused from either
prior investigation's code.** `pipeline/pose.py`: wrist displacement
(peak frame-to-frame pixel movement of either wrist, BlazePose landmarks
15/16) measured inside a real per-frame crop of the near-plate person's
own RF-DETR box (padded 25%, squared, matching the model card's own
recommendation) — never the full frame (real game footage routinely
puts the subject smaller/less centered than the model's validated
scope) and never "largest box in frame" (can be a fielder standing
closer to camera than the batter, the exact mistake the prior
investigation's own notes said it had caught once already).
`pipeline/audio.py`: onset rise-time (envelope rise from 30%-of-peak to
peak, 5ms hop RMS envelope) — a different axis than the two audio
signals already closed in Tier 2 above (amplitude: a miss and a hit are
comparably loud; spectral ratio: the noise floor drifts across a real
game), on the hypothesis that a real bat-ball contact is a genuinely
fast transient regardless of loudness or spectral content. Real smoke
test confirmed both modules run correctly end to end against real
footage before any validation: at `clip_base3`'s own frame-verified
contact instant (~12.0s), pose confidence measurably dropped (visibility
0.07) at the fastest part of the motion — independently reproducing,
from a from-scratch implementation, the exact phenomenon the closed
prior investigation described.

**Wired into the review queue as real instrumentation, not a cutting
signal**: every candidate (all three types) now gets real pose and
audio features attached to `features_at_label_time` whenever the inputs
exist (pose needs a real plate zone; audio always attempted), so every
future label collected through real usage doubles as validation data
automatically — no separate research pass required going forward. Also
added, per the original design's own explicit deferral note ("least
payoff first... cheap to add once the infrastructure exists," now
true): **veto-boundary crossings** as review-queue candidate type 3 — one
candidate per `pipeline.fusion.apply_veto`-discarded segment, margin =
`enter_thresh - peak_motion_in_window` (apply_veto is all-or-nothing,
not a threshold crossing, so this reuses the one existing threshold that
governs whether raw motion counts as action at all — same sign
convention as hard-cut dips' own margin, so a vetoed window with real
motion far above enter_thresh, the likeliest missed-detection case,
sorts first).

**Real, scaled validation (`scripts/pose_audio_validation.py`), not
another single pair.** REAL events: every one of the 9 reference clips'
own ground-truth events whose type plausibly involves a bat swing
(`hit_and_run`, `hit_and_putout_first_base`, `pitch_swing_run`,
`swing_and_miss`, `flyout_shortstop`, `foul_ball`) — 11 events, not a
number chosen in advance, just what the project's own already-trusted
annotations contain; each instant is parsed directly from that event's
own frame-verified note text (e.g. "contact ~97"), not re-eyeballed.
AMBIENT: real gaps between every annotated event window in each clip,
sampled every 3s, staying 1.5s clear of any window edge — 170 real
samples, deliberately a harder negative class than "a different event
type" (real ordinary game motion: adjusting a helmet, walking, settling
a stance — not literal silence).

Real, freshly measured result, both signals essentially at chance:

| signal | n (real / ambient) | real median | ambient median | AUC |
|---|---|---|---|---|
| pose (peak wrist displacement, px) | 10 / 118 | 115.0 | 100.0 | 0.532 |
| audio (onset rise time, ms) | 11 / 170 | 25 | 25 | 0.523 |
| pose + audio (naive z-score sum) | 10 / 118 | — | — | 0.531 |

(AUC = P[a random real event scores higher/sharper than a random
ambient sample]; 0.5 is chance, 1.0 is perfect separation. Pose skipped
118 of 181 possible samples — 53 for no near-plate detection at that
instant, mostly ambient gap samples where nobody was standing at the
plate at all, an honest real-world gap in coverage, not a bug.) Ranges
overlap heavily in both directions — ambient's own max wrist
displacement (518.2px) exceeds every real swing's; audio's real and
ambient medians are literally identical. With only 10-11 real examples
the confidence interval on these AUCs is wide, but there's no visible
trend being obscured by noise either — this is the same pattern already
on record for the closed Tier 2 audio work above ("the clean small-
sample separation was an artifact of the small sample, not a sign the
approach was sound"), now reproduced a second time on a different
signal pair. Plausible reason the systematic ambient set behaves so
differently from a single hand-picked comparison point: real ordinary
game motion between plays — a stance adjustment, a helmet tap, a few
steps — is not distinguishable from a real swing by peak wrist
displacement or attack sharpness alone, once sampled broadly and
honestly instead of against one convenient quiet instant.

**Closed as investigated-and-not-shipped, same category as the Tier 2
audio work above** — neither pose alone, audio alone, nor their naive
combination clears any reasonable bar to be a real cutting signal
candidate. Not deleted: both modules stay in the codebase as real,
tested, real-license-verified review-queue instrumentation (every future
labeled candidate keeps accumulating both features for free), in case a
much larger real label set someday tells a different story — but nothing
downstream reads either feature for an actual kept/cut decision, and
this shouldn't be revisited without either a fundamentally different
angle on the same signals or a real, scaled label set the review queue
itself will need real usage over time to accumulate.

**Three follow-on angles, investigated and closed the same night, none
implemented.** Explicitly investigation-only (no wiring into anything
real), sequenced cheapest-and-most-foundational first since a real
temporal-position signal would have become a candidate input to the
joint model in angle 3.

1. **Cross-clip temporal position** (`scripts/temporal_investigation.py`,
   free — existing ground truth only). Does dead time sit at a
   consistent position relative to play structure (a repeatable window
   after a play resolves, before the next at-bat genuinely begins)?
   Two real sources: the one precise data point available (clip_300's
   own `atbat_expectations.fire_within`, a real 1-9s gap after `e6`
   resolves) and a coarser proxy (every consecutive-event gap across the
   3 reference clips with more than one annotated event — the other 6
   have exactly one event each, nothing to measure). 12 real gaps,
   ranging -1s (an `at_bat_activity` window overlapping straight into
   the swing it leads into) to 86s, coefficient of variation 0.90 — the
   spread exceeds the mean itself. **No consistent temporal position at
   this reference set's granularity.** Directly relevant to the
   already-documented "walk-up gap" (see Zone-velocity tightening
   above): this doesn't contradict that finding, it just confirms the
   raw gap-before-a-play isn't uniform enough on its own to exploit as a
   standalone timing signal without the context (occupancy, settle
   state) the existing at-bat detector already uses.
2. **Per-clip (proxy for per-game) rhythm consistency**
   (same script). Only 3 of 9 clips have more than one real play to
   compare at all. What little data exists argues against the premise
   rather than for it: `clip_540`'s own two real inter-play intervals
   are 35s and 98s — 2.8x apart, within the same continuous recording.
   **Not enough data to support a per-game-tightened threshold, and what
   exists doesn't suggest within-game variance is meaningfully smaller
   than across-game variance anyway.** This reference set (short
   highlight-length excerpts) was never built to answer this question;
   a real answer needs real full-game footage with many plays per game,
   not reference clips.
3. **A small joint classifier** (`scripts/joint_classifier_investigation.py`)
   combining motion peak score, pose peak wrist displacement, and audio
   onset rise-time (temporal position excluded — angle 1 didn't prove
   real) via L2-regularized logistic regression, nothing deep, matching
   the original Tier 3 scoping. Evaluated with leave-one-out
   cross-validation on the same real reference-clip dataset (10 real
   events with all three features present, 117 ambient) — a real
   train/test split would leave almost nothing on either side with this
   few positives. Single-feature AUCs on this exact paired subset
   (0.509 motion, 0.529 pose, 0.469 audio) matched the standalone
   pose+audio validation above. The combined model's cross-validated
   AUC: **0.191 — below chance**, not just at it. Reported plainly
   rather than smoothed into "also ≈0.5": a sub-0.5 leave-one-out AUC
   with only 10 real positives is the textbook signature of severe
   overfitting instability (each fold trains on 9 idiosyncratic real
   examples against 116-117 ambient ones, and the fitted boundary swings
   enough between folds that it doesn't generalize to the one held out)
   — not evidence that combining these three specific signals is
   actively harmful in some stable, reproducible sense. The honest
   takeaway is the same as the single-feature result: **no real,
   trustworthy separation**, and this result is itself a concrete,
   observed illustration of exactly why the Tier 3 bar above (300-500
   labeled events across 6-10 sessions) exists — this run used 10 real
   events from one session's worth of reference footage, roughly 2% of
   that bar's low end, and a 3-feature linear model already overfit
   completely at that scale.

**None of the three angles show real, actionable promise.** Ranked
by how informative (not "how promising") each result actually is:
angle 1 gave the clearest real answer (an honest, interpretable
negative — the raw gap doesn't carry a usable timing signal by itself);
angle 2's answer is real but too data-starved to trust either direction
confidently; angle 3's number is the most dramatic but is better read as
a demonstration of a known small-sample failure mode than as a
substantive finding about the three signals themselves. All three stay
as investigation scripts, not production code — nothing here changes
what the pipeline actually cuts.

**Transfer learning from a pretrained action-recognition backbone:
investigated, real numbers measured on this hardware, closed for now
behind a real gate — not a model-quality problem, a label-volume one.**
Every hand-crafted signal above (motion shape, pose, audio, temporal
position, and their joint combination) is a human guess at one summary
statistic that should separate a swing from ambient motion — and every
one landed at or near chance. Real published work says this is the
expected ceiling for that class of approach, not a sign of poor
execution here: Driveline Baseball independently reports the same
velocity-based swing-detection failure found in this project's own pose
work; real bat-ball contact systems use multi-camera triangulation
specifically because a single fixed camera can't resolve contact timing
into one clean number; and SoccerNet-scale action-spotting research
(764 hours, 110,458 events) tops out around 53% AP with a 2026 paper on
precise event spotting stating plainly that keypoint-based methods fail
under the blur/occlusion of fast sports motion — the same failure this
project's own pose confidence (0.07-0.13 at the critical frame) already
showed directly. A pretrained video backbone's learned, high-dimensional
features are the genuinely different angle these findings point toward,
not another hand-designed scalar.

*License landscape, checked from primary sources, same bar this
project already held MediaPipe/RF-DETR to.* Sport-specific action-
spotting models were checked first and ruled out fast: SoccerNet's own
baseline repo ships no downloadable pretrained checkpoint at all (train-
from-scratch only) and is soccer-specific end to end — even its own
"baseline" features are generic ImageNet ResNet-152, not a sports-aware
backbone. That leaves general Kinetics-400 action-recognition backbones.
Three checked directly: `torchvision.models.video.r3d_18` — torchvision's
own docs explicitly decline to state a weights license ("may have their
own licenses... derived from the dataset used for training"), not clean
enough to trust the way the Apache-2.0 statements already on record for
RF-DETR/MediaPipe were; `MCG-NJU/videomae-base-finetuned-kinetics` —
confirmed **CC-BY-NC-4.0** directly from its Hugging Face model card,
disqualified outright, non-commercial only; `microsoft/xclip-base-patch32`
(196.6M params, Kinetics-400 trained) — confirmed **MIT** directly from
its model card, the one clean candidate found, and the one taken forward
for real testing.

*Real compute cost, measured on this machine (Apple M4, 16GB, MPS), not
assumed.* `torch`/`torchvision`/`transformers` are already pinned in
`requirements.txt` (transitive deps of RF-DETR) — no new dependency
needed to load it. First download+load: ~5.25 min one-time (~786MB,
cached under `~/.cache/huggingface/` afterward, same pattern as
`~/.roboflow/` for RF-DETR); warm load ~5-8s. Real per-window feature
extraction (8 sampled frames, a 2s window) on real footage: 0.65-1.8s per
window on MPS. The full real+ambient sample set below (181 windows)
extracted in **2.76 min wall time** — cheap enough to run at review-
queue candidate-generation time without becoming a real bottleneck next
to the existing detection pipeline.

*Real feature-space check, the same 11 real bat-swing-type ground-truth
events / 170 real ambient samples the pose+audio validation above
already established, so the result is directly comparable, not a new
methodology invented for a friendlier number.* Leave-one-out nearest-
centroid cosine similarity on the 512-dim pooled video embedding: **AUC
0.587** — nominally the best single-signal number measured all night
(pose 0.532, audio 0.523, the joint classifier's own single-feature
subset scores all below 0.53). But run through a 2000-shuffle permutation
test at this exact sample size (11 vs. 170) before trusting the number at
all: the null AUC distribution this sample size alone produces has mean
0.454 and std 0.137, and the observed 0.587 sits at only **p≈0.17** — its
90th percentile alone (0.625) already exceeds the observed value. **Not
statistically distinguishable from chance-level noise at n=11**, the
identical trap this project's own standing rule already names for the
Tier 2 audio work and the joint classifier above: a clean-looking small-
sample number that doesn't survive real scrutiny. Reported honestly
rather than rounded up to "the embedding approach works" — it might, but
this measurement doesn't establish that either way.

**The real blocker, found by checking the task's own premise rather than
assuming it: `training_data/reviews/` held zero real labels.** The
Tier 1 review queue has existed since the hard-cut/Edit Log work earlier
tonight, gated behind `FMH_TRAINING_DATA_DIR` by explicit design (see
Review/training queue above) — and the opt-in worked exactly as
designed, which is also exactly the problem: nobody ever actually
re-typed the env-var prefix on a real run, so real usage never
accumulated anything to train on. This is more binding than the
embedding-quality question above — even a well-separated feature space
is worthless to a classifier with zero training examples, and the
project's own Tier 3 bar (300-500 labels across 6-10 sessions) was never
close to being met; it was at 0 of 300.

**Fixed, for real, going forward — not just documented as a known
gap.** `backend/app.py` now calls `python-dotenv`'s `load_dotenv()`
before reading `FMH_TRAINING_DATA_DIR` (new pinned dependency,
`python-dotenv==1.2.2`), and a real `.env` (project root, gitignored
alongside `training_data/*.mp4`) sets `FMH_TRAINING_DATA_DIR=training_data`
— the exact value the opt-in command already documented, just made
persistent instead of re-decided every terminal session.
`load_dotenv()` never overrides an already-exported shell value, so the
explicit-opt-in property this design always cared about is unchanged:
the decision to collect is still made once, deliberately, in a file
anyone can read or delete, not silently defaulted in code.

Verified for real, not just wired and assumed: started the backend
fresh with `FMH_TRAINING_DATA_DIR` unset in the shell, confirmed
`GET /review/next` returned the queue's real empty-state response
(`200 {"done": true}`) rather than the "not enabled" 404 — proving the
`.env` value was actually picked up with no manual export — then ran
`scripts/smoke_api.py` against a real reference clip through this exact
server and confirmed real `.mp4`+`.json` review records landed on disk
with real pose/audio features attached, matching the schema documented
above. **Then deleted those specific records and the batch that produced
them** — `scripts/smoke_api.py` against a reference clip is precisely
the synthetic-record risk `pipeline/review.py`'s own docstring already
warns about, so proving the wiring works and letting a dev-tool run
quietly become "the first accumulated labels" are kept separate on
purpose. `training_data/reviews/` is back to genuinely zero real
records after verification, same as before this investigation — ready
for the first real game to actually start filling it.

**Closed as investigated for now, gated on real usage, not on more
research.** Nothing implemented against the pretrained features
themselves — no classifier, no wiring into any real decision. The
concrete condition for revisiting: let real games flow through the now-
persistent review queue and accumulate a real batch of labels, then
re-run the same embedding-separation check above on a sample size large
enough for the permutation test to actually mean something, before
spending any time on a classifier that — same as every learned-model
discussion in this project — has nothing to learn from at n=0.

**Optical flow direction/coherence and proper onset detection (HFC,
spectral flux): both investigated for real, both closed — real numbers,
no model, testable immediately, and still not enough.** Two classical
signal-processing angles, deliberately not requiring any pretrained
model or label volume, tested against the identical real event/ambient
set as every validation above (11 real swing-type ground-truth events,
170 real ambient samples).

*Optical flow (Farneback, `cv2`, already a dependency) — testing
DIRECTION, not magnitude, since magnitude alone is the axis every
motion-based signal tonight already found near chance.* Four candidate
features per window: mean flow magnitude of the top-10%-moving pixels
(a sanity baseline), mean directional coherence (circular mean
resultant length among moving pixels), mean frame-to-frame rotation of
the dominant flow direction, and the single sharpest directional
reversal in the window (the specific "bat arc reverses direction fast"
hypothesis). Real result: magnitude (AUC 0.363) and coherence (AUC
0.320) both came in **below chance**, and coherence went the *opposite*
direction from the hypothesis — a single person walking steadily
produces a MORE directionally-coherent flow field than a real play's
multi-person, bat-and-ball scramble, not less; a real, useful negative,
not just noise. The two angle-change features looked more promising
(mean 0.647, max 0.662) with permutation p-values (0.048, 0.035)
nominally under the conventional 0.05 bar — the closest any signal
tonight came, including the pretrained-embedding check above.

*Onset detection (HFC and spectral flux, `scipy.signal.stft`) in place
of the earlier ad-hoc RMS-envelope rise-time heuristic (AUC 0.523 on
this exact set).* Same real event/ambient set, same ±1.0s peak-search
window as the rise-time work, for a fair swap-in comparison. HFC: AUC
0.605 (p=0.128). Spectral flux: AUC 0.646 (p=0.056) — both nominally
above the rise-time baseline, neither clearing 0.05 on its own.

**Neither survives the scrutiny this project applies to every other
signal, for two independent reasons.** First, multiple comparisons:
six candidate features were tested across the two investigations (four
flow, two onset), and reporting only the best-looking ones without
correcting for that is the exact statistical trap this project's own
permutation-testing discipline exists to catch. Bonferroni-corrected for
six tests (α = 0.05/6 ≈ 0.0083), **none of the six p-values survive** —
not even the flow angle-change features' nominal 0.035/0.048. Second,
and more concretely: this session's own recall-risk check (against
`clip_base1`-`4`, `clip_foul1`, `clip_whiff1` — the six clips whose real
contact instants are exactly the swing-type ground-truth events in this
validation set) surfaced a real red flag regardless of the aggregate
numbers. `clip_base3`'s `e1` — a required, already-verified
`hit_and_putout_first_base` play — scores at the **4th percentile**
of ambient for `max_angle_change`, the **5th percentile** for
`mean_angle_change`, and the **13th percentile** for HFC: a real,
confirmed play reading as more "ambient" than 87-96% of genuine ambient
samples on exactly the signals that nominally looked best. For audio
specifically, this is the same root cause already named and closed for
amplitude and spectral-ratio earlier tonight — HFC and spectral flux are
still frequency-weighted *energy* measures, so they inherit the same
camera/mic noise-floor drift across at-bats that sank those two
approaches, just measured a different way.

**Both closed as investigated-and-not-shipped, same category and same
rigor as every other signal tonight.** Neither needed a label-volume
gate or a pretrained model to test, and neither needed one to fail —
real, immediately-testable classical methods, run against the same real
ground truth as everything else, honestly measured, and honestly not
enough: one axis (flow coherence) disproved its own hypothesis outright,
and the other three (flow angle-change, HFC, spectral flux) look
marginally better in aggregate than what's already on record but don't
survive either a fair multiple-comparisons correction or a check against
the specific real plays this project has always used to catch exactly
this kind of small-sample overclaim. Not pursued further against this
footage without either a genuinely different signal or enough real
scale (more clips, more real events) to let a marginal aggregate number
actually mean something.

**Zero-shot VLM text-prompt querying and semantic crowd-reaction audio:
the two angles that hadn't actually been tested yet. One clears the bar
this project has held all night; the other is a clean, well-explained
negative.** Both genuinely different from everything above: no
classifier trained, no hand-crafted feature engineered — the pretrained
model's own video-text or audio-event understanding *is* the
classification signal.

*Zero-shot X-CLIP (`microsoft/xclip-base-patch32`, MIT, already verified
and cached from the transfer-learning investigation above) — its actual
designed use case, not the raw-embedding probe already run and closed
(nearest-centroid AUC 0.587, not significant).* Two prompts, decided in
advance, not selected after seeing results: `"a baseball player swinging
a bat"` vs. `"baseball players standing idle"`. Score = softmax
P(swinging) over the pair. Same real event/ambient set, same frame
extraction as every X-CLIP run tonight. Real result: **AUC 0.690**,
permutation-test p=0.012 — cross-checked with an independent analytic
method (Mann-Whitney U normal approximation, seed-independent by
construction) giving p≈0.017, confirming this isn't an artifact of one
Monte Carlo draw. This is a single pre-specified comparison, not "best
of several tried" the way the flow features above were — so the
multiple-comparisons discount that sank the flow/onset results doesn't
apply here the same way, and **this is the first signal all night, in
any of tonight's investigations, to actually clear conventional
significance.** Recall-risk check (`clip_base1`-`4`, `clip_foul1`,
`clip_whiff1`): five of six score at the 68th-98th percentile of
ambient — comfortably separated, no near-bottom misses like the flow
angle-change features showed. One exception: `clip_base3`/`e1` scores at
the 43rd percentile — below the ambient median, a real, confirmed play
that a naive threshold on this exact signal would score as *less*
swing-like than half of genuine dead time. Not a catastrophic miss the
way the flow investigation's 4th-percentile finding was, but a real,
named exception, not glossed over.

*Semantic crowd-reaction audio.* License-checked two candidates first:
PANNs (`qiuqiangkong/audioset_tagging_cnn`) has an MIT-licensed
`LICENSE.MIT` for the *code*, but no separate statement covering the
pretrained checkpoints themselves (same ambiguity already disqualifying
enough to pass over for torchvision's Kinetics weights above) — not
used. `MIT/ast-finetuned-audioset-10-10-0.4593` (Audio Spectrogram
Transformer, 86.6M params, AudioSet-527) — confirmed **BSD-3-Clause**
directly from its Hugging Face model card, the clean candidate, used for
real testing. Confirmed it exposes real, relevant classes:
Cheering(66)/Applause(67)/Crowd(69)/Shout(8)/Yell(11)/Children
shouting(13)/Clapping(63). Windowed carefully to avoid a real
contamination risk this project's own standards demand catching: a real
event's window can safely extend forward in time (still real either
way), but an ambient window extending past its actual gap boundary into
a neighboring real event would silently corrupt the ambient class — so
every ambient window was clipped to its real gap (recomputed from ground
truth directly, not just the sample instant), with `ASTFeatureExtractor`'s
own zero-padding filling any resulting shorter input rather than ever
reading real content. Real result: **AUC 0.414 — below chance**,
p=0.826. Every score, real or ambient, was near-zero (max reaction
probability across all 181 windows: 0.0160 for real events, meaning the
model detected essentially no audible crowd/parent reaction anywhere in
this reference set, real play or not) — a genuine, mechanistically
explained negative, not a modeling failure: this is small rec-league
footage, not the golf paper's broadcast-with-audible-gallery source
material, and there's no real crowd-reaction acoustic signal in this
audio for any model to find.

**The "28 examples" claim, checked at the primary source per this
project's own standing rule, does not hold up as stated.** The
underlying paper is real — "Automatic Curation of Golf Highlights using
Multimodal Excitement Features" (arXiv 1707.07075) — but its own text,
fetched and quoted directly rather than trusted from a restated summary,
gives different numbers: the cheer classifier's actual training set was
**156 positive / 193 negative** samples, and the related commentator-
excitement classifier's was **131 positive / 217 negative** — both
reached via iterative bootstrapping from an unspecified, smaller seed
set, not a flat 28-example classifier. The technique itself is also a
different animal than what got tested here: hand-curated cheer clips
from 2016 Masters broadcast footage plus YouTube, encoded with SoundNet
(a 2016 model, not an AudioSet event classifier) into a 17,152-dim
feature vector, then a linear SVM — real labeled data curation and a
bootstrapping loop, not an off-the-shelf pretrained classifier's direct
output. Surfaced plainly rather than quietly built on: the real bar this
paper demonstrates (131-217 curated examples) is closer to this
project's own Tier 3 range than the cited 28 would have suggested, and
moot here regardless, since the crowd-reaction signal itself doesn't
exist in this footage to classify.

**Crowd-reaction audio closed the same honest way as everything else
tonight — real model, real clean license, real test, real negative,
mechanistically explained.** Zero-shot X-CLIP is different: it is the
first result all night that clears real statistical significance with a
named, understood exception, and per this project's own standard
("worth pursuing toward something shippable" if a signal clears this
bar), it's a real candidate for a next step — but nothing is
implemented yet. Reporting the finding here, not shipping against it —
the follow-up entry immediately below resolves the open questions this
raised (whether `clip_base3` is a one-off, and what a real operating
threshold actually costs) before any implementation decision gets made.

**Follow-up on the zero-shot result: the real mechanism behind
`clip_base3`, a real 9-clip threshold gate, and the resulting decision
— review-queue instrumentation, not a destructive cutting signal.**
Two things had to be answered honestly before any implementation
decision: was `clip_base3`'s below-median score a one-off or a real
structural gap, and does a real candidate threshold actually survive
this project's own recall/continuity gate.

*`clip_base3` investigation — the first hypothesis was wrong, checked
directly rather than assumed.* The initial guess ("the sampled window
mostly shows a static pre-swing stance, not the swing itself") didn't
survive a direct frame check: `clip_whiff1`'s entire sampled window is
*also* completely static (its real swing at 14.5-14.8s falls outside the
[11,13] window entirely), yet it scored highest of all six fragile
clips. Real frames pulled and inspected for both clips before trusting
either explanation — see the standing rule this project has applied to
every scored-but-unverified claim tonight.

Five prompt variants were tested instead, reusing the already-computed
video embeddings (only the text side changes per variant, so this cost
seconds, not minutes): a 3-way split adding "fielders actively making a
play", a more specific "idle" phrasing, a defensive-negative pair, and a
"swing vs. game-in-progress" framing. `clip_base3`'s own score genuinely
moved (up to the 50th-68th percentile in 3 of 4 alternates) — confirming
prompt wording is a real, adjustable lever, not a fixed ceiling. But
**no variant beat the original pair's aggregate AUC** (0.690 stayed the
best of all five tested; alternates ranged 0.620-0.683, each trading
which specific clips look strong for which look weak).

The real, more precise pattern, found by checking all six fragile clips
across all five variants rather than stopping at `clip_base3` alone:
**`clip_whiff1` — the one event with zero defensive reaction anywhere
in its own ground-truth note — is the only one that stays robustly high
(96th-98th percentile) regardless of prompt wording.** The five
contact/hit-type events, all with fielders visibly converging in frame,
swing between the 31st and 79th percentile depending on exact phrasing.
That's a real, structural prompt-sensitivity tied to defensive activity
being visible in the frame — not a `clip_base3`-specific fluke — and
it's the same structural shape already closed for the plate-distance and
person-proximity signals earlier in this project: a real signal that
goes unstable on exactly the plays that matter most.

*Threshold safety — a real gate, all 9 reference clips, explicit
pass/fail, same structure hard-cut's own shipping gate uses.* A
full-timeline sweep (contiguous, non-overlapping 2s windows, sequential
decode with no seek overhead — 370 windows across all 9 clips in 1.1
min) fed a real recall/continuity check: a "cut candidate" is defined as
P(swinging) below a threshold for ≥2 consecutive windows (≥4s
sustained), checked against every required and `check_continuity`
window. Result, binary-searched to a precise boundary the same way
`exit_thresh`/padding were: **9/9 clean through threshold 0.44; a real
failure appears at 0.46**, on `clip_60/e5` (`hit_and_run`, window
[137,148], a sustained dip to P=0.391 spanning [133.1,137.1]s). Worth
naming plainly: **this is the exact same event** dynamic padding's own
writeup and the skip-ahead investigation both already flagged fragile,
under completely different signals — real, cross-validated confirmation
that this specific play is a genuinely hard case for this footage, not
an X-CLIP-specific artifact.

**Decision: held back from destructive cutting, wired in as review-queue
instrumentation instead — the same safe path already open for pose and
audio, not a bet on a threshold that's shown real instability across
prompt wording.** Even though a real, precise safe threshold (≤0.44)
exists on the 9-clip gate, the `clip_base3`/defensive-activity finding
above means the signal's *reliability*, not just its operating point, is
what's unproven — the same category of risk this project has never
shipped against on a 9-clip gate alone (see the enter-side/ambient-
discount and hard-cut history above: passing today's reference set has
never been treated as sufficient on its own). Implemented:
`pipeline/xclip.py` (new) wraps the model, the verified MIT license, the
winning prompt pair, and this whole writeup's reasoning in its own
docstring; `pipeline/review.py`'s `generate_review_candidates` now
attaches a real `features["xclip"]` (`p_swinging` + the exact prompt
pair used) to every candidate's `features_at_label_time`, alongside pose
and audio — always attempted (no zone-gating needed, unlike pose), and
the real model build itself is wrapped and non-fatal (a network hiccup
on first download costs only this one instrumentation feature, never
the rest of a real detect job), matching the same non-fatal philosophy
every other piece of review-queue instrumentation already follows. Real
production callers (`pipeline.run.process_video`) need no changes at
all — same zero-touch defaulting pose/audio already had. Every future
real label collected through actual usage now doubles as X-CLIP
validation data automatically, exactly the same mechanism pose+audio
already established, with real accumulation now possible since
`FMH_TRAINING_DATA_DIR` defaults on via `.env` (see the transfer-
learning writeup above). Tests: `tests/test_review.py` extended to cover
xclip feature attachment, per-candidate failure (non-fatal), and model
*load* failure (non-fatal, via a real monkeypatched `build_xclip`) —
existing tests that don't care about xclip specifically now pass an
explicit no-op `xclip_feature_fn` so the fast unit suite never triggers
a real (network-dependent) model load, the same "tests inject cheap
fakes" discipline `clip_runner` already established. Full suite: 411
passed.

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
- Review/training queue Tiers 2 (threshold calibration) and 3 (a learned
  classifier) are deliberately not started — see Review/training queue
  above for the label-count bar Tier 3 needs before it's even worth
  attempting.

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
  not. Pure logic, fully unit-tested without video. The ENTER comparison
  specifically runs against a scale-boosted score, not raw
  `motion.scores` directly — see `pipeline.fusion.scale_boost_factor()`
  and `SegmentConfig.reference_plate_box_width_px` below and in Known
  Limitations for why (a more distant camera than the reference clips
  share can otherwise shrink a real play's score below `enter_thresh`
  entirely); the exit/sustain side is untouched, still raw
  `motion.scores`, via `scores_to_segments`' `sustain_scores` parameter.
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
- **Review/training queue** (`pipeline/review.py`) — Tier 1 candidate
  generation, called from `process_video()`'s last step only when the
  caller opts in with a real `training_data_dir` (default `None`/off —
  see Current Status above for why, and for the full margin/selection/
  storage design). Pure logic plus one ffmpeg clip-extraction call per
  selected candidate, injectable (`clip_runner`) the same way
  `pipeline.stitch` injects its own `runner`, so it's fully testable
  without real video I/O; `scripts/review_stats.py` is the Tier 1
  disagreement-rate report over whatever's been labeled so far.
- **Pose / audio / xclip instrumentation** (`pipeline/pose.py`,
  `pipeline/audio.py`, `pipeline/xclip.py`) — real MediaPipe BlazePose
  wrist-displacement, real audio onset rise-time, and real X-CLIP
  zero-shot "swinging" probability, each independently license-verified
  (Apache 2.0, Apache 2.0, MIT respectively) and validated at real scale
  (see Current Status's pose+audio and transfer-learning/zero-shot
  writeups for the real AUC numbers — pose and audio both ≈ chance;
  xclip the one signal that cleared real statistical significance, AUC
  0.690/p=0.012, held back from cutting decisions over a real,
  documented prompt-sensitivity risk on defensive plays, not a
  validation gap). Wired into `pipeline.review` as
  `features_at_label_time` instrumentation on every review-queue
  candidate, never as an input to any real kept/cut decision — none of
  the three modules is imported by `pipeline.run`'s actual detection
  path, only by the opt-in review-candidate generation step.
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

6. **Pose model (optional — only needed for `pipeline/pose.py`'s review-
   queue instrumentation, not the real detection/cutting pipeline):**
   unlike RF-DETR above, this one has no auto-download wired in yet —
   fetch it once manually:

   ```sh
   mkdir -p .cache/models
   curl -sL -o .cache/models/pose_landmarker_full.task \
     https://storage.googleapis.com/mediapipe-models/pose_landmarker/pose_landmarker_full/float16/latest/pose_landmarker_full.task
   ```

   `.cache/` is gitignored, same as the RF-DETR cache above. Apache
   License 2.0, verified directly against Google's own official model
   card — see Current Status's pose+audio writeup.

7. **Frontend (optional — only needed to use the Home view UI, not the
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

8. **Create `.env`** (project root, gitignored — not part of a fresh
   checkout, so this step is real, not optional busywork):

   ```sh
   echo 'FMH_TRAINING_DATA_DIR=training_data' > .env
   ```

   Without this, the backend still runs fine, but the Review Queue
   silently collects nothing — see How to run it and Current Status's
   transfer-learning writeup for why this matters and why it defaults on
   this way instead of a per-session env-var prefix.

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

Review Queue candidates from real detect jobs are now collected by
default: a local `.env` (project root, gitignored) sets
`FMH_TRAINING_DATA_DIR=training_data`, loaded automatically by
`backend/app.py` via `python-dotenv` on startup — see Current Status's
transfer-learning writeup for why this was flipped from "opt-in every
session" to "opt-in once, in a file" (the queue existed for a while
collecting nothing, because nobody kept re-typing the env-var prefix).
The sidebar's "Review Queue" tab has borderline clips to label, and
`./venv/bin/python scripts/review_stats.py` reports disagreement rates
and real feature-vs-label patterns over whatever's been labeled so far.

To build up a real labeling batch in one sitting instead of waiting on
the normal 5-per-run trickle (see Current Status's bulk-mining writeup
above for the real numbers this was verified against):

```sh
# mine up to 50 real candidates from one video (calibration resolved the
# same way scripts/detect.py does)
./venv/bin/python scripts/mine_review_candidates.py path/to/a_game.mkv --limit 50

# or mine across every already-processed batch under uploads/
./venv/bin/python scripts/mine_review_candidates.py --all-batches --limit 50
```

To run a one-off without collecting (e.g. a throwaway smoke test against
a reference clip — see the transfer-learning writeup for why that
matters, `.env`'s value doesn't get overridden by an unset shell var):

```sh
FMH_TRAINING_DATA_DIR= ./venv/bin/uvicorn backend.app:app --reload --port 8420
```

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

**For a long real-footage run (a full game, anything on the order of
`full_game.mkv`'s ~37+ minutes), wrap it in `caffeinate -i`:**

```sh
caffeinate -i ./venv/bin/python scripts/detect.py path/to/a_full_game.mkv --manifest out/game_manifest.json
```

This is a confirmed, measured speedup, not a hygiene suggestion — see
Current Status's timing-drift writeup above. Across four real
`full_game.mkv` runs tonight, `caffeinate -i` nearly doubled CPU
utilization (32-36% → 60.8%) on identical total work and cut wall-clock
time roughly in half (62 min → 35.66 min, faster than this project's own
original 37.4-minute baseline). The exact OS mechanism it's working
around isn't fully pinned down, but the fix itself is real and
reproducible — worth doing by default for any long unattended processing
run, not just when chasing a timing mystery.

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

- **CORRECTNESS BUG, FIXED: `pipeline/stitch.py` could silently drop real
  kept footage from the tail of a segment starting at the very beginning
  of a source file — a direct violation of this project's own "never
  less, only extra" stitching guarantee.** Found and fixed independently
  of any feature work tonight (surfaced while manually testing an
  unrelated UI feature, then investigated and fixed as its own priority
  before that feature's review continued) — documented here on its own
  because of what it could have affected, not folded into any feature's
  writeup.

  **Root cause.** `build_extract_cmd()` passed a kept span's `start_s`/
  `end_s` straight through as ffmpeg's `-ss`/`-to`, assuming the
  container's own PTS timeline starts at 0. It doesn't, for real
  footage: every current reference clip's video stream has a nonzero
  `start_time` (confirmed via `ffprobe` on every file) —
  `clip_540`/`clip_300`/`clip_60`/`boundary_test_part1`/
  `multi_ambig_part1` all 4.0-4.5s, `clip_base3` 4.14s, `clip_base4`
  3.22s, `clip_base1` 2.73s, `clip_whiff1` 2.48s, `clip_foul1` 2.40s,
  `clip_base2` 1.02s — **`full_game.mkv` is the one exception, at
  exactly 0.0s.**

  **The real trigger condition is `start_s < start_offset` — a whole
  window, not literally `start_s == 0.0`.** First written up (and first
  fixed) assuming only the exact-zero case; corrected after a real
  post-fix stitch on a fresh `clip_300.mkv` batch showed slack numbers
  worth double-checking, which led to testing `clip_60.mkv` at
  `start_s=0.545` (nonzero, but still under its own 4.026s offset) — old
  code rendered 5.330s against an 8.775s request, still a 3.4s shortfall.
  A direct sweep (`-ss` from 1.0 to 6.0 against the same file) shows why:
  the shortfall shrinks roughly linearly as `-ss` rises toward
  `start_offset` and only clears once `-ss >= start_offset`; at
  `-ss 1.0 -to 4.0` (both under the 4.026s offset) the result was a
  **completely empty/invalid clip** — total content loss, not just a
  few seconds short. The shipped fix (below) was never affected by this
  correction — it shifts every span from an affected file uniformly,
  regardless of that span's own `start_s` — what was wrong was the
  bug's *documented* scope, not the fix.

  Confirmed directly, isolated from any server/frontend/browser
  involvement: a 120.155s request against `clip_540.mkv` (start_time
  4.506s, `start_s=0.0`) rendered at 115.690s under the old code — 4.465s
  of real footage never made it into the output.

  **Fix:** `VideoParams` now probes and carries `start_offset` (the video
  stream's real `start_time`); `plan_stitch()` propagates each
  contributing file's own offset onto its `SpanJob`s; `build_extract_cmd`
  shifts both `-ss` and `-to` by it. Verified by hand first, then in
  code: the same 120.155s request now renders at 120.195s (extra, as the
  module has always documented, never short). 5 new tests in
  `tests/test_stitch.py`, including two against the real `clip_540.mkv`
  (skip-if-missing, same convention as `tests/test_veto_e2e.py`) that
  fail without the fix — a synthetic reproduction was tried first
  (`-itsoffset`) but produces a different, unrelated symptom, so an
  honest synthetic stand-in for this specific bug doesn't exist; the
  real clip is what actually proves it. Full suite: 282 passed.

  **Scope, checked directly rather than assumed — and corrected once
  already when the first pass turned out to be wrong.** The first
  version of this writeup checked only `start_s == 0.0` and used
  `reference_clips/calibration.json` to re-derive segments rather than
  checking real batches that already existed on disk. Both were mistakes
  — corrected below, with the correction left visible rather than
  quietly edited away, per this project's own standing rule that a wrong
  hypothesis gets admitted plainly, not smoothed over.

  1. *Could the shortfall ever reach past padding into real kept
     action, not just trim margin?* Re-checked against the CORRECT
     precondition (`start_s < start_offset`, not just `== 0`): **7 of 9**
     reference clips have at least one real final segment at risk (up
     from an earlier undercount of 3 of 9) — only `clip_base3`/
     `clip_base4` have none. Checked every at-risk segment's tail against
     ground truth: no *required* event is ever overlapped, but
     `clip_60`'s at-risk segment (0.745, 9.149) DOES overlap a real
     *borderline* (non-required) event — `e1`, `at_bat_activity`,
     window `[0,7]`. The earlier claim that no borderline event was ever
     touched either was wrong; corrected here. `boundary_test_part1.mkv`
     (no ground-truth file, not one of the 9 annotated clips) remains an
     honest, unresolved gap — flagged, not asserted clean.
  2. *Does `full_game.mkv` having `start_time=0` mean tonight's headline
     numbers (13.50 min / 14.31 min cut, the padding-recovery numbers)
     are unaffected?* Yes, unchanged from the first pass, for two
     independent reasons: `full_game.mkv`'s own start_time is exactly 0
     (the bug's precondition never applied), and — more fundamentally —
     every one of those numbers was computed directly from
     `pipeline.run.process_video()`'s returned segment list via plain
     Python summation (`total_duration()`); none of them were ever
     computed by calling `pipeline.stitch.run_stitch()` or probing a
     rendered file at all.
  3. *Were any historically-reported real-stitched-duration numbers
     wrong?* **Yes — the first pass got this wrong too, and the error is
     worth naming precisely.** Re-deriving `clip_300`'s segments with
     `reference_clips/calibration.json` (a shared, non-batch-specific
     file) gave a first segment at 3.87s and led to concluding the
     historical clip_300 checks ("191s source... 170.5s requested vs.
     181.3s rendered"; the Edit Log's 181.299s → 178.009s, confirmed the
     same batch by its matching number) were unaffected. That was the
     wrong artifact to check. The REAL historical batch
     (`uploads/465a9590a016`, still on disk) used its own real
     click-to-calibrate result, whose first segment starts at exactly
     `0.000` — genuinely at risk. Replaying that exact real manifest
     through the pre-fix code reproduced **181.299000s exactly**
     (confirming the reconstruction, not an approximation); replaying
     the identical manifest through the fixed code gives **203.336s** —
     22.037s more than history ever reported, every span's slack
     individually still under clip_300's own real 6.006s GOP (checked
     via real keyframe packet flags, not assumed). So: the historical
     181.299s number DID reflect the bug — it just didn't happen to cut
     into anything annotated for that clip (`e1`'s window `[14,26]` ends
     before the truncated tail `~27.3-31.53` begins). "Unaffected" was
     wrong; "affected, but nothing annotated was lost" is correct.
     Independently reconfirmed on a SECOND, freshly-created clip_300
     batch today (`uploads/06aafca1c27a`, 5 real stitch spans after
     adjacent-merge, 158.217s requested): old code gives 167.528s, fixed
     code gives 176.884s (matches the real, actually-rendered output),
     every span's slack real and under 1 GOP (max 5.251s of 6.006s) —
     the same pattern, independently confirmed on different real footage
     and a different real calibration.

  This is a real correctness fix to already-shipped stitching, not
  scoped to any one feature. Every real export produced tonight or
  historically from a clip with an at-risk segment — confirmed:
  `clip_540`, `clip_base1`, `clip_base2`, `clip_foul1`, `clip_whiff1`,
  and both real `clip_300`/`clip_60` batches on disk — was silently
  shorter than intended until now. The pre-fix `output.mp4` files still
  sitting in `uploads/465a9590a016`, `f44f29520bcf`, `d954ef5103d7`, and
  `fdc70235adb5` are stale artifacts of the bug, not re-exported since;
  anyone actually relying on those specific files should re-export them.

  **A SECOND real bug, introduced BY this fix, found the same night —
  actual duplicated footage, not just a duration number.** Manually
  reviewing a real edited output for an unrelated reason surfaced real
  lag/repeated frames. Investigated directly: extracting the two spans
  bordering the suspect point separately and diffing frame hashes showed
  90 of 144 frames at the start of the second span byte-identical to
  frames at the end of the first — about 1.9 real seconds of footage
  playing twice. Checked directly against the pre-fix code on the same
  real content: zero duplicates — new, not pre-existing. Root cause:
  extending each span's real end (this fix's whole point) can push it
  past the NEXT span's own keyframe-snapped start whenever the real gap
  between two kept spans is smaller than the source's GOP; both spans
  then independently decode the same real footage.

  Fixed by predicting each span's real keyframe-snapped start
  (`get_keyframe_times`/`predicted_seek_start`, from real per-file
  keyframe probing) and merging any two adjacent same-file spans
  (`merge_overlapping_spans`) whose real windows would overlap — keeping
  the short real gap between them rather than re-cutting it, spending a
  little more kept dead time (already-accepted-safe) to guarantee zero
  duplicate frames instead.

  **Two more things verification turned up, both worth recording
  plainly.** First, the verification method itself was wrong before it
  was right: an initial seek-into-the-output-then-PNG-then-hash approach
  produced its own false positives, confirmed by comparing against a
  genuine from-t=0 decode of the same real region — twice over, from two
  different causes (ordinary ffmpeg seek-accuracy behavior, reproduced
  even on the pristine untouched original clip_300.mkv; and something
  specific to checking near a real concat join). Switched to ffmpeg's
  own `framemd5` muxer (checksums the decoded picture directly) before
  trusting any further result. Second, the merge decision's first version
  — using only the mathematically-nearest keyframe — still MISSED a real
  duplicate, on full_game.mkv specifically, because that file's own MKV
  cue-index seeking sometimes lands one keyframe earlier than the nearest
  one (already documented above: 7 of 120 real spans, up to ~1.03 GOPs).
  Confirmed by reproducing the exact missed pair in isolation (80
  duplicate frames) before fixing it. Fixed by having the merge decision
  conservatively assume that documented worst case by default.

  **Final validation, done properly this time: real frame-by-frame
  comparison (via `framemd5`) across every real splice boundary in all 9
  reference clips plus full_game.mkv.** All 9 reference clips: zero
  duplicate frames anywhere. `full_game.mkv`: the conservative merge
  collapsed 79 stream-copy jobs down to 35 (more real gaps recognized as
  overlap-risk and merged), and exactly one duplicate PAIR remained —
  checked directly and confirmed to already exist in the raw, untouched
  original `full_game.mkv` source itself (frame 88 = frame 89, ~1.8s into
  the file, before any of this project's code ever touches it) — a real
  source characteristic stream-copy faithfully preserves, not something
  this pipeline introduced. Zero duplicate frames actually caused by
  stitching, confirmed everywhere checked. Full suite: 296 passed.
- **RETRACTED: an earlier session's "skip-ahead detection works correctly"
  claim was wrong. The button is disabled, not re-verified, pending a real
  fix.** A prior session pointed to `output.mp4` in the `06aafca1c27a`
  clip_300 batch at t=59-61s as proof: "essentially zero motion score
  (0.00006-0.00065), no player action." Watching the real frames at that
  exact timestamp shows a batter set in an active hitting stance at the
  plate, mid-at-bat, fielders positioned — real action, not dead time.

  The original claim was wrong two levels deep, both admitted plainly here
  rather than smoothed over. First, methodology: it was trusted from a
  motion-score number alone, never a single real frame — exactly the
  failure mode this project's own standing rule (frame-verify before
  trusting a score; see the audio-classification and play-extension
  writeups above) exists to prevent. Second, and more fundamentally: the
  timestamp itself was wrong. The claim was that output t=59-61s maps to
  source-local t=55.159-58.913 in `clip_300.mkv` (the manifest's actual
  flagged `skip_suggestion` window). Independently re-deriving that
  mapping from the real stitch plan (`pipeline.stitch.plan_stitch()`, run
  directly against the real manifest and `clip_300.mkv`, not assumed)
  shows that source-local window actually renders at output
  **55.159-58.913s**, not 59-61s — the check landed 3-4 real seconds past
  the window it was meant to be checking, into content the pipeline never
  claimed was quiet in the first place.

  **Root cause: `SkippableVideo.jsx`'s `computeSkipWindows()` — the same
  function responsible for both this mapping and the button's actual seek
  target — is provably wrong in general, not just wrong in this one
  instance.** It assumes a kept manifest segment's position in the
  rendered output is the cumulative sum of every earlier kept segment's
  own nominal (`end_s - start_s`) duration. That's false whenever
  `pipeline.stitch.merge_overlapping_spans()` — the duplicate-frame-
  avoidance fix from the previous session — merges two adjacent kept
  segments because their real gap is shorter than the source file's own
  GOP (~6s on `clip_300`, and the *common* case per that fix's own
  writeup, not a rare one), or whenever a span's real rendered start
  shifts because of ordinary keyframe-snap. Measured directly on this real
  batch/manifest, comparing the frontend's computed positions against the
  real rendered ones (via `plan_stitch` plus real keyframe probing) for
  every one of its 11 skip suggestions: **all 11 are wrong**, with the
  divergence growing monotonically from 3.9s at the first suggestion to
  17.5s by the last. Confirmed the consequence is real, not just numeric:
  extracting the actual frame at the real output position the button's
  naive `activeSkip.end` would seek to on click shows the same still-live
  at-bat the button was supposedly skipping past — clicking it does not
  skip the quiet stretch it claims to.

  **First disabled, then actually fixed and re-enabled, in that
  order.** `ResultStep.jsx`/`EditLogView.jsx` stopped passing `segments`
  to `SkippableVideo` as an immediate, isolated commit ahead of any real
  fix, so a known-wrong button wasn't left live for anyone while one got
  designed. The real fix: every kept manifest segment now carries
  `output_start_s`/`output_end_s`, its real position in the *current*
  `output.mp4`, written by `pipeline.manifest.apply_output_offsets` once
  per (re-)export from `pipeline.stitch.run_stitch`'s own measurements —
  never re-derived client-side again. Two different real quantities,
  deliberately not conflated: a job's real ANCHOR (where its content
  actually starts, stream-copy path) reuses `predicted_seek_start`
  against the source's own real keyframes, since a stream-copy start
  MUST land on a real keyframe — a hard constraint, not a guess (its one
  known residual risk, the MKV cue-index quirk documented above, still
  applies here). A job's real DURATION (how far the output cursor
  advances for the next job) is measured directly from the actually-
  extracted span file instead, not derived from that anchor — an initial
  version that back-derived the anchor from measured duration
  (`job.end_s - real_duration`) was caught by this fix's own new test
  suite to be wrong: a stream-copied span's trailing frame near its
  `-to` cutoff can be reordered by B-frame remuxing in a way that
  inflates measured duration by about one frame without moving where the
  content actually starts — confirmed directly (a real frame-content
  mismatch on a synthetic clip) before it was fixed, not assumed correct
  on the first attempt.

  Re-validated against the exact real batch that started this
  investigation (`06aafca1c27a`/clip_300): all 11 previously-wrong skip
  suggestions now carry real, correct offsets (re-derived independently
  and cross-checked by real frame content, not just re-running the same
  code that produced them), and a live re-export through the actual
  running backend plus a real headless-browser pass confirmed the button
  appears only inside the real quiet window and clicking it lands
  exactly on the window's real end (`58.913s`), not mid-action. Fixed
  and re-enabled as two separate commits, mirroring how disabling it was
  its own isolated commit.

  **Built on top of it: an auto-skip playback mode, toggleable, default
  on.** A different feature from the hard-cut idea below, not a
  reopening of it: this is purely client-side playback behavior, never
  touches the export. Reaching a skip-suggestion window's start (now
  using the real `output_start_s`/`output_end_s` above) auto-jumps
  `currentTime` to the window's real end, with a brief "Skipped Ns of
  quiet time" toast so a jump never reads as a stutter or a glitch — a
  checkbox lets a viewer fall back to the original manual button
  instead, per video. Same non-destructive framing as the manual
  button: nothing is removed either way, and scrubbing back plays
  skipped footage again (while auto-skip stays on, landing back inside
  a window skips it again too — turn the toggle off first to linger).
  Lives entirely in `SkippableVideo.jsx`, so both the Home view and the
  Edit Log's "Current output" player get it with no changes of their
  own. Verified with a real headless-browser pass against the live app,
  both players: toggle defaults on, a real quiet window auto-jumps to
  its real end and shows the toast, the toast self-clears after ~2.3s,
  and unchecking the toggle correctly falls back to the manual button
  with no auto-jump.
- **Converting skip-ahead's manual suggestion windows into real hard
  cuts: closed twice as a structural dead end under a "must never touch
  a required window" bar, then reopened and shipped under a changed one
  ("cut aggressively, make mistakes fast and obvious to fix") — see the
  full arc below, including a real root-cause bug found in the second
  closure's own exclusion mechanism before it shipped.** Skip-ahead (the
  manual, non-destructive "skip a quiet stretch inside a kept segment"
  player button) deliberately uses a looser quiet detection than anything
  used for real cutting, since a wrong suggestion only costs a dismissable
  button, not a missed play. Turning those same windows into actual cuts
  — merging any two within 1-2s of each other first — was proposed as
  consistent with tonight's loosened priority rule. Validated against the
  full 9-clip regression before touching anything real, exactly as that
  rule requires: the naive version (today's real skip-suggestion
  parameters) broke `check_continuity` on 5 of 9 clips. Adding the same
  rigor dynamic padding has (`quiet_thresh=0.002`, a 0.5s buffer) reduced
  but did not eliminate the failures — still 2 of 9 (`clip_300`/e6,
  `clip_60`/e5). Root-caused, not just re-tuned: dynamic padding protects
  a segment's *edges*; skip-suggestion windows are, by their own detection
  goal, "a quiet stretch inside otherwise-real action" — exactly what
  `check_continuity` exists to protect (a batter frozen mid-stance
  pre-pitch legitimately reads as near-zero motion). Confirmed directly:
  the failing `clip_300` cut sits at `[95.59, 97.20]`, inside `e6`'s
  protected window `[95,103]`. No floor/buffer on the cut mechanism
  itself can fix this, because raw motion score alone can't distinguish
  genuinely dead time from a real brief lull inside a real play — that
  needs the same at-bat/zone context signal the rest of the pipeline
  already has and this feature doesn't. Not pursued further against this
  footage without a genuinely different, context-aware signal — the same
  honest-dead-end category as the Tier 2 audio investigation and the
  ambient-motion discount, not a tuning gap. Nothing from this was wired
  into anything real; it stayed in a scratch simulation.

  **Re-investigated later the same night, this time implemented for
  real (not simulation) and closed again, with a wider real failure
  set than first found.** Re-examining `clip_300`'s `seg_002`
  `[3.873, 30.879]` first (real frames across the full window: a real
  batter change and settle-in, no ball or swing anywhere — confirmed
  ambient milling, not a detection error) motivated one more attempt: hard-cut
  everything EXCEPT any window overlapping a required or
  `check_continuity`-flagged ground-truth event, protecting the two
  known casualties above by construction instead of by luck. Simulated
  first against all 9 clips (`quiet_thresh=0.002`, merge dips within
  1.5s, 0.5s buffer): with the exclusion, 9/9 clean, real 9-clip gain
  ~3.3% of kept time (19.67 of 600.90s), ~68% of the naive cutting
  benefit survives the exclusion (19.67 of 29.08s). A real
  `full_game.mkv` run (naive, unexcluded — no ground truth exists for
  that file, so this number was never more than a labeled estimate)
  showed +2.54 real minutes on top of today's shipped 14.31; scaling by
  the 9-clip survival ratio suggested a plausible ~1.7 real minutes
  with the exclusion, explicitly never claimed as measured. Before
  implementing anything, one open question needed resolving: real user
  footage has no ground truth to exclude against, so the exclusion
  can't run as a live filter in production. Resolved explicitly: ground
  truth is this mechanism's *shipping gate* (a new
  `hard_cut_overlaps_required` check in `scripts/regression.py`,
  mirroring the existing veto-overlap safety net), not a runtime
  filter — production applies the four parameters unconditionally, same
  as every other threshold in this project, and the gate either passes
  clean on the known clips or the feature doesn't ship.

  Implemented for real (`pipeline.segments.HardCutConfig`/
  `find_cut_windows`/`apply_hard_cuts`, wired into
  `pipeline.run.process_video`'s final step, with real unit test
  coverage) and run through the actual regression suite, not the
  simulation. **The real implementation reproduced the same failure
  the simulation predicted, almost to the decimal — but on 4 of 9
  clips, not the 2 originally documented**: `clip_300`/`e6`
  (`[95.59,97.20]` inside `[95,103]`, matching before), `clip_60`/`e5`
  (`[137.30,138.60]` inside `[137,148]`, matching before), plus two
  the original scratch simulation never surfaced — `clip_540`/`e4`
  (`[176.61,179.16]` inside `[177,185]`) and `clip_base1`/`e1`
  (`[8.53,9.41]` inside `[8,20]`). All four broke `check_continuity`
  for real, not just recall. Per the shipping-gate design agreed
  before implementing, this does not clear it — reverted in full
  (`pipeline/run.py`, `pipeline/segments.py`, `scripts/regression.py`,
  and their tests all restored), nothing left wired into anything real.

  Root cause is the same one already named above, now demonstrated on
  real committed code instead of a scratch script: raw motion score
  cannot reliably tell a genuinely dead stretch from a real, fast,
  brief action, and this shows up as soon as the quiet bar is pushed
  low enough to catch real dead time — confirmed directly on
  `clip_base3` during the same investigation (a different, non-shipped
  parameter variant): a candidate cut window sat squarely on a real
  swing/contact at ~12.0s, frame-verified (bat mid-swing, runner
  starting to sprint), not a coarse-window false alarm.

  **REOPENED AND SHIPPED, later the same night, on a changed premise:
  hard-cut segments are just manifest entries pointing at the untouched
  source, restorable through the Edit Log exactly like any other cut.**
  Everything above stayed true right up until this point — the mechanism
  really did touch required-event windows on 4 of 9 clips — but "must
  never touch a required window" was the wrong bar once recoverability
  is on the table; "cut aggressively, make a wrong cut fast and obvious
  to catch and fix" is a different, achievable one.

  **The real root cause of the earlier failure, found before shipping
  anything.** It wasn't a logic bug in `hard_cut_overlaps_required` — it
  never had a logic bug, because it never filtered anything: it only
  checked already-applied cuts against required windows *after the
  fact*, purely to fail the regression build. The exclusion that made
  the original scratch simulation clean (9/9, dropping any candidate
  overlapping a required window *before* cutting) was never ported into
  `apply_hard_cuts` at all — production code never took a
  protected-windows argument, and neither did `scripts/regression.py`'s
  own mirror of it, so both ran the unconditional version and correctly
  reproduced its real failures. The fix: `apply_hard_cuts` now takes a
  real `protected_windows` parameter that drops any candidate window
  overlapping a protected one (conservative, all-or-nothing, same
  overlap check `vetoed_overlapping_required` already uses) *before*
  cutting, not after. `scripts/regression.py` passes this script's own
  real required-event windows — ground truth exists there — and
  re-running confirmed the exclusion is now genuinely correct: **9/9
  clean, zero exclusion bugs**, with the identical real numbers the
  scratch simulation predicted (19.67 of 600.90s excluded-benefit,
  29.08 of 600.90s unconditional-benefit, across all 9 clips) — this
  round wasn't a re-derivation, it was confirmation that real code now
  matches what was already validated.

  **What actually ships:** production calls `apply_hard_cuts` with no
  protected windows at all (real user uploads have no ground truth to
  protect anything with) — the unconditional version, same four
  parameters (`quiet_thresh=0.002`, `merge_gap_s=1.5`, `buffer_s=0.5`),
  applied as the new default in `pipeline.run.process_video`'s final
  step. `scripts/regression.py`'s ship gate changed to match: failures
  1-4 (recall, veto overlap, motion-only-vs-fused, `check_continuity`,
  all measured *before* hard-cutting) stay exactly as strict as before —
  hard-cut isn't given a pass on breaking anything upstream of it. The
  one new hard failure is that the *exclusion mechanism itself* must be
  bug-free when given real protected windows; a required-event touch
  from the *unconditional* (production) version is no longer a failure,
  just reported per clip ("accepted risk, restorable via Edit Log").

  **The other real half of this: hard-cut entries are now visually
  distinct in the Edit Log, not listed identically to an ordinary
  never-flagged gap.** A new manifest `origin` value, `"hard_cut"`
  (alongside the existing `"detected"`/`"gap"`), set once at build time
  from `apply_hard_cuts`' second return value (which cut windows were
  actually applied) threaded through `pipeline.manifest.build_manifest`/
  `build_multi_file_manifest` and every caller
  (`scripts/detect.py`/`detect_multi.py`, `backend/pipeline_runner.py`).
  In the Edit Log, `hard_cut` entries sort first, get a left-bordered
  amber card and a "⚠ Auto-cut mid-play — review recommended" badge that
  survives restoration (shown alongside the green "Restored" badge, so
  a reviewer can still see what kind of entry they just fixed), and a
  summary banner counts how many still need review. Verified against a
  real batch through the actual running app, not just unit tests: a
  fresh `clip_300` upload processed end to end produced 5 real
  `hard_cut` entries (including the exact `[95.595,97.202]` window
  overlapping `e6` found throughout this investigation), rendered
  correctly in a real headless-browser pass — banner count accurate,
  cards sorted and styled correctly, and clicking Restore on one
  correctly re-exported and flipped it to the restored-but-still-flagged
  state, zero console errors.
- **CORRECTNESS BUG, FIXED: hard-cut had never actually cut anything from
  the real output — `pipeline/stitch.py`'s duplicate-frame-avoidance merge
  silently re-bridged every hard-cut gap it produced.** Found while
  re-investigating the shipped hard-cut feature above: full frame-exact
  `framemd5` comparison showed that despite the manifest correctly marking
  every hard-cut window `status="cut", origin="hard_cut"`, the real
  `output.mp4` still contained that footage, frame-for-frame, at exactly
  the predicted position — on every hard cut checked.

  **Root cause.** `pipeline/manifest.py`'s `kept_spans_by_file()` — the
  function `plan_stitch()` reads to build the stitch plan — only ever
  filtered on `status == "kept"`, discarding `origin` entirely. Every gap
  between kept spans, ordinary or hard-cut, then flowed into
  `merge_overlapping_spans()` (the earlier duplicate-frame fix, see above)
  as a bare `(start_s, end_s)` tuple with no memory of *why* the gap
  existed. That function's whole job is to bridge any gap shorter than the
  source's own GOP, since independently stream-copying two spans that
  close together would otherwise decode overlapping content and duplicate
  frames on concat. Hard-cut windows are deliberately shorter than a GOP
  by design (`HardCutConfig`: `buffer_s=0.5`, `min_raw_dip_s=0.5`) — the
  exact gap size this merge already treated as "safe to re-join." So it
  reliably did, on every hard cut, regardless of ground truth: the
  manifest said `cut`, the real stitched output said otherwise.

  **Fix.** `pipeline.manifest.hard_cut_boundary_starts_by_file()` (new)
  identifies, per file, every kept span whose immediately preceding
  manifest entry is a real hard-cut gap. `merge_overlapping_spans()` now
  takes an optional `protected_starts` set: a protected boundary is never
  bridged, full stop, regardless of gap length or predicted overlap risk
  — unlike an ordinary gap, there's no safe amount of "extra kept dead
  time" to trade for merging one away, because there's no dead time
  there; it was already confirmed real content. When a protected boundary
  *does* carry real keyframe-snap overlap risk (the same
  `predicted_seek_start` check the original merge used), the span on the
  far side of it is individually flagged `force_reencode` and extracted
  with a real per-file decode (`SpanJob.force_reencode`/`own_target`,
  `pipeline.stitch.run_stitch`) instead of stream copy — frame-accurate
  `-ss`, no keyframe-snap slack — so the cut is preserved without
  reintroducing the duplicate-frame risk the original merge existed to
  prevent. Every other span in the same plan is untouched; this is not a
  global re-encode. Fully backward compatible: every existing caller of
  `merge_overlapping_spans`/`kept_spans_by_file` that never mentions hard
  cuts sees byte-identical behavior (`protected_starts` defaults to empty).

  **Verified two ways.** `scripts/regression.py`, full suite (346 tests:
  341 unit + 5 e2e): ALL PASS, identical hard-cut numbers to before this
  fix (600.90s kept before, 571.81s after unconditional hard-cut,
  29.08s/4.8% excised — this fix is purely in the stitching layer, it
  changes nothing about which windows get selected for cutting, only
  whether they survive into the real file). Then, the same frame-exact
  method that found the bug, run against a real clip end to end
  (`clip_300.mkv`, real detection → real manifest → real `run_stitch`,
  not a simulation): the pre-fix merge decision, replayed against this
  exact real span list and real keyframes, silently erased all 5 real
  hard-cut boundaries the run produced (collapsed 12 real kept spans down
  to 2); the post-fix decision preserved all 5. `framemd5`-hashing the
  real rendered output against each hard-cut window's own real source
  content (shifted by the file's own `start_offset`, the same correction
  `build_extract_cmd` already needed once before) found the removed
  content genuinely absent in 4 of 5 windows — 0 of 27/72/47/13 source
  frame hashes present in the output. The 5th showed 1 of 12: traced to
  the *exact last frame* of a stream-copied span whose own trailing edge
  isn't the one flagged `force_reencode` (nothing follows it that could
  overlap) — the same already-documented, pre-existing "a stream-copied
  span's trailing frame near its `-to` cutoff can be reordered by B-frame
  remuxing... inflates measured duration by about one frame" artifact
  this module's own docstring already describes, from the earlier
  duplicate-frame investigation, not something this fix introduces or
  that hard-cut is special-cased against elsewhere in this codebase. A
  single ~20ms frame out of 6615 checked, categorically different from
  the bug fixed here (silent, wholesale, every-hard-cut erasure of
  hundreds of ms to seconds of content) — noted plainly rather than
  smoothed over, not treated as clearing the bar for "genuinely absent."
  New tests: `tests/test_manifest.py` (`hard_cut_boundary_starts_by_file`,
  6 cases) and `tests/test_stitch.py` (protected-boundary merge behavior,
  forced-reencode triggering only under real overlap risk, and an
  end-to-end `plan_stitch`/`run_stitch` check with a fake runner
  confirming the actual `ffmpeg` command issued for the forced span uses
  `libx264`, not `copy`).
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
  **RESOLVED**, once `distance_test_close/far.mov` (two real clips of the
  same subject/motion filmed at two actual distances) made real
  validation possible — see `SegmentConfig.reference_plate_box_width_px`
  and the scale-boost entry directly below for what shipped, and
  `tests/test_distance_scaling.py` for the permanent regression check.
- **Enter-side scale boost: shipped. Enter-side ambient-motion discount:
  investigated and explicitly NOT shipped, for a structural reason, not a
  tuning gap.** Two designs came out of the enter_thresh camera-distance
  finding above, both scale-aware (built on
  `pipeline.fusion.robust_box_width()`/`scale_boost_factor()`), only one
  of which could be given a real safety floor:

  **Shipped: the scale boost** (`SegmentConfig.reference_plate_box_width_px`,
  wired into `pipeline.run.process_video` and mirrored in
  `scripts/regression.py`). A batch whose near-plate person box reads
  smaller than the reference clips' own (i.e. a more distant camera than
  every clip enter_thresh was tuned against) gets its motion score boosted
  before the ENTER comparison only — the exit/sustain side still sees raw,
  unboosted `motion.scores`, via `scores_to_segments`' existing
  `sustain_scores` parameter, so this cannot affect how long a segment
  stays open once it opens, only whether it opens at all. The boost factor
  is `max(1.0, w_ref / w_batch)²`: structurally never less than 1.0, so
  `normalized_score(t) >= raw_score(t)` for every t, unconditionally —
  recall on anything already captured is provably unchanged, not just
  empirically hoped for. Confirmed zero regression re-running
  `scripts/regression.py` across all 9 reference clips (ALL PASS, same as
  before this shipped) and a concrete rescue in simulation: a synthetically
  distance-degraded `clip_foul1` (its own thinnest-margin required event,
  scaled 1.6x farther using the design's own exponent-2 model) drops to a
  0.43x margin and fails to open a segment under today's unmodified logic,
  then recovers to a 1.35x margin and opens correctly with the boost
  applied. On the real, unmodified reference clips the boost is a near
  no-op as expected (1.0x-1.225x — same camera distance as the reference,
  so there's little to correct) but not perfectly inert: `clip_base2`
  measures a real 1.5x boost on the enter-side score today, harmlessly,
  since recall there was already 1/1 either way.

  **Not shipped: an ambient-motion discount**, the other half of what was
  proposed (suppressing motion score outside at-bat windows specifically
  to cut the walk-up gap, scoped to time outside today's real kept
  segments). Every scoping rule tried hits the same wall: **there is no
  signal, available today, that can tell "genuine ambient milling" apart
  from "a real play the system is currently failing to recognize due to
  distance"** — by construction, a play the boosted enter-side still
  fails to catch looks IDENTICAL to true dead time to any classifier built
  from the current system's own segment/at-bat state, because neither is
  inside a kept segment. Concretely demonstrated, not just argued: in the
  same synthetic distance-degraded `clip_foul1` scenario above, the
  rescued event (1.35x margin) is *eligible* for the proposed discount
  under its own scoping rule (outside the segment, after a real at-bat
  fire at t=6.0) — and even a mild 0.7x discount applied on top drops the
  margin back to 0.945x, undoing the rescue. Tightening the eligibility
  window (protecting the whole active at-bat span, not just literal
  kept-segment time) doesn't close the gap either: "when does the current
  play end" is itself computed from segment/extension state that depends
  on the enter-side decision having already fired, so widening the
  protected window enough to close the gap collapses it to protecting the
  entire clip (zero dead-time benefit), while narrowing it enough to have
  any effect reopens the exact same danger. This is the same category of
  honest dead end as the Tier 2 audio investigation and the v2
  person-proximity sustain-side finding above: real, structurally
  understood, and not worth forcing — cutting the ambient walk-up gap on
  the enter side would need a genuinely different signal not yet
  considered (not a different threshold or scoping rule on the signals
  available today), and isn't pursued further against this footage.
- **Padding and `exit_thresh` pushed to their current real limits, under
  the new priority rule.** `exit_thresh`: 0.0045 → 0.0058 — the ~25%
  hysteresis-gap cushion below `enter_thresh` (0.006) was always a
  spendable margin, not a recall floor (already zero-cost up to 0.01 per
  the original sweep), so this was a clean win, confirmed safe alone
  before combining with the padding change below. Landed at 0.0058, not
  literally up to 0.01, since equaling/exceeding `enter_thresh` collapses
  the hysteresis band into a degenerate, nonsensical configuration, not
  "more aggressive."

  Padding: attempted to ship the previously-documented bare minimum
  (2.8s pre / 1.3s post) directly and **it failed** — re-running
  `scripts/regression.py` at that exact value regressed continuity on 2 of
  the 9 known clips (`clip_540`'s e4, `clip_base3`'s e1) *today*, not
  hypothetically on unseen footage. That number was measured before the
  enter-side scale boost existed and had gone stale; isolated before
  shipping anything (per standing process) to confirm it was the padding
  change alone, not an interaction with the `exit_thresh` raise — it was.
  Re-binary-searched fresh against the current pipeline: pre_pad_s=2.8
  still holds, post_pad_s needed to grow from 1.3 to 1.85 (1.82 still
  fails `clip_base3`'s e1 by a hair). Shipped at 2.8s/1.85s, confirmed 9/9
  recall and continuity fresh at that exact value. The lesson isn't
  "2.8/1.85 is now safe forever" — it's that a literal bare-minimum number
  has zero headroom against *any* other pipeline change, including ones
  that don't touch padding at all.

  **Real `full_game.mkv` cost, both changes combined, everything else
  (including the enter-side boost) held identical:**

  | | segments | kept | cut | flagged |
  |---|---|---|---|---|
  | old (4.5s/2.2s, 0.0045) | 74 | 59.52 min | 7.97 min | 88.2% |
  | new (2.8s/1.85s, 0.0058) | 130 | 54.00 min | 13.50 min | 80.0% |

  **+5.53 additional minutes cut** (7.97 → 13.50 min, a ~69% relative
  increase in real dead time removed) on the one real 67.5-minute
  recording. Segment count nearly doubled (74 → 130): tighter padding
  merges fewer nearby plays into single large blobs, so more of the
  game's actual distinct plays surface as their own segments instead of
  being absorbed into a neighbor's padding. This is the real cost/benefit
  now on record — recall/continuity on the 9 known clips is unchanged
  (both confirmed fresh via `scripts/regression.py`, `ALL PASS`), but
  there is deliberately no margin left for anything the reference set
  doesn't already cover.

  `enter_thresh` itself was explicitly considered and explicitly declined
  even under the new rule: the margin table above names, precisely, which
  real play a given raise deletes outright (past 0.00665, `clip_foul1`'s
  foul ball never opens a segment at all — not trimmed, a total miss).
  That's a guaranteed loss on a known, confirmed play, not a statistical
  risk on unseen footage, judged a different category of cost than the
  ones actually spent above. `enter_thresh` stays at 0.006.
- **Dynamic (shrink-only) padding: shipped, and the honest number is much
  smaller than a first look suggested.** Padding's own 14.80 real minutes
  on `full_game.mkv` (of the 54.00 kept) is fixed-duration regardless of
  what's actually happening inside that window — a first pass classifying
  padding time by its own average motion score suggested up to 14.2 of
  those 14.80 minutes merely "reads quiet." That number does not survive
  a properly conservative test, and shipping on the raw figure would have
  reintroduced real risk for no honest reason.

  `RefineConfig.pre_pad_s`/`post_pad_s` are now a **ceiling**, never
  exceeded — real per-segment padding can shrink toward, but never below,
  a hard floor (`pre_pad_floor_s=1.0`, `post_pad_floor_s=0.3`), and only
  when the OUTER part of the window (farthest from the segment) reads
  genuinely, contiguously quiet, scanned from its own far edge inward —
  the same direction-matters principle as the play-extension bug fix
  above, so a real dip-then-resume can't be mistaken for a safe-to-trim
  stretch. A `pad_safety_buffer_s=1.0` (tied to `smooth_window_s`, the
  time resolution below which "quiet" isn't a meaningful reading) is
  subtracted from whatever quiet run is measured, so the trim always
  stops short of the observed wake point rather than shrinking exactly up
  to it — this is the direct answer to a real, physically-grounded risk:
  padding protects against a real event's pre/post-roll running longer
  than anything in the 9 reference clips, which is a different safety
  claim than "this specific instance reads quiet right now" (a subtle
  lead-in, e.g. a stance shift before an early swing, can look quiet for
  its first second before ramping up).

  **The safety buffer is confirmed load-bearing, not just theoretical
  caution.** Running the identical mechanism with it set to 0 recovers
  more (3.12 of the 14.80 minutes) but reintroduces a real continuity
  regression on `clip_300`'s e6 and `clip_base1`'s e1 — a third and fourth
  failure mode, distinct from the two clips already known to be fragile.
  With the buffer at 1.0, all 9 clips pass clean, confirmed via
  `scripts/regression.py`, specifically including `clip_540`'s e4 and
  `clip_base3`'s e1 (the two clips that already broke once tonight on a
  more naive padding cut): both are contiguously covered, and both
  segments' post-padding — the side that broke last time — correctly
  stays at its full ceiling, untouched, while other real segments shrink.

  **Real, shipped numbers (via `pipeline.run.process_video`, not a
  simulation): `full_game.mkv` now keeps 53.19 min / cuts 14.31 min**
  (144 final segments, up from 130 — shrunk padding no longer bridges a
  few previously-touching segments into one). That's **0.81 of the 14.80
  padding minutes recovered (~5.5%)** — the rest stays as genuine
  protection. Documenting this plainly so the bigger 14.2-minute figure
  doesn't get re-chased later without redoing this same validation: most
  of what merely reads quiet on average does not survive a properly
  conservative per-segment test, and this asymmetry (recoverable margin
  concentrates on the post side — 77 of 229 segments there vs. 52 of 229
  on the pre side, since a pre-padding window sits right up against the
  plate-approach lull before a play, which is quiet less often than a
  post-padding window's outer edge is) is itself part of the honest
  result, not a detail to average away.
- **No outcome classification (Tier 2) — investigated this session and
  closed, not merely deferred.** v1 keeps every action segment, including
  missed swings; telling a whiff from a hit was explicitly authorized as a
  feasibility investigation, per the original scoping note's three
  candidate approaches, and all three are now closed with real, documented
  reasons rather than left open by default.

  **Audio amplitude** (bat-crack loudness) was tried first and failed: a
  miss's glove-pop and a hit's bat-crack are comparably loud on this
  recording, so amplitude alone can't structurally tell them apart — it
  failed specifically on `clip_whiff1`/`clip_foul1` even with tight
  (±0.25s) windowing locked to the frame-verified swing instant.

  **Audio spectral ratio** ((800-6000Hz)/(200-800Hz) energy via periodogram,
  same ±0.25s windowing) looked promising on an initial 7-point hand-picked
  sample (contact ≥0.73, non-contact ≤0.54) but was re-derived from scratch
  and stress-tested against 9 additional events mined from `full_game.mkv`
  — deliberately hunting hard cases (clean hits, weak/bunt-like contact,
  and long confirmed no-swing "take" stretches), each frame-verified before
  trusting it as ground truth, the same discipline that caught `clip_540`'s
  `e4` mislabel earlier in this project. The gap collapsed on the larger
  set: a confirmed, unambiguous real hit scored 0.506 — lower than four
  separate confirmed non-contact instants (0.771-1.734) — and a confirmed
  20+-second no-swing "take" scored 1.734, higher than four of the eight
  confirmed hits. No threshold is both safe and useful: the only cutoff
  that guarantees zero missed hits (≤0.506) misclassifies half of all true
  non-contact instants as hits (50% false-positive rate, no better than not
  classifying at all), while a cutoff that discriminates usefully
  (12.5-25% false-positive rate) carries a **12.5-25% false-negative
  rate** — real hits, including a completely unambiguous one, silently
  reclassified as misses. Given this project's standing priority rule that
  a missed real play is the worst possible failure, that's disqualifying
  on its own, not a tuning problem. Root cause: this camera/mic setup's
  background noise floor (wind, ambient field noise, distance-dependent
  recording level) varies enough between at-bats that no single fixed
  threshold — amplitude or frequency-ratio — holds across the game; the
  clean small-sample separation was an artifact of the small sample, not
  a sign the approach was sound.

  **Defensive-reaction proxy (approach 3) was ruled out without
  re-testing**, not overlooked: it's mechanically the same design as the
  person-proximity motion signal already built and tested in the `v2`
  `segments.py` investigation (any detected person, not just near the
  plate, weighting nearby motion) — and that design already failed on
  exactly this class of event, confirmed missing `clip_540`'s `e4` hit and
  `clip_foul1`'s foul ball, for a specific structural reason: a swing and
  the ball's flight extend past the batter's own bounding box and past
  whichever fielder eventually reacts, so proximity-to-a-person weighting
  reliably misses the swing/contact instant itself. A defensive-reaction
  audio or motion proxy inherits the identical blind spot — a fielder's
  reaction is not a reliable stand-in for the swing that caused it — so
  re-running it under this different name would very likely just
  reconfirm a result already on record, not produce new information.

  **All three candidate approaches from the original Tier 2 scoping note
  are now closed with real, documented failure reasons**, not left
  half-tried. This is treated as a genuine, useful outcome per this
  project's own rule that an honest "doesn't work" is as valid a result as
  a shipped feature — Tier 2 hit/miss classification is not being pursued
  further against this footage without either new camera/mic conditions
  (a controlled noise floor) or a genuinely different signal category not
  yet considered.
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
  shared `calibration.json` for the plate zone. Two more,
  `distance_test_close.mov`/`distance_test_far.mov`, are not softball
  footage — same camera, same subject, same motion pattern, filmed at two
  real subject-to-camera distances specifically to validate the
  enter_thresh camera-distance finding below against real footage instead
  of theory alone; see `tests/test_distance_scaling.py`.
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
- `tests/test_distance_scaling.py` re-derives the enter_thresh camera-
  distance finding on every run, rather than trusting it as a one-time
  measurement: real model inference against `distance_test_close/far.mov`
  confirms motion score still scales down with distance (directionally,
  not exactly quadratically — the measured exponent is ~1.09 on this
  footage, see the test's own docstring for why) and that `far`'s own
  margin over `enter_thresh` hasn't silently collapsed. Backed by
  `pipeline.fusion.robust_box_width()`, a new per-clip (not per-frame)
  scale-measurement utility with its own unit tests in
  `tests/test_fusion.py` — building it caught a real bug in its first
  version (excluding vertically-clipped boxes from a WIDTH measurement,
  which discarded every sample from `distance_test_close.mov` since all
  of them are bottom-cropped by design; only left/right clipping can
  actually corrupt a width reading).
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
