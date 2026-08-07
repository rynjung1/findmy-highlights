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
(whiff vs. hit vs. called strike) was investigated and explicitly closed as
not viable on this footage (see [Known limitations](#known-limitations--non-goals-for-this-version)).

> **Full development history lives in
> [docs/INVESTIGATION_LOG.md](docs/INVESTIGATION_LOG.md)** — every signal
> design tried and why it was accepted or rejected, the full timing/thermal
> investigation, every padding/threshold derivation, review-queue statistics
> over time, and every bug found and fixed, in detail. This README stays
> short on purpose; that document is what prevents re-litigating settled
> questions.

## Priority rule

Detection is tuned under a bias-toward-inclusion rule: missing a real play is
worse than keeping extra dead time, so borderline segments are kept by
default. Partway through the project this was deliberately relaxed for
*new* work going forward — the owner explicitly accepted some risk of
trimming into or missing a play in exchange for more aggressive dead-time
cutting, with every resulting tradeoff still reported honestly and any real
miss on a reference clip still flagged, never silently absorbed. `enter_thresh`
itself was explicitly considered and left alone under the relaxed rule (see
Known limitations). Full reasoning, and the designs that were built and
rejected under the original strict rule, are in the
[investigation log](docs/INVESTIGATION_LOG.md#priority-rule-v1-default-vs-current-live-setting).

## Key results

**v1 is complete and end-to-end tested.** Detection pipeline, multi-file
handling/stitching, the backend API, the full Home/Upload/Edit Log/Review
Queue frontend, multi-base calibration, and zone-velocity tightening are all
shipped, tested, and exercised against real footage through a real running
server — not just in-process test clients.

- **Tests:** `pytest tests/` runs 466 unit/integration tests; `pytest tests/
  -m e2e` runs 6 tests that invoke real model inference.
- **Safety gate:** `scripts/regression.py` requires zero missed required
  events across all 9 hand-annotated reference clips before anything ships —
  every threshold, padding, and cutting change in this project's history was
  validated against this gate first. Also strictly decodes each clip's real
  stitched output end to end (not just checking segment boundaries) — added
  after a real shipped stitching bug (see the investigation log) went
  undetected because nothing previously checked whether the actual rendered
  file was playable.
- **Dead time cut, real footage:** on a real 67.5-minute game recording
  (`full_game.mkv`), the shipped pipeline keeps 53.19 min / cuts 14.31 min
  (~21% of the recording removed) with 9/9 reference-clip recall and
  continuity preserved. An additional unconditional hard-cut pass removes a
  further ~4.8% of kept time on the reference set (restorable via the Edit
  Log if it ever cuts into a real play).
- **Safety margins:** the thinnest recall margin measured is `clip_foul1`'s
  foul ball at 1.11x over `enter_thresh` at the reference camera distance —
  a camera-distance-aware scale boost was shipped specifically to protect
  more-distant camera setups from losing that margin entirely.
- **What's shipped as a real cutting/keep signal:** motion detection, person
  detection, plate/base occupancy, at-bat boundary detection, play extension,
  dynamic (shrink-only) padding, the enter-side scale boost and occupancy
  debounce, and unconditional hard-cut windows.
- **What was investigated and deliberately NOT wired into any cutting
  decision:** pose (wrist displacement), audio (onset rise-time, amplitude,
  spectral ratio), a joint pose+audio+motion classifier, a pretrained
  action-recognition embedding, optical flow, onset-detection (HFC/spectral
  flux), semantic crowd-reaction audio, an ambient-motion discount, a local
  open-weight VLM, and a multi-feature review-queue model — each is real
  signal, none clears this project's safety bar. Zero-shot X-CLIP is the one
  exception that reached real statistical significance (AUC 0.690, p=0.012)
  and is shipped as review-queue *ranking* instrumentation only, held back
  from cutting decisions over a documented prompt-sensitivity risk on
  defensive plays. Full numbers, methodology, and every rejected design are
  in the [investigation log](docs/INVESTIGATION_LOG.md#current-status).

## Architecture overview

Built so far (see the
[investigation log](docs/INVESTIGATION_LOG.md#architecture-overview) for the
full writeup of each component, including the bugs found building it):

- **Motion detection** (`pipeline/motion.py`) — scans at ~10 samples/sec,
  removes global camera-pan motion via phase correlation, and scores the
  residual (localized motion: players, swings, runs).
- **Segment extraction** (`pipeline/segments.py`) — hysteresis thresholding
  over the motion score (opens higher than it closes, so brief mid-play dips
  don't split a segment) plus merging and blip-dropping. The ENTER
  comparison runs against a scale-boosted score (see `pipeline.fusion`) so a
  more distant camera doesn't shrink a real play below threshold; exit stays
  on raw motion.
- **Person detection** (`pipeline/detection.py`) — RF-DETR Base (Apache-2.0),
  ~1 fps, cached by file + config.
- **Plate/base calibration** (`scripts/calibrate.py`, `pipeline/calibration.py`)
  — one-time manual click per camera setup; a per-file override handles a
  camera that moved mid-batch.
- **Signal fusion** (`pipeline/fusion.py`) — combines motion, person boxes,
  and plate/base occupancy; vetoes a segment only when detection is certain
  no person was ever near its motion. Person detection alone didn't improve
  recall over motion-only on this footage — its real value is the
  occupancy timeline that at-bat detection needs.
- **Shared settle logic** (`pipeline/settle.py`) — one implementation of
  "has the field been quiet long enough," shared by play extension and
  at-bat detection so the two mechanisms can't disagree about when a play
  is over.
- **At-bat boundary detection** (`pipeline/atbat.py`) — fires when the plate
  is vacated then sustainably re-occupied and motion has settled, so a new
  batter can safely close the previous play.
- **Segment refinement / play extension** (`pipeline/refine.py`) — holds a
  segment open until motion genuinely settles (not just dips), then applies
  pre/post padding and a final merge.
- **Manifest** (`pipeline/manifest.py`) — the single source of truth for
  kept/cut status, restore state, and multi-file span bookkeeping; see
  [How the manifest works](#how-the-manifest-works).
- **Multi-file ordering & stitching** (`pipeline/multifile.py`,
  `pipeline/stitch.py`) — orders files by capture-time metadata when
  unambiguous (otherwise asks, never guesses), and renders kept spans via
  stream-copy when possible or a re-encode fallback otherwise. A file
  boundary is a hard boundary by design: a play split across two files
  becomes two clips, never silently bridged.
- **Review/training queue** (`pipeline/review.py`, `pipeline/pose.py`,
  `pipeline/audio.py`, `pipeline/xclip.py`) — Tier 1 candidate generation
  plus pose/audio/X-CLIP instrumentation attached to every candidate, opt-in
  via `FMH_TRAINING_DATA_DIR`, never read by the real detection path.
- **Backend API** (`backend/`) — FastAPI service wrapping the pipeline
  (upload, calibration, trigger-processing, progress, manifest, re-export),
  with durable on-disk job state, a single-job-at-a-time lock, and
  auto-chained detect→export.
- **Frontend** (`frontend/`, React + TypeScript + Vite) — Home (upload →
  calibrate → process → player), Edit Log (restore/cut-again with automatic
  re-export), and Review Queue views.

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
   License 2.0, verified directly against Google's own official model card.

7. **Frontend (optional — only needed to use the Home view UI, not the
   CLI or bare API):** TypeScript throughout — install Node.js (developed
   against Node 18.20; the very latest `create-vite`/tooling needs Node 20+,
   but this project's own `frontend/package.json` is pinned to work with
   18), then:

   ```sh
   cd frontend
   npm install
   ```

   `npm run build` runs a real type-check (`tsc -b`) before Vite's own
   build, so a type error fails the build rather than silently shipping —
   `npm run typecheck` runs the same check alone, without building. Kept in
   its own `package.json`/lockfile, separate from the Python venv.

8. **Create `.env`** (project root, gitignored — not part of a fresh
   checkout, so this step is real, not optional busywork):

   ```sh
   echo 'FMH_TRAINING_DATA_DIR=training_data' > .env
   ```

   Without this, the backend still runs fine, but the Review Queue
   silently collects nothing.

## How to run it

**Full app (backend + frontend), two terminals:**

```sh
# terminal 1: backend, must be on port 8420 -- the frontend dev server's
# proxy (frontend/vite.config.ts) is hardcoded to that port
./venv/bin/uvicorn backend.app:app --reload --port 8420

# terminal 2: frontend dev server
cd frontend && npm run dev
```

Then open `http://localhost:5173` in a browser (not `127.0.0.1` — see
Known limitations for why). Upload a video, click home plate on the
preview frame, and it'll walk through processing to a finished,
downloadable highlight video.

Review Queue candidates from real detect jobs are collected by default (the
`.env` from Setup step 8). The sidebar's "Review Queue" tab has borderline
clips to label, and `./venv/bin/python scripts/review_stats.py` reports
disagreement rates and feature-vs-label patterns over whatever's been
labeled so far.

To build up a labeling batch in one sitting instead of waiting on the
normal 5-per-run trickle:

```sh
# mine up to 50 real candidates from one video (calibration resolved the
# same way scripts/detect.py does)
./venv/bin/python scripts/mine_review_candidates.py path/to/a_game.mkv --limit 50

# or mine across every already-processed batch under uploads/
./venv/bin/python scripts/mine_review_candidates.py --all-batches --limit 50
```

To run a one-off without collecting (e.g. a throwaway smoke test):

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

**For a long real-footage run** (a full game, anything on the order of
`full_game.mkv`'s ~37+ minutes), wrap it in `caffeinate -i`:

```sh
caffeinate -i ./venv/bin/python scripts/detect.py path/to/a_full_game.mkv --manifest out/game_manifest.json
```

This is a confirmed, measured speedup (roughly doubles CPU utilization and
halves wall-clock time on long unattended runs), not a hygiene suggestion —
full investigation in the
[log](docs/INVESTIGATION_LOG.md#current-status).

## Deployment

Two independent pieces, deployed separately: the **backend** (FastAPI + the
full ML pipeline) as a Docker container with a persistent volume, and the
**frontend** (`frontend/dist/`, a static build) on any static host. They
talk over plain HTTP with CORS, not a shared origin.

### Backend: build and run

```sh
docker build -t findmy-highlights-backend .

docker volume create fmh-data   # real user data + the detection cache
docker run -d --name fmh-backend -p 8420:8420 \
  -v fmh-data:/data \
  -e FMH_CORS_ORIGINS="https://your-frontend-domain.example.com" \
  findmy-highlights-backend
```

`/data` is where `FMH_UPLOADS_ROOT`/`FMH_DETECTION_CACHE_DIR` already point
(baked in as image `ENV` defaults) — mounting a volume there is what makes
uploads, manifests, and `output.mp4` survive a container restart. RF-DETR's
weights and the bundled demo clip's detection cache are downloaded and
warmed up at **build time**, not on a visitor's first request. Measured
build time ~3 min; image size **4.66 GB** (`torch`/`torchvision` installed
from the CPU-only wheel index to avoid unused CUDA runtime libraries).

Measured resource use: idle container ~150 MB; a real detect job needs
~1 GB RSS and drives CPU hard while running (single-job-at-a-time by
design — see the log). No GPU required.

### Backend: environment variables

| Variable | Default | Purpose |
|---|---|---|
| `FMH_UPLOADS_ROOT` | `./uploads` | Batch uploads, job state, manifests, `output.mp4`. Point at a mounted persistent volume. |
| `FMH_DETECTION_CACHE_DIR` | `./.cache/detections` | RF-DETR result cache, keyed by file mtime+size+config. Safe to lose. |
| `FMH_CORS_ORIGINS` | *(unset — no cross-origin access)* | Comma-separated allowed origins for the deployed frontend. **Required** whenever frontend and backend are on different domains. Never defaults to `*` — this API accepts real file uploads. |
| `FMH_TRAINING_DATA_DIR` | *(unset — review queue off)* | Opts real detect jobs into Tier 1 review-queue instrumentation. Off by default on the public deployment (see Decisions below). |
| `FMH_POSE_MODEL_PATH` | `./.cache/models/pose_landmarker_full.task` | Only read if `FMH_TRAINING_DATA_DIR` is set. Not baked into the Docker image — mount it manually if the review queue is enabled in production. |
| `RF_HOME` | `~/.roboflow` (baked to `/data/cache/roboflow` in the image) | RF-DETR's weight cache. Pre-warmed at build time. |
| `HF_HOME` | HuggingFace's default (`~/.cache/huggingface`) | Only relevant if `FMH_TRAINING_DATA_DIR` is set — X-CLIP instrumentation downloads `microsoft/xclip-base-patch32` (~600 MB) on first use. |

### Frontend: build and deploy

```sh
cd frontend
VITE_API_BASE_URL="https://your-backend-domain.example.com" npm run build
```

`VITE_API_BASE_URL` is a **build-time** substitution (Vite's
`import.meta.env.VITE_*`), so it must be set in the static host's
build-command environment (e.g. Vercel/Netlify project settings), not a
committed `.env` file. Left unset, requests stay relative paths, which is
what makes `npm run dev` work against Vite's own proxy with zero config.

Deploy `frontend/dist/` to any static host. Its domain must be added to
the backend's `FMH_CORS_ORIGINS`.

### Demo mode

The "Try the demo" button (`backend/demo.py`, `POST /demo/run`) runs a real
~45-second bundled clip through the exact same pipeline path a real upload
takes — only the RF-DETR detection pass is pre-computed. Measured
end-to-end time: 6s locally, 11s in the built Docker container. Demo runs
never opt into the review queue.

### Decisions made

- **Backend:** GCP Compute Engine (a plain VM running the Docker image
  directly, not GKE/Cloud Run). **Frontend:** Vercel. **Backend stays warm**
  (always-on by construction — no scale-to-zero to leave on by accident).
  **Review queue disabled** on the public deployment. **No custom domain
  purchase.**

### GCP Compute Engine (backend)

A bare Compute Engine VM has no managed TLS the way Railway/Vercel did —
without it, Vercel's HTTPS frontend gets mixed-content-blocked from calling
a plain-HTTP backend, so the app simply wouldn't work. Fixed with a free
[sslip.io](https://sslip.io) hostname + [Caddy](https://caddyserver.com) as
a TLS-terminating reverse proxy, no domain purchase needed.

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

**2. Firewall** (targeted by the instance tag, not applied VPC-wide):

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

(SSH on 22 is open by default via GCP's own `default-allow-ssh` rule.)

**3. Persistent disk for real user data:**

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

**5. Run the container**, `/data` mapped onto the persistent disk from step 3:

```sh
sudo docker run -d --name fmh-backend --restart unless-stopped \
  -p 8420:8420 -v /mnt/data:/data \
  -e FMH_CORS_ORIGINS="https://your-vercel-app.vercel.app" \
  ghcr.io/your-org/findmy-highlights-backend:latest
```

(`--restart unless-stopped` is the Docker-level "stays warm" mechanism here
— no platform-level always-on setting the way Railway had.) Building the
image directly on the VM instead of pulling from a registry works
identically — `git clone`, then `docker build`.

**6. TLS via Caddy**, once the VM has an external IP:

```sh
sudo docker run -d --name fmh-caddy --restart unless-stopped \
  -p 80:80 -p 443:443 \
  -v caddy-data:/data -v caddy-config:/config \
  caddy caddy reverse-proxy --from your-vm-ip.sslip.io --to localhost:8420
```

Then `FMH_CORS_ORIGINS` and `VITE_API_BASE_URL` both use
`https://your-vm-ip.sslip.io`, not the raw IP or port 8420 directly.

### Instance sizing

Measured peak RSS with everything loaded and run through real inference:
RF-DETR alone (the actual public-deployment default, review queue off)
**~0.95-1 GB**; RF-DETR + X-CLIP + MediaPipe pose (worst case, if the
review queue were ever enabled) **~1.6 GB**. Recommendation:
**`e2-medium`** (2 vCPU, 4 GB RAM) — comfortable headroom over both. Rough
cost, not freshly re-verified against live pricing: **~$25/month**
always-on for the VM plus ~$5/month for a 50GB disk (~$30/month total) —
check [the calculator](https://cloud.google.com/products/calculator) before
committing.

### Vercel (frontend)

No `vercel.json` needed — Vercel's Vite preset auto-detects this repo.

- **Root Directory:** `frontend`
- **Framework Preset:** Vite (auto-detected)
- **Build Command:** `npm run build` (default)
- **Output Directory:** `dist` (default)
- **Environment Variable:** `VITE_API_BASE_URL` = the real backend URL
  (the Caddy-fronted `sslip.io` URL, not the raw IP/port). Build-time —
  must be set before the build, not added after and expected to take
  effect without a rebuild.

### Still genuinely open

- **Custom domain:** decided against — TLS is already handled via Caddy +
  `sslip.io`; a purchased domain would only replace that hostname.
- **Whether to enable the review queue on the public deployment later:**
  deferred (off for now) — would need `FMH_TRAINING_DATA_DIR` set on the
  VM, the pose model mounted, and instance sizing re-checked against the
  1.6 GB worst case.

Full narrative (the Railway→GCP switch, the TLS/mixed-content discovery,
the memory-measurement methodology) is in the
[investigation log](docs/INVESTIGATION_LOG.md#deployment).

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

Several real correctness bugs were found and fixed during development
(silent footage loss in stitching, duplicate frames across a splice,
hard-cut windows that were being silently re-bridged, a path-traversal
upload vulnerability, an inflated duration reading) — all fixed, all with
regression tests, full root-cause writeups in the
[investigation log](docs/INVESTIGATION_LOG.md#known-limitations--non-goals-for-this-version).
What follows is what's still actually true today:

- Detects action vs. dead time only — doesn't distinguish outcomes
  (whiff/hit/called strike). Investigated (audio amplitude, spectral
  ratio, defensive-reaction proxy) and closed as not viable on this
  footage: no camera/mic signal reliably separates a hit from a miss here.
- Detection is tuned to **never miss a real play**: borderline segments are
  kept by design, so some dead time will survive into the output.
- `enter_thresh` has a thin recall margin on quiet-contact events (e.g. a
  foul ball) at the reference camera distance. A camera-distance-aware
  scale boost is shipped to protect more-distant setups from losing that
  margin; a further ambient-motion discount to cut the remaining
  "walk-up gap" was investigated and explicitly not shipped — no available
  signal can safely tell a real slow play from ambient milling.
- Zone-velocity tightening doesn't yet handle a double play or continued
  relay at the *same* base within one play (no reference clip exercises
  this case), and only closes the smaller, extension-layer half of the
  walk-up gap.
- The review queue's multi-feature model and a local open-weight VLM are
  both real, statistically measurable signals, but both fail the recall-risk
  safety check — neither is wired into any cutting decision. Review-queue
  Tiers 2 (threshold calibration) and 3 (a learned classifier) aren't built
  yet; not enough labeled data has accumulated to clear this project's own
  bar for attempting either.
- A play split across a file boundary renders as two separate clips, not
  one continuous shot — deliberate, by design, not a bug.
- Nothing in `uploads/` is ever auto-deleted in v1 — storage grows
  unboundedly until a retention policy is added.
- The one full-length real-game run (`full_game.mkv`) validates
  performance and plausibility, not recall — it has no hand-annotated
  ground truth the way the 9 reference clips do.
- Multi-file/boundary handling was validated against real footage the user
  supplied but never committed (gitignored) — a fresh clone can run the
  synthetic-metadata tests but not repeat that exact end-to-end validation.
- The Edit Log's segment preview serves the original uploaded container
  (e.g. `.mkv`) as-is — plays fine in Chromium-based browsers, but Safari
  doesn't support MKV natively.
- Calibration accuracy depends on click precision — small offsets
  (roughly ±10-15px) are normal and fine; visually confirm the marker
  before continuing.
- Use `localhost:5173`, not `127.0.0.1:5173`, for the frontend dev server
  — a Node/Vite binding quirk in this environment, not a config bug.
- No manual "cut a kept segment" feature (the reverse of Restore) yet —
  the data model supports it, it's just not built.
- v1 targets **one sport at a time** (softball first); basketball comes
  later as a separate modular ruleset.
- No login, accounts, or user profiles. No team features, sharing, or
  recruiting/scouting layer. No personalization or per-user learning; no
  model training/fine-tuning shipped. No voice commands or
  natural-language editing. No native mobile app — v1 is a local web app.

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
  are never committed): nine softball clips (three general ~190s clips,
  four for base-occupancy validation, one foul ball, one missed swing) plus
  `distance_test_close/far.mov` (same subject/motion at two real camera
  distances, validating the enter_thresh scale finding against real footage).
- **Ground truth** lives in `tests/ground_truth/*.json`, one file per clip,
  hand-annotated by frame-level visual review: required events the
  detector must capture, non-required borderline events reported but not
  scored, and optional `atbat_expectations` pinning where the at-bat
  detector must or must not fire.
- **Regression script** (`scripts/regression.py`) runs the motion-only
  baseline and the full pipeline on every reference clip and fails the
  build if: a required event is missed; refined recall drops below the
  motion-only baseline; a vetoed segment overlaps a required event; a
  `check_continuity`-flagged window isn't covered by one contiguous kept
  segment; or an at-bat fire/no-fire expectation is violated.
- `tests/test_veto_e2e.py` and `tests/test_distance_scaling.py` run real
  model inference against constructed/real footage to validate the veto
  and the camera-distance scaling finding directly, rather than trusting a
  one-time measurement.
- **Backend API tests** (`tests/test_backend_api.py`, `tests/test_jobs.py`,
  `tests/test_storage.py`) run against FastAPI's test client with a fast
  fake standing in for the real pipeline; `scripts/smoke_api.py` is the
  real-server end-to-end check (upload → calibrate → detect → restore →
  re-export → download) against a live `uvicorn` process and a real clip.
- **Multi-file tests** cover ordering, calibration resolution, manifest
  file-boundary locking, and that no extension/at-bat state leaks between
  files processed back to back — see the
  [investigation log](docs/INVESTIGATION_LOG.md#testing) for the full list
  and the real two-file set they were validated against.
