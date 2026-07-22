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

*(To be filled in as components are built.)*

Planned pieces and how they connect:

- **Detection pipeline** — analyzes video with OpenCV (motion intensity, later
  a pretrained player/object detector) and flags candidate action segments.
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

## How to run it

*(To be filled in — no runnable pipeline or UI exists yet.)*

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

*(To be filled in as the test suite is built.)*

- Unit tests (pytest) will live in `/tests`, covering manifest read/write,
  segment merge/padding math, multi-file ordering, and status updates.
- A small set of reference sample clips (kept outside version control) with
  hand-written ground-truth annotations serve as the fixed yardstick for
  detection quality.
- A regression script will run the full detection pipeline against every
  reference clip and report recall (known plays captured) and rough
  over-inclusion, with recall weighted more heavily.
