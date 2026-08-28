# Find My Highlights — Investigation Log

This is the full, unabridged development history: every signal design
tried and why it was accepted or rejected, the complete timing/thermal
investigation, every padding/threshold derivation, review-queue
statistics over time, and every bug found and fixed along the way — in
the original words they were written in, moved here verbatim rather than
summarized, specifically so none of it has to be re-investigated or
re-litigated later.

See [README.md](../README.md) for the short version: what the project
does, current results, and how to run it.

This document is organized into the same sections the main README used
to carry inline, in the order they were originally written:

- [Priority rule: the full reasoning](#priority-rule-v1-default-vs-current-live-setting)
- [Current status: the night-by-night investigation journal](#current-status)
- [Architecture overview: full component writeups](#architecture-overview)
- [Deployment: full investigation and decision narrative](#deployment)
- [Known limitations: full bug investigations and derivations](#known-limitations--non-goals-for-this-version)
- [Testing: full supplementary notes](#testing)

---

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

**Correction (2026-08-16), covering every number above in this
sub-investigation plus the pose+audio table and both temporal-position
angles earlier in this same night's write-up: `clip_60#e6` was a real
at-bat mislabeled as practice (fixed in `tests/ground_truth/clip_60.json`,
commit `73f45a9`; see the 2026-08-14 windup-to-release doc correction for
how it was caught). All three affected scripts re-run for real against
the corrected label, before -> after:**

| result | before | after |
|---|---|---|
| pose alone (n real/ambient, AUC) | 10/118, 0.532 | 11/118, **0.535** |
| audio alone (n, AUC) | 11/170, 0.523 | 12/170, **0.508** |
| pose+audio combined (paired n, AUC) | 10, 0.531 | 11, **0.531** (same rounded value, different n) |
| temporal angle 1 (n gaps, range, CV) | 12, -1 to 86s, 0.90 | **identical**, digit-for-digit (only a type label changed) |
| temporal angle 2 (pooled n, CV) | 5, 0.65 | 6, **0.64** (never a published headline number before this correction) |
| X-CLIP zero-shot AUC | 0.690 (documented) / **0.688** (this session's faithful same-methodology reconstruction, since no committed script for the original run exists) | **0.6527** |

**Every non-X-CLIP number keeps its original verdict** — pose, audio,
their naive combination, and both temporal angles all stay at or near
chance / too-sparse-to-trust exactly as before; this correction moves
precise digits, not conclusions, for those five.

**X-CLIP is the one real, load-bearing exception, and its significance
was re-checked for real rather than assumed carried over — priority
item, not left open.** Two independent tests, both against the corrected
n=12 real / n=167-scored-ambient sample: a 2000-shuffle permutation test
(seed 20260816, fixed and reported) gives **p=0.0395**; an analytic
Mann-Whitney U test (U=1308.0, alternative="greater") gives **p=0.0390**,
cross-validating the permutation result and independently reproducing
the same 0.6527 AUC from the U statistic itself. **Still clears
conventional significance (p<0.05) — but only just, not comfortably.**
The original claim was p=0.012 (permutation) / p≈0.017 (Mann-Whitney);
the margin has roughly shrunk to a third of what it was. This remains
technically the best real result on record in this log, but "the first
signal all night to actually clear conventional significance," stated
with real headroom before, now describes a result sitting much closer to
the line — a materially weaker headline than the original write-up
conveys, even though the verdict (real, not chance) hasn't flipped.

**Five other results also rest on the pre-correction 11-event set and
were NOT rebuilt tonight, by explicit scope decision, not oversight:**
the X-CLIP raw-embedding nearest-centroid probe (0.587, referenced a few
paragraphs above), the HFC/spectral-flux onset-detection comparison
(0.605/0.646, benchmarked against the pre-correction audio AUC of 0.523),
this X-CLIP entry's own 5-prompt-variant robustness table (0.620-0.690),
`scripts/joint_classifier_investigation.py`'s single-feature and
cross-validated AUCs (0.509/0.529/0.469 paired, 0.191 combined), and the
2026-08-13 embedding-level audio+visual fusion entry (which reused this
same 11-event set throughout and compared its own results against 0.690
as "the baseline to beat"). Several of these, like the X-CLIP zero-shot
case above, have no committed script to simply re-run — rebuilding them
would mean reconstructing lost methodology, not just re-executing
something that exists. Left as real, open, explicitly-tracked debt
rather than silently assumed still valid.

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

**Deployment-ready: real Dockerfile, real env-based config, real demo
mode, all verified locally before being called done.** Driven by a real
deadline (an application demo), not a scoped feature request — see
Deployment for the actual how-to; this entry covers what was built and
the real problems caught along the way.

Backend config that was previously hardcoded repo-relative
(`backend.storage.DEFAULT_UPLOADS_ROOT`, `pipeline.run.DEFAULT_CACHE_DIR`,
`pipeline.pose.DEFAULT_MODEL_PATH`) is now overridable via `FMH_*` env
vars, same convention `FMH_TRAINING_DATA_DIR` already established —
every existing caller that doesn't set them is byte-for-byte unaffected.
CORS is off by default (real security reasoning: this API accepts real
file uploads, a wildcard origin would be a real hole, not a
convenience), on via `FMH_CORS_ORIGINS`, verified with real preflight
requests against both an allowed and a disallowed origin.

**Demo mode** (`backend/demo.py`, `POST /demo/run`): runs a real ~45s
bundled clip (`clip_whiff1.mkv`, a genuine swing-and-miss from this
project's own reference set) through the exact same
`run_detect_then_export_job` code path a real upload takes — no special
fake pipeline. The one real engineering problem this needed solving: RF-
DETR detection is the only slow, ML-inference step in the core pipeline,
and a live cold run of it on unknown demo hardware would threaten the
"well under a minute" target. Pre-computing it and shipping the result
seemed simple until the cache key turned out to be derived from the
video file's real mtime (`pipeline.detection._cache_key`) — and neither
`git` nor `docker build` reliably preserve a source file's real mtime,
so a naively pre-cached result would silently miss its own cache on
every real deployment. Fixed by stamping a fixed, deterministic mtime on
the copied file at the moment each demo batch is created
(`backend/demo.py`'s `seed_demo_batch`), computed fresh every time
rather than trusted from disk — verified directly: real, repeated demo
runs (both bare-metal and through the built Docker container) hit the
seeded cache every time, no RF-DETR cold-start. Real, measured demo
run time: 6s locally, 11s in the built container. Real detected output
was checked, not assumed: the pipeline's kept segments genuinely cover
the clip's real labeled swing-and-miss event
(`tests/ground_truth/clip_whiff1.json`'s e1, `[12,18]`).

**A real process mistake caught mid-verification, not swept past:**
testing the demo endpoint through the actual dev-server proxy path
(rather than an isolated scratch instance) accidentally hit the user's
own already-running dev backend and frontend on their standard ports
instead of new isolated ones, because both were already occupied and
the new instances silently fell back to different ports without that
being checked first. This wrote one real test batch into the project's
actual `uploads/` directory. Caught by checking `lsof`/`ps` before
concluding what had actually served the request (rather than assuming
the just-started process had), the test batch was removed, and the
user's real running servers were confirmed untouched throughout — an
accidental but real confirmation that the new code works correctly
against a live `--reload` server that had picked up the changes via a
hot reload, not just a clean from-scratch process.

The Docker image itself (`Dockerfile`) installs `torch`/`torchvision`
from PyTorch's CPU-only wheel index specifically (not PyPI's default,
which resolves to CUDA-bundled wheels several GB larger and entirely
wasted on a GPU-less demo host) — real, measured image size 4.66 GB.
RF-DETR's pretrained weights are downloaded and warmed up at build time,
not on a visitor's first real request, via a real call through
`detect_persons()` against the bundled demo clip itself (if this step
fails, the image fails to build, not a visitor's first click). Runs as a
non-root user. All verified with a real `docker build` + `docker run` +
a real demo run through the running container + a real named-volume
persistence check (mounted the same volume from a second, fresh
container and confirmed the real output files were there) — not just
"the Dockerfile looks right."

Frontend: every API call was already routed through `frontend/src/api.js`
(no path build logic duplicated across components), so making the
backend URL configurable was a single-file change — added a
`VITE_API_BASE_URL` build-time env var (Vite's own `import.meta.env.VITE_*`
substitution), defaulting to empty/relative (unchanged local-dev
behavior via the existing Vite proxy). Verified for real, not assumed:
built once with no override and grepped the output bundle for the env
var name (absent — correctly inlined away), built again with a real
URL configured and grepped for that URL (present — correctly baked in),
then ran `npm run preview` against the default build and confirmed it
serves standalone. Caught one real bug in the same pass: the dev-server
proxy (`vite.config.js`) only forwarded `/batches` and `/review`, not
the new `/demo` prefix — the demo button would have 404'd in local dev
if this hadn't been checked.

Explicitly NOT decided here, flagged rather than picked silently:
hosting provider for the backend, static host for the frontend, whether
to keep the backend warm vs. scale-to-zero, whether to enable the review
queue (`FMH_TRAINING_DATA_DIR`) for a public deployment, and TLS/custom
domain — see Deployment's "Open decisions" for the real reasoning behind
leaving each of these open.

**Deployment target switched from Railway to GCP Compute Engine, and the
frontend converted from JavaScript to TypeScript — both real, both
verified, not just written.** See Deployment for the full GCP how-to
(real `gcloud` commands, a real measured 1.6GB worst-case memory number
driving the `e2-medium` instance recommendation, and a real gap the
platform switch surfaced: Railway/Vercel both provided managed TLS
automatically, a bare Compute Engine VM doesn't, and Vercel's HTTPS
frontend would be silently blocked by the browser's mixed-content policy
calling a plain-HTTP backend -- fixed with Caddy + a free `sslip.io`
hostname, no domain purchase needed).

**TypeScript conversion:** all 10 frontend source files renamed
`.jsx`/`.js` → `.tsx`/`.ts`, `tsconfig.json`/`tsconfig.app.json`/
`tsconfig.node.json` added (the standard Vite+React project-references
split), `strict: true` plus `noUnusedLocals`/`noUnusedParameters` --
real strictness, not just enough to compile. A `src/types.ts` module
defines the real backend response shapes (`Job`, `Manifest`, `Segment`,
`ReviewRecord`, etc.) once, shared across every component, instead of
each one re-guessing its own. `npm run build` now runs `tsc -b` before
Vite's own build, so a real type error fails the build -- verified this
is actually wired up and not vacuously passing, not just assumed:
deliberately typed a prop as `number` instead of `string`, confirmed
`tsc` failed with real errors (including a real cascading error in the
*calling* component, `App.tsx`, not just the one directly edited), then
reverted and confirmed a clean build again. Also verified the installed
`@types/react`/`@types/react-dom` actually match the real `react`/
`react-dom` major version in use (18, not 19) -- `npm install` initially
pulled the latest type packages by default, which would have produced
real but spurious type errors unrelated to any real code problem, since
this project's own `react`/`react-dom` are pinned to 18.

Real caveat, not swept past: a few `!` non-null assertions remain
(`EditLogView.tsx`, `ReviewQueueView.tsx`) where a value's non-null-ness
is guaranteed by this component's own control flow (e.g. `batchId` is
confirmed non-null before `loadState` can reach `'ready'`) but isn't
something the type system can see from the render path alone -- each is
commented with the specific invariant that makes it safe, not left bare.
This is a real, deliberate, narrow use of the escape hatch, not a
substitute for real typing elsewhere.

Full backend test suite re-run after every step (frontend-only changes,
but re-verified rather than assumed unaffected): 457 passed throughout.
Browser-level verification (every feature, by hand) intentionally left
to the user, same as every other UI change tonight -- this write-up
covers what was verified at the build/type level, not a substitute for
that pass.

**Multi-feature review-queue model: a real, statistically significant
signal that still fails the recall-risk check -- reported honestly as a
real no, not stretched into a maybe.** The review queue reached 149
labeled records (39 real_action / 110 downtime); once the single n=1
`control` sample is excluded as unmodelable, 148 real labeled records
remain (39/109). `scripts/multi_feature_review_model.py` (new real
dependency: `scikit-learn`, added specifically for this -- not wired
into the shipped pipeline) combines the three independent signals this
project has already investigated alone and found real-but-modest each
time -- pose peak wrist displacement (missing for a real 45/148, given
an explicit missingness indicator rather than dropped), audio onset
amplitude/rise-time, and X-CLIP `p_swinging` -- into one small,
standardized logistic regression.

Real LOO-CV AUC: **0.690**. Real permutation test (1000 permutations,
each a full fresh LOO-CV on shuffled labels, not a cheaper
approximation): the real AUC beat all 1000 null values, **p=0.0010** --
a real, statistically significant combined signal, not noise.

**But two honest disqualifiers, not glossed over:**
1. **Accuracy at a real 0.5 decision threshold (72.3%) does not beat
   the naive majority-class baseline (73.6%)** -- the same honest-
   calibration check used throughout this project. A statistically real
   AUC (good ranking/discrimination across all thresholds) is not the
   same thing as a practically useful classifier at an actual operating
   threshold, and this result is a real instance of that gap, not just
   a theoretical caveat.
2. **Recall-risk check against the six fragile clips' required real
   events: 3 of 6 would be misclassified as downtime**
   (`clip_base1`/e1 p=0.453, `clip_base4`/e1 p=0.357, `clip_foul1`/e1
   p=0.384 -- all below the 0.5 threshold; `clip_base2`/e1,
   `clip_base3`/e1, `clip_whiff1`/e1 correctly scored above it). A
   lower decision threshold (~0.35) would flip all three to correct --
   but choosing that threshold only because it happens to fix exactly
   these six known clips is circular, not real validation; it wasn't
   done, and doing so wouldn't count as clearing this bar even if it
   had been.

**Verdict: real signal, real significance, real disqualification.**
This is not "not enough data" or "try again later" -- it's a genuine
multi-feature combination that measurably beats chance and still isn't
safe, on the same real events this project has used to reject every
prior enter-side/gating attempt. Not wired into anything. The path this
doesn't close off: a model that's allowed to say "unsure" and defer to
the existing pipeline decision (rather than being forced to output
real_action/downtime at a fixed threshold) is a real, different
question from the one asked and tested here.

**Local VLM revisited at real scale: two real bugs caught in this
project's own evaluation code, and a final result that closes this
avenue for now, honestly, not stretched.** Picking back up
`scripts/local_vlm_feasibility_check.py` (Qwen2-VL-2B-Instruct,
Apache-2.0) at the user's request, to test it against the full 149-record
real labeled dataset instead of the original 16-example subset:

1. **Real disk crisis found before anything else could run.** Free disk
   had dropped to **711MB** (not increased, despite an earlier
   `~/.cache/huggingface` clear) -- traced to Docker Desktop's own VM
   backing file (`Docker.raw`, 4.5GB) and, more precisely, 6.18GB of
   Docker build cache plus other real projects' images/volumes
   (`payout-warehouse`, `mini-bfcm`, `clean-clone`, `readme-test` --
   confirmed none of these were this project's own artifacts before
   touching anything). `docker builder prune` (100% safe, pure
   rebuildable cache) plus clearing `npm`/`pip`/Homebrew caches (also
   100% safe, official-command, zero-data-loss operations) recovered
   711MB -> **~9-11GB** real free space. Docker's other images/volumes
   deliberately left untouched -- not this project's data to delete.

2. **Real accuracy on the full 149-record dataset, existing (`original`)
   prompt: 107/149 correct (71.8%) -- below the majority-class baseline
   (73.8%).** Recall on real_action (the safety-critical direction):
   **56.4% (22/39)** -- misses nearly half of all real plays when forced
   into a binary call. The same disqualifying shape as the multi-feature
   model above: real-looking but doesn't clear the honest bar.

3. **The bare-label bug, real root cause found, not guessed:** verified
   directly (150-token budget, only 5 tokens ever used, real EOS token
   present every time) that the model satisfies "state the label" and
   emits end-of-sequence immediately, never reaching "then justify" --
   an instruction-following limitation, not a token-budget problem.
   Reordering the prompt (describe first, label last) produced real,
   specific, frame-by-frame reasoning -- confirmed on real examples, not
   assumed. **But a second, honest problem surfaced in that reasoning**:
   on a real downtime instant with no batter visible at all, the model
   confabulated a full swing narrative not supported by the actual
   frames, flipping a previously-correct prediction to wrong --
   interpretability and accuracy are not the same axis, demonstrated
   directly, not just asserted as a caveat.

4. **A real bug in this project's OWN evaluation code, caught before
   trusting the result -- the same discipline every real number tonight
   has been held to.** The reasoning-first prompt's free-form conclusion
   ("...the person is in the active swing phase") never contained the
   literal token `ACTIVE_SWING` the classifier searched for, so every
   reasoning-first response was silently scored DOWNTIME regardless of
   the model's actual conclusion -- caught by manually reading the raw
   responses, not by a passing test. Fixed by demanding the exact
   literal token on its own final line.

5. **The fix looked like a real win on the 6 named recall-risk clips
   (`clip_base1`/e1, `clip_base4`/e1, `clip_foul1`/e1, `clip_60`/e5,
   `clip_540`/e4, `clip_whiff1`/e1) -- 0/6 with the original prompt
   (visually verified against the real extracted frames first, to rule
   out an extraction bug: these are genuine live-play frames, pitcher
   winding up, ball in flight, runners moving -- not empty/ambiguous
   footage), 6/6 with the fixed prompt. That looked like a genuine
   capability recovery. It wasn't.** Re-running the FIXED prompt against
   the full 149-record dataset (not just the 6 hand-picked clips) --
   exactly the "understand it on real, larger data before trusting a
   small win" check the user asked for -- found the model predicting
   `ACTIVE_SWING` on **all 149 of 149** records: FN=0, but TN=0 too.
   Accuracy 26.2%, precisely what a trivial always-predict-positive rule
   gives at this dataset's real 39/149 real_action rate. **The 6/6 was a
   degenerate constant-output collapse, not real discrimination** -- it
   would have looked like a clean win on the 6 safety-critical clips
   alone, which is exactly why the full-dataset re-check mattered enough
   to run before believing it.

**Verdict: neither prompt configuration shows real, trustworthy
discriminative ability on the real, full dataset -- one scores below
the majority baseline, the other degenerates to a constant classifier.**
Per the user's own explicit condition for escalating ("only if the
existing model still shows real promise") -- it doesn't, so a larger
local model was not tried. Disk space is no longer the blocker (as
established in step 1); the model's own real accuracy is. Not wired
into anything.

**A real, user-reported truncated export, root-caused to a genuine
stitching bug silently shipping since Aug 2, fixed, and closed with a
permanent regression gate that should have caught it in the first
place.** A real manual browser pass (base calibration + clip_300)
reported an exported video stopping after ~4s. Investigated per the
user's own explicit order, not guessed at:

1. **Real batch ID found (`705e659881e7`), real manifest pulled, real
   `ffprobe` run on the real `output.mp4`.** The file was NOT truncated
   to ~4s -- video stream 145.9s, audio/container 166.8s, a real internal
   split, not a short file.
2. **Manifest kept segments were completely healthy** -- 12 kept
   segments, normal boundaries, no collapse. Compared directly against
   the most recent pre-base-calibration `clip_300` run (Aug 1,
   `12adc76dbaf6`): same segment boundaries, essentially identical.
3. **Base calibration checked directly, not assumed -- ruled out.** Real
   root cause: `clip_300.mkv`'s native audio is Opus. The stitcher's
   `force_reencode` mechanism (added by commit `6cdf859`, Aug 2, for an
   unrelated hard-cut-boundary fix -- see `pipeline/stitch.py`'s module
   docstring) re-encodes just the one span after a risky hard-cut
   boundary to AAC, while every other span in the same plan stays a pure
   stream-copy on the source's own Opus audio. The final concat's blind
   `-c copy` mixes both into one file. Confirmed this predates base
   calibration entirely: the Aug 1 comparison run predates commit
   `6cdf859`, so it never had `force_reencode` at all (git-confirmed --
   every line of that mechanism is a `+` in that commit) and was pure
   stream-copy end to end, hence clean. Base calibration shipping the
   same day was coincidental timing, not a cause.
4. A strict full decode (`ffmpeg -v warning -i out.mp4 -f null -`)
   confirmed real, repeated decode failures ("Error parsing the packet
   header", "Invalid data found when processing input"), not just
   ffprobe-level curiosities -- exactly the kind of file a browser's
   stricter decoder stops on partway through, consistent with the
   reported ~4s symptom (right at the first stream-copy -> re-encode
   transition).

**Blast-radius audit, done BEFORE fixing anything:** every real batch
processed since `6cdf859` landed (Aug 2, 15:46:47) -- only two exist,
both real production runs, not synthetic. Strict-decode-checked both:
- `705e659881e7` (`clip_300.mkv`, Aug 6): corrupted, 7,220 real decode
  errors, video 145.9s vs. audio/container 166.8s.
- `d46f312fa057` (`full_game.mkv`, Aug 5): corrupted, far worse --
  133,006 real decode errors, video 3250.3s vs. audio/container 3406.6s
  (**156s of video missing**). **Needs re-export.**

**Fix:** `pipeline.stitch.plan_stitch` now promotes the WHOLE plan to
the re-encode path (reusing the already-validated multi-file-mismatch
mechanism) whenever ANY span would need forcing, instead of forcing just
that one span -- every span in a plan now shares one codec, always,
never a silent mix. Real, not just a fake-runner check: a new end-to-end
test (`test_mixed_codec_hard_cut_boundary_produces_a_strictly_clean_decode`)
generates a real tiny clip with real Opus audio and a real risky
hard-cut boundary, runs the real `run_stitch`, and strictly decodes the
real output -- verified this test genuinely fails pre-fix (manually
reverted `pipeline/stitch.py` to the pre-`6cdf859`-successor commit,
reran: `reencoded=False`, 2 real decode errors) and passes post-fix.

**Permanent regression gate added, exactly where the user said this
should have lived all along:** `scripts/regression.py` now actually
stitches every reference clip's real `kept_unconditional` segments
(the exact ones production ships) via real `run_stitch`, and strictly
decodes the real result -- a new hard failure condition (#6), gated
like every other real correctness check in that script. Every existing
check there only ever looked at segment BOUNDARIES; this is the first
one that confirms the real stitched FILE is actually playable end to
end. All 9 reference clips: clean, 0 real decode errors, `ALL PASS`.

Full test suite: 466 passed (up from 465 -- one new real end-to-end
stitch test). Not yet done: re-exporting the two real corrupted
batches (`705e659881e7`, `d46f312fa057`) -- flagged to the user, not
done silently, since re-exporting overwrites their existing
`output.mp4`.

**2026-08-09 correction: a "homography/reprojection" investigation
referenced in later planning turns was never real -- conversational
analysis mistaken for shipped infrastructure, not a case of forgetting
to commit something.** An investigation requested a basepath-corridor
refinement to `compute_zone_velocity`, describing it as reusing
"already built and validated" homography/calibration infrastructure
and a "~2.24ft mean" reprojection-error figure "established earlier."
Neither exists, and never did: exhaustive search (grep across the
current tree and docs, plus `git log --all -S` for `homography`,
`findHomography`, `getPerspectiveTransform`, `warpPerspective`,
`perspective`, `reprojection`, and `2.24` across all history) found
zero hits, anywhere, ever. What's actually shipped as of `2ea6bed`
(multi-base calibration) is `pipeline.calibration.
resolve_calibrated_scale_px` / `pipeline.fusion.
calibrated_scale_boost_factor` -- a single 2-point Euclidean `hypot()`
distance between home and first base, not a projective transform. The
"~2.24ft" figure appears to have been generated in conversation and
never persisted anywhere; treat any future reference to it as
unverified until re-derived. Flagging this here specifically so a
future session doesn't inherit the same false premise from a stale
conversation summary again.

**What this session's real calibration pass adds (diagnostic only, not
wired into production):** real home+first+second+third pixel
coordinates for all 6 clips used in this project's standing fragile-clip
set (`clip_base1`, `clip_base4`, `clip_foul1`, `clip_60`, `clip_540`,
`clip_whiff1`) -- every point independently visually identified from
extracted frames (not hand-typed) and submitted through the real,
shipped `POST /batches/{id}/calibration` endpoint, two independent
passes per clip from different timestamps of the same static camera to
measure real click noise. Home-to-first distance landed 421-426px
across all 6 clips, tightly clustered around the already-shipped
`reference_calibrated_scale_px=421.4` -- real, independent confirmation
these clips share one camera setup.

A real homography (`cv2.findHomography`, exact 4-point fit per clip,
real-world square home=(0,0) first=(60,0) second=(60,60) third=(0,60)
per `BASE_PATH_FT`) was fit on pass A per clip and tested against pass
B's independently-reclicked points, error reported in feet against the
known true corners. Real result: **1.365ft mean overall, but wildly
inconsistent across clips (std 1.925ft)** -- `clip_foul1` 0.648ft,
`clip_540` 0.326ft, `clip_base4` 1.004ft, `clip_whiff1` 0.619ft,
`clip_base1` 1.432ft, but `clip_60` **4.164ft mean, 6.175ft max**, 3-10x
every other clip. Root-caused, not left unexplained: `clip_60`'s home
point had the largest pass-A/pass-B pixel shift of all 6 clips (9.43px
vs. 1-5px elsewhere) -- with only 4 exact point correspondences (no
redundancy), click noise in the anchor point propagates into the whole
transform's predictions at every other corner. Per this investigation's
own explicit stop condition ("if error is too large or wildly
inconsistent, don't force the corridor comparison to run against
unusable geometry"), the corridor-vs-pixel-zone-velocity comparison
(the original Step 3) was **not run** -- real reprojection accuracy on
this footage, from a single 4-point calibration pass with no
redundancy, is not reliably trustworthy enough to build a basepath
corridor on, at least not without either more calibration points (an
over-determined, noise-robust fit) or correcting for this camera's real
fisheye/wide-angle distortion first (visible barrel distortion in every
one of these clips' backstop-net framing -- a straight real-world
basepath will not project to a straight line in pixel space, which a
plain homography can't model). Negative result, reported plainly, not
softened.

**Open methodology gap, not fixed now, flagged for whenever calibration
work resumes: single-click calibration is fragile for at least some
real cases.** `clip_60`'s home point alone drifted 9.43px between two
independent passes on the same static camera -- 2-9x every other
clip's home-point drift -- and that one point's noise was enough to
swing its homography's reprojection error to 4-6ft (vs. 0.3-1.4ft
everywhere else). A single calibration click, human or otherwise, has
no way to know it landed on a noisy instance until a second independent
click exists to compare against -- which today's real calibration flow
(one submission per batch) never collects. Whether this matters
depends entirely on what the calibration feeds: `resolve_calibrated_scale_px`
(shipped) is a single 2-point distance, where this kind of noise is
already accounted for (the ~1.7% click-spread finding from the
calibrated-distance work). A homography amplifies the same magnitude of
per-point noise far more, because 4 exact-fit points have zero
redundancy -- any future homography-based work should assume single-click
input is not sufficient on its own and plan for either redundant
points or an explicit noise check before trusting the fit.

**Follow-up on the two items above, same day:** (1) `clip_540
.calibration.json` reverted, uncommitted -- `regression.py` confirmed
back to `ALL PASS` with it removed, and a full diff of the two runs'
per-clip output (excluding `clip_540`'s own section) confirmed
byte-identical, so no other clip's real signal was affected by removing
just that one file. (2) e4's real margin was investigated as its own
issue -- see the next entry.

**`clip_540`'s e4 real-margin investigation: root-caused and
quantified, not fixed yet -- pending a real decision, not a silent
reversion to the box-width proxy's imprecision.** Reproduced the
honest-1.0x-boost scenario directly (real calibrated distance restored
in a scratch dir, not committed) and inspected the raw smoothed-motion
trace: the gap is a genuine, deep quiet stretch (score falls to
~0.0006, roughly 10x below `exit_thresh`, for a real ~5.6s span, not
threshold noise), and the ground truth for e4 explains exactly what
it is -- `tests/ground_truth/clip_540.json` describes contact at
~183-184s with the batter running to first, meaning the quiet dip at
t~175-179 is the batter settling into the box before the pitch. This
is the exact "batter frozen mid-stance" category `RefineConfig`'s own
padding docstring already names as what padding/hysteresis exists to
bridge -- shipped padding (`pre_pad_s=2.8`/`post_pad_s=1.85`) already
closes most of the raw 5.63s gap, leaving a real ~0.7-1.0s residual
inside the required window that the old box-width proxy's accidental
`1.039x` boost happened to close, and the new honest `1.0x` no longer
does.

Three real levers tested (binary search, each isolated the same way
padding/`exit_thresh` have always been isolated in this log), full
9-clip reference set, all with clip_540's real calibration active:

| lever | shipped -> tested | result | real cost (kept-before-hardcut, all 9 clips) |
|---|---|---|---|
| `post_pad_s` | 1.85 -> 2.4s (real min is in (2.3, 2.4]) | fixes e4, `ALL PASS` | 598.67s -> 609.60s (+10.93s, ~+1.8%) |
| `pre_pad_s` | 2.8 -> 3.4s (real min is in (3.2, 3.4]) | fixes e4 (isolated check only, full-set cost not yet run) | not yet measured |
| `exit_thresh` | 0.0058 -> 0.003 | fixes e4, `ALL PASS` | 598.67s -> 629.09s (+30.42s, ~+5.1%) -- ~3x `post_pad_s`'s cost |

`enter_thresh` was not tested as a lever: this log's own earlier
enter_thresh margin investigation already established it has
"essentially no real margin" project-wide, and lowering it risks a
segment never opening at all (categorically worse than a late-closing
one) -- not a proportionate response to one clip's tail-end gap.
`post_pad_s` is both the cheapest real option found and the most
semantically direct (it's literally "how long to wait through a real
quiet lull before calling a segment closed," which is exactly what e4
needs) -- but it's a global config value, so the real cost above is
paid on every segment boundary in every clip, not just clip_540's.
Not implemented: the user asked for options and real tradeoffs, not a
unilateral fix, per the standing rule on guaranteed named real-play
loss. `clip_540.calibration.json` stays reverted (see above) until
this is decided.

**2026-08-10: a second fabricated-data incident risk pattern confirmed
this session, in two separate forms, both caught before anything was
built on them -- reinforcing standing practice, not a one-off.** (1)
Uncommitted `reference_clips/*.calibration.json` files (modified
`clip_base1`/`clip_base4`, new `clip_60`/`clip_foul1`/`clip_whiff1`)
carried a `created_from` field citing "the two-pass reprojection-error
methodology" in this very doc -- plausible-sounding, but `git log --all`
showed zero history for the three new files and the geometry didn't even
match the real over-determined methodology being asked for. Reverted
before use (git-clean on the tracked two, deleted the untracked three),
confirmed via a clean `pytest` + `regression.py` baseline. (2) Separately,
a request referenced "tonight's paid-VLM test," "27 known enter-side
disagreements," and a "0.31%-margin band" -- none of which trace to
anything in this repo (`docs/INVESTIGATION_LOG.md` names a real but
different 16/11/9-record set from a prior session; `ANTHROPIC_API_KEY` is
not configured anywhere; zero files anywhere in the repo had been
modified in the preceding 6 hours). Flagged plainly instead of
inventing a matching case set; the user confirmed and the work was
rescoped against real, live-rederived data instead (see below). Both
caught the same way: check the specific claim against real repo state
(`git log`, `git diff`, file existence, live re-derivation) before
acting on it, not against how plausible it reads.

**Over-determined calibration for corridor geometry -- CLOSED, real
negative result, two distinct root causes found.** Following up the
single-pass homography result above (1.365ft mean, `clip_60` a 4.164ft/
6.175ft outlier blamed on one noisy click), 30 real independent
calibration passes were collected this session -- 5 per clip, each a
fresh visual read from a different real timestamp, submitted through the
real `POST /batches/{id}/calibration` endpoint (a same-origin diagnostic
page, `scripts/calibration_multipass_diagnostic.html`, plus an additive
`?at_seconds=` override on `GET /batches/{id}/preview.jpg` -- both real,
tested, and safe to delete later; not linked from the production
frontend) -- across all 6 target clips (`clip_base1`, `clip_base4`,
`clip_foul1`, `clip_60`, `clip_540`, `clip_whiff1`). A least-squares
homography (`cv2.findHomography`, `method=0`, over all redundant
correspondences per clip) was fit per clip, with leave-one-pass-out
cross-validation for a real, held-out reprojection-error estimate.

**Redundancy did not fix it -- the overall picture got worse, not
better: 2.331ft mean / 17.174ft max across all 6 clips, vs. 1.365ft mean
single-pass.** Two real, distinct root causes, not one:

1. **A universal problem in every one of the 6 clips, not just
   `clip_60`.** Second base's reprojection error is 3-9x every other
   point's, in every single clip (e.g. `clip_base4`: plate 0.115ft,
   first 1.534ft, third 2.173ft, second **3.263ft**) -- confirmed
   in-sample, not just LOO, so it isn't a generalization artifact.
   Second base is the farthest, most obliquely-angled corner from this
   backstop-mounted camera -- consistent with the barrel/fisheye
   distortion already flagged above ("a plain homography can't model")
   -- and it caps how good *any* clip's fit can get, regardless of click
   count.
2. **`clip_60` specifically has real camera drift mid-clip, confirmed
   with real pixel evidence, not inferred from clicks alone.**
   `cv2.phaseCorrelate` on the actual preview frames showed `clip_base1`
   essentially frozen between timestamps 23s apart (shift <1px, high
   confidence), while `clip_60` showed a real, internally-consistent
   ~15-20px scene shift between every pair of sampled timestamps (the
   directly-measured 50s->90s shift matched what chaining the other two
   measurements predicted, ruling out correlation noise). Fitting only
   on the temporally-converged later passes (90-170s) and testing
   in-sample gave errors comparable to the *good* clips (plate/first/
   third 0.13-1.0ft); testing that same fit against the scattered early
   passes (15s/50s) gave 6-7ft mean / 13-15ft max. Averaging
   temporally-inconsistent frames into one homography doesn't reduce
   noise, it blends two different camera geometries, which is worse than
   either alone.

**"More redundant clicks" is now a fully tested, honestly negative
result -- don't revisit without addressing distortion correction or a
per-window/per-segment homography first.** A basepath corridor is
realistically a few feet wide; second base alone carries 3-9ft
systematic error in every clip before `clip_60`'s drift problem even
enters the picture. The corridor-vs-pixel-zone comparison (the original
Step 3, still not run) stays not-run -- real reprojection accuracy is
not there, and this session closes the "just add redundancy" branch of
trying to get it there.

**Local open-weight VLM re-test on current hard cases -- small positive
signal, not conclusive.** The real target category from the
`sustained_ambient_xclip_check.py` "presence without action" finding
above has shrunk since that entry was written: live re-derivation
against today's larger labeled dataset (34 real labeled enter-type
records now, not 16) found 25 current disagreements (not 11) but only
**3** confirmed "sustained ambient, no batter visible" cases (not 9) --
`bc_121f61ed3d15`, `bc_9bcbb8332ff4`, `bc_eaeb1bcef9e4`. The other 22
disagreements from the old 11 now fall into a different bucket
("occupancy present somewhere in the wide window"), already explained a
different way; the debounce/occupancy work already absorbed most of the
category this test targets. Re-ran `scripts/local_vlm_feasibility_check.py`
(Qwen2-VL-2B-Instruct, `--prompt-mode original` -- `reasoning_first` is
the mode already documented above to collapse into an always-
`ACTIVE_SWING` constant classifier at full scale) against just these 3:
**3/3 correct.** Because all 3 share one ground-truth label, a trivial
always-DOWNTIME classifier would also score 3/3 here -- checked directly
against 4 real_action-labeled clips from the same dataset, which scored
3/4 with genuine `ACTIVE_SWING` predictions, ruling out that collapse.
**n=3 is too small to reopen the real full-scale "insufficient"
conclusion** (71.8% accuracy / 56.4% real_action recall on the full
149-record set, unchanged) -- treat this as one encouraging data point
on a category that's gotten thinner and more marginal since it was
first identified, not as new evidence the local model works. Local
compute only, zero API cost, nothing wired into `pipeline/` or
`backend/`.

**Paid-VLM cost-bounded verification -- still scoped, not yet run, no
change from prior status.** Blocked on Anthropic account billing (no
`ANTHROPIC_API_KEY` configured; user does not want to add funds right
now) -- a real, standing blocker, not a technical one. No fresh
feasibility math was done this session (a request referencing specific
numbers here didn't trace to anything real, per the fabricated-data note
above). No scripted paid-VLM check currently exists in this repo
(confirmed by search) -- if one existed in an earlier session it hasn't
persisted, matching this thread's own prior warning not to assume it
does. If revisited: the case set and thresholds need to be re-derived
fresh from real repo data at that time, the same way the local-VLM
target category above had to be re-derived fresh rather than trusted
from a stale count.

**Same night, follow-up: local VLM re-tested against the FULL current
disagreement set (all candidate types), not just the legacy 3-case
category above -- confirms the same "insufficient" picture, closes the
avenue for good.** The 3-case result above only covered one narrow
category (enter-type, sustained-ambient). Queried
`training_data/reviews/` for every current real disagreement (pipeline's
final decision vs. human label) across all three candidate types present
(`boundary_crossing`, `hard_cut_dip`, `control`), re-derived live against
the real shipped pipeline the same way as above.

**A real methodology bug caught before trusting the number, same
discipline as everything else tonight.** The first pass gave 105/149
(70.5%) disagreements -- implausible against a pipeline that passes
`regression.py`'s `ALL PASS` gate. Root cause: the reconstruction
(adapted from `sustained_ambient_xclip_check.py`, which only ever checks
enter-type records) never called `apply_hard_cuts()`, the real final
step `pipeline.run.process_video` always applies (`pipeline/run.py:209`)
-- without it, `hard_cut_dip` candidates never actually get excised from
the reconstructed "final kept" set, which alone inflated that one
category from a real 36/88 to a false 70/88. Fixed and re-run. Corrected
real count: **78 of 149 labeled records (52.3%)** --
`boundary_crossing`/enter 24, `boundary_crossing`/exit 17, `control`/kept
1, `hard_cut_dip`/cut 36. A >50% disagreement rate against a shipped,
tested pipeline sounds alarming in isolation but isn't: `pipeline.
review`'s own selection rule is "lowest margin first, most borderline"
(see module docstring) -- this queue is deliberately built from the
hardest cases, not a representative sample.

**Local VLM (`Qwen2-VL-2B-Instruct`, `--prompt-mode original`, same
4-frame/640px setup) against all 78: 56/78 correct (71.8%) -- no
category clears a usable bar:**

| category | accuracy |
|---|---|
| `boundary_crossing`/enter | 18/24 (75.0%) |
| `boundary_crossing`/exit | 12/17 (70.6%) |
| `hard_cut_dip`/cut | 25/36 (69.4%) |
| `control`/kept | 1/1 (n=1, not meaningful) |
| by ground truth: downtime | 39/51 (76.5%) |
| by ground truth: real_action | 17/27 (63.0%) |

Every category with a meaningful n clusters within ~6 points of the
overall 71.8% -- no standout.

**Bidirectional sanity check, and a real, slightly counterintuitive
finding: the model's accuracy doesn't track case difficulty at all.** A
10-case stratified sample from the 71 records the pipeline already gets
*right* (the "easy" cases) scored only **4/10 (40%)** -- worse than the
71.8% on the hard disagreement set. Both `ACTIVE_SWING` and `DOWNTIME`
predictions appear, both correct and wrong, ruling out a degenerate
constant-output collapse the same way the 3-case test's bidirectional
check did. But the direction of the gap is real evidence against an
easy-sample-inflation explanation too: if anything the "hard" set scored
better than the "easy" one. The honest read is that this model's ~40-75%
accuracy band on this task is closer to noise around a middling
capability ceiling than a signal that tracks anything about how
borderline a given case is.

**Final verdict: the local-VLM avenue is CLOSED for good, confirmed
across every current disagreement category, not just at the original
full scale.** The one earlier positive signal (3/3 on the legacy
enter-type sustained-ambient category, 9-case-turned-3-case) stands as
real, not contradicted by this broader test -- but it's isolated to that
one now-thin, now-marginal category and does not generalize to
`boundary_crossing`/exit or `hard_cut_dip`, which make up the bulk of
today's real disagreement set. Don't revisit the local-VLM path again on
this project's current label set without either a materially different
model/prompting approach or new labeled data that changes the picture.

**As of tonight, every free/local downtime-reduction avenue has been
tested and closed honestly: sliding-window classification, joint audio+
visual fusion, over-determined calibration for corridor geometry, and
now the local open-weight VLM across its full real hard-case surface.**
The two paths that remain real are a paid-VLM cost-bounded check
(blocked on Anthropic account billing, not a technical blocker) and lens-
distortion correction for the corridor-geometry homography (a real
engineering task, not yet scoped). Both require real cost -- money or
engineer time -- to pursue further. Nothing more is available to try for
free right now.

**Same night, a new thread opened and closed: practice-swing/warm-up
footage getting kept as live segments -- real repro, real generalization
test, honest negative result on the main hypothesis.** User-reported real
repro: `clip_300.mkv` 0-20s, currently kept by the shipped pipeline.
Investigated from real frames first, not theory.

**1. Real repro confirmed by direct frame inspection.** Pulled frames
across 0-20s and looked: no pitcher windup/delivery, no ball, no catcher,
and -- the clearest tell -- **multiple people holding bats simultaneously
in different spots** (one bundling 2-3 bats together, a recognizable
warm-up technique), people milling near the backstop rather than in
fielding positions. Real pre-game batting-practice activity, not a live
at-bat.

**2. Real signal check: raw motion/enter_score doesn't distinguish it**
(comparable magnitude to a confirmed real at-bat elsewhere in the same
clip -- both repeatedly cross `enter_thresh=0.006`), **but plate
zone-velocity (already computed via `pipeline.fusion.
compute_zone_velocity`, currently unused on the enter side) initially
looked promising**: flat 0.000 throughout clip_300's practice stretch and
all 3 of today's confirmed sustained-ambient "no batter visible" cases
from `full_game.mkv`, vs. a clear 0.665 spike at real contact in
clip_300's own confirmed at-bat (ground truth event e2), and 7 of 8
tested real at-bat windows across the 6 target clips landing 0.33-1.11.

**3. CLOSED as a blanket enter-side gate -- three real, independent
failure modes found on generalization testing, not one:**
- **False positive:** `bc_0d12dcc4d70d` (a real, human-labeled `downtime`
  disagreement) spikes zone-velocity to 0.65 -- comparable to genuine
  contact. Real frames show why: a batter walking briskly away from the
  plate after their at-bat ended. Zone-velocity measures motion
  *magnitude* in the zone, not "is this a swing" specifically -- it isn't
  swing-selective on its own.
- **False negative, disqualifying on its own:** `clip_60`'s real,
  required taken-pitch window (`[120,127]`, ball visible, no swing) peaks
  at only **0.08** -- an order of magnitude below every swing-containing
  window checked. A strict gate here would risk cutting confirmed real
  content, directly against this project's standing no-guaranteed-
  real-play-loss rule.
- **Doesn't generalize to the general practice-swing problem:**
  `clip_60`'s own ground truth already names a `required: false`
  "possible practice swings ~174" moment -- checked its real
  zone-velocity: peaks at **0.61**, indistinguishable from genuine
  contact. Clip_300's apparent success is explained by *where* the
  practice swinging happened (spatially outside the plate zone, near the
  backstop), not by any property that generalizes to practice swings
  that happen near the plate.

**4. A narrower idea surfaced but explicitly NOT tested:** near-zero
occupancy *and* near-zero zone-velocity together, sustained over a wide
window, as corroborating evidence for the narrow "truly nothing
happening" case specifically (matches the 3 confirmed sustained-ambient
cases and clip_300's specific geometry) -- alongside the existing
occupancy check, not replacing it. Different, much smaller scope than a
blanket gate. Flagged as a possible future direction, not proposed as
ready, no cost measured.

**5. A real documentation bug found along the way, not fixed in this
pass:** `tests/ground_truth/clip_300.json`'s prose `notes` field ("one
long at-bat... from t=0 to roughly t=66") is misleading against its own
itemized `events` -- no event covers t=0-14 at all, and the first event
touching the window (`e1`, `[14,26]`) is marked `required: false` with
its own hedge ("pitches likely but no ball confirmed"). Worth a
follow-up doc correction later so the loose prose doesn't mislead someone
who doesn't check the itemized events.

**Same night, follow-up: two more real angles tested for cutting
practice-swing time, both closed -- three independent approaches
attempted tonight, all three failed for different concrete reasons.**

**1. Zone-velocity as an enter-side gate -- CLOSED.** Initially promising
on clip_300 (flat 0.000 during the practice stretch, clear spikes at real
contact). Three real, independent failure modes found on generalization
testing, any one of which is disqualifying on its own:
- **False positive:** a real, human-labeled `downtime` disagreement
  (`bc_0d12dcc4d70d`) spikes zone-velocity to 0.65 -- real frames show a
  batter walking briskly away from the plate after their at-bat ended,
  not swinging. Zone-velocity measures motion magnitude in the zone, not
  "is this a swing."
- **False negative, disqualifying under the standing no-real-play-loss
  rule:** `clip_60`'s real, required taken-pitch window (`[120,127]`,
  ball visible, no swing) peaks at only 0.08 -- an order of magnitude
  below every swing-containing window tested. A strict gate would risk
  cutting confirmed real content.
- **Doesn't generalize to the general problem:** `clip_60`'s own
  documented "possible practice swings ~174" case (`required: false`)
  spikes to 0.61 -- indistinguishable from real contact. Clip_300's
  apparent success was explained by *where* its practice swinging
  happened (outside the plate zone, near the backstop), not by any
  swing-specific property of the signal itself.

**2. Multiple-simultaneous-batting-postures via RF-DETR bat detection --
CLOSED.** Technically free: `pipeline/detection.py`'s `model.predict()`
already computes all 80 COCO classes per frame, including class 39
("baseball bat") -- the code just discards everything but `person`. But
tested directly on real frames, it failed on its own origin clip: at
clip_300's confirmed real at-bat instant (t=61), the two bats RF-DETR
detected were a bat propped in a rack against the fence (a flat,
22px-tall box, clearly not held) and an on-deck player's bat -- **not the
actual batter's bat**, which wasn't detected at all in that frame (likely
motion blur on a thin, fast-moving object). Bat counts were comparable
during the practice stretch (0-4/frame) and the real at-bat (1-3/frame)
-- real structural clutter (racked bats, on-deck players who legitimately
hold bats during real at-bats too) swamps the signal. Raw bat count does
not distinguish practice from real play on this footage.

**3. Pre-first-real-play window via `atbat_start_times` -- CLOSED.** The
existing mechanism (already shipped, used for closing play-extensions) is
built to detect a new at-bat starting after a *previous* one vacates and
the plate re-occupies -- not "first real action from a cold clip start."
Real fires computed across clip_300 and all 6 target clips, cross-checked
against ground truth, found two red flags:
- `clip_540`: first fire (44.04) lands essentially exactly at its
  required event's start (44) -- a ~0.04s margin, the same kind of
  fragile-margin pattern already documented for this clip (the e4/
  `post_pad_s` investigation earlier this project).
- **`clip_whiff1`, disqualifying on its own:** first fire (17.02) lands
  *after* the required swing-and-miss event's actual swing (14.5-14.8,
  window `[12,18]`) -- a stricter pre-fire cutting rule would cut into
  the swing itself. Root cause: this clip's batter was already
  present/settled from early on with no preceding vacancy-then-arrival
  transition for the detector to key off, exactly the structural mismatch
  expected from a tool built for intra-clip at-bat transitions, not a
  cold-start first-action detector. No 9-clip regression cost was
  measured, per standing practice of not costing an already-disqualified
  mechanism.

**Honest overall bottom line: three independent, real approaches tested
tonight, all three failed for different concrete reasons -- an imprecise
motion signal, insufficient object-detection granularity on this
footage, and a wrong-tool timing heuristic -- not for lack of effort.**
Two real, uncosted options remain if this gets revisited: pose-associated
bat-posture detection (using existing MediaPipe keypoints to confirm
active holding/swinging rather than raw bat presence) and a
purpose-built cold-start real-action detector, distinct from
`atbat_start_times`. Neither was scoped or attempted tonight.
Practice-swing/walkup cutting remains an open problem with no safe,
cheap solution found so far.

**2026-08-13: a fourth practice-swing/downtime angle tested and closed --
pitcher windup-to-release via MediaPipe pose, as a POSITIVE signal for "this
is a real at-bat." Real repro, real measurement against a verified sample,
honest negative result.** The idea: practice swings (tee work, soft toss,
self-toss, cage feeds) structurally lack a pitcher delivering to a batter,
so a detected windup (leg lift, arm circle back, stride, arm coming forward)
should positively confirm a real at-bat where the three prior closed
attempts (zone-velocity gate, raw bat-count, `atbat_start_times` cold-start)
all failed. Standalone feasibility script only
(`scripts/`-pattern, not committed to the repo -- lived in an agent
scratchpad for this investigation), no `pipeline/`/`backend/` changes.

**1. Verified sample, built from real frame inspection, not trusted labels.**
Per explicit instruction, no existing label (ground-truth `required` flag or
review-queue `label` field) was trusted without independently re-verifying
via ffmpeg-extracted contact sheets viewed frame-by-frame. Final sample:
- **19 verified real-pitch/real-action instants** (corrected from an
  initial pass's 16, then 18 -- see correction notes below): 13 sampled
  instants from 11 named `tests/ground_truth` events (`clip_300.json#e2`
  x2 instants t=59.0/60.5, `#e4` x2 instants t=120.5/123.0, `#e6` x1
  instant t=96.0; `clip_540.json#e2/e3/e4` x1 instant each;
  `clip_60.json#e4/e5/e6` x1 instant each -- `#e6` RECLASSIFIED from
  practice this session, t=171.5, see correction note below;
  `clip_foul1.json#e1` x1 instant t=10.0; `clip_whiff1.json#e1` x1
  instant t=10.0 -- 5+3+3+1+1=13) plus 6 from `training_data/reviews`
  (`bc_2e636269472c`, `bc_38346e7bd32e`, `bc_dc24666af1b5`,
  `bc_ed7054ea5521`, `hc_09b6abe617f8`, `hc_5f2905bcab76`, all `real_action`)
  -- each confirmed by eye (real swing/contact/ball-flight/fielder-reaction,
  not just a label). 13+6=19.
- **8 verified practice-swing/downtime instants**, all 8 named explicitly
  (a first draft of this entry under-enumerated this list as 7 items while
  citing "9"; `clip_60.json#e6` has since been removed entirely, see
  correction note below): `clip_300.mkv`'s known-but-unitemized 0-20s
  stretch sampled at 2 instants (t=5.0, t=10.0 -- re-pulled fresh this
  session, continuous solo swing-reset-swing cycling, no ball, no second
  player, batter walks off to retrieve balls ~17-19s), `clip_60.json#e2`
  sampled at 2 instants (t=30.0, t=40.0 -- owner-corrected `warmup_throws`,
  confirmed casual catch + practice cuts with no delivering pitcher), plus
  4 from `training_data/reviews` at 1 instant each (`bc_121f61ed3d15`,
  `bc_374fa9fd1fa4`, `hc_081647af24b4`, `hc_2677d77a90c4`, all `downtime`:
  a casual jog, a static bat-up non-swing, and two static ready-stances
  with no completed swing arc). 2+2+4=8.
- **4 additional verified real plays, `clip_base1`-`clip_base4.json#e1`,
  EXCLUDED from the quantitative pass**: real, confirmed plays (batter
  loads/swings, contact/ball-flight implied, fielders converge, defense
  resolves) but shot on a different camera framing that excludes the mound
  area from the shot entirely -- 0/4 show any pitcher pixel presence
  anywhere in their full event windows. Counted directly toward the
  occlusion finding below, not toward the confusion breakdown (no candidate
  box is possible by construction).
Each case is tagged above by its exact source file/event id per instruction.
Given the effort already spent, this is the largest sample that could be
genuinely hand-verified this session -- not padded to a round number.

**Correction, caught on owner review before commit:** a first draft of this
entry cited `clip_foul1.json#e1`/`clip_whiff1.json#e1` as part of the
measured 16-case real sample, but the feasibility script's actual
`REAL_CASES` list never included either clip -- they were verified as real
events in the labeling pass but never run through the pose measurement,
so citing them as contributing to the "16"/"8/16 (50%)" figures was wrong.
Separately, the practice-swing/downtime list named only 7 distinct sources
while citing "9," because `clip_300`'s stretch and `clip_60#e2` were each
sampled at 2 instants without saying so. Both are fixed here by actually
adding `clip_foul1`/`clip_whiff1` to the real measurement (re-verified via
fresh RF-DETR box checks at t=10.0 in both clips, confirming a mound-region
candidate is present) and re-running the full script rather than just
editing the prose -- see the updated 18/9 counts and all downstream
percentages below, which reflect the real re-run, not the original numbers
with new labels pasted on.

**Second correction, found later (2026-08-14) while building a separate
audio-only investigation, caught before that investigation reused this
data, not by re-auditing this entry directly.** `clip_60.json#e6` (t=174.0)
had been verified for THIS entry using only its mound-region candidate
crop (`neg_clip_60_t174.0.png`) -- a tight zoom built for checking pitcher
pose visibility, which structurally excludes the batter, the rest of the
field, and any ball. Judged from that crop alone, the scene reads as calm
and ambiguous, which is how it got resolved to "practice" here. Pulling
the FULL frame for the first time (prompted by an unrelated finding: a
suspiciously periodic ~0.45s-spaced audio pulse train in this exact
window, which turned out to be unrelated background noise, not bat
contact -- see the standalone audio investigation) shows: at t=171.5-173.5
the batter is loaded with a COMPLETE DEFENSE positioned around the
infield/outfield (pitcher, infielders, outfielders all visible); at
t=174.0-175.0 the ball is visible in flight above the infield, the pitcher
is running, and the batter is running toward first base. This is a real,
live at-bat with contact and a baserunning sequence, not a practice swing
-- the earlier "resolved TRUE" call was wrong, caused by verifying a
real/practice judgment from a crop that was never built to show that
context. **Fixed for real, not just in prose**: moved this case from
`NEG_CASES` to `REAL_CASES` at a windup-phase instant (t=171.5, chosen the
same way every other real case was -- a few seconds before contact,
confirmed via fresh full-frame pulls showing the loaded batter and set
defense) and re-ran the complete script. Every number below reflects that
real re-run.

**Every downstream number, before -> after this second correction** (both
numbers are real script output, not estimates): total real instants 18 ->
**19**; total negative instants 9 -> **8**; total instants overall
unchanged at 27 (one case moved between groups, none added or removed
overall); real-side pose-hit rate 8/18 (44.4%) -> **8/19 (42.1%)** (the
reclassified case itself scored pose=no, so the successful-pose count
stayed at 8 while the denominator grew); negative-side pose-hit rate 6/9
(66.7%) -> **6/8 (75.0%)** (removing a pose=no case from the negative
group leaves the same 6 successes over a smaller base); real-side box
heights 56-110px mean 87.5px -> **56-110px mean 87.6px** (range unchanged,
the new case's own 90px height sits inside it); the real-vs-negative gap
widens from 22.3 points (66.7-44.4) to **32.9 points (75.0-42.1)**; the
three-consecutive-success estimate drops from ~8.8% (roughly 1-in-11) to
**~7.5% (roughly 1-in-13)** at the corrected real-side rate.

**2. Mound-region geometry had to be independently re-derived and checked,
not trusted from an unrelated prior session's hypothesis.** No pitcher/mound
calibration exists anywhere in this repo (checked `pipeline/calibration.py`
and every `*.calibration.json` -- only home/1st/2nd/3rd base). An unrelated
prior session's scratch script hypothesized `MOUND_X=(920,1100)
MOUND_Y=(480,650)` (eyeballed, not calibrated); one specific reuse attempt of
it this session (`c60_e4_pitcher_zoom` crop) produced a crop of pure sky and
net -- direct evidence it can't be reused blindly. Instead: printed every
real RF-DETR person box at known real-pitch instants and checked which one's
x-coordinate matched the batter's own in-zone box x (both stand on the
home-to-mound line, which projects to a near-vertical line in this camera).
Concrete match found: `clip_300` t=59, batter box center x=1055, and a
box at (1056, 566) sits almost exactly above it -- the strongest evidence
available that this is really the pitcher, not an unrelated bystander. Final
region used, `MOUND_X=(900,1150) MOUND_Y=(485,660)`, ended up close to the
external hypothesis but only after this independent check, not on trust.

**3. Measured occlusion/undetectability rate: real, not assumed.** Using
`pipeline.detection.detect_persons` (cached for `clip_300`/`540`/`60`/
`foul1`/`whiff1`) plus direct RF-DETR calls for `full_game.mkv` instants (no
existing cache; a full 1fps scan of a ~68-minute file just to answer a
handful of single-instant questions was avoided), a mound-region candidate
box was found at every one of 19/19 real-pitch instants (was 18/18 before
the `clip_60#e6` correction above) and every one of 8/8 practice-swing
instants (was 9/9) --
**box presence alone is common in BOTH classes and not discriminating**,
because other players (fielders warming up, ball-shaggers, the next batter's
on-deck partner) routinely stand or walk through the mound area during both
real pitches and downtime. Box heights across the 19 real cases: 56-110px,
mean 87.6px (was 87.5px across 18) -- small but not zero, better than the worst-case fear from the
model card's "further than 14 feet" out-of-scope warning
(`pipeline/pose.py`'s own docstring), though still well below a comfortable
margin. Adding `clip_base1`-`4`'s 4/4 zero-pitcher-visibility instants to
the picture: **the pitcher is either genuinely absent from frame or reduced
to a marginal ~90px box, across every verified real-pitch case checked.**

**4. Built and tested a real detector -- closed, doesn't discriminate.**
Ran `pipeline.pose.build_landmarker` on the mound-region candidate's own
padded crop (reusing `_padded_crop_bounds`, the same construction
`pipeline/pose.py` already uses for the batter) at each of the 27 verified
instants (19 real + 8 negative, same 27 total as before the `clip_60#e6`
correction -- one case moved groups, none added or removed), and checked
whether MediaPipe returned any pose at all, plus mean visibility across the
10 windup-relevant landmarks (shoulders/elbows/wrists/hips/knees -- not the
full 33, which includes ankles/feet routinely and legitimately cropped
off).
- **Real-pitch cases: pose returned in 8/19 (42.1%)** (was 8/18, 44.4%,
  before the `clip_60#e6` correction -- the reclassified case itself
  scored pose=no, so the successful count held at 8 while the base grew,
  pulling the rate down slightly). (`clip_foul1`/`clip_whiff1`, added in
  the first correction, also both returned no pose -- every correction
  applied to this entry so far has made the real-side hit rate lower, not
  higher, strengthening rather than weakening the finding below.)
- **Practice-swing/downtime cases: pose returned in 6/8 (75.0%)** (was
  6/9, 66.7%) **-- HIGHER, not lower, than the real-pitch rate, and by a
  wider margin after this correction than before it.** Root cause,
  confirmed by visually comparing saved crops side by side: a static, idle
  person standing in the mound area (shagging balls, waiting) gives
  MediaPipe an easier, cleaner single-frame target than a pitcher's own
  fast, blurring windup motion. A real-pitch crop
  (`real_full_game_t3149.4.png`) and a downtime crop
  (`neg_full_game_t1485.0.png`, `neg_full_game_t261.0.png`) show visually
  indistinguishable small, static, ready-stance figures -- there is no
  single-frame visual property this pass found that tells the classes
  apart. Only actual multi-frame windup motion (arm-circle amplitude/
  timing, mirroring how `pipeline.pose.wrist_displacement` already measures
  swing motion for the batter) could in principle discriminate, and that
  needs several consecutive successful-pose frames in a row -- which the
  measured 42.1-75.0% single-frame hit rate (was 44.4-66.7%) makes a
  materially harder bar than it sounds (three consecutive real-pitch
  successes, even under a favorable independence assumption at the
  corrected real 42.1% rate, lands around 7.5%, roughly 1-in-13 -- was
  ~8.8%, roughly 1-in-11, before this correction).
- **Failure-mode crops examined directly** (`real_clip_300_t120.5.png`,
  `real_clip_60_t121.0.png`, `real_clip_540_t81.0.png`,
  `real_clip_300_t96.0.png`): failures correlate with small box size
  (<~75px) and this footage's frequent evening backlighting producing
  near-silhouette figures -- both real, structural properties of this
  camera/footage, not an artifact of the test script.

**5. Bottom line: does NOT cross the bar to pursue further, same tier as
the three already-closed attempts, for a genuinely new reason this time --
not an imprecision or a wrong-tool mismatch, but the opposite of the needed
correlation.** Zone-velocity failed because it wasn't swing-selective; raw
bat-count failed because clutter swamped it; `atbat_start_times` failed
because it's the wrong tool for a cold start. This one fails because
single-frame pose success in the mound region is *more* likely during
downtime than during a real windup -- a heuristic built naively on top of
"pose found here" would be anti-correlated with the thing it's trying to
detect, not just noisy. Not directly comparable to the 71.8% local-VLM
accuracy figure elsewhere in this log (different problem: VLM measures
whole-window real/downtime classification broadly, this measures one
component signal's raw availability), but worth noting neither free/local
path (VLM prompting, this pose approach) has cleared a usable bar for this
specific problem. A real multi-frame windup-motion detector, built on top of
this pass's mound-region localization, remains a real, uncosted option if
this gets revisited -- but the 42.1-75.0% single-frame reliability found
here means it starts from a real headwind, not a clean slate, and would need
real engineering investment (temporal tracking, occlusion handling across
consecutive frames) to even reach the point of being testable. Practice-
swing/walkup cutting remains an open problem; four independent free
approaches now closed.

**2026-08-13: a fifth angle, embedding-level (early/representation)
audio+visual fusion, tested and closed -- real prototype, real numbers,
honest negative, with one flagged caveat rather than a clean win.**
Scoped after correcting an initial framing: no "late fusion" (combining
independent classifiers' final decision outputs) has ever existed in this
project to distinguish from. What's actually closed is fusion of
HAND-CRAFTED SCALAR features -- pose peak wrist-displacement + audio onset
rise-time, naive z-score sum (AUC 0.531) and an L2-logistic joint
classifier over motion+pose+audio (cross-val AUC 0.191, below chance,
n=10 overfitting). The real, previously-untried question: does fusing raw
learned REPRESENTATIONS (model embeddings, not hand-picked scalars) do any
better, and does it beat X-CLIP's own zero-shot text-prompt score (AUC
0.690, p=0.012 -- the best result anywhere in this log)?

**1. Architecture, concretely.** X-CLIP's raw video embedding already
exists in this codebase, one line away from what `pipeline.xclip.
swing_probability` already computes: `model.get_video_features(...)
.pooler_output`, L2-normalized, 512-d (the exact tensor the zero-shot
text-similarity is computed from, just stopped one step earlier, before
the text comparison). No audio embedding model existed in `pipeline/` --
`pipeline.audio` is a hand-crafted onset-rise-time feature, not a learned
representation. Built fresh: AST (`MIT/ast-finetuned-audioset-10-10-0.4593`,
BSD-3-Clause, already license-verified in this project for the closed
crowd-reaction work), pooled CLS output, 768-d, L2-normalized, fed a
2-second audio window matching X-CLIP's own validated `window_s`
convention (same real time-window for both modalities, not an arbitrary
mismatch). Fusion method: **concatenation** (1280-d) -- the only
architecturally sound choice at this sample size. Cross-attention was
considered and explicitly rejected, not just left undiscussed: a trained
cross-attention layer needs real trainable parameters (thousands, even at
a modest hidden dim) fit from data, and this project's own joint
classifier already showed a 3-DIMENSIONAL hand-crafted linear model
collapsing to below-chance performance (0.191 AUC) from severe overfitting
at n=10 positives -- a cross-attention mechanism at 1280 input dimensions
would be a strictly worse version of the exact failure mode already
documented, not a new idea worth building untested.

**2. Feasibility: real, confirmed by actually running it, not assumed.**
Both models load and run locally in this project's existing venv, no paid
API calls. X-CLIP was already cached from prior work; AST downloaded
cleanly (86.19M params measured directly via `sum(p.numel() ...)` on the
loaded `ASTModel` -- close to, not identical to, the log's earlier-cited
86.6M for `ASTForAudioClassification`, the ~0.4M gap consistent with
loading the base model without its classification head). A live smoke
test (real audio window from
`clip_60.mkv`, real AST forward pass) confirmed the embedding shapes and
pipeline before committing to a full run. Total wall-clock for the full
181-instance run on this Mac (M4, MPS): **521 seconds (~8.7 minutes)** --
genuinely prototypable locally in well under an hour, no infrastructure
blocker.

**3. Real prototype, tested on the SAME real sample the 0.690 baseline
was measured against -- not a stale citation, re-derived fresh from
source ground truth every run.** Reused `scripts/pose_audio_validation.py`'s
own `load_real_events()`/`load_ambient_samples()` directly (11 real
swing-type ground-truth events across the 9 reference clips, 170 real
gap-sampled ambient instances, mechanically recomputed from
`tests/ground_truth/*.json` each execution) -- this is the exact
construction `pipeline.xclip`'s own docstring cites as what the 0.690
number was measured against, so evaluating the new fusion prototype
against it is a fair, apples-to-apples comparison, not a different
question dressed up as the same one. Every one of the 11 real events
produced both embeddings successfully (confirmed: 0 no-video-embedding, 0
no-audio-embedding on the real side). Paired set used for evaluation: 11
real, 167 of 170 ambient -- the script did not separately log which 3
ambient instances dropped or why, so that specific cause is NOT claimed
here (a real gap in this run's own logging, not a claim to make up after
the fact); it doesn't affect the real-side numbers or the conclusion
below, since all 11 real cases are intact.

**4. Results: fusion never beats video alone, at any setting tried, and
never beats the 0.690 baseline. One number nominally exceeds 0.690 but
is flagged, not claimed, as a near-certain artifact.**

| method | video alone | audio (AST) alone | concatenated |
|---|---|---|---|
| nearest-centroid (LOO, parameter-free) | 0.579 | 0.285 | 0.327 |
| LOO logistic regression, C=0.001 (strong reg.) | 0.557 | 0.194 | 0.371 |
| LOO logistic regression, C=0.01 | 0.698 | 0.269 | 0.453 |
| LOO logistic regression, C=0.1 | 0.753 | 0.292 | 0.474 |
| LOO logistic regression, C=1.0 (weak reg.) | 0.762 | 0.311 | 0.491 |

(Nearest-centroid excludes each scored point from its own class centroid,
same LOO discipline as the trained classifier, so a real event can't
inflate its own reference point. Video-alone's 0.579 nearest-centroid
result is close to -- not identical to, different LOO discipline applied
this time -- the 0.587 X-CLIP-alone raw-embedding probe already on record,
a real consistency check, not a re-derivation of the same exact number.)

**Audio (AST) is consistently, not marginally, BELOW CHANCE** -- 0.194 to
0.311 across every method and regularization strength, never once
crossing 0.5. This isn't noise scattered around chance the way the
hand-crafted audio feature was (0.523, essentially exactly 0.5); this is
a real, repeated below-chance pattern across two structurally different
evaluation methods (one with zero trainable parameters). Directly
consistent with, and a natural extension of, the already-closed
crowd-reaction finding: AST detected essentially no audible
crowd/reaction signal anywhere in this footage (max probability 0.016
across 181 windows, closed elsewhere in this log) -- this is small
rec-league footage, and a general AudioSet-pretrained representation
apparently has nothing swing-relevant to key on in it, so folding it into
a fused vector adds anti-signal, not signal. **Concatenated fusion is
worse than video-alone at every single comparable setting** (0.327 vs
0.579 nearest-centroid; 0.371-0.491 vs 0.557-0.762 across all four C
values) -- audio actively drags the fused representation down, not just
failing to help.

**The one number that nominally beats 0.690 -- video-alone LOO logistic
regression at C=1.0, 0.762 -- is flagged as a near-certain small-sample
artifact, not reported as a win.** 512 input dimensions against 11 real
positives is deep in the classic n-much-less-than-d regime where a linear
classifier can often separate almost any small sample close to perfectly
by chance alone, and LOO evaluation in this regime is known to read
optimistic for exactly this reason (each fold trains on n-1 points in a
space with far more dimensions than examples). This is the same
"textbook signature of severe overfitting instability" this log already
named for the hand-crafted joint classifier (0.191 AUC at n=10, 3
dimensions) -- same instability class, just manifesting as an inflated
number instead of a collapsed one because the feature space here is
~170x higher-dimensional. Trusting this number over the already-validated
0.690 zero-shot result, on 11 positive examples, would repeat exactly the
mistake this project's standing Tier 3 bar (300-500 labeled events) exists
to prevent.

**5. Bottom line: closed, same ultimate conclusion as the hand-crafted
fusion attempt, for the same root cause, at a different representation
level.** Whether fusing hand-crafted scalars (z-score sum 0.531, joint
logistic 0.191) or raw pretrained embeddings (nearest-centroid 0.327,
LOO-logistic 0.371-0.491), audio consistently fails to add value on this
project's footage, for a concrete, mechanistically-understood reason
established independently in the crowd-reaction investigation: there
isn't a strong, general-purpose-model-legible acoustic signal in this
specific small rec-league recording setup for either a hand-crafted
attack-sharpness feature or a large pretrained AudioSet representation to
find. Video-only remains the strongest real result on record --
X-CLIP's zero-shot text-prompt score, AUC 0.690, p=0.012, unbeaten by
anything tried here or before. Not directly comparable to the pose-based
windup-to-release result above (different modality pair, different
question), but the same honest-negative category: real effort, real
prototype, real numbers, no forced positive spin. A future audio angle
would need either fundamentally different footage (broadcast-quality
audio with real crowd/commentary, per the golf-highlights paper already
checked and ruled out for this footage) or a much larger real label set
to responsibly evaluate a high-dimensional fused classifier without
falling into the same overfitting trap flagged above -- not a next step
this project's own current data supports.

**2026-08-17: a sixth angle, acoustic bat-crack signature detection
(contact-transient sound, audio-only, no visual signal), tested and
closed -- but for a genuinely different, more fundamental reason than
either prior audio result tonight, not a repeat of "no usable signal."**
Distinct from both closed audio results earlier tonight (AST
crowd-reaction AUC 0.414, AST raw-embedding AUC 0.194-0.311, both
explained by "no audible crowd/reaction sound on this small rec-league
footage") -- this asked about the contact transient itself, a genuinely
different, untested question that isn't automatically disproven by
either prior result.

**1. Real-contact sample: 9 instances, independently verified, both
visually and acoustically -- and one real methodology trap caught along
the way.** Every ground-truth/review-queue event with CONFIRMED contact
(not just "real" broadly -- excludes taken pitches like `clip_60#e4` and
misses like `clip_whiff1#e1`, which have no contact to analyze; also
excludes `clip_60#e6` per direct instruction, now correctly classified
as real but not reused here): `clip_540#e2/e3/e4`, `clip_60#e5`,
`clip_base1-4#e1`, `clip_foul1#e1`. For each, the documented contact
window was searched for a real audio transient, THEN checked against
frames -- not the reverse. This caught a real methodology trap on
`clip_foul1`: the LOUDEST peak in its wider event window (t=16.40) is
crowd/vocal reaction to the foul ball (frame-confirmed: batter already
walking away by then), not the bat-crack -- the real contact transient
is a much smaller peak at t=15.49, inside the documented 15.25-15.5s
window. Every one of the 9 final instants was frame-checked and shows a
real, sharp transient 6.9x-33x above the local noise floor.

**2. Practice-contact sample: n=1, a real and informative limitation,
not a shortcut around the sample-building work.** Systematic peak search
(not cherry-picked) across `clip_300`'s entire known 0-20s practice
stretch, cross-checked against dense (5-8fps) frame sequences, found
exactly ONE instant with visually-confirmed swing motion (bat sweeping
across the body, t=13.5-14.6) -- and even that swing's own audio
transient (2.9x above floor) is far weaker than any of the 9 confirmed
real contacts. Every OTHER strong audio peak in the practice stretch
(including two nearly as strong as real contacts, 17.4x and 12.7x at
t=10.73/18.18) was checked against frames and corresponds to something
else -- people standing relatively still, a person crouching to pick up
a bat (matching the documented "walks off to retrieve balls ~17-19s"),
not a swing. Four additional `training_data/reviews` "downtime"
candidates were also checked (not exhaustively -- 4 of 106 unchecked
ones) and showed walking or throwing, not batting. **The honest
conclusion this points to: this footage's practice-swing activity
appears to be predominantly dry/no-ball swings, not tee or self-toss
hitting** -- there may simply not be much confirmable practice-CONTACT
sound on this footage to detect, a data-availability finding, not a
signal-quality one.

**3. Environmental audio quality: real, measured, not the dominant
blocker here.** Per-clip noise-floor stability (1s-block RMS across each
full file): median floor 0.034-0.049 across 5 clips checked, but real
variability in how stable that floor is -- coefficient of variation
0.26 (`clip_base3`, calm) to 0.75 (`clip_60`, noisier/more variable).
Real, non-trivial environmental noise exists, but it did not block this
test the way the task's step 3 worried it might: the 9 real-contact
transients all cleared 6.9x+ above their local floor regardless of which
clip's baseline noise level they sat in. The actual blocker was upstream
of audio quality -- finding enough confirmable practice-CONTACT events
to test against in the first place.

**4. Feature comparison: peak amplitude, attack sharpness (rise time),
and a new frequency-domain feature (high-frequency energy ratio, not
tried in any prior audio investigation tonight) -- honest result, no
forced AUC.**

| feature | real (n=9) | practice (n=1) | separates? |
|---|---|---|---|
| peak amplitude | 0.143-0.486, mean 0.289 | 0.098 | below real min, but n=1 -- not a real finding |
| attack rise time (ms) | 5-55, mean 16.1 | 40 | inside real range -- no |
| HF energy ratio (>2kHz) | 0.101-0.637, mean 0.454 | 0.325 | inside real range -- no |

With only one practice example, no AUC or precision/recall is honestly
computable -- reported as exactly that rather than forced. What IS real
and worth noting: the confirmed real-contact class itself spans almost
the entire plausible range on 2 of 3 features (rise time 5-55ms is an
11x spread; HF ratio 0.10-0.64 is a 6x spread) -- different clips, camera
distances, and contact types (clean hit vs. foul tip vs. different bat/
ball combinations) produce real bat-cracks that don't share one tight
acoustic signature even among themselves. That internal spread is real
evidence that even a larger practice sample would likely need many more
than a handful of examples to separate cleanly from a real class this
heterogeneous -- echoing the same small-sample-instability lesson this
project's joint classifier and X-CLIP-fusion investigations already
demonstrated, before ever reaching the real-vs-practice question.

**5. Bottom line: closed, but as a genuinely different result from
tonight's other two audio closures, not a repeat.** The crowd-reaction
and raw-embedding tests failed because the target signal doesn't exist
in this footage at a well-populated sample. This one hits a more
fundamental wall first: **the practice-contact class barely exists as
confirmable data on this footage** -- not enough dry-swing-vs-real-hit
contrast to even reach a well-powered signal-quality test. No guaranteed
real-play loss risk either way, since nothing here was wired into any
cutting decision. A real future attempt at this specific angle would
need footage that actually contains batting-practice-with-a-ball
(tee work, soft toss) in enough volume to build a real sample -- not
achievable by searching harder within this project's current reference
clips and review-queue data, which is a real, checked limit, not an
assumption.

**2026-08-27: walkup time investigated as its own problem category,
deliberately scoped apart from the 11 closed practice-swing
investigations -- those ask "is this real play or FAKE/practice play,"
this asks "is this real-but-uneventful time between one real play ending
and the next batter's first meaningful pitch." Decomposed into two
structurally distinct sub-problems on real, hand-verified footage: one
new signal that works (guarded), one that reproduces the same closed
wall from a different angle.** Went in explicitly not assuming either
sub-problem inherited the earlier investigations' failure mode --
verified from scratch.

**1. Real definition, from frame-by-frame review, not trusted labels.**
Pulled three real inter-play gaps (`clip_300` ~t79-97, `clip_540`
~t89-174, `clip_60` ~t125-170) and reviewed them via ffmpeg contact
sheets (1fps, and higher-fps/full-resolution single frames wherever a
sheet read was ambiguous) rather than trusting `tests/ground_truth`'s
own window boundaries for anything finer than "which play." Two
structurally different real things showed up:
- **Type A -- batter physically approaching the plate.** Cleanest
  instance: `clip_60` t~147-165 -- plate reads empty 8+ continuous
  seconds (near-zero zone velocity throughout), then a real person
  visibly walks in from off-frame (confirmed on the contact sheet),
  registering as a genuine 0.75/0.46 box-heights/sec velocity spike
  (`pipeline.fusion.compute_zone_velocity`) immediately before settling
  occupied at t=165.16.
- **Type B -- batter already established at the plate, loading/waiting.**
  `clip_300` t~79-96 (new batter's ready-stance/load motions before the
  real swing at 97), `clip_60` t~127-137 (SAME batter, between an
  already-required pitch and the next required swing), `clip_540`
  t~158-174 (established since ~148, load motions before contact
  ~183-184).
A third, related-but-distinct thing was found and deliberately NOT
folded into either category: general field milling with **nobody at the
plate at all** (`clip_540` t~95-119, t~138-149 -- confirmed via contact
sheet, players scattered around the infield, no one in the batter's
box). Real, uneventful, but doesn't match "batter approaching" or
"batter loading" -- flagged as its own separate open question, not
assumed to share either sub-problem's answer.

**2. Current pipeline behavior, confirmed by actually running it, not
assumed from reading the code.** Walkup has no dedicated handling
anywhere today. It leaks into kept output through three existing
mechanisms, none built for it: raw hysteresis + the 3.0s `merge_gap_s`
silently welds walkup motion onto the adjacent play's own raw segment
before extension logic ever runs (directly confirmed: `clip_540`'s real
required play `[79,91]` raw-extends to 110.84 with zero extension logic
involved yet -- matches this same document's own "RAW motion segment
already covers 30-64% of a given gap" walk-up-gap note above); Stage 3
extension/padding, built for a play's genuine tail, has no way to tell
"real aftermath" from "next batter's walkup already starting"; and
walkup motion (a load motion, an approach step) independently crosses
`enter_thresh` and opens its own short kept segment, entirely apart from
extension (`clip_300`'s 92.59-94.57s segment, `clip_60`'s
133.99-145.35s segment -- this one MERGES the walkup blip and the real
swing into one raw segment, non-separable by any enter-side timing fix).
`apply_hard_cuts`, the one existing safety net, only removes near-total
silence (`HardCutConfig.quiet_thresh=0.002`, far below `enter_thresh`'s
0.006) -- moderate walkup motion (a step, a load) clears that trivially,
so hard-cut barely touches this. The already-closed enter-side
ambient-motion-discount investigation is a different question (a
generic motion discount across the whole enter side, closed because
nothing separates real slow play from ambient milling *in general*) --
this investigation is narrower and asks something that idea never tried:
whether the already-shipped, already-validated zone-occupancy/velocity
machinery (proven on the *exit* side, Stage 11 tier 1) works as an
*enter*-side gate instead.

**3. Signals tested against 7 hand-verified instances (3 confirmed
Type-A/walkup-only opens, 4 confirmed required-event opens) -- two real
negatives, reported honestly rather than discarded, one real positive
that needed a second iteration to be safe.**
- **Time-since-occupancy-established (`since_rise`)**: does NOT separate
  them. Walkup-only instances measured 18.6-20.9s; two of the four real
  required-event opens measured HIGHER (32.0s, 25.2s) than every
  walkup-only instance. Honest negative, not assumed away.
- **Zone-velocity spike magnitude alone**: also fails, and in the
  counter-intuitive direction -- real swing opens averaged LOWER zone
  velocity (0.03 bh/s) than walkup-only opens (0.08 bh/s), because a
  swing barely moves the batter's own box centroid while a load/stance
  shift or a step does more. `compute_zone_velocity` was built to catch
  arrivals, not swings, and behaves exactly that way here.
- **Occupancy state-transition, order-sensitive (vacant -> arrival-spike
  -> settled), reusing `compute_occupancy`/`compute_zone_velocity` and
  the already-shipped `zone_arrival_thresh=0.20`**: this is the one
  signal that works -- but the first, naive version of it is UNSAFE, a
  real near-miss caught before it went anywhere near being proposed as
  safe. A forward-looking rule ("segment opens while occupancy reads
  False, and occupancy becomes True again within 10s -> gate the open to
  that settle time") delays `clip_300`'s REQUIRED hit-and-run event
  (e6) from its real open at 98.22s to 107.11s -- 8.88s past the
  required window's own start at t=95. Root cause: at t=98.22 the plate
  reads vacant not because nobody has arrived, but because the
  *previous* batter just sprinted off after contact (a departure, not a
  pre-arrival vacancy) -- occupancy alone can't tell those apart without
  also checking the trend immediately before it. Fixed with a
  **recent-departure guard**: refuse to gate if a real
  occupied-then-vacant transition, preceded by a velocity spike at or
  above `zone_arrival_thresh`, happened within the last N seconds.
  Real, checked bracket for N on the 3 reference clips: must be >=3.13s
  to protect `clip_300`'s e6, and <5.86s to keep `clip_60`'s own two
  real gate opportunities -- landed on N=5.0s, a real but thin ~2.7s
  margin derived from exactly two bracketing data points, not a swept
  or robustly validated constant. At N=5.0s: 4 real gates fire across
  the 3 reference clips, 15.27s total savings, **zero safety violations
  checked against all 9 required events across the 3 clips** (the
  earlier unguarded version's violation does not reproduce).

**4. Type B (established-batter load/practice motion) independently
reproduces the same wall the 11 closed content-classification
investigations already hit -- reached from a completely different
(timing/gating) angle, not a repeat of the same test.** Neither
signal tested (occupancy duration, zone velocity) separates a real
pitch/swing from a load/practice motion once the batter is already
established in the box -- every candidate scalar overlapped in both
directions on the verified sample (point 3 above). Worth stating
plainly since it was tested fresh rather than assumed: X-CLIP, pose,
audio, and a local VLM all failed to classify practice-swing *content*;
this failed to find any *timing* signal either, on the same underlying
footage, independently.

**5. Full-game cross-validation, guarded gate only (N=5.0s), spot-checked
by hand since `full_game.mkv` has no ground truth.** Detection had to
be fully re-run (not reused from the existing cache) because the recent
`DetectionConfig.model_variant` default flip invalidated the prior
cache entries -- real wall-clock cost, ~2000s (~33 min) of RF-DETR
inference over the full 67.5-minute game, run in the background.
232 raw segments total; the guarded rule fired **34 real gates**, and
the recent-departure guard correctly blocked 4 more that would otherwise
have been gated; **136.37s (~2.27 min) total savings**. Against the
already-shipped full-game numbers (53.19 min kept / 14.31 min cut, see
README), this would move kept time down ~4.3% and cut time up ~16%
relative, if shipped as-is at this exact threshold.

Three instances (spanning early/mid/late in the game: t~286-295,
t~1545-1552, t~3400-3408) were hand-verified via frame extraction rather
than trusted from the numbers alone. Two were unambiguous on a 1fps
contact sheet (a player walking in from off-frame and settling into the
box, plate empty beforehand, no ball or swing visible in the gated
sliver). The third (t~1545-1552) looked genuinely concerning on the
1fps sheet -- bat-like motions and what read as a possible throwing
release inside the gated window. Pulled full-resolution single frames
at the ambiguous instants and resolved it cleanly: a person standing
near the backstop fence, OUTSIDE the plate area, tossing a ball back --
this project's own already-established `warmup_throws` category (see
`clip_300#e5`, `clip_60#e2`), not a live pitch -- while the actual
batter stood outside the box the entire gated window, bat down at their
side. Confirmed safe, but only after the closer look; the 1fps read
alone would not have been enough to trust it.

**6. Explicitly NOT YET SHIPPABLE -- flagging this now rather than
letting the full-game numbers above read as a green light.** The N=5.0s
guard threshold is bracketed by exactly two reference-clip data points,
a first real result, not a swept or robustly validated constant --
this project's own standard (every other threshold in this document,
`enter_thresh`, `exit_thresh`, both padding ceilings, the zone-velocity
thresholds) went through a real sweep and the full 9-clip
`scripts/regression.py` gate before shipping, and this margin was
already shown to be thin once (a 2.7s bracket from n=2). This
investigation's own scripts were never committed to `pipeline/` or
`backend/` -- they lived in an agent scratchpad only, same convention as
the pitcher-windup investigation above. No cutting decision anywhere in
this project currently depends on any signal from this entry.

**7. Product judgment surfaced, not decided here.** Type A's savings
come from delaying/potentially cutting the batter's real, visible WALK
to the plate -- not noise, not silence. Whether a highlight video should
keep or cut that walk is a real call: this project's own stated
definition of dead time argues for cutting it (it isn't itself the
play), but it's real, visible activity some viewers may want as
scene-setting, and the per-instance savings are small (single-digit
seconds to ~9s per instance). This is the same kind of tradeoff the
project's owner has made explicitly elsewhere (see this document's
Priority rule section) -- surfaced for an explicit decision, not assumed
either way here.

**Honest bottom line: one new, real, safely-guardable-but-thin signal
found (Type A) and cross-validated on real full-length footage, not
just the reference clips; one sub-problem (Type B) confirmed as a
genuine dead end from a fresh angle, not assumed. No guaranteed
real-play loss was found in the mechanism that shipped promise, checked
against all 9 required events on the reference clips it was tested
against and hand-verified on 3 real full-game instances -- but a
concrete near-miss WAS found along the way (the unguarded version) and
is reported here rather than hidden, exactly the kind of danger this
project's standing process exists to catch before anything goes near
production.**

**2026-08-27 follow-up: the Type-A guard threshold (N, recent-departure
guard) swept across the full 9-clip reference set, same rigor as every
other threshold sweep in this document -- the safety question is now
resolved with real margin, not the thin 2-datapoint bracket the entry
above shipped with.** The entry above explicitly flagged N=5.0 as NOT
YET SHIPPABLE on exactly this basis; this closes that gap.

**1. Real safe bracket, all 9 clips.** Swept N=1.0-15.0 at 0.5s
resolution, then fine-swept the boundary at 0.05s resolution. Result:
**N=4.15-6.0s is safe, zero violations against any required event on
any of the 9 reference clips, 7 gates / 26.57s total savings flat
across that entire range** -- there is no cost anywhere in this window
to picking a larger N within it. Above 6.0s savings start eroding (6.5s
drops to 22.81s/6 gates; 7.0s+ drops to and holds at 20.52s/5 gates
through 15.0s) for zero additional safety benefit, so nothing above
~6.0s is worth choosing. N=5.0, the value already logged above, sits
inside this confirmed plateau -- not merely carried over from the
earlier 3-clip estimate, independently reconfirmed against all 9.

**2. Correction to the earlier bracket estimate.** The entry above
reported "N must be >=3.13s" from manual arithmetic on the raw
departure/open timestamps (`clip_300`'s e6 case). Running the actual
guard implementation at fine resolution shows the real measured minimum
is **4.15s**, not 3.13s -- the earlier number undersold the true
constraint. The exact knife-edge between 4.10 (unsafe) and 4.15 (safe)
is itself an artifact of RF-DETR's ~1Hz detection sampling grid (the
departure sample that trips the guard sits right at the lookback
window's boundary at that exact N) -- flagged as thin/sampling-bound,
not a robust physical margin, the same honesty standard this document
applies to every other margin derivation (`enter_thresh`,
`clip_540`'s e4 real-margin investigation, etc.).

**3. Three new gates, on clips outside the original 3-clip sample,
frame-verified rather than trusted from the automated check alone.**
At N=5.0: `clip_base1` [2.71-7.40]->5.00 (2.29s -- frames show the
plate area empty, only a distant fielder, well before the required
event at t=8); `clip_base2` [0.00-3.23]->4.00 (4.00s -- a bystander at
the cage and a second figure walking toward the plate, no bat/ball
action, required event at t=11); `clip_foul1` [0.00-3.23]->5.00 (5.00s
-- batter still settling into position, no pitch visible, required
event at t=12). All three read as genuine walkup on direct frame
inspection, matching what the automated required-window check already
showed. `clip_300`, `clip_base3`, `clip_base4`, and `clip_whiff1`
produce zero gates at N=5.0 -- nothing to verify there.

**4. `full_game.mkv` numbers unchanged.** N=5.0 was already inside the
now-confirmed 4.15-6.0s safe/max-savings bracket, so the full-game run
already reported above (232 raw segments, 34 real gates, 4 correctly
guard-blocked, 136.37s total savings) remains valid at the
newly-validated threshold -- re-running at the same N would only
reproduce it, so it wasn't re-run.

**5. Explicit status: the safety question is resolved, integration is
still open.** The specific hard-rule question -- does N=5.0 ever risk a
real required event, on any clip -- is now answered NO, with real
margin (a 1.85s-wide safe plateau, not a razor edge), checked against
all 9 reference clips rather than 3. This is a materially different
result from the entry above's own explicit "not yet shippable" flag,
and does not join the closed investigations. But "shippable" in the
full sense is still open: this was validated via a standalone script
re-implementing only the required-event-overlap criterion, not the
actual `scripts/regression.py` suite, which also checks continuity and
at-bat fire/no-fire expectations this gate has never been run against
-- because it isn't wired into `pipeline/segments.py` or
`pipeline/run.py` at all. The real next step, if this gets picked back
up, is wiring the gate into the actual pipeline and running the full
regression harness, not just this standalone safety check.

**2026-08-27 closing entry: the Type-A walkup gate wired into production
(`pipeline/segments.py`, `pipeline/run.py`), full `scripts/regression.py`
suite run for real across all 9 clips, real `full_game.mkv` before/after
-- ALL PASS, shippable.** Closes the "integration/shippability still
open" gap the entry above left explicitly flagged.

**1. Wiring, same pattern as `apply_hard_cuts`.** `WalkupGateConfig`/
`apply_walkup_gate()` added to `pipeline/segments.py`; called in
`pipeline/run.py` right after raw segment formation (`scores_to_segments`
on the real, boosted+debounced `enter_scores`) and before veto -- so a
gated (delayed) open is padded/extended around its real, later start,
not the pre-arrival raw one. Reuses the plate occupancy and zone-velocity
arrays `process_video` already computes for the enter-side debounce and
Stage 11 tier 1 (`compute_zone_velocity` was previously called twice for
the same plate zone at two different points in the function; now
computed once and reused, a small real cleanup that fell out of this
work). Shipped default `departure_guard_s=5.0` -- the value validated
across all 9 reference clips in the entry above. Manifest entries get
`origin="walkup_gate"` (`pipeline/manifest.py`'s `_spans_with_gaps`
extended to check both `hard_cut_windows` and the new
`walkup_gate_windows`, hard_cut checked first as a tie-break for the
unobserved-in-practice case of a cut span overlapping both). A real gap
caught while wiring this, not assumed away: `hard_cut_boundary_starts_by_file`
(the function `pipeline.stitch.merge_overlapping_spans` uses to know
which cut boundaries are too deliberately-short to silently re-bridge)
only checked `origin=="hard_cut"` -- a walkup-gate window is exactly as
short by design and exactly as vulnerable to the same re-bridging bug,
so this now checks both origins. Kept the function's original name
(not renamed) since `pipeline.stitch` and its own tests already
reference it by that name and both origins need identical protection
for the identical reason.

`process_video`'s return tuple grew a 6th element
(`walkup_gate_windows`), threaded through all 3 real callers
(`scripts/detect.py`, `scripts/detect_multi.py`,
`backend/pipeline_runner.py`) and into `scripts/regression.py`, which
reimplements the pipeline's stages inline rather than calling
`process_video` (deliberately, so it can mirror production exactly --
see that script's own comments on the scale boost and occupancy
debounce) -- without mirroring the gate there too, the regression suite
would have silently kept testing the pre-gate pipeline and "ALL PASS"
would have meant nothing about this specific mechanism.

**2. Real `scripts/regression.py`, all 9 clips: ALL PASS -- every check
it runs, not just the required-event-overlap criterion the standalone
sweep validated.** Recall unchanged at 3/3, 3/3, 3/3, and 1/1 on the
six single-play clips (100% throughout, same as before this shipped);
every `check_continuity` window contiguously covered, zero coverage
gaps; every at-bat fire/no-fire expectation held; the hard-cut exclusion
mechanism clean; **stitch decode clean on all 9 real stitched outputs**.
The gate itself fired on 5 of the 9 clips -- `clip_540` (1 window,
4.90s), `clip_60` (3 windows, 8.30s), `clip_base1` (1 window, 2.29s),
`clip_base2` (1 window, 3.44s), `clip_foul1` (1 window, 3.23s), 22.16s
combined -- and stayed correctly silent on `clip_300`, the one clip
with the near-miss case that motivated the departure guard in the first
place. Real numbers, not a repeat of the standalone sweep's own count:
same 7 gates in the same 5 clips as the standalone check found, but
smaller total savings (22.16s here vs. 26.57s standalone) -- the real,
production `enter_scores` (boost + debounce applied) shifts raw open
times slightly earlier than the standalone script's unboosted
reimplementation, leaving less genuinely-vacant time before some opens
for the gate to trim. Same direction and same underlying explanation as
finding 3 below on `full_game.mkv`, at a smaller scale here.

**3. Real `full_game.mkv`, run twice for a true apples-to-apples
comparison -- gate on, and gate monkeypatched off, identical code
otherwise -- rather than trusting a possibly-stale README baseline.**

| | kept | cut |
|---|---|---|
| gate off (true baseline) | 50.82 min | 16.67 min |
| gate on (shipped default) | 47.51 min | 19.99 min |

**36 gates, 118.99s direct savings.** A real, explained difference from
the earlier standalone figure (34 gates, 136.37s): that number came
from a standalone script that never applied the enter-side scale boost
or occupancy debounce production actually uses before raw segments
form -- the same class of discrepancy as finding 2 above, just larger
here since `full_game.mkv` is real long-form footage with more varied
camera-distance/occupancy conditions than the short reference clips.
The wired run is the methodologically correct one. All 3 previously
frame-verified-safe instances from the earlier entry are still present
and nearly unchanged (2 of 3 identical to the decimal, e.g.
`1544.98-1551.55` and `3399.85-3407.15`; the third differs by 0.11s).
Two tiny new gates appeared that weren't in the earlier list (1.04s and
0.25s) -- the larger one frame-verified fresh: a clean batter walking
in from off-frame and picking up a bat, no ball or pitch visible in the
gated window, same pattern as every other verified instance.

The total kept-time delta (198.94s) exceeds the direct gate savings
(118.99s) by ~80s -- traced to real, already-validated downstream
effects, not new or unaccounted-for risk: dynamic pre-padding
(`pipeline.refine`'s shrink-only mechanism) shrinks further around a
gated segment's new, genuinely-quieter start than it would have around
the old, earlier one, and `apply_hard_cuts` finds fewer quiet dips left
to trim inside a kept segment once the walkup gate has already removed
some of that material upstream (206 hard-cut windows with the gate off,
189 with it on). Exactly the interaction the 9-clip regression suite
above exercises directly and passed clean on -- not a new mechanism,
the existing ones just have less material to act on.

**4. Edit-Log restorability confirmed on real data, not just synthetic
test fixtures.** Built an actual manifest from `clip_540.mkv`'s real
walkup-gate window (`(141.60, 146.50)`, from a genuine production run):
confirmed via `pipeline.manifest.kept_spans()` that the span is excluded
while cut and correctly included after `set_status(..., "kept")`. Also
added a real backend integration test
(`test_walkup_gate_windows_from_process_video_reach_the_manifest_as_origin`)
that exercises the identical restore flow through the actual FastAPI
PATCH endpoint, not just the pure manifest functions -- both pass.

**5. Test coverage added:** 9 new unit tests directly on
`WalkupGateConfig`/`apply_walkup_gate` (defaults, the genuine-arrival
delay case, the whole-segment-dropped case, the already-occupied
no-op, the departure-guard block, the departure-guard expiring outside
its own window, no-settle-within-lookahead, a stale-detection-sample
no-op, and multiple segments gated independently against their own
lookahead windows -- one of these caught a real mistake in the test's
own first-draft expectation, not the code: `lookahead_s` correctly
capped a distant gate opportunity the test wrongly expected to fire,
fixed by correcting the test, not the implementation), 8 manifest-layer
tests mirroring the existing `hard_cut_windows` suite (including one new
test the mirrored suite never needed: `hard_cut` and `walkup_gate`
windows coexisting in the same manifest without interfering), and the
backend wiring+restore test above. **501 tests pass** (up from 466 at
the start of tonight's session).

**6. No guaranteed real-play loss -- held at every step, including full
integration.** No required-event miss, no continuity gap, no
exclusion-mechanism bug, anywhere in the 9-clip suite, with the gate
now genuinely exercised by that suite rather than a standalone
reimplementation of one safety property. **Shippable** -- the specific
gap the previous entry's own "not yet shippable" flag named is closed.

**2026-08-27: catcher return-throws investigated as a new problem
category, deliberately scoped apart from every closed content-
classification investigation -- closer in kind to walkup Type A (a
distinct spatial/motion pattern) than to the practice-swing/audio/
embedding work. Closed: not a viable cutting category on this footage,
for a structural reason distinct from both the closed content-
classification wall and walkup Type A's shippable result.**

**1. Real definition, hand-verified via frame inspection, not trusted
labels.** One clean instance found via fine-grained (4fps) frame review:
`clip_60`, right after the required pitch e4 (ball arrives ~124, taken,
no swing) -- the catcher becomes visible and makes a clear, sharp
throwing motion in **t~126.5-128.0, ~1.5s total**. That's the real
duration -- far shorter than walkup's 2-30+ second range, over almost
as soon as it starts. `clip_540`'s ~t89-95 window (already examined
during the walkup investigation) shows a related pattern -- a fielder
throwing back toward the mound -- but it's the tail of a real defensive
play resolving, not an isolated between-pitch return; kept as a second,
structurally different real instance rather than folded into the first.
Cross-checked two more real, independently-documented pitch instants
directly in `full_game.mkv` (the exact global timestamps `clip_foul1`/
`clip_whiff1`'s own ground-truth notes cite, ~411s and ~495s): the
second shows the ball clearly visible in the catcher's glove, but the
actual throw-back is too fast to land cleanly even at 3fps -- consistent
with the same brevity found in `clip_60`, not a contradicting data
point.

**2. Current pipeline behavior, checked directly, not assumed.** For
the clean `clip_60` instance, pulled the real per-sample motion score
during the throw window: it never exceeds **0.00017** -- three orders
of magnitude below `enter_thresh` (0.006), below `exit_thresh` too. This
time is already fully cut as ordinary gap time: no raw segment ever
opens there, so hard-cut has nothing to trim and the walkup gate (which
only ever delays a raw segment's *open*) has nothing to act on either.
For the `clip_540` case, the opposite: embedded inside one continuous
raw segment covering the real required play, already kept, untouched by
hard-cut (real sustained motion there, not the near-total silence
`HardCutConfig.quiet_thresh` requires).

**3-4. Two candidate signals tested, both hit real, distinct walls --
neither a repeat of "no signal exists," each diagnosed to a specific,
different cause.**
- **Detectable throw trajectory (motion/ball):** tested directly against
  real spatial grid data (`pipeline.motion`'s 16x9 per-block grid) during
  the verified throw. Frame-score ~0 throughout; no coherent
  catcher-toward-pitcher directional streak in the grid either -- a few
  low-amplitude, flickering cells, not a trajectory. Not a
  sensitivity-tuning gap: the throw is essentially invisible to
  frame-differencing at this camera's distance/contrast/speed.
- **Catcher-position occupancy (walkup Type A's own signal, mirrored to
  the opposite end of the throw):** pulled real RF-DETR boxes during the
  same window. Only one detected box ever falls inside the calibrated
  plate zone (280px radius) -- almost certainly the batter; every other
  nearby box sits 300-410px away, just outside it. No catcher-specific
  zone exists anywhere in this project's calibration data model (only
  plate + first/second/third base) -- a real missing-infrastructure gap,
  not a failed signal or an untried idea.

n=2 verified instances (plus the two full_game.mkv qualitative
cross-checks) -- explicitly too small for a real accuracy number,
reported as exactly that rather than forced, the same discipline the
closed acoustic-bat-crack investigation applied at its own n=1.

**5. No guaranteed real-play loss -- not applicable in the way it was
for walkup, since nothing found here actually opens a cuttable window
to threat-model in the first place.** The one real risk (cutting into
the `clip_540`-style embedded case) is avoided by construction: that
time is already inseparable from a required event's own kept segment,
same as it would be under any mechanism.

**Honest bottom line: a different kind of wall from both the closed
content-classification investigations and from walkup Type A's
shippable result.** Not "no signal exists to classify real vs. fake"
(the closed wall) and not "a real, guardable timing signal exists"
(walkup Type A) -- structural: when genuinely isolated, the event is
too brief and low-contrast to ever register as kept content in the
first place, so there is nothing to gain by cutting it; when connected
to real play, it's inseparable from required content. **Closed.**

**2026-08-27: offense/defense transition time (between half-innings, or
after the third out, before the next half-inning's action begins)
investigated on `full_game.mkv` -- the only real source for this
pattern, since no reference clip spans multiple half-innings. Went in
genuinely open on whether this overlaps with the shipped walkup Type A
mechanism or is structurally distinct. Confirmed: substantially
overlaps, with one genuinely distinct piece left deliberately
untouched, and no new mechanism found worth building.**

**1. Real definition, hand frame-verified, not inferred from gap size
alone.** Found the largest gaps across all 313 gaps in `full_game.mkv`'s
final kept segments, then frame-verified the top 3 by hand rather than
trusting duration as a proxy for "real transition." The clearest
confirmed instance (t~1545-1663, ~26-27.7 min into the game) shows a
real three-phase structure: a **head** (~1549-1614, ~65s) of the last
play's aftermath -- players visibly gathering and talking near the
plate, sustained but modest motion; a **core** (~1614-1658, ~44s) that
is genuinely quiet, sparse, mostly-static field; and a **tail**
(~1658-1664, ~6s) of the next batter arriving, matching walkup Type A's
own pattern. Two other candidates (~44s and ~40s cores elsewhere in the
game) show the same three-phase shape, noisier.

**2-3. Current behavior and walkup-gate overlap, traced exactly, not
assumed.** The **core** (the genuinely quiet ~44s) is already fully
cut: real motion score there drops to 0.00077-0.00096 mean per 5s
bucket, the same "already below `enter_thresh`, no segment ever opens"
pattern the closed catcher-throw investigation found -- nothing to
gain. The **tail** is already covered by the shipped walkup Type-A
gate: traced the exact mechanics -- raw open at 1655.72 gated to
1660.66, dynamic pre-padding (full 2.8s ceiling, unshrunk) pulls the
final kept start back to 1657.86, a real, quantified **4.94s savings**
matching the gate's own reported window exactly. This confirms Type A
already generalizes to this context -- it was validated on isolated
at-bat walkups, but the same vacant-to-settled plate-zone pattern fires
correctly here too, with no changes needed. The **head** (~65s of
post-play gathering/dispersing) is currently kept in full (one trivial
0.46s hard-cut aside) -- structurally distinct from walkup's own
pattern: multiple people already present and dispersing/regrouping, not
one person arriving at an empty zone. Left untouched deliberately: real
sustained motion here (max scores 0.014-0.038 in 5s buckets, well above
the hard-cut quiet threshold) means a naive new threshold would risk
cutting into legitimate last-play/next-play adjacent content -- the
same class of risk `WalkupGateConfig`'s departure guard exists to
prevent on the other side of an at-bat.

**4. New signal tested: field-wide person-occupancy count, not scoped
to the plate zone.** Real and cheap to check directly (cached
detections, no recompute needed). In the cleanest instance, total
detected person count drops from a sustained 12-17 down to 6-10 almost
exactly during the core quiet window, recovering as the next play sets
up -- a real, measurable "field clearing" effect. Two honest problems
found before it went any further: **non-additive** -- the low-count
window is the same window already cut by motion threshold, so even the
best case identifies no time beyond what's already excluded, a
confirmatory signal, not a new lever; and **non-reproducible** -- checked
against the other two candidate transitions, where person count dips
only modestly (12->7.6) and partially recovers mid-gap rather than
staying low throughout, too noisy to threshold safely as an independent
mechanism. Not pursued further.

**5-6. No guaranteed real-play loss.** No new mechanism is proposed, so
no new risk introduced. The one ambiguous region (the head) is left
untouched on purpose rather than forced into a cut.

**Honest bottom line: this category substantially overlaps with
already-shipped mechanisms.** Not a forced negative -- a real
confirmation that walkup Type A generalizes beyond the isolated-at-bat
case it was validated on, plus one genuinely distinct sub-pattern (the
head) correctly identified and correctly left alone rather than forced
into a new mechanism it doesn't safely fit. No new mechanism needed.

**2026-08-27: non-contact pitches -- a real pitch that's thrown, taken
or missed, no swing or no contact, no play -- investigated as the
fourth and final original observation. Went in without assuming this
was Type B under a different name; confirmed on its own terms it
precisely dissolves into three already-answered questions, not a new
category.**

**1. Real definition -- two different things get conflated under "non-
contact pitch."** Hand-verified `clip_60#e4` (t=120-127, "ball visible
... no swing") and `clip_300#e4` (t=122-129, same type) via the frame/
motion data already gathered earlier tonight. A non-contact pitch is
genuinely two different targets: **the pitch itself** (ball thrown,
taken or swung-and-missed) -- brief, 2-7s -- and, per this project's own
ground truth, explicitly required content in both cases; and **the dead
time around it** -- the catcher's return throw immediately after, and
the batter's wait before the next pitch. Only the second is plausibly
"downtime"; the two are not the same target and answering for one says
nothing about the other.

**2. Current pipeline behavior -- already a legitimate keep, not an
oversight.** Confirmed directly: `clip_60#e4`'s required window is
fully covered by a kept segment (`119.4-126.6`, from tonight's wired
`scripts/regression.py` run) -- recall 3/3, unbroken. Not unflagged or
accidentally kept -- kept because ground truth says it must be.

**3-4. Tested whether any signal distinguishes a "skippable no-action
pitch" from a "required pitch" -- reproduced the existing wall
directly on a fresh comparison set, not just cited it.** Real peak
motion scores across six required events spanning every outcome type:

| event | outcome | peak smoothed score |
|---|---|---|
| `clip_60#e4` | taken pitch, no swing | 0.0149 |
| `clip_300#e4` | taken pitch, no swing | 0.0070 |
| `clip_whiff1#e1` | swing and miss | 0.0114 |
| `clip_foul1#e1` | **contact** (foul) | **0.0067** |
| `clip_540#e2` | real hit | 0.0211 |

The foul ball -- real bat-ball contact -- scores LOWER than both no-
contact taken pitches and the swing-and-miss. No amplitude ordering
separates "had contact" from "no contact" at all. A fresh, direct
reconfirmation of this project's own founding v1 scope decision (README:
"no camera/mic signal reliably separates a hit from a miss here") --
not a coincidental repeat, an independently-constructed comparison set
landing on the same wall.

**The dead-time framing decomposes entirely into tonight's other two
closures, using the same connecting event.** Using `clip_60#e4` as the
thread: the catcher's return throw right after (t~126.5-128) is the
exact instance already characterized in tonight's catcher-throw
investigation -- motion score ~0.00017, already fully cut, closed. The
batter's wait before the next pitch (t~128-137, same clip) is the exact
instance already characterized in tonight's walkup investigation as
Type B -- no signal distinguishes a real pitch-taking pause from load/
practice motion once the batter is established. Every piece of "the
dead time around a non-contact pitch" maps onto a category already
closed tonight, on the same real footage, not an assumed resemblance.

**5. No guaranteed real-play loss.** No cutting mechanism is proposed,
so no new risk. The one real danger this observation named --
conflating "no contact" with "safe to cut" -- never gets a chance to
bite, because no signal was found that could make that call in the
first place.

**Honest bottom line: not a fourth category, a precise dissolution into
answers already found.** The pitch itself is required, correctly kept,
and provably indistinguishable-by-outcome; the dead time around it is
tonight's catcher-throw closure plus walkup Type B, not a new pattern.

**All four of the night's original observations are now resolved:**
walkup time (shipped, `pipeline/segments.py`'s `WalkupGateConfig`),
catcher return-throws (closed, structural), offense/defense transitions
(closed, already covered by the shipped walkup gate plus correctly-
untouched real content), non-contact pitches (closed, dissolves into
the above plus this project's founding scope limit).


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
  `frontend/src/components/CalibrateStep.tsx`.

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
  a top-level Home / Edit Log nav (`App.tsx`), independent of Home's own
  linear stage machine: upload → click to calibrate → (order
  confirmation, only if needed) → progress → player + download.
  Switching to Edit Log and back doesn't reset in-progress upload state,
  since it's a separate `view` toggle, not a stage. No pipeline or
  business logic here either — `src/api.ts` is a thin fetch wrapper, one
  function per backend endpoint.

  **Edit Log** (`EditLogView.tsx`) — lists every segment detection ever
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
  (`localStorage`); on load, `App.tsx` always re-fetches the batch's
  actual job status from the server and resumes whatever stage that
  implies, rather than assuming a fresh session — durable server-side
  job state (see Backend API, above) is what makes this possible at all.

  **Coordinate scaling for click-to-calibrate**, since this is the one
  most likely to fail silently: the browser displays `preview.jpg` at
  whatever size fits the layout, not its native resolution.
  `CalibrateStep.tsx` reads the loaded `<img>`'s `naturalWidth`/
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


## Deployment

Two independent pieces, deployed separately: the **backend** (FastAPI +
the full ML pipeline) as a Docker container with a persistent volume,
and the **frontend** (`frontend/dist/`, a static build) on any static
host. They talk over plain HTTP with CORS, not a shared origin — real,
not hypothetical: verified locally the same way as everything else in
this project (`docker build`, `docker run`, a real end-to-end demo run
through the running container, real CORS preflight checks against both
an allowed and a disallowed origin — see the real numbers below).

### Backend: build and run

```sh
docker build -t findmy-highlights-backend .

docker volume create fmh-data   # real user data + the detection cache
docker run -d --name fmh-backend -p 8420:8420 \
  -v fmh-data:/data \
  -e FMH_CORS_ORIGINS="https://your-frontend-domain.example.com" \
  findmy-highlights-backend
```

`/data` inside the container is where `FMH_UPLOADS_ROOT` and
`FMH_DETECTION_CACHE_DIR` already point (baked in as image `ENV`
defaults, see `Dockerfile`) — mounting a volume there is what makes
uploads, manifests, and `output.mp4` survive a container restart or
redeploy. Skipping the `-v` flag still works for a quick throwaway demo
(the container's own writable layer stands in), it just means every
restart starts from zero: no past uploads, no warm detection cache.

**What's baked into the image at build time, and why:** RF-DETR's
Apache-2.0 pretrained weights (~355 MB) are downloaded and warmed up
during `docker build`, not on a visitor's first real request — the whole
point of demo mode is a fast, reliable first impression, and a cold
multi-hundred-MB download on an unknown network at demo time is exactly
the kind of invisible risk that would undermine that. The bundled demo
clip and its precomputed detection cache (see `backend/demo.py` and
Current Status's demo-mode writeup) are baked in the same way. Real
measured build time on this machine: ~3 minutes total (pip installs
~45s, RF-DETR download+warmup ~2 min, image export ~45s). Real image
size: **4.66 GB** — the ML dependency stack (torch, torchvision,
mediapipe, transformers, opencv, RF-DETR's weights) dominates this;
`torch`/`torchvision` are installed from PyTorch's CPU-only wheel index
specifically to avoid pulling in several extra GB of unused CUDA
runtime libraries on what's expected to be CPU-only hosting.

**Real resource numbers, measured, not estimated:** idle container
memory ~150 MB. A real detect job (RF-DETR inference) needs ~1 GB RSS
and drives CPU hard while it runs (see this doc's single-job-at-a-time
design elsewhere) — size the host accordingly; 2 GB RAM / 1-2 vCPU is a
reasonable floor for a demo deployment handling one job at a time, more
if concurrent real uploads (not just the demo) are expected. No GPU
required or used. Disk: the image itself (4.66 GB) plus whatever the
mounted volume accumulates from real uploads (source videos run
100s of MB to low GB each) and `output.mp4` exports.

### Backend: environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FMH_UPLOADS_ROOT` | `./uploads` | Real user data: batch uploads, job state, manifests, `output.mp4`. Point this (via the image's own `ENV` default, `/data/uploads`) at a mounted persistent volume. |
| `FMH_DETECTION_CACHE_DIR` | `./.cache/detections` | RF-DETR result cache, keyed by file mtime+size+config. Safe to lose (just means re-detecting), nice to persist for repeat runs on the same file. |
| `FMH_CORS_ORIGINS` | *(unset — no cross-origin access at all)* | Comma-separated allowed origins for the deployed frontend, e.g. `https://findmy-highlights.vercel.app`. **Required** whenever frontend and backend are on different domains — unset is safe-by-default, not a bug, but the app won't work cross-origin until this is set. Deliberately never defaults to `*`: this API accepts real file uploads. |
| `FMH_TRAINING_DATA_DIR` | *(unset — review queue off)* | Opts real detect jobs into the Tier 1 review-queue instrumentation (see Current Status's transfer-learning writeup). An explicit choice, not a default-on for a public deployment — see "Open decisions" below. |
| `FMH_POSE_MODEL_PATH` | `./.cache/models/pose_landmarker_full.task` | Only read if `FMH_TRAINING_DATA_DIR` is set. Not auto-downloaded (Setup step 6) and not baked into the Docker image (not needed for the core pipeline or demo mode) — must be mounted onto the volume manually if the review queue is enabled in production. |
| `RF_HOME` | `~/.roboflow` (baked to `/data/cache/roboflow` in the image) | Where RF-DETR's own weight cache lives. Already pre-warmed into the image at build time — this only matters if you rebuild against a newer `rfdetr` pin. |
| `HF_HOME` | HuggingFace's own default (`~/.cache/huggingface`) | Only relevant if `FMH_TRAINING_DATA_DIR` is set — the review queue's X-CLIP instrumentation downloads `microsoft/xclip-base-patch32` (~600 MB) on first use. Not baked into the image; not needed for the core pipeline or demo mode. |

### Frontend: build and deploy

```sh
cd frontend
VITE_API_BASE_URL="https://your-backend-domain.example.com" npm run build
```

`VITE_API_BASE_URL` is a **build-time** substitution (Vite's own
`import.meta.env.VITE_*` mechanism, verified locally: built with a real
URL configured, grepped the output bundle, confirmed it's baked in
literally — see `frontend/src/api.ts`), not a runtime setting, so it has
to be set wherever the build itself runs (a static host's build-command
environment, e.g. Vercel/Netlify project settings — not a `.env` file
committed to the repo). Left unset, every request stays a relative path,
which is what makes local dev (`npm run dev`) work against Vite's own
proxy with zero configuration — see `frontend/vite.config.ts`.

Deploy the resulting `frontend/dist/` to any static host (Vercel,
Netlify, S3+CloudFront, etc.) — it's a plain static bundle (verified
locally with `npm run build` + `npm run preview`, no server-side
rendering or build-time API calls involved). Whatever domain it ends up
on must be added to the backend's `FMH_CORS_ORIGINS`.

### Demo mode

The "Try the demo" button on the upload screen (`backend/demo.py`, see
Current Status for the full design writeup) runs a real ~45-second
bundled clip through the complete real pipeline — no upload, no
calibration step, the exact same `run_detect_then_export_job` code path
a real upload takes. The only thing pre-computed is the RF-DETR
detection pass itself; everything else (motion, fusion, segment
detection, hard-cut, ffmpeg export) runs live. Real, measured end-to-end
time: **6 seconds** on this dev machine, **11 seconds** through the
built Docker container on this machine's Docker Desktop VM — both
comfortably under the "well under a minute" target. Demo runs never opt
into the review queue regardless of `FMH_TRAINING_DATA_DIR` (see
`backend/app.py`'s `/demo/run`) — the same fixed clip every time would
only ever mine exact duplicate candidates.

### Decisions made

- **Backend: GCP Compute Engine (a plain VM running the Docker image
  directly, not GKE/Cloud Run).** **Frontend: Vercel.** **Backend stays
  warm** — a Compute Engine VM is always-on by construction (nothing to
  configure; there's no scale-to-zero mode to accidentally leave on).
  **Review queue disabled on the public deployment**
  (`FMH_TRAINING_DATA_DIR` unset on the VM; keep it set locally, same
  `.env` as always). **No custom domain purchase** — see the TLS note
  below for what that decision actually implies once the backend moved
  off a PaaS host.

### GCP Compute Engine (backend)

Switching from Railway to a raw Compute Engine VM removes every
Railway-specific assumption the Dockerfile had — re-verified directly,
not just reasoned about: rebuilt the image and ran it with **no `PORT`
env var set at all** (confirmed via `docker exec ... env`, no `PORT`
present) and it still served correctly on 8420. The `${PORT:-8420}`
fallback in `CMD` is harmless and kept (works for either case with zero
changes), but nothing here requires it anymore. `railway.json` removed
from the repo — it was Railway-specific config-as-code with no GCP
equivalent, and leaving it in place would have been misleading dead
config for a target no longer in use.

**A real, load-bearing gap the platform switch introduces, not a minor
detail:** Railway and Vercel both provide automatic managed TLS on their
own subdomains. A bare Compute Engine VM provides neither — it's just an
IP address. Vercel always serves the frontend over HTTPS, and a browser
will block an HTTPS page from calling a plain-HTTP backend (mixed-
content blocking) — so **without TLS on the GCP side, the deployed app
would not actually work**, not just "be insecure." This doesn't require
buying a custom domain, though: a free IP-to-hostname service
([sslip.io](https://sslip.io), e.g. `34-123-45-67.sslip.io` for external
IP `34.123.45.67`, no signup) plus [Caddy](https://caddyserver.com) as a
reverse proxy (automatic Let's Encrypt certs, ~5 lines of config) gets
real TLS with zero domain purchase, consistent with "no custom domain
needed right now."

**1. Create the VM:**

```sh
gcloud compute instances create fmh-backend \
  --machine-type=e2-medium \
  --image-family=debian-12 \
  --image-project=debian-cloud \
  --zone=us-central1-a \
  --tags=fmh-backend \
  --boot-disk-size=20GB
```

**2. Firewall — open only what's needed, targeted by the instance tag
above (not applied VPC-wide):**

```sh
# app port (or skip this and use only 80/443 once Caddy is in front of it)
gcloud compute firewall-rules create fmh-allow-app \
  --direction=INGRESS --action=ALLOW --rules=tcp:8420 \
  --source-ranges=0.0.0.0/0 --target-tags=fmh-backend

# HTTP/HTTPS, needed for Caddy's Let's Encrypt handshake + real traffic
gcloud compute firewall-rules create fmh-allow-web \
  --direction=INGRESS --action=ALLOW --rules=tcp:80,tcp:443 \
  --source-ranges=0.0.0.0/0 --target-tags=fmh-backend
```

(SSH on 22 is open by default in a new GCP project's default network via
its pre-existing `default-allow-ssh` rule — not something this repo
needs to create.)

**3. Persistent disk for real user data** (uploads, manifests,
`output.mp4`, the detection cache — the same things `FMH_UPLOADS_ROOT`/
`FMH_DETECTION_CACHE_DIR` already point at):

```sh
gcloud compute disks create fmh-data --size=50 --zone=us-central1-a --type=pd-balanced
gcloud compute instances attach-disk fmh-backend \
  --disk=fmh-data --device-name=fmh-data --zone=us-central1-a
```

Then, SSH'd into the VM (`gcloud compute ssh fmh-backend --zone=us-central1-a`):

```sh
sudo mkfs.ext4 -F /dev/disk/by-id/google-fmh-data
sudo mkdir -p /mnt/data
sudo mount /dev/disk/by-id/google-fmh-data /mnt/data
echo '/dev/disk/by-id/google-fmh-data /mnt/data ext4 defaults,nofail 0 2' | sudo tee -a /etc/fstab
```

**4. Install Docker on the VM** (Debian 12 doesn't ship it):

```sh
curl -fsSL https://get.docker.com | sudo sh
```

**5. Run the container**, `/data` mapped onto the real persistent disk
from step 3:

```sh
sudo docker run -d --name fmh-backend --restart unless-stopped \
  -p 8420:8420 -v /mnt/data:/data \
  -e FMH_CORS_ORIGINS="https://your-vercel-app.vercel.app" \
  ghcr.io/your-org/findmy-highlights-backend:latest
```

(`--restart unless-stopped` is the real Docker-level equivalent of
"stays warm" on a bare VM — no platform-level always-on setting exists
here the way it did on Railway, this is the actual mechanism.) Building
the image directly on the VM instead of pulling from a registry works
identically — `git clone`, then `docker build` — if a registry isn't
set up yet.

**6. TLS via Caddy**, once the VM has an external IP (`gcloud compute
instances describe fmh-backend --zone=us-central1-a --format='get(networkInterfaces[0].accessConfigs[0].natIP)'`):

```sh
sudo docker run -d --name fmh-caddy --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v caddy-data:/data -v caddy-config:/config \
  caddy caddy reverse-proxy --from your-vm-ip.sslip.io --to localhost:8420
```

Then `FMH_CORS_ORIGINS` and `VITE_API_BASE_URL` both use
`https://your-vm-ip.sslip.io`, not the raw IP or port 8420 directly.

### Real memory measurement and instance sizing

Measured directly, not estimated from parameter counts (same discipline
as every other real number in this project) — peak RSS with RF-DETR,
X-CLIP, and MediaPipe pose all loaded **and run through real inference**
on the demo clip, in one process:

```
after real RF-DETR inference (full clip):        0.948 GB
after real X-CLIP inference:                      1.508 GB
after real MediaPipe pose inference:              1.595 GB
```

Two real numbers, not one, because they answer different questions:
**the actual default public deployment** (review queue off, per the
decision above) only ever loads RF-DETR — real peak **~0.95-1 GB**,
consistent with this doc's existing "~1GB RSS" figure elsewhere. **The
worst case, if the review queue were ever turned on** (X-CLIP + pose
join RF-DETR in the same process) — real measured peak **1.6 GB**. X-CLIP's
own real inference step, not just loading it, was the largest single
contributor (+560 MB) — a model that "loads small" isn't the same as one
that "runs small."

**Recommendation: `e2-medium` (2 vCPU, 4 GB RAM).** Comfortable real
margin over both numbers above (2.5-4x headroom, room for OS/Docker
daemon overhead plus a safety buffer against OOM, not a bare-minimum
fit) — `e2-small` (2 GB) would leave uncomfortably little headroom
against the 1.6 GB worst case once OS overhead is added, even though
it'd likely be fine for the RF-DETR-only default. Machine type spec:
**2 vCPU, 4 GB memory** (confirmed against GCP's own machine-type
documentation).

**Cost — honest caveat: could not independently re-verify live current
pricing just now** (`cloud.google.com/compute/vm-instances/pricing`
repeatedly returned truncated/404 responses to automated fetching in
this session) — the following are well-established approximate
figures, not freshly confirmed, and should be checked against
[the real-time calculator](https://cloud.google.com/products/calculator)
before committing:

| Item | Approx. cost (us-central1, on-demand) |
|---|---|
| `e2-medium` (2 vCPU, 4GB) | ~$0.034/hr ≈ **~$25/month** always-on |
| `e2-small` (2 vCPU, 2GB), if review queue stays permanently off | ~$0.021/hr ≈ ~$15/month |
| 50GB `pd-balanced` disk | ~$0.10/GB-month ≈ ~$5/month |
| **Total, `e2-medium` + 50GB disk** | **~$30/month**, always-on |

### Vercel (frontend)

No `vercel.json` added — Vercel's built-in Vite framework preset
auto-detects this project correctly (a `vite.config.ts` + `vite` in
`package.json`, exactly this repo's setup) with no config file needed,
and since dashboard behavior can't be exercised without a real account,
adding config here that couldn't be verified end to end seemed worse
than documenting the exact dashboard values directly:

- **Root Directory:** `frontend` (this is a subdirectory of the repo,
  not the repo root — the one setting that actually matters here).
- **Framework Preset:** Vite (auto-detected once Root Directory is set).
- **Build Command:** `npm run build` (default, verified locally).
- **Output Directory:** `dist` (default, verified locally — `npm run
  build` produces `frontend/dist/`, confirmed served correctly with
  `npm run preview`).
- **Environment Variable:** `VITE_API_BASE_URL` = the real GCP backend
  URL (the `https://your-vm-ip.sslip.io`-style Caddy-fronted URL, not
  the raw IP or port 8420 directly — see the TLS note above for why).
  Build-time, not runtime (Vite's own
  `import.meta.env.VITE_*` substitution — see this doc's Frontend
  section above) — must be set before/at the build, not added after the
  fact and expected to take effect without a rebuild.

### Still genuinely open

- **Custom domain:** decided against for now — TLS itself is handled
  (Caddy + a free `sslip.io` hostname, see above), a purchased domain
  would only replace that hostname, not add a capability that's missing.
- **Whether to ever enable the review queue on the public deployment
  later:** intentionally deferred (decided: off for now) — if that
  changes, it's `FMH_TRAINING_DATA_DIR=training_data` set on the GCP VM
  (`docker run -e ...`) plus mounting `pose_landmarker_full.task` onto
  `/mnt/data` if the pose signal is wanted too (see the env var table
  above) — and re-checking instance sizing against this doc's real 1.6GB
  worst-case measurement, since `e2-medium`'s margin would shrink.


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

- **2026-08-19: `clip_300.mkv` has been uploaded/re-run at least 5 times,
  producing duplicate review-queue data, not new information.** Batches
  `fb07ea71f4b3`, `ac262f25bf56`, `705e659881e7`, `cc8de6558105`, and
  `a2116018e6b0` all reprocess the same source file and yield the exact
  same 5 physical candidate events (2 `boundary_crossing`, 3
  `hard_cut_dip`) — `features_at_label_time` is byte-identical across
  batches, only `id`/`created_at`/`batch_id`/`config_hash` differ. One of
  these, `705e659881e7`, is separately confirmed corrupted (7,220 real
  ffmpeg decode errors, video 145.9s vs. audio/container 166.8s
  mismatch — see the stitch multi-file-mismatch fix above). If untracked
  files reappear in `training_data/reviews/` referencing `clip_300.mkv`,
  check for this duplication before assuming they're new labeled data.

- **2026-08-19: confirmed, real distinction between "cut" (genuinely
  removed on export) and `skip_suggestions` (in-app-only, survives every
  download).** Prompted by a direct question about whether cut segments
  are physically excised or just marked for the app's own player.

  **"Cut" segments are genuinely removed, verified four independent real
  ways** against a real production batch (`fb07ea71f4b3`, `clip_300.mkv`,
  12 kept + 13 cut segments): (1) a real `run_stitch()` run with every
  ffmpeg command logged shows exactly 12 EXTRACT commands, matching only
  the 12 kept spans' timestamps — zero commands reference any of the 13
  cut windows; (2) `segment_output_offsets` (the app's own real
  "where does this live in output.mp4" map) has `ABSENT` for all 13 cut
  segment ids and valid offsets for all 12 kept ones; (3) real ffprobe
  duration on the freshly-regenerated output is 147.272104s — matching
  the sum of kept spans (147.075s), not the 185.71s source, and matching
  the already-shipped `output.mp4`'s duration exactly, clean strict
  decode; (4) a real extracted frame from a kept window matches (SSIM
  0.898) the real output frame at its computed offset, while a real
  frame from inside a cut window does not (SSIM 0.567 against that same
  output frame) — visually confirmed, not just measured.

  **`skip_suggestions` (quiet stretches inside an already-kept segment)
  are a separate, deliberately non-destructive UI feature, confirmed
  never read by the export path.** `pipeline/stitch.py` never references
  `skip_suggestions`; the only consumer is
  `frontend/src/components/SkippableVideo.tsx`, which does a browser
  `currentTime` jump during in-app playback and says so directly in its
  own comment: "export/download always contains the complete output
  regardless of this component's state." Real magnitude on the same
  batch: 16.16s across 5 windows sit inside kept segments as
  skip-suggestions, against a 147s exported video (~11%) — fully present
  in any downloaded/shared file, invisible only inside this app's
  player. This is a real gap for the church-volunteer-downloads-and-
  shares use case, not a bug: the feature was built this way on purpose
  (see `pipeline.segments.SkipSuggestionConfig`'s own docstring).

- **2026-08-20: correction — a "tightened export" second pass using
  `HardCutConfig` is not new work; it already shipped.** Prompted by a
  request to build exactly this. `apply_hard_cuts()` already loops `for
  seg_a, seg_b in kept_segments` (`pipeline/segments.py`), i.e. it
  already scans each individual kept span, and is already called
  unconditionally as the literal last step of `process_video()`
  (`pipeline/run.py:209`) — the single entry point both the single-file
  and multi-file detect scripts share. There is no code path for a real
  upload that skips it.

  **Real confirmation on the same `fb07ea71f4b3` clip_300 batch used
  throughout tonight's stitch investigation:** its manifest already
  contains 6 `origin="hard_cut"` entries (only 2 were visible in an
  earlier truncated listing; the other 4 sit in the segments not shown
  then), totaling 6.408s already genuinely removed via this exact
  mechanism, already reflected in the 147.272104s output duration
  verified earlier tonight. An idempotency check — rerunning
  `apply_hard_cuts()` with the identical default `HardCutConfig()`
  against the CURRENT (already-tightened) kept spans, using freshly
  recomputed real motion data — found zero new cut windows and left the
  kept spans unchanged. Not a coincidence of this clip: `find_cut_windows`
  scans a kept span's entire motion timeline in one linear pass before
  any splitting happens, so the first (already-shipped) application
  already exhausts everything the current 0.002 threshold / 0.5s buffer
  / 1.5s merge-gap config can find. No code was written or changed —
  reimplementing an identical already-shipped pass would be redundant by
  construction, not a cautious negative result.

- **2026-08-20: real measurement pass — sweeping `HardCutConfig`'s three
  parameters independently on `clip_300` (`fb07ea71f4b3`), starting from
  the shipped baseline (threshold 0.002, min_raw_dip_s 0.5, merge_gap_s
  1.5, buffer_s 0.5 -> 6 cuts, 6.41s).** No default changed, nothing
  shipped — this is a real cost/risk measurement only, real motion data
  recomputed against the real clip, every new candidate window frame-
  checked at the same rigor as the earlier SSIM stitch verification, not
  just read off the motion score.

  **Threshold sweep (0.002 -> 0.0058, the skip_suggestion bar):** 0.003
  adds 4.53s (6 new windows), 0.004 adds 11.25s (7 new), 0.005 adds
  18.99s (10 new), 0.0058 adds 23.47s (10 new). Frame-checked all 10
  distinct new windows (22-frame contact sheet, dense-resampled the two
  largest). Real risk found starting at 0.005: window 52.74-58.41s shows
  a second player visibly walking up progressively closer to the batter
  across a 0.5s-spaced dense sample — real motion, not just a quiet
  motion score — sitting immediately before required ground-truth event
  e2's own documented window (pitch, load 59-62, ball ~63). Window
  113.22-119.83s (appears at 0.0058) is the single largest fraction-of-
  span removal in the sweep (83% of its 7.987s span); its 0.8s-spaced
  sample looked genuinely static (batter stepping out, no swing) but
  that spacing can't rule out something sub-second between samples. The
  other 8 new windows looked genuinely idle on inspection.

  **Min-dip sweep (0.5s -> 0.1s):** 0.35 adds 2.02s (1 new), 0.2 adds
  4.70s (5 new), 0.1 adds 5.89s (6 new) — smallest apparent risk of the
  three levers by raw numbers, but every new window here is a small
  fragment (<=1s) sitting inside a region already frame-checked under
  the threshold sweep above (including the flagged 52.74-58.41s walk-up
  region), so it is NOT independently safer, just a smaller slice of the
  same already-flagged risk.

  **Buffer sweep (0.5s -> 0.0s):** 0.35 -> 8.91s total, 0.2 -> 12.73s,
  0.0 -> 17.93s. This lever mostly grows the edges of the 6 already-
  shipped windows rather than finding new ones. Highest risk of the
  three: at buffer=0 the largest existing window (95.595-97.202s) grows
  to 95.09-97.70s, and that window sits inside required ground-truth
  event e6 (hit-and-run). A dense 0.5s-spaced check of 93.5-99.0s showed
  a person in active running/fielding motion through that stretch, not
  visible dead time — shrinking the buffer here erodes margin at exactly
  the point closest to a documented required real play. Recommendation
  reported, not acted on: threshold should not move past ~0.003 without
  per-window frame review; min-dip is the least-bad lever by raw
  numbers but inherits the same flagged risk, not a separate safer one;
  buffer is the lever least likely to be worth moving at all.

- **2026-08-20: NEW open item, distinct from the sweep above — the
  EXISTING shipped baseline hard-cut window on `clip_300`
  (95.595-97.202s, currently live in production, part of the same 6
  cuts the idempotency check above confirmed as already-shipped, not
  something this session introduced) sits adjacent to required
  ground-truth event e6 (hit-and-run, note: "swing/contact ~97, batter
  drops bat and runs ~98, fielding through ~102").** A handful of
  0.5s-spaced stills taken while investigating the buffer sweep's edge
  growth showed what looks like real running/fielding motion near this
  window, but that check does not meet this project's own bar for a
  real verification — it was incidental to the buffer-sweep check above,
  not a dedicated pass. This needs the same rigor as the earlier
  `clip_60#e6` ground-truth relabel correction tonight (full-frame
  context, not narrow/sparse sampling, before concluding anything about
  already-shipped production behavior) as its own dedicated follow-up.
  Not yet confirmed as a real problem — flagged as an open question,
  not a verdict.

- **2026-08-20: dedicated review closes the open item above — the
  existing shipped hard-cut (`clip_300`, 95.595-97.202s) is confirmed
  SAFE, zero real content lost.** Dense chronological check, 34 real
  frames: the disputed 95.595-97.202s window itself sampled at 0.1s
  spacing (16 frames, dense enough to catch anything sub-second), plus
  0.25s spacing through 93.5-95.595s and 97.202-99.0s margin. Every one
  of the 16 in-window frames shows the identical static scene — batter
  standing at the plate holding the bat, mid-field figure standing
  still — no swing, no contact, no bat drop, no running anywhere in the
  full 1.6s cut. The real action from ground-truth event e6 (hit-and-
  run) brackets the cut without touching it: real fielding/running
  motion resolves into stillness by ~95.0s, BEFORE the cut starts
  (95.595s); the batter's drop-and-run transition becomes visible at
  ~98.2s, roughly a full second AFTER the cut ends (97.202s). The cut
  sits in a genuine ~1.6s lull between two real moments, neither of
  which it reaches.

  **This corrects the earlier informal flag above, not just supersedes
  it.** That check used sparse 0.5s-spaced stills and, without a clear
  enough timeline, read the real pre-cut fielding motion (93.5-94.75s,
  already fully kept, never at risk) as adjacent to or inside the cut
  window — it wasn't; dense sampling resolves exactly where that motion
  starts and stops relative to the cut boundary.

  **Separate, lower-priority note:** e6's own "swing/contact ~97"
  annotation doesn't match anything visible in dense sampling of that
  region — no swing is visible anywhere from 93.5-99.0s. The note is
  explicitly approximate ("~"), so this isn't treated as a ground-truth
  error needing a fix, just flagged so future work keying off that exact
  sub-timestamp within e6 knows it's imprecise.

- **2026-08-21: full hard-cut safety verification on `full_game.mkv`
  reaches 100% coverage — zero confirmed failures, deployment target
  cleared.** Extends the clip_300/e6 review's method (dense 0.4s-spaced
  frame sampling + margin, real visual judgment, not motion score alone)
  across every hard-cut window the shipped mechanism produced on a real
  67.5-minute game recording, not just a 3-minute reference clip. Final
  tally: **207 of 207 windows checked, 172.96s of 172.96s hard-cut time
  verified (100% by both count and time)**. Done in two passes: 53
  windows / 88.49s (all 33 windows over 1.5s plus a random 20-window
  cross-section of the rest) first, then the remaining 154 windows /
  84.47s to close out full coverage.

  **Zero confirmed failures.** Two borderline cases were surfaced and
  documented explicitly rather than silently folded into "pass," same
  standard as this project uses for every ambiguous call:
  - **L12** (1194.09-1196.53s): real player movement (walking/
    repositioning) visible during the cut, no swing/contact/pitch.
  - **R132** (3503.687-3504.042s): real, sustained running motion during
    the actual cut window (not just the margin) — a person jogging along
    the sideline fence. Home plate confirmed empty throughout (no
    batter, no pitcher, no ball anywhere in frame) — no live batted-ball
    play for this motion to be baserunning from.

  Both ruled **PASS**, consistent with this project's standing
  definition of protected real play (swing/contact/pitch/baserunning
  tied to a live play, not any human motion on screen) — confirmed by
  owner review: R132 is genuinely intended dead time (sideline jogging),
  not a missed edge case the definition should be widened to catch.

  **The hard-cut mechanism (`HardCutConfig`/`apply_hard_cuts`, 0.002
  threshold / 0.5s buffer / 1.5s merge-gap, unchanged from its shipped
  defaults throughout this entire verification) is now verified safe at
  full 100% coverage on the real full-length deployment target, not just
  the 190s reference clips it was originally tuned and tested against.**

- **2026-08-22: a seventh practice-swing/downtime angle closed --
  ensemble-of-weak-signals, and X-CLIP's reliability called into
  question on independent data.** Explicitly not a repeat of
  `joint_classifier_investigation.py`'s collapse to 0.191 AUC from
  overfitting at n=10 -- that failure mode was designed around, not
  repeated with different features.

  **Real data volume, tallied and cross-checked before any modeling, not
  estimated.** Only **27 hand-verified real-vs-practice instants exist
  project-wide with rigorous common ground truth** (19 real / 8
  negative) -- the windup-pose investigation's set, built by
  independently re-verifying every case via full-frame contact sheets
  rather than trusting existing labels. Larger-sounding numbers cited
  elsewhere tonight (170 "ambient" samples, 149 review-queue records)
  are not rigorous practice-swing labels: the 170 are mechanically
  synthesized from gaps between annotated events, never human-verified;
  the review-queue negatives are generic "downtime," not specifically
  confirmed practice swings. **n=27 sits in the exact same regime that
  already caused the joint classifier's overfitting collapse at n=10** --
  per standing instruction, this rules out any trained model. Not
  attempted.

  **Three of the five nominal input signals have no reusable form.**
  Zone-velocity, bat-count, and atbat-timing were each closed as one-off
  ad-hoc investigations with no reusable committed scoring code and no
  per-instant dataset. Recomputing them under time pressure would mean
  building new, untested infrastructure from scratch -- declined, rather
  than risk repeating the count-drift self-corrections the windup-pose
  investigation itself already had to fix twice (16→18→19 reals,
  9→7→8 negatives, self-flagged in its own entry above).

  **Windup-pose excluded for a mechanistic reason, not a data gap: it is
  anti-correlated with real play** (pose detected in 75.0% of negatives
  vs. only 42.1% of reals -- the opposite of what a positive-agreement
  "both signals agree" rule needs). ANDing it in would make a
  combination worse, not better.

  **That leaves no viable second signal to combine with X-CLIP.** Ran
  the more useful real test instead: fresh X-CLIP scores computed on
  this same 27-instant hand-verified set (`pipeline.xclip.
  swing_probability`, unchanged default config) -- an independent sample
  from the one that produced the earlier 0.653 result, not the same
  cases re-measured. Full-sample AUC: **0.428, below chance** (negative-
  case mean score 0.729 higher than real-case mean 0.692). A real
  stratified 70/30 holdout (threshold chosen on the design set only,
  applied blind to 8 held-out cases) produced 75% test accuracy -- but
  every one of the 8 predictions was "real": **zero of the 2 real
  negatives in the test set were correctly identified.** The 75% figure
  is a pure base-rate artifact (design/test sets were ~68-75% real by
  construction), not real discrimination, and is reported as such rather
  than left to look like a positive result.

  **Explicit framing, not a contradiction of the earlier result:** this
  does NOT overturn the 0.653 AUC / p≈0.039 significance finding on its
  own original sample (12 real / 167 ambient) -- both are real findings
  on different samples. It does mean X-CLIP's practical reliability is
  now in real doubt beyond what `pipeline/xclip.py`'s own docstring
  already flagged (prompt-sensitivity on contact/hit-type events) --
  worth treating with real caution going forward, not as a settled
  positive result.

  **Nothing from this investigation is wired into `pipeline/`/cutting
  logic** -- no real-play-loss risk either way. Bottom line: this joins
  the six already-closed angles, not from a data-volume argument alone
  but from an actual test that failed on independent data.

- **2026-08-22: an eighth angle closed -- two targeted mitigations for
  X-CLIP's documented prompt-sensitivity, tested for real, don't hold up
  under significance testing.** Not a new signal: multi-prompt
  ensembling, temporal windowing, and their combination, applied to the
  one real positive result from angle seven's own instability finding.
  Tested on both the original 0.653 sample and the independent
  27-instant sample that scored 0.428.

  **Raw AUC numbers superficially suggest convergence toward ~0.58-0.62
  on both samples** (worse on the sample that was already good, better
  on the sample that was broken) -- **but a real 10,000-shuffle
  permutation test shows none of it clears p<0.05, on either sample, for
  any variant tested.** Single prompt, multi-prompt average, multi-prompt
  vote, temporal median, temporal vote, and the full combination were
  all tested; the best-looking candidates by raw AUC (multi-prompt vote,
  combined mean) came back at p=0.266-0.427. The apparent stabilization
  is not statistically distinguishable from noise.

  **Honest framing: this does not stabilize X-CLIP, it moves the
  instability around without resolving it.** Sample A's own baseline not
  reaching its previously-documented significance in this run (p=0.090
  here vs. the earlier full-sample p≈0.039) is noted as a likely
  artifact of the smaller ambient subsample used for computational
  practicality (40 of 170, disclosed at the time), not a contradiction
  of the earlier full-sample result.

  **Nothing from this investigation is wired into `pipeline/`/cutting
  logic** -- no real-play-loss risk either way.

**Closing summary: eight independent, real investigations into
practice-swing/downtime detection on this footage have now closed, each
with a specific, evidenced, mechanistic reason for failure** -- structural
clutter (raw bat-count), unreliable small-object detection (zone-velocity
distance sensitivity), non-differentiating motion (zone-velocity gate),
an anti-correlated pose signal (windup-to-release), no audio signal at
two representation levels (hand-crafted fusion and embedding fusion),
insufficient practice-contact data (acoustic bat-crack), overfitting risk
at the available sample sizes (ensemble-of-weak-signals), and now
confirmed statistical instability in the one apparent positive signal
(X-CLIP mitigation). This converges with the sibling stat-tracker
project's own independent conclusion: **the limitation is this footage,
not the software.** Further angles on this same data are very unlikely to
succeed without different input -- either better/closer camera footage or
a non-visual/non-audio signal source (e.g. a companion sensor).

- **2026-08-23: `HardCutConfig.quiet_thresh` threshold-raise attempts
  (0.003 and 0.004) both closed as unsafe on real evidence; the shipped
  0.002 default's full "touches required" surface is now completely
  verified clean.** Two separate real findings, not one.

  **Threshold raise closed.** 0.004 (13 hard-cut windows on clip_300,
  7 new vs. baseline, all individually frame-verified clean on
  clip_300) looked clean in isolation, but a full 9-clip regression
  sweep -- not just the clip_300 batch it was designed against --
  surfaced a new required-event overlap on `clip_foul1.mkv`: real
  batter load motion clipped immediately before a confirmed foul ball
  (contact ~15.25-15.5s). 0.003, swept the same way across all 9 clips,
  fared worse: a confirmed real violation on `clip_base2.mkv` (the
  batter shown in an active, cocked bat-load stance inside the newly-cut
  window, with the real swing beginning t≈11.2, immediately after the
  window closes), plus further real concerns on `clip_base3.mkv` (a
  tiny new window sitting inside the ground truth's documented
  "contact ~12-13, fielders converging" description) and `clip_foul1.mkv`
  (the same load-motion content flagged at 0.004, largely unchanged at
  the narrower 0.003 window). **`HardCutConfig.quiet_thresh` stays at
  0.002 -- no change shipped.** Both attempted code changes were reverted
  and confirmed via `git diff` before any of this was logged.

  **Separately, and prompted by finding these two real regressions: the
  shipped 0.002 default itself had 9 "touches required" overlaps already
  live in production, and only one (`clip_300#e6`, reviewed earlier
  tonight) had ever been individually frame-verified.** The other 8 --
  `clip_300` e4; `clip_540` e2 (two separate windows) and e4; `clip_60`
  e4, e5, and e6; `clip_base1` e1 -- were checked fresh, same dense-
  sampling rigor as every other check tonight. **All 8 verify clean.**
  Each sits in a real, static holding stance with the actual described
  action (swing, contact, ball flight, or defensive convergence)
  beginning with real measured margin after the cut window closes.

  `clip_base1#e1` needed a second pass to reach that conclusion, worth
  recording honestly rather than glossing over: an initial read, leaning
  on the ground truth note's prose timing ("bat trails down at ~9.0,
  drops to a relaxed hang... at ~9.5") plus a lower-resolution, poorly-
  framed first crop, suggested the cut window might be capturing real
  bat-lowering motion. A second, correctly-cropped and more finely
  spaced (0.15s) frame sequence resolved this cleanly: the bat is
  already raised and held in a static, unmoving cocked position well
  before the cut window starts, stays static throughout it, and is
  still in the identical held position immediately after -- poses
  matching on both sides of the cut, no motion discontinuity. The real
  swing doesn't begin until t≈10.0-10.2, roughly 0.6-0.8s after the cut
  window closes. The note's approximate prose timing didn't match the
  real frame-level evidence here; the direct frame check is what
  settled it, same standing rule this project has followed all night.

  **Closing summary: the shipped hard-cut mechanism, at its current
  0.002 threshold, is now fully verified safe across every
  "touches required" overlap on every reference clip** -- not just
  `full_game.mkv` (207/207 windows, logged earlier) and `clip_300`
  (also logged earlier), but all 9 clips' pre-existing overlaps too.
  0.002 is the real, evidence-backed safety ceiling for this mechanism
  right now: both tested alternatives (0.003, 0.004) produced confirmed
  real-content-loss risk the moment verification widened past the single
  clip either was designed and swept against.

- **2026-08-23: the 5-prompt-variant X-CLIP robustness table (open debt
  from the 2026-08-16 consolidated correction) re-verified against the
  corrected `clip_60#e6` ground truth -- closes as a 9th negative result
  in the same pattern as the eight above, on the same signal already
  held back from cutting logic.**

  **First: the sample this table actually uses, checked rather than
  assumed.** This table reuses `scripts/pose_audio_validation.py`'s
  `load_real_events()`/`load_ambient_samples()`, the same loaders the
  main X-CLIP zero-shot 0.690 -> 0.6527 correction used -- **12 real /
  167 ambient**, not 18/9. The 18 -> 19 real-count pairing belongs to a
  different investigation entirely (the windup-pose mound-occlusion
  script's `REAL_CASES`/`NEG_CASES`), and even there the negative count
  doesn't "stay 9" -- it moves 9 -> 8. `load_real_events()` reads
  `tests/ground_truth/*.json` live, so it picks up the corrected
  `clip_60#e6` label (`hit_and_run`, now inside `SWING_TYPES`) with no
  manual adjustment: real count is 12, matching the already-logged
  0.6527 result exactly, confirmed by reproducing that exact number
  below as a sanity check before trusting anything new.

  **Reconstruction, not a byte-identical rerun -- same gap as the main
  baseline's own "0.688 (this session's faithful same-methodology
  reconstruction)" line.** No script for this table was ever committed;
  only prose descriptions survive. Rebuilt from those descriptions
  (`scripts/prompt_variant_recheck.py`, now committed, closing that part
  of the debt for future reruns): a 3-way split adding "fielders
  actively making a play", a more specific idle phrasing ("standing
  around doing nothing, no action happening"), a defensive-negative pair
  ("fielders defending a play in the field"), and a broader "game in
  progress" framing. Video embeddings computed once per window and
  cached across all 5 variants, same real cost-saving property the
  original exploited.

  **Real before/after, per variant, permutation test on each (2000
  shuffles, seed 20260816, same standard as the main X-CLIP recheck --
  not assumed to carry over):**

  | variant | AUC | perm p | Mann-Whitney p |
  |---|---|---|---|
  | baseline (original pair, reproduction) | **0.6527** (matches the logged correction exactly) | 0.0445 | 0.0390 |
  | 3-way split (+ fielders making a play) | 0.6442 | 0.0500 | 0.0481 |
  | more specific idle phrasing | 0.6826 | 0.0195 | 0.0175 |
  | defensive-negative pair | 0.6717 | 0.0220 | 0.0238 |
  | swing vs. game-in-progress framing | 0.5594 | 0.2605 | 0.2472 |

  Two of four new alternates nominally score higher than the corrected
  baseline (0.6826, 0.6717 vs. 0.6527) and nominally clear p<0.05
  individually. **Neither survives Bonferroni correction across the
  4-alternate family (alpha=0.05/4=0.0125)** -- the same multiple-
  comparisons standard this project already applied to the flow/onset
  investigation earlier the same night. `more specific idle phrasing`
  comes closest (p=0.0195) and still doesn't clear it.

  **The specific structural fragility already named for this table
  reproduces almost exactly on the corrected sample.** Per-clip
  percentile-of-ambient across all 5 variants, the same six clips the
  original check used (`clip_base1`-`4`, `clip_foul1`, `clip_whiff1`):
  `clip_whiff1` (zero defensive reaction) stays at the **96th-100th
  percentile regardless of wording** (98, 96, 100, 98, 99) -- matching
  the original "96th-98th percentile" finding almost digit-for-digit.
  `clip_base3` (defensive activity visible, the original's named
  fragile case) swings from the **19th to the 77th percentile**
  depending on wording alone (19, 41, 42, 69, 77) -- wider than the
  original's 43rd-68th range, not narrower. `clip_base1`, `clip_base2`,
  and `clip_foul1` show the same real swing (e.g. `clip_base1`: 96th
  percentile on the baseline pair, 46th on the game-in-progress
  framing). The `game-in-progress` variant is the clearest mechanistic
  case: broadening the negative prompt pulls every defensive-activity
  event down near or below the ambient median while leaving
  `clip_whiff1` untouched at the 99th -- direct evidence the instability
  is tied to defensive activity in frame, not noise.

  **Honest bottom line: no variant meaningfully and reliably beats the
  corrected 0.653 baseline.** Two nominally score higher; none survive
  the same multiple-comparisons correction this project already holds
  every other multi-test result to, and the exact structural weakness
  that kept X-CLIP out of cutting logic in the first place -- unstable
  on exactly the contact/defensive plays that matter most -- is
  unchanged, if anything slightly wider on the corrected sample. This
  closes as a 9th data point in the same pattern as the eight
  investigations above and the same conclusion as the 2026-08-16
  multi-prompt-ensembling/temporal-windowing closure: prompt wording is
  a real, adjustable lever, but adjusting it doesn't stabilize the
  signal, it just moves the noise around. Nothing changes for cutting
  logic -- X-CLIP stays Tier 1 review-queue instrumentation only, same
  as before this recheck; no guaranteed real-play loss risk was ever on
  the table since nothing here touches a destructive decision.

- **2026-08-23: the X-CLIP raw-embedding nearest-centroid probe (open
  debt from the 2026-08-16 consolidated correction, originally AUC
  0.587, not significant at p~0.17) re-verified against the corrected
  `clip_60#e6` ground truth -- closes as a 10th data point, genuinely
  narrower than the other nine but still not enough to change anything.**

  **Same sample-verification discipline as the prompt-variant recheck.**
  Reused `scripts/pose_audio_validation.py`'s `load_real_events()`/
  `load_ambient_samples()` -- **12 real / 167 ambient**, matching the
  main 0.690 -> 0.6527 correction's sample exactly. Before trusting
  anything new, reproduced the zero-shot text-prompt baseline from
  these same cached video embeddings as a sanity check: **AUC 0.6527,
  exact match to the documented corrected value** -- confirms the
  embedding extraction here is methodologically identical to every
  other X-CLIP recheck tonight, not a subtly different pipeline.

  **Reconstruction, not a rerun -- no committed script existed for this
  probe either.** Rebuilt from the original prose ("leave-one-out
  nearest-centroid cosine similarity on the 512-dim pooled video
  embedding"): score = cos_sim(embedding, LOO real-centroid) -
  cos_sim(embedding, LOO ambient-centroid), each point's own class
  centroid excluding itself, raw `get_video_features()` pooler output,
  no text prompts involved at all (a genuinely different question from
  every prompt-variant number above -- this measures whether the
  embedding space separates the classes on its own, with no
  language-supervision assist). Now committed as
  `scripts/embedding_centroid_recheck.py`, closing that debt item.

  **Real before/after: AUC 0.587 -> 0.6352 (n 11/170 -> 12/167),
  permutation p 0.17 -> 0.0550, Mann-Whitney p 0.0594 (2000 shuffles,
  seed 20260816, same standard as every recheck tonight).** The number
  moved up more than any other rechecked figure this session -- the gap
  to the zero-shot baseline narrowed from 0.103 (0.587 vs. 0.690) to
  0.0175 (0.6352 vs. 0.6527) -- and it went from clearly chance-level to
  sitting right at the edge of conventional significance. **It still
  does not clear p<0.05 on its own.** Reported exactly as measured, not
  rounded down to "still nothing" or up to "now significant": this is a
  real, meaningfully closer number that still falls on the wrong side of
  the line this project holds every other signal to.

  **Honest bottom line: no change to the project's conclusion.**
  Zero-shot text-prompt scoring (0.6527, p=0.0395/0.0390, clears
  significance) remains the one real, load-bearing X-CLIP signal.
  Raw pooled embeddings, scored with no text-prompt assist at all, stay
  below it and outside conventional significance, even though the
  margin narrowed substantially on the corrected sample -- consistent
  with, not contradicting, the existing conclusion that the model's
  zero-shot video-text alignment (not the raw visual embedding space by
  itself) is where this project's one real signal actually lives.
  Nothing changes for cutting logic -- X-CLIP stays Tier 1
  instrumentation only, same as before this recheck; no guaranteed
  real-play loss risk was ever on the table since nothing here touches
  a destructive decision.

- **2026-08-23: the HFC/spectral-flux onset-detection comparison (open
  debt from the 2026-08-16 consolidated correction, originally AUC
  0.605/0.646, neither significant) re-verified against the corrected
  `clip_60#e6` ground truth -- a genuinely mixed result, not a clean
  10th/11th data point either way, reported exactly as measured.**

  **Same sample-verification discipline as every recheck tonight.**
  Reused `scripts/pose_audio_validation.py`'s `load_real_events()`/
  `load_ambient_samples()` -- 12 real / 170 ambient (audio has no
  calibration-dependent drop the way pose/X-CLIP video embedding did,
  so the ambient count stays the full 170 here, unlike the 167 the
  X-CLIP rechecks saw). Sanity check: reproduced the RMS-envelope
  rise-time audio-alone baseline from `pipeline.audio` directly --
  **AUC 0.5083, matching the documented corrected value (0.508)**
  before trusting anything new.

  **Reconstruction, not a rerun -- no committed script existed for
  either onset feature.** Rebuilt from the original prose ("HFC and
  spectral flux via `scipy.signal.stft`, same +/-1.0s peak-search
  window as the rise-time work"). The exact STFT frame/hop size was
  never recorded -- reconstructed as nperseg=512/noverlap=384 (32ms
  frames, 8ms hop), flagged plainly as a reconstruction choice, not a
  recovered original parameter. HFC = frequency-bin-weighted energy
  (Masri 1996); spectral flux = half-wave-rectified frame-to-frame
  magnitude increase; score = peak novelty value within the +/-1.0s
  window. Now committed as `scripts/onset_detection_recheck.py`,
  closing that debt item.

  **Real before/after: HFC 0.605 -> 0.6877 (p 0.128 -> 0.0185, a real,
  non-trivial move); spectral flux 0.646 -> 0.6316 (p 0.056 -> 0.0625,
  essentially unchanged).** HFC's new p nominally clears both 0.05 on
  its own AND a 2-test Bonferroni correction scoped to just this
  recheck's family (alpha=0.025). **This is deliberately NOT reported
  as an 11th positive signal, for two concrete reasons, not caution for
  its own sake:**

  1. *Correction scope is genuinely unresolved, not favorable by
     default.* The original 0.605/0.646 numbers were judged against a
     6-test family (4 optical-flow angle-change features + these 2),
     Bonferroni-corrected at alpha=0.05/6~0.0083 -- HFC's new p=0.0185
     does NOT clear that threshold. The flow features weren't
     rechecked today (out of scope for this task), so the honest
     answer to "does HFC survive the real correction" is: unresolved
     until the flow side is rechecked too, not "yes" by only
     re-scoping the family to the 2 tests actually run tonight.

  2. *The original investigation's own real-play recall-risk check
     reproduces, and gets a second confirmed case.* `clip_base3/e1` --
     the play already named "a real, confirmed play reading as more
     ambient than 87-96% of genuine ambient samples" on these exact
     features -- scores at the **15th percentile (HFC) / 7th percentile
     (spectral flux)** on the corrected sample, matching the original's
     13th-percentile HFC finding almost exactly. `clip_base2/e1`, not
     flagged before, now also reads low: **31st (HFC) / 16th
     (spectral flux) percentile.** Two of the six recall-risk clips
     now read as more "ambient" than most genuine ambient samples on
     the signal whose aggregate number nominally looks best tonight --
     the identical structural weakness the original investigation
     used to hold this signal back, not resolved by the correction,
     arguably one confirmed case wider.

  **Honest bottom line: no change to the project's audio conclusion.**
  HFC's aggregate AUC moved further than any other number rechecked
  tonight and nominally clears a narrowly-scoped significance test --
  reported exactly as measured, not hidden. But it fails the same
  recall-risk bar every other signal in this project has been held to,
  on two confirmed real plays rather than one, and its significance
  verdict depends on a multiple-comparisons scope this recheck can't
  fully resolve without also rechecking the flow features. No usable,
  reliable audio signal is confirmed to exist on this footage --
  consistent with, not overturned by, the crowd-reaction and
  raw-embedding audio findings already closed. Nothing changes for
  cutting logic; no guaranteed real-play loss risk was ever on the
  table since nothing here touches a destructive decision.

- **2026-08-23: the joint classifier collapse (open debt from the
  2026-08-16 consolidated correction, originally 0.191 AUC at n=10)
  re-verified against the corrected `clip_60#e6` ground truth -- the
  collapse reproduces and deepens, exactly the failure mode expected,
  now with real per-fold instability numbers instead of a qualitative
  description.**

  **Script already existed and is already committed**
  (`scripts/joint_classifier_investigation.py`, from the original
  2026-08-13 session) -- no "no committed script" debt here, unlike the
  three prior rechecks tonight. Extended in place to report per-fold
  variance (real-side/ambient-side held-out score spread, and the
  fitted coefficient vector for every fold, not just the single
  aggregate AUC) rather than writing a parallel script, since it
  already reuses the same live `load_real_events()`/
  `load_ambient_samples()` loaders and picks up the corrected label
  automatically.

  **Real n, checked rather than assumed: 11, not 10 -- and not 12
  either.** This script's own "fully-paired" discipline (only samples
  where motion, pose-near-plate, AND audio all succeed) means the
  corrected sample's 12 real events don't all make it in; **11 real /
  117 ambient** actually got scored, one shy of the corrected ground
  truth's full real count, for reasons this run doesn't individually
  log (same category of gap already flagged honestly for the
  X-CLIP-fusion investigation's own paired-subset dropouts).

  **Single-feature AUCs on the paired subset, before -> after: motion
  0.509 -> 0.542, pose 0.529 -> 0.532, audio 0.469 -> 0.459** -- all
  three stay in the same near-chance band as before, no individual
  feature meaningfully moved.

  **The joint model's cross-validated AUC: 0.191 -> 0.100 -- further
  below chance, not less.** Adding a single additional real example
  (10 -> 11) made the number worse, not better -- itself a concrete
  demonstration of exactly how unstable LOO-CV logistic regression is
  at this scale, not a sign the corrected label broke anything.

  **Per-fold variance, quantified for the first time rather than
  described qualitatively:** real-side held-out scores are tightly
  clustered and consistently low (mean 0.072, std 0.008, range
  0.060-0.083); ambient-side scores run higher on average (mean 0.088,
  std 0.010, range 0.038-0.103) -- the model isn't randomly noisy
  fold-to-fold on its *predictions*, it's **systematically** scoring
  real events lower than ambient ones, the direct, stable cause of the
  below-chance AUC. The instability lives in the **coefficients**
  instead: across the 11 real-holdout folds, `motion_peak`'s weight
  flips sign 3 of 10 fold-to-fold transitions (range -0.20 to +0.06),
  `pose_peak_px` flips sign 6 of 10 (range -0.17 to +0.09) -- the model
  can't even agree on which DIRECTION pose should point from one fold
  to the next. `audio_rise_ms` is the one stable coefficient (0 sign
  flips, consistently negative, the theoretically correct direction) --
  the only piece of this joint model that isn't actively unstable, and
  it's also the single weakest univariate feature (AUC 0.459, below
  chance on its own). This is a real, direct quantification of the
  original writeup's own qualitative claim ("the fitted boundary swings
  enough between folds") -- confirmed for 2 of 3 features, not merely
  asserted.

  **Honest bottom line: no change, and if anything a sharper
  illustration of the same conclusion.** The corrected sample doesn't
  rescue this classifier -- it collapses further (0.191 -> 0.100), the
  per-fold coefficient instability is real and now measured rather than
  assumed, and the underlying cause is unchanged: 11 real positives
  against 3 features is deep in the same n-much-less-than-needed regime
  this project's own Tier 3 bar (300-500 labeled events) exists to
  guard against. Sample size has not meaningfully grown (10 -> 11 is
  the same order of magnitude, not a scale change), so this was always
  expected to reproduce the same failure mode, and it did, more
  starkly. Nothing changes for cutting logic -- this was never wired
  into anything real; no guaranteed real-play loss risk was ever on the
  table.

- **2026-08-23/24: the 2026-08-13 embedding-level (X-CLIP+AST) fusion
  investigation -- the last of the five items the 2026-08-16
  consolidated correction flagged as not yet rebuilt -- re-verified
  against the corrected `clip_60#e6` ground truth. Confirmed by direct
  doc check, not assumed, that this one genuinely needed a fresh
  recheck (same pre-correction 11-event set, explicitly named in that
  correction's own "not rebuilt" list). Audio/concatenated parts
  reproduce cleanly as before. The video-alone part does NOT reproduce
  cleanly -- a real, different pattern than the original table showed,
  reported honestly rather than folded into the same overfitting
  dismissal as before.**

  **No committed script existed; now committed as
  `scripts/embedding_fusion_recheck.py`, closing the last item on the
  2026-08-16 debt list.** Reconstructed from the original prose: X-CLIP
  `get_video_features().pooler_output` (512-d, L2-normalized, 2.0s
  window) and `MIT/ast-finetuned-audioset-10-10-0.4593` pooled CLS
  output (768-d, L2-normalized, matching 2.0s window), concatenation
  fusion (1280-d), scored via LOO nearest-centroid and LOO L2-logistic
  at C in {0.001, 0.01, 0.1, 1.0} -- the same 5-method x 3-feature-set
  structure as the original table. 12 real / 167 ambient fully-paired
  (both embeddings present), matching the main correction's sample.
  Every number permutation-tested (2000 shuffles, seed 20260816, same
  standard as every recheck tonight) via a fast rank-based AUC verified
  equivalent to the project's canonical pairwise implementation before
  trusting any output.

  **Audio (AST) alone and concatenated fusion reproduce the original
  conclusion cleanly.** Audio alone: 0.2445-0.3728 (was 0.194-0.311),
  consistently below chance, p=0.92-1.0 (one-sided, i.e. genuinely
  worse than chance, not noise). Concatenated: 0.3488-0.4970 (was
  0.327-0.491), never beats video alone at any setting, never clears
  significance. **Unchanged: audio still actively drags fusion down,
  not merely fails to help.**

  **Video alone does NOT reproduce the original pattern -- a real,
  qualitatively different result.** Original: only the weakest-
  regularization extreme (C=1.0, AUC 0.762) nominally beat the 0.690
  zero-shot baseline, flagged as a near-certain overfitting artifact
  because strong regularization (C=0.001, AUC 0.557) looked near-
  chance -- exactly the huge-swing-across-C signature overfitting
  produces. **On the corrected sample, every C value scores 0.7365-
  0.7839, individually significant (p=0.000-0.003), clustered in a
  tight band regardless of regularization strength** -- the opposite
  signature from what sank the original number. Nearest-centroid stays
  consistent with the raw-embedding-probe recheck (0.6352, exact match,
  a real cross-check that this run's embeddings are the same ones
  already verified).

  **Before trusting this, ran the same recall-risk check that caught
  the zero-shot signal's `clip_base3` fragility -- and it does NOT
  reproduce here.** LOO held-out percentile-of-ambient for
  `clip_base1`-`4`/`foul1`/`whiff1` (video-alone logistic, C=0.01):
  59%, 87%, 92%, 96%, 70%, 94% -- no near-median or below-median
  misses, genuinely different from the zero-shot text-prompt signal's
  own documented 31st-79th-percentile instability on these same six
  clips. One real caveat, not glossed over: the actual predicted
  probabilities for 5 of 6 real events are near-zero (0.0000-0.0858)
  despite outranking nearly all ambient samples -- well-RANKED but
  poorly CALIBRATED outputs, a real symptom of 512 dimensions against
  only 12 positives that regularization alone doesn't fully resolve.

  **Honest bottom line: this is the single most promising number from
  every recheck run tonight, and also the one this project's own
  standing rule most clearly says not to act on yet.** A tight,
  significant, recall-risk-clean band across all four regularization
  strengths is real evidence against the specific "overfitting
  artifact" explanation that closed this exact number before -- worth
  flagging plainly, not re-explained away by default. But 12 real
  positives against 512 input dimensions remains deep in the same
  n-much-less-than-d regime this project's Tier 3 bar (300-500 labeled
  events) exists to guard against, regardless of how clean a permutation
  test or recall-risk check on this SAME finite sample looks -- neither
  check can speak to generalization on real games this exact 12-event
  sample has never seen. Not implemented, not wired into cutting logic,
  same as every other candidate signal in this log. Flagged as the
  clearest concrete candidate for revisiting once real review-queue
  usage accumulates enough labels to properly test generalization,
  not a result to build against today. No guaranteed real-play loss
  risk was ever on the table -- nothing here touches a destructive
  decision.

- **2026-08-24: a ninth angle, hand-specified AND/OR rules over
  already-weak signals (motion, mound-region windup pose, audio
  transient), tested as a structurally different alternative to the
  collapsed trained ensemble -- closed negative, real numbers, one real
  mechanistic surprise along the way.** The premise: a hand-specified
  rule has no coefficients to fit or flip sign fold to fold, so it
  can't fail via the SPECIFIC mechanism that collapsed the joint
  logistic-regression ensemble to 0.100 AUC (see the 2026-08-23
  recheck). Carried in explicitly, not glossed over: the individual
  signals are each already characterized as weak or actively
  anti-correlated on this footage (windup-pose succeeds MORE during
  downtime than real play; audio has no confirmed usable signal;
  motion alone isn't swing-selective) -- a rule built from already-weak
  parts can still fail, and this was a real, live test of whether it
  would, not a foregone conclusion either way.

  **Sample: the 27-instant hand-verified set (19 real/8 negative) from
  the 2026-08-14 windup-to-release investigation -- reconstructed, not
  rerun, since that investigation "lived in an agent scratchpad, not
  committed."** 22 of 27 instants were exactly recoverable from the
  doc's own prose citations or `training_data/reviews/*.json`'s own
  `window.start_s` (source of truth for those 10). 5 (clip_540#e2/e3/
  e4, clip_60#e4/e5) were never individually recorded with an exact
  instant -- reconstructed this session via fresh ffmpeg frame pulls,
  visually verified before use, same discipline the original
  investigation itself used. One real discrepancy surfaced and resolved
  during that check: a documented crop filename implied t=121.0 for
  `clip_60#e4`, but that frame shows what looks like an in-progress
  swing, contradicting the event's own ground-truth note ("no swing").
  Checked t=120/122/123 directly: both 120 and 123 cleanly show a
  loaded, non-swinging stance matching the note; t=123.0 was used.
  Flagged plainly: these 5 are this session's own verified picks, not a
  recovered byte-identical match to whatever the original investigation
  actually used. Now committed as `scripts/hand_rule_ensemble_check.py`,
  closing the "lived in a scratchpad" gap for this sample going forward.

  **Signal definitions reused already-established project bars, not new
  numbers fit to this test.** Motion-low: peak motion within +/-1.0s
  against `HardCutConfig.quiet_thresh` (0.002), the single strictest,
  most extensively safety-verified "quiet" bar already shipped in this
  project. Pose-detected: single-frame MediaPipe check on the same
  fixed mound region the original investigation derived (reusing
  `pipeline.pose`'s own crop/landmarker construction). Audio-transient:
  `rise_time_s <= 0.030s`, reasoned from `pipeline/audio.py`'s own
  docstring, not fit to this sample.

  **Real result: all three specified rules land at or below chance,
  none beat the corrected X-CLIP zero-shot baseline (0.653 native /
  0.428 on this same independent sample).**

  | rule | balanced-acc-equivalent AUC | real plays lost (FP) |
  |---|---|---|
  | 1: motion_low AND NOT pose_detected | 0.500 | 0 |
  | 2: + AND NOT audio_transient | 0.500 | 0 |
  | 3 (baseline): motion_low alone | 0.474 | 1 |

  **Real, honest mechanism, not just a number: the shipped 0.002
  quiet_thresh essentially never fires on isolated +/-1.0s point-instant
  windows -- true for only 1 of 27 cases in this sample, and that one
  case was a REAL play (`clip_300#e6`), not a downtime instant.** Rules
  1 and 2 are therefore degenerate on this sample: they never predict
  "downtime" for anything at all, which trivially produces zero
  real-play loss (a real, correct number) but zero actual
  discrimination either (balanced accuracy exactly 0.500, "always
  predict real" by construction) -- not a working rule, a threshold
  mismatch between what `quiet_thresh` was calibrated for (sustained
  quiet stretches inside an already-kept segment) and what this test
  asked of it (single isolated point instants). Rule 3, using motion
  alone, does fire once -- and that one prediction is a real-play loss,
  landing it below chance (0.474).

  **Diagnostic only, explicitly NOT a proposed rule, run to actually
  answer the task's specific question about pose's directional effect
  once the motion gate isn't saturating everything to False:**
  median-split this same 27-point sample's own motion scores (0.00891,
  vs. the shipped 0.002) -- flagged plainly as fit to the exact
  evaluation sample, the same small-sample overclaim risk this project
  has named everywhere else tonight, not a candidate for anything.
  Motion-relaxed alone: balanced-acc-AUC 0.513, but **9 of 19 real
  plays wrongly flagged** -- unacceptably unsafe on its own. AND-ing in
  `NOT pose_detected`: balanced-acc-AUC 0.572, real-plays-lost drops
  from 9 to 2. **This does NOT reproduce "including windup-pose makes
  things worse" the way the task's caveat anticipated -- if anything,
  the opposite, on this one diagnostic.** But the honest mechanism
  matters more than the direction: `pose_detected` was `True` in 84.2%
  of real cases and 62.5% of negative cases in this exact sample
  (nearly saturated, not clearly discriminating either way) -- so
  AND-ing it in mostly just makes the overall rule more conservative
  (fires far less often), which mechanically trades recall for
  precision almost regardless of whether pose carries real signal here.
  The safer, more honest reading: this doesn't establish pose as newly
  useful, it shows that ANY sufficiently restrictive extra condition
  would likely shrink false positives the same way at this sample size
  -- a real result, reported exactly as measured, not spun toward
  either "pose helps" or "pose hurts" beyond what this data actually
  supports.

  **Honest bottom line: closes negative, a structurally different test
  from the collapsed logistic ensemble, reaching the same practical
  conclusion by a different, real mechanism.** No specified rule beats
  chance meaningfully; none approach the X-CLIP zero-shot baseline in
  either its native (0.653) or independent-sample (0.428) form. The
  real, new finding isn't "the parts are still weak" (already known
  going in) -- it's that the project's own strictest already-shipped
  motion threshold is fundamentally the wrong tool for point-instant
  classification, a genuine, previously-undocumented mismatch between
  what `quiet_thresh` was built for and what this test asked of it.
  Combined with the pose-inclusion diagnostic's ambiguous, likely-
  conservatism-driven (not signal-driven) result, this is a real,
  structurally different angle that also closed negative -- consistent
  with, not just repeating, this project's standing conclusion that
  further angles on this exact footage are unlikely to succeed without
  different input. Nothing implemented, nothing wired into cutting
  logic; no guaranteed real-play loss risk was ever on the table.

- **2026-08-24: follow-up to the hand-rule check -- full ROC/AUC sweep
  of raw whole-frame motion, completely unconstrained by any threshold,
  to settle threshold-placement vs. information-problem for real.**
  The hand-rule check found `HardCutConfig.quiet_thresh` (0.002) fires
  on almost nothing in the 27-instant sample (1/27) -- this leaves open
  whether a DIFFERENT threshold would have worked, or whether the
  signal itself carries no separating information at any cutoff. Tested
  directly: every possible threshold on this sample's own real-valued
  range, not just quiet_thresh or any other value used elsewhere in
  this project. Reused the exact same 27-instant sample and motion-peak
  extraction as `scripts/hand_rule_ensemble_check.py` (imported
  directly, not re-derived, so the two scripts' numbers are guaranteed
  comparable), now committed as `scripts/motion_roc_sweep.py`.

  **Full AUC (motion-low-as-downtime-predictor direction): 0.4605 --
  below chance.** Real events have a HIGHER median raw motion (0.01124)
  than downtime instants (0.00859) in this sample -- the raw signal
  points the opposite direction from the hypothesis, though the effect
  is small at n=27 and shouldn't be over-read as its own finding.

  **Best possible balanced accuracy anywhere on the full ROC curve:
  0.5625 -- barely above the 0.500 chance line, and achieved only by an
  almost-degenerate rule.** At the single best cutpoint found by
  sweeping every distinct value in the sample (raw motion < 0.037763),
  the confusion is TP=19 FP=7 TN=1 FN=0 -- this "best" rule predicts
  "downtime" for 26 of 27 cases, correctly excluding only the single
  highest-motion negative outlier in the whole sample
  (`clip_60#e2 warmup b`, 0.04798). That is not meaningful
  discrimination by any reasonable reading -- it is "always predict
  downtime" plus one lucky exclusion, and the ROC curve in between (see
  the full table in `scripts/motion_roc_sweep.py`'s own output) shows
  no cutpoint anywhere climbing meaningfully above 0.55 balanced
  accuracy.

  **Real, direct answer to the actual question: this is an information
  problem, not a threshold-placement problem.** No cutoff exists
  anywhere on the continuous range that gives useful discrimination on
  this sample -- `quiet_thresh`, or any other choice, was never the
  bottleneck. Adjusting a safety bar could not have helped here,
  independent of whether that bar itself should ever change for other
  reasons.

  **Direct comparison to the already-closed zone-velocity investigation:
  consistent with its closure, not contradicting it -- but a weaker,
  more fundamental failure, and that difference makes mechanistic
  sense.** Zone-velocity (motion restricted to the batting zone
  specifically, a different, more spatially-targeted signal) DID show
  real separating power at the coarse level: a full order-of-magnitude
  gap between a required no-swing taken-pitch (0.08) and real contact
  windows (0.33-1.11) -- it was closed for failing the NEXT question up
  (swing-selectivity: practice swings and a batter simply walking away
  both spike to 0.61-0.65, comparable to real contact). Whole-frame
  motion, tested here, doesn't even clear that first, coarser bar -- it
  barely separates real from downtime at all (AUC 0.4605, best
  balanced accuracy 0.5625), let alone reaching the swing-selectivity
  question zone-velocity got to before failing. This isn't a
  contradiction: restricting to the batting zone should carry more
  signal than the whole frame, which dilutes it with fielders,
  background players, and camera motion -- the same "structural
  clutter swamps the signal" mechanism already named for the closed
  raw bat-count attempt, one level removed. Whole-frame motion failing
  even more completely than zone-velocity did is the expected direction
  of that relationship, not a surprising or contradictory result.

  **Honest bottom line: closes the threshold-vs-information question
  definitively for this signal.** Nothing about this changes any
  existing conclusion -- zone-velocity stays closed for swing-
  selectivity, whole-frame motion is now confirmed closed for an even
  more basic reason (no real separation at any threshold), and the
  hand-rule check's own negative result is now explained at the root
  rather than left as "the shipped threshold happened not to fire."
  Purely diagnostic, as scoped -- nothing wired into cutting logic, no
  guaranteed real-play loss risk was ever on the table.

- **2026-08-25: real detect-stage speed investigation -- where the
  ~45min cold-detect time actually goes, three real levers tested, one
  real viable candidate found and left opt-in pending explicit sign-off,
  not silently flipped as the default.**

  **1. Real per-component profiling (`scripts/detect_profile.py`,
  clip_300.mkv, no cache): inference is 99.3% of real wall clock.**
  Frame grab+retrieve (cv2): 0.3%. BGR->RGB + PIL convert: 0.4%.
  `model.predict()`: **99.3%, 352.9ms/sample average**. Box
  post-processing: ~0%. Confirms the earlier full-length test's
  assumption directly rather than leaving it assumed -- there is
  exactly one component worth optimizing, and it's the one this
  investigation spent its time on.

  **2. `optimize_for_inference()` -- rfdetr's own suggested lever,
  tested and closed. Real, on Apple Silicon/MPS the library's own "~8x
  on T4" claim does not transfer, and the one variant that does show a
  speedup fails on accuracy.** All three tested on the identical 186
  real sampled frames from clip_300.mkv, compared against the
  unoptimized baseline:
  - `compile=False` (export only, fp32): 1.02x -- real but negligible,
    0.000px difference from baseline (byte-identical).
  - `compile=True, batch_size=1` (JIT-traced, fp32): **0.89x -- slower
    than baseline**, plus 21.84s one-time trace overhead. JIT tracing
    actively hurts single-image inference on this MPS setup. Still
    0.000px difference from baseline (safe, just not useful).
  - `compile=True, batch_size=1, dtype=fp16`: 1.19x speedup, but
    **8/186 real box-count mismatches, max coordinate difference
    107.4px** -- far beyond a fp16-rounding tolerance (3px), a real,
    confirmed detection change, not numeric noise. Ruled out
    immediately under the no-silent-regression rule.
  Closed: no `optimize_for_inference()` configuration gives both real
  speed and unchanged accuracy on this hardware.

  **3. Batching (`scripts/detect_batch_check.py`, batch_size=8): real,
  confirmed byte-identical detections, but no real speedup on MPS.**
  Verified first, not assumed, that rfdetr's own batch path is a
  genuine stacked-tensor forward pass (`torch.stack` + one model call,
  not a hidden per-image loop) before testing. Sequential: 364.7ms/frame.
  Batched (size 8): 375.9ms/frame -- **0.97x, no speedup, if anything
  marginally slower.** 0/186 box-count mismatches, max coordinate
  difference 0.0000px -- perfectly identical, confirming the batched
  path is numerically exact, just not faster here. Consistent with
  finding #1: inference is real GPU compute time, not per-call Python/
  dispatch overhead, and Apple's MPS backend doesn't give the same
  batched-throughput benefit CUDA does for this architecture. Closed,
  real negative.

  **4. Frame sampling rate: already reduced, not a new lever.**
  `DetectionConfig.sample_fps=1.0` already samples 1 frame in ~48 on
  this footage (measured: clip_300 at 47.95fps source, step=48) -- the
  literal "detect every Nth frame instead of every frame" ask is
  already shipped, not untried. Going lower (0.5fps) was NOT tested
  further: it would touch already-extensively-verified at-bat-boundary
  and hard-cut timing margins (see the 2026-08-23 threshold-sweep
  closure) and would need the same full-9-clip re-verification rigor
  that sweep required, for a lever whose real bottleneck (per-2 below)
  isn't sampling density in the first place.

  **5. Faster RF-DETR variant -- the one real, viable candidate found,
  verified via the full regression gate, not just an aggregate count.**
  Quick screen first (`scripts/detect_variant_speed_check.py`,
  clip_whiff1.mkv, 43 frames) before paying for the expensive full
  suite: RFDETRNano ruled out immediately (551 vs Base's 590 total
  person detections across 43 frames, -6.6%, and a WORSE min detected
  box height, 18.2px vs 17.0px -- real degradation on exactly the
  distant-fielder case Base was chosen for). RFDETRMedium (271.8ms/frame,
  588 detections) and RFDETRSmall (262.3ms/frame, 588 detections) both
  looked close enough to warrant the real check; Small taken forward as
  the faster of the two.

  Added `DetectionConfig.model_variant` (default `"base"`, unchanged)
  and a `--model-variant` flag to `scripts/regression.py`, cache key
  updated to include it so this never reads or pollutes the existing
  `base` cache. Ran the FULL 9-clip regression suite with `small`, then
  again with the (now cache-invalidated, freshly computed) `base`
  default for a real same-methodology comparison, not a stale citation.

  **Both: ALL PASS. 100% required-event recall on every clip, hard-cut
  exclusion mechanism validated, every stitched output decodes clean.**
  Diffed line-by-line, not just pass/fail: 6 of 9 clips are byte-
  identical or differ only in sub-0.3s scale-boost/segment-boundary
  noise (clip_540, clip_base2, clip_foul1). **One real, honestly-flagged
  difference: `clip_base3`'s kept segment ends 21.0s under Small vs.
  18.6s under Base (+2.4s), producing 2 hard-cut windows (-1.75s) instead
  of 1 (-0.25s).** Still recall 1/1, still passes the hard-cut exclusion
  and stitch-decode gates -- not a safety failure -- but a real,
  measurable behavioral difference, reported exactly as found rather
  than smoothed into "identical." Aggregate kept-time across all 9 clips:
  Base 610.85s before hard-cut / 580.01s after; Small 612.73s / 580.39s
  -- within 0.3% either way.

  **Real speedup: 352.9ms/frame (Base) -> 262.3ms/frame (Small),
  1.34x on the component that's 99.3% of real detect-stage time.**
  Extrapolated (not directly measured on `full_game.mkv` itself, flagged
  as an extrapolation): a ~45min cold detect would become roughly
  ~34min, a real ~11min reduction on a full-game job -- worth verifying
  directly against `full_game.mkv` before trusting the exact number,
  same as this project's own standing "measure the real thing, don't
  extrapolate and call it done" discipline.

  **Honest bottom line: a real, viable ~1.34x speedup exists (RFDETRSmall)
  and clears the project's own full safety gate -- left opt-in
  (`model_variant="small"`), not flipped as the default.** The one real
  behavioral difference found (clip_base3) doesn't cost recall or fail
  any gate, but this project's own standing pattern (see the 2026-08-23
  threshold-sweep closure, which kept the safer default even after
  testing alternatives) is not to silently change shipped pipeline
  behavior without explicit sign-off, even when a change measures safe.
  Reported as a real, concrete, ready-to-adopt option, not shipped
  unilaterally. `optimize_for_inference()` and batching both close as
  genuine negatives on this specific Apple Silicon/MPS deployment --
  honest outcomes, not failures to avoid reporting. Sample-rate
  reduction was already done before this investigation started, not a
  new lever tonight. No guaranteed real-play loss and no silent
  detection-quality regression: the one real change tested and left
  available was verified against the full 9-clip gate with recall
  reported before and after, not assumed.

- **2026-08-26: real export encoding audit -- current settings measured,
  not assumed, and found to be genuinely oversized for the real target
  use case (church volunteers sharing over phones/messaging apps). One
  real, evidence-backed change proposed and implemented as an opt-in
  parameter, default left unchanged pending explicit sign-off, same
  pattern as the 2026-08-25 detect-variant investigation.**

  **1. Real current state, traced directly in `pipeline/stitch.py`, not
  assumed.** Two distinct paths: pure stream-copy (`-c copy`, no
  transcoding, output resolution/bitrate/codec is simply whatever the
  source already is) whenever a plan doesn't need re-encoding, and a
  real libx264 encode (`-preset veryfast -crf 18`, AAC audio at
  ffmpeg's own default ~128kbps, no explicit `-b:v` -- pure CRF quality-
  target mode, no bitrate cap) whenever it does. Re-encoding is NOT rare
  in practice: it fires on any multi-file resolution/codec/fps mismatch
  OR whenever a hard-cut boundary needs frame-exact re-encoding for even
  one span -- and hard cuts now ship unconditionally in production (see
  the 2026-08-23 threshold-sweep closure), so most real exports with any
  hard-cut activity go through this path. Confirmed against a real,
  already-produced export (`uploads/fb07ea71f4b3/output.mp4`, via
  `ffprobe`): **1920x1080, ~5.41Mbps video, ~130kbps AAC, 97.48MB for
  147.3s of output** -- ~40MB/min.

  **2. Real assessment against the real target use case.** At the
  measured ~40MB/min, a realistic 20-30min highlight reel extrapolates
  to **800MB-1.2GB** -- not a hypothetical concern but a direct,
  concrete problem for a church volunteer downloading over home/mobile
  data and sharing via a messaging app, most of which either hard-cap
  file size well below that (traditional MMS/chat limits) or silently
  re-compress on upload anyway, meaning CRF 18's quality investment is
  substantially wasted for this specific distribution path. CRF 18 is a
  real, deliberately very-high-quality setting ("visually near-
  lossless"), well above what's typically needed for casual mobile
  viewing of moderate-motion outdoor sports footage.

  **3. Real test, not assumed: same real footage, four CRF values, file
  size AND a real visual check, twice.** Encoded the exact same
  `build_extract_cmd` command (same scale/pad, same `veryfast` preset,
  only CRF varied: 18/21/23/26) against two real reference-clip windows
  -- a static loaded-batter moment (clip_300, t=55-65s) and a genuine
  fast-motion swing-and-miss (clip_whiff1, t=10-18s, real ball-in-flight
  motion blur present). File size, same clip both times:

  | CRF | file size | vs. CRF 18 |
  |---|---|---|
  | 18 (current) | 5.83MB / 4.75MB | -- |
  | 21 | 4.13MB / 3.38MB | -29% |
  | 23 | 3.21MB / 2.63MB | -45% |
  | 26 | 2.02MB / 1.66MB | -65% |

  Real visual check, not just file-size math: extracted a frame at the
  same real timestamp from each of the four encodes, both moments,
  built as side-by-side composites and inspected directly -- including a
  full-resolution zoomed crop on the single highest-frequency detail in
  frame (the batting-cage chain-link netting against open sky, exactly
  where compression artifacts show first if they're going to show at
  all). **No visible difference at any CRF tested, up to and including
  26, on either the static or the fast-motion test.** Sky gradients
  stayed smooth (no banding), netting mesh stayed crisp, motion blur
  present in the swing frame was identical real subject/shutter blur
  across all four, not additional compression blur.

  **4. Real change: implemented, not just recommended, as an opt-in
  parameter.** Added `crf: int = 18` to `build_extract_cmd` and
  `run_stitch` (both default exactly the current shipped value --
  byte-for-byte unchanged behavior for every existing caller that
  doesn't pass it). No backend/CLI caller updated to pass a non-default
  value yet -- that's the real, deliberate decision left open, not an
  oversight. Full test suite: 481 passed, unaffected.

  **Honest bottom line: CRF 18 is real, measured, unnecessary size for
  this project's real footage and real target use case, with real
  evidence (not assumption) that CRF 23 -- x264's own long-established
  default, a conservative pick even though CRF 26 also showed no visible
  difference in this testing -- costs nothing visible while cutting
  output size by 45%.**

  **5. Flipped for real, in the actual production call site, not left
  opt-in.** Confirmed `backend/pipeline_runner.py:run_export_job` is the
  single real production path first, not assumed: it's the only non-
  test/non-script caller of `run_stitch` in the codebase, invoked both
  by the auto-chained export right after detection
  (`run_detect_then_export_job`) and by the Edit Log's real
  restore/cut-triggered re-export (`backend/app.py`'s
  `POST /batches/{id}/export`) -- every real export in this project goes
  through this one function. Changed its `run_stitch(...)` call to pass
  `crf=23` explicitly. Full test suite re-run after: **19 real failures**
  surfaced first (`TypeError: fake_run_stitch() got an unexpected
  keyword argument 'crf'`) -- three `run_stitch`-shaped fake fixtures in
  `tests/test_backend_api.py` had fixed signatures that didn't accept
  the new parameter; updated all three to accept `crf=18` (matching
  `pipeline.stitch`'s own default) and re-ran: **481 passed, 0 failed.**

  **Real full-export verification, not just the earlier frame-level
  clips.** Ran a real, complete `run_stitch` against the actual
  `fb07ea71f4b3` batch's real manifest (the same batch used for all of
  tonight's earlier Edit Log browser verification) at `crf=23`, to a
  separate output file so the live production `output.mp4` was never
  touched. Confirmed real, not assumed: correctly took the re-encode
  path (`reencoded=True`, "a hard-cut boundary needs frame-exact
  re-encoding..."), the exact path production hard-cut-bearing exports
  actually use.

  | | crf=18 (real production output.mp4) | crf=23 (real full export, same manifest) |
  |---|---|---|
  | file size | 97.48MB | 54.76MB |
  | video bitrate | ~5.41Mbps | ~2.98Mbps |
  | resolution | 1920x1080 | 1920x1080 (unchanged) |
  | duration | 147.29s | 147.29s (unchanged) |

  **-43.8% real file size, matching the earlier frame-level test's -45%
  prediction almost exactly**, on a real, complete, production-path
  export -- not just the isolated 8-10s test clips. Confirmed both
  outputs are genuinely distinct files (different MD5s), not a stale
  probe artifact, before trusting the size numbers. Test export file
  deleted after verification; the real batch's own `output.mp4` and
  `manifest.json` were never touched by this check.

  Left as-shipped, not opt-in: matches this project's own pattern for a
  change that's real, evidence-backed, tested against a real production
  export, AND explicitly requested to be flipped -- unlike the 2026-08-25
  detect-variant investigation, which stayed opt-in because it had one
  flagged, non-trivial behavioral difference on real content
  (`clip_base3`'s kept-segment length) and had not yet been explicitly
  approved for production. No guaranteed real-play loss risk: this
  changes image compression quality only, never which content is kept
  or cut.

- **2026-08-27: `DetectionConfig.model_variant` flipped from "base" to
  "small" -- explicit decision, real frame-verification of the one
  flagged difference done FIRST, not skipped in favor of "the gate
  already passed."**

  **1. Real frame-verification of the `clip_base3` difference, before
  flipping anything.** The 2026-08-25 investigation found Small's kept
  segment for `clip_base3` runs to 21.0s vs. Base's 18.6s (+2.4s), with
  hard-cut then trimming more of it back out (2 windows/-1.75s vs. 1
  window/-0.25s). Rather than trust "both configurations still pass the
  gate" as sufficient, pulled real frames across t=16-22s and looked
  directly, same rigor as every other borderline check tonight. The
  clip's own ground truth (`tests/ground_truth/clip_base3.json`)
  already states the real play resolves and "defense relaxed... by
  ~18s" -- the frames confirm this exactly: t=17-18 shows the defensive
  play winding down, and t=19-21 (the extra window Small keeps) shows
  players casually walking, then the **next batter approaching the
  plate with a bat raised at t=21** -- genuine post-play transition
  content, not real defensive action tied to this required event. Real
  answer to the actual question asked: this is **not a detection
  improvement** (Small isn't catching more of the real play) and **not
  a real risk** either (no real content anywhere near being lost -- the
  required event's actual content, resolved by ~18s per the clip's own
  ground truth, is fully captured under both configurations, and
  `hard_cut_overlaps_required` already validated neither Base nor Small
  produces a hard-cut that overlaps it). It's a benign difference in how
  much non-essential walkup footage gets included before hard-cut trims
  it back -- final kept duration differs by under 1 second either way
  once hard-cut is applied (Base: 12.4s - 0.25s = 12.15s; Small: 14.8s -
  1.75s = 13.05s).

  **2. Flipped for real.** `DetectionConfig.model_variant` default
  changed `"base"` -> `"small"`. Added `tests/test_detection_config.py`
  (3 new tests: the default value itself, the real `_MODEL_CLASSES`
  mapping, and that an unknown variant raises) specifically so a future
  change to this default is a visible, deliberate diff against a real
  assertion, not silently unnoticed. `scripts/regression.py`'s own
  `--model-variant` flag was hardcoded to `"base"` separately from
  `DetectionConfig`'s real default -- a real, found drift risk (this
  script exists to validate what production ships; a stale hardcoded
  default would silently stop doing that the moment the two diverged).
  Fixed to read `DetectionConfig().model_variant` directly so it always
  tracks the real default unless explicitly overridden.

  **3. Re-verified for real, not assumed from the earlier run.** Full
  pytest suite: **484 passed** (481 + the 3 new tests), 0 failed. Full
  9-clip `scripts/regression.py`, now defaulting to `small` with no
  flag needed: **ALL PASS** -- 100% required recall on every clip,
  hard-cut exclusion mechanism validated, every stitch decode clean.
  Aggregate totals matched the 2026-08-25 investigation's own Small run
  exactly (612.73s kept before hard-cut / 580.39s after), confirming
  this is the same, already-verified real behavior now running by
  default, not a fresh unverified path.

  **Honest bottom line: real evidence, checked directly rather than
  inferred, supports the flip.** The one flagged behavioral difference
  is confirmed benign by direct frame inspection (post-play walkup
  content, not real play content, and not a hard-cut-safety risk
  either way) -- not just "the aggregate gate happened to still pass."
  `model_variant="small"` now ships as the real default. No guaranteed
  real-play loss risk: both configurations were independently verified
  to fully cover every required event across all 9 reference clips, and
  the flagged difference was confirmed, by direct visual inspection, to
  never touch real play content.


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
