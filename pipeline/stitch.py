"""Stitch a manifest's kept segments into one output video (Stage 5).

Takes the manifest built by scripts/detect.py or scripts/detect_multi.py
and the directory holding the original source files, and produces one
finished video containing exactly the kept spans, in file order
(`source_file_index`), each file's own kept spans in their own local
order (from `pipeline.manifest.kept_spans_by_file` — spans are never
merged across a file boundary, matching the Stage 3/4 boundary decision;
a play split across two files stays two clips back to back in the
output, not stitched into one continuous shot).

Two paths, chosen automatically per the project spec:
- **Stream copy** (`-c copy`): each kept span is extracted losslessly and
  the pieces are joined with ffmpeg's concat demuxer, no re-encode. Used
  whenever every source file that contributes a kept span shares the same
  codec, resolution, fps, and rotation — the concat demuxer requires
  uniform streams to produce a valid output. Fast and lossless, which
  matters a lot on a 30-60+ minute video.
- **Re-encode fallback**: used when source files disagree (mismatched
  resolution/fps/orientation — exactly what `pipeline.multifile` already
  flags at ordering time). Every span is normalized (scaled, padded to
  fill without distortion, rotated) to one common target — the largest
  resolution and highest fps among the inputs, so no source is
  downscaled — before joining. `StitchResult.reencoded`/`.reencode_reason`
  report when and why this happened; per the spec this must be surfaced,
  never silent.

Trimming a stream-copy span can't cut mid-GOP (no re-encode happens), so
ffmpeg's input-level seek starts a copied span at a keyframe at or before
the requested time — meaning a stream-copy export may include extra
footage before a kept span, never less. That's the right side to round on
per the priority rule (extra footage is acceptable, losing real action is
not). The re-encode path has no such rounding since every frame is
decoded.

How much extra depends on the source file's own keyframe interval (GOP),
which this module doesn't control and varies by camera/encoder — a
synthetic clip with a small GOP will show small slack, but that doesn't
bound what a real file with a larger GOP will show. It can also exceed
one GOP: verified on a real 67.5-minute MKV file (a 6.006s GOP throughout)
that ffmpeg's demuxer-level seek sometimes lands on the keyframe *before*
the nearest preceding one rather than the truly nearest one (observed on
7 of 120 real spans — max slack 6.17s, ~1.03 GOPs, vs. ~3.0s expected for
a uniformly-placed nearest-keyframe snap) — a known ffmpeg/MKV cue-index
seeking characteristic, not a bug in this module's command construction.
Still strictly extra, never lost, content — safe per the priority rule —
but worth knowing the bound is "roughly one to a bit over one *real*
source GOP," not a fixed number, and not something a small-GOP synthetic
test alone can validate.

**A real, since-fixed bug that inverted the above guarantee for a common
real-world case:** every current reference clip except full_game.mkv has
a nonzero video-stream start_time (2.4-4.5s each, likely encoder priming
delay from the recording device) — a container detail this module didn't
account for. build_extract_cmd's old `-ss {start_s} -to {end_s}` measured
those values against the container's own PTS timeline, which does NOT
start at 0 when start_time is nonzero; the effect was invisible for any
span with a nonzero start_s (ffmpeg still had to seek, and the normal
keyframe-snap slack above just looked a little larger than expected), but
for any span starting at start_s=0.0 — an extremely common case, e.g. a
clip's very first kept segment — the extracted clip came out SHORTER than
requested by roughly the stream's own start_time, a direct violation of
"never less, only extra." Found via an unrelated check (why didn't a
restored gap make the exported output longer), reproduced in complete
isolation with no server/frontend involved (direct `run_stitch()` call,
before/after ffprobe on the real rendered file), and root-caused precisely
(manually reproducing the exact byte-for-byte shortfall by testing several
-ss/-to pairs against the same file before touching any code). Fixed by
carrying each contributing file's own start_offset (from
`VideoParams.start_offset`, newly probed) onto every `SpanJob` from that
file and shifting BOTH -ss and -to by it — confirmed directly: the same
120.155s request that used to measure 115.690s now measures 120.195s
(extra, as intended, never short). This is a real correctness fix to
already-shipped stitching, not a change scoped to any one feature.

**A second real bug, introduced BY the fix above, found the same night:**
extending each span's own real end (the fix's whole point) can push it far
enough forward to overlap the NEXT span's own keyframe-snapped start,
whenever the real gap between two kept spans is smaller than the source's
GOP — both spans then independently decode the same real footage, and
after concatenation it plays twice. Confirmed with real frame-level
evidence, not inferred: extracting two adjacent spans from a real
clip_300.mkv batch separately and diffing frame hashes showed 90 of the
first 144 frames of the second span byte-identical to frames at the end
of the first — about 1.9 real seconds of footage duplicated. Checked
directly against the pre-fix code on the same real historical output:
zero duplicate frames there — this was new, not pre-existing, confirming
it came from extending spans' real ends rather than from anything
already shipped. Root cause: each span is extracted independently, and a
stream-copy `-ss` seek always snaps back to the nearest keyframe at or
before its target — when the real gap between two spans is smaller than
that snap-back distance can reach, their independently-decoded content
overlaps.

Fixed by predicting each span's real keyframe-snapped start
(`get_keyframe_times`/`predicted_seek_start`, from real per-file keyframe
probing, not an assumed constant GOP) and merging any two adjacent
same-file spans (`merge_overlapping_spans`) whose real extraction windows
would overlap into one — keeping the short real gap between them rather
than re-cutting it. This only ever adds a little more kept dead time
(the same direction this module's priority rule already treats as safe),
never a duplicate frame. Only applied on the stream-copy path; the
re-encode path decodes every frame exactly and has no keyframe-snap
behavior to guard against.

**Verification found two more real things worth recording.** First, the
verification method itself needed fixing before it could be trusted: an
initial approach (seek with `-ss` into the rendered output, re-encode to
PNG, hash each frame) produced its own false positives — confirmed by
comparing against a genuine from-t=0 decode of the same real region —
both from ordinary ffmpeg seek-accuracy behavior (any mid-file seek
reliably emits a spurious duplicate right at the seek point, reproduced
even on the pristine, untouched original clip_300.mkv) and, separately,
from something specific to checking near a real concat-demuxer join.
Switched to ffmpeg's own `framemd5` muxer (checksums the decoded picture
directly, no seek behavior to work around when scanning from a file's
true start) before trusting any result. Second, `predicted_seek_start`'s
first version — using only the mathematically-nearest keyframe — MISSED
a real duplicate on full_game.mkv specifically, because that file's own
MKV cue-index seeking sometimes lands one keyframe earlier than the
nearest one (already documented above, measured on 7 of 120 real spans).
Fixed by having the merge decision conservatively assume that worst case
by default (`predicted_seek_start(..., conservative=True)`) — confirmed
by reproducing the exact missed pair in isolation (80 duplicate frames)
and confirming the conservative prediction now merges it correctly.
Verified with real frame-by-frame comparison across every splice
boundary in all 9 reference clips plus full_game.mkv, using the corrected
verification method — see README's Known Limitations for the exact
numbers.
"""

import bisect
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoParams:
    path: str
    codec_name: str
    width: int
    height: int
    fps: float
    rotation: int  # degrees (0, 90, 180, 270); 0 if no rotation metadata
    # The video stream's own start_time (seconds), as ffprobe reports it --
    # NOT always 0. Real recordings commonly carry a nonzero value here
    # (encoder priming delay, an edit-list offset from how the device or a
    # remux wrote the container) -- confirmed on every current reference
    # clip except full_game.mkv (2.4-4.5s each). See build_extract_cmd's
    # docstring for why this matters: it is NOT just metadata to display,
    # it is required to correctly interpret -ss/-to.
    start_offset: float = 0.0


@dataclass
class SpanJob:
    """One kept span to extract, in final output order."""
    source_path: str
    start_s: float
    end_s: float
    clip_name: str  # filename for this span's intermediate clip
    # See VideoParams.start_offset -- copied onto the job so
    # build_extract_cmd doesn't need the full VideoParams, just the one
    # number it actually needs. Defaults to 0.0 (today's behavior) so
    # every existing caller/test that constructs a SpanJob directly is
    # unaffected.
    start_offset: float = 0.0
    # Which manifest source_files entry this job came from -- lets
    # compute_output_offsets match a real job back to the original
    # manifest segment(s) it was built from without string-matching on
    # source_path. Defaults to 0 so every existing caller/test that
    # constructs a SpanJob directly (single-file, index 0) is unaffected.
    source_file_index: int = 0


@dataclass
class SpanPlacement:
    """Where one real, already-extracted SpanJob actually landed in the
    output, measured from the real produced file rather than predicted
    from keyframe timestamps -- see compute_output_offsets and
    run_stitch's docstring for why measuring beats predicting here."""
    job: SpanJob
    output_start_s: float
    # The real source-local instant this job's rendered content actually
    # begins at. Equal to job.start_s on the re-encode path (frame-exact,
    # no keyframe-snap slack) and to job.end_s - <real measured duration>
    # on the stream-copy path (may be earlier than job.start_s: extra
    # real footage from keyframe-snap and/or merge_overlapping_spans).
    snapped_source_local_start: float


@dataclass
class StitchPlan:
    jobs: list  # list[SpanJob], in final concat order
    reencode: bool
    reencode_reason: str | None
    target: tuple | None  # (width, height, fps) if reencode else None


@dataclass
class StitchResult:
    output_path: str
    span_count: int
    reencoded: bool
    reencode_reason: str | None
    output_duration_s: float
    # {segment_id: (output_start_s, output_end_s)}, the REAL rendered
    # position of every kept manifest segment in this output -- measured
    # from the actually-extracted span files, not predicted. See
    # compute_output_offsets. Callers persist this onto the manifest via
    # pipeline.manifest.apply_output_offsets so the frontend never has to
    # re-derive it (see README's skip-ahead retraction writeup for why
    # re-deriving it client-side was wrong).
    segment_output_offsets: dict


def probe_video_params(path) -> VideoParams:
    """Read codec, resolution, fps, rotation, and the video stream's own
    start_time via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,start_time:"
         "stream_tags=rotate:stream_side_data=rotation",
         "-of", "default=noprint_wrappers=0", str(path)],
        capture_output=True, text=True, check=True).stdout

    info = {}
    for line in out.splitlines():
        line = line.strip()
        if "=" in line:
            k, v = line.split("=", 1)
            info.setdefault(k.removeprefix("TAG:"), v)

    num, den = (info.get("r_frame_rate", "0/1")).split("/")
    fps = float(num) / float(den) if float(den) else 0.0

    rotation = 0
    for key in ("rotation", "rotate"):
        raw = info.get(key)
        if raw not in (None, "N/A"):
            rotation = int(float(raw)) % 360
            break

    start_raw = info.get("start_time")
    start_offset = float(start_raw) if start_raw not in (None, "N/A") else 0.0

    return VideoParams(
        path=str(path), codec_name=info.get("codec_name", ""),
        width=int(info.get("width", 0)), height=int(info.get("height", 0)),
        fps=fps, rotation=rotation, start_offset=start_offset)


def probe_duration(path) -> float:
    """Real duration of an ALREADY-EXTRACTED span file's video stream --
    measured directly from the file ffmpeg actually produced, not
    predicted from keyframe timestamps. This is the ground-truth input
    to compute_output_offsets: predicting a real ffmpeg seek's landing
    point (predicted_seek_start, used for the merge decision) is a MODEL
    of ffmpeg's behavior, already documented elsewhere in this module as
    imperfect (the MKV cue-index quirk); measuring the file that was
    actually produced has no such risk. Reads the video stream's own
    duration first (matching pipeline.motion.py's own precedent of
    trusting only what's actually decodable over a nominal container
    value), falling back to the container-level duration only if the
    stream doesn't report one."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    if out and out != "N/A":
        return float(out)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def get_keyframe_times(path) -> list:
    """Real keyframe (packet-level) presentation timestamps for the video
    stream, via ffprobe packet flags -- the same real data source used to
    measure this project's own documented GOP numbers (see this module's
    docstring). Used to PREDICT exactly which keyframe a stream-copy `-ss`
    seek will actually land on, so two adjacent spans whose real gap is
    smaller than the source's GOP can be detected and merged instead of
    independently decoding overlapping real content (see
    merge_overlapping_spans)."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags",
         "-of", "csv=print_section=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    keyframes = []
    for line in out.splitlines():
        parts = line.strip().split(",")
        if len(parts) == 2 and "K" in parts[1] and parts[0] not in ("", "N/A"):
            keyframes.append(float(parts[0]))
    return sorted(keyframes)


def predicted_seek_start(requested_s: float, keyframes: list,
                         conservative: bool = True) -> float:
    """The real timestamp a stream-copy `-ss requested_s` will actually
    start decoding from: the nearest keyframe at or before it (matching
    ffmpeg's own input-seek behavior), or the first keyframe if
    requested_s is before every keyframe (e.g. before the stream's own
    real start). Returns requested_s unchanged if no keyframes are known
    (e.g. an empty/unprobeable list) -- callers treat that as "can't
    predict an overlap, don't merge on this basis alone."

    conservative=True (the default, and what merge_overlapping_spans
    always uses) additionally steps back ONE MORE keyframe beyond the
    mathematically-nearest one. This is not theoretical caution: this
    module's own docstring already documents, from real measurement on
    full_game.mkv, that ffmpeg's real MKV cue-index seek sometimes lands
    on the keyframe *before* the nearest preceding one (7 of 120 real
    spans, up to ~1.03 GOPs of slack) -- a real characteristic of this
    exact file, not a hypothetical. A first version of the overlap
    prediction used only the mathematically-nearest keyframe and MISSED a
    real duplicate-frame case on full_game.mkv as a direct result: it
    predicted no overlap between two spans, but the real seek landed one
    keyframe earlier than predicted, and the two spans' independently
    decoded content genuinely overlapped anyway. Confirmed directly:
    reproducing that exact pair in isolation showed 80 duplicate frames;
    stepping back one extra keyframe here predicts the real (further-back)
    start correctly and the merge now catches it."""
    if not keyframes:
        return requested_s
    idx = bisect.bisect_right(keyframes, requested_s) - 1
    if idx < 0:
        return keyframes[0]
    if conservative and idx > 0:
        idx -= 1
    return keyframes[idx]


def merge_overlapping_spans(spans, start_offset: float, keyframes: list,
                            safety_margin_s: float = 0.1) -> list:
    """Merge consecutive (start_s, end_s) spans (already sorted, all from
    the SAME source file) whenever independently stream-copy-extracting
    them would decode overlapping real content.

    Root cause this guards against (a real, confirmed bug, not a
    theoretical one): each span's own `-ss` seeks to the nearest keyframe
    AT OR BEFORE its requested start. If the real gap between two kept
    spans is smaller than the source's own keyframe interval (GOP), the
    LATER span's keyframe-snapped start can land BEFORE the EARLIER span's
    own real end -- both spans then independently decode the same real
    footage, and after concatenation that footage plays twice (confirmed
    directly on a real file: 90 of 144 frames at the start of one span's
    extracted clip were byte-identical duplicates of frames at the end of
    the previous span's clip). Merging the two spans into one — keeping
    the real (short) gap between them rather than re-cutting it — trades
    a small amount of extra kept dead time for eliminating the duplicate
    entirely; extra kept footage is exactly the direction this module's
    priority rule already accepts as safe, a duplicated frame is not.

    Only meaningful for the stream-copy path — the re-encode path decodes
    every frame exactly and has no keyframe-snap behavior at all, so
    callers should not apply this there (see plan_stitch)."""
    if not spans:
        return []
    merged = [list(spans[0])]
    for a, b in spans[1:]:
        current_end_real = merged[-1][1] + start_offset
        next_start_real = predicted_seek_start(a + start_offset, keyframes)
        if next_start_real <= current_end_real + safety_margin_s:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(s) for s in merged]


def needs_reencode(infos: list) -> tuple:
    """Whether the given VideoParams disagree enough to require a
    re-encode for a valid concat-demuxer join. Returns (bool, reason)."""
    if len(infos) <= 1:
        return False, None
    codecs = {i.codec_name for i in infos}
    dims = {(i.width, i.height) for i in infos}
    fpses = {round(i.fps, 2) for i in infos}
    rotations = {i.rotation for i in infos}
    reasons = []
    if len(codecs) > 1:
        reasons.append(f"mismatched codec ({sorted(codecs)})")
    if len(dims) > 1:
        reasons.append(f"mismatched resolution ({sorted(dims)})")
    if len(fpses) > 1:
        reasons.append(f"mismatched frame rate ({sorted(fpses)})")
    if len(rotations) > 1:
        reasons.append(f"mismatched orientation ({sorted(rotations)})")
    if not reasons:
        return False, None
    return True, "; ".join(reasons)


def choose_target_params(infos: list) -> tuple:
    """Largest resolution and highest fps among the inputs — the
    re-encode target never downscales or downsamples a source file."""
    # orientation-normalized dimensions: a 90/270-rotated stream's
    # displayed width/height is swapped from its stored width/height
    def displayed(i):
        return (i.height, i.width) if i.rotation in (90, 270) else (i.width, i.height)

    w = max(displayed(i)[0] for i in infos)
    h = max(displayed(i)[1] for i in infos)
    fps = max(i.fps for i in infos)
    return (w, h, fps)


def plan_stitch(manifest: dict, source_dir, prober=probe_video_params,
               keyframe_prober=get_keyframe_times) -> StitchPlan:
    """Pure planning logic: resolve source paths, probe them, decide
    stream-copy vs. re-encode, and build the ordered list of span
    extraction jobs. `prober`/`keyframe_prober` are injectable so this is
    testable without real video files or ffmpeg.

    On the stream-copy path only, adjacent same-file spans are merged
    first (merge_overlapping_spans) whenever independent extraction would
    decode overlapping real content — see that function's docstring for
    the real duplicate-frame bug this prevents. Never applied on the
    re-encode path: every frame there is actually decoded, so there is no
    keyframe-snap behavior to predict or guard against."""
    from pipeline.manifest import kept_spans_by_file

    by_file = kept_spans_by_file(manifest)
    contributing = [f for f in by_file if f["spans"]]
    if not contributing:
        return StitchPlan(jobs=[], reencode=False, reencode_reason=None, target=None)

    source_dir = Path(source_dir)
    infos = [prober(source_dir / f["source_file"]) for f in contributing]
    reencode, reason = needs_reencode(infos)
    target = choose_target_params(infos) if reencode else None
    # one probe per contributing file, keyed by source_file so every span
    # from that file gets the SAME offset -- see build_extract_cmd
    offset_by_file = {f["source_file"]: info.start_offset
                      for f, info in zip(contributing, infos)}

    jobs = []
    seq = 0
    for f in by_file:
        if not f["spans"]:
            continue
        src_path = source_dir / f["source_file"]
        start_offset = offset_by_file.get(f["source_file"], 0.0)
        spans = f["spans"]
        if not reencode:
            keyframes = keyframe_prober(src_path)
            spans = merge_overlapping_spans(spans, start_offset, keyframes)
        for start_s, end_s in spans:
            seq += 1
            jobs.append(SpanJob(
                source_path=str(src_path), start_s=start_s, end_s=end_s,
                clip_name=f"span_{seq:04d}.mp4", start_offset=start_offset,
                source_file_index=f["source_file_index"]))

    return StitchPlan(jobs=jobs, reencode=reencode, reencode_reason=reason,
                      target=target)


def build_extract_cmd(job: SpanJob, out_path, reencode: bool,
                      target: tuple | None = None) -> list:
    """ffmpeg command to extract one kept span to its own clip file.

    job.start_s/end_s are in the SOURCE FILE's own nominal timeline (t=0
    at the very start of the file, matching the manifest and every other
    part of this project) -- but ffmpeg's -ss/-to, as INPUT-side options,
    are measured against the container's own internal PTS timeline, which
    is not guaranteed to start at 0. job.start_offset (the video stream's
    own start_time, see VideoParams) corrects for that mismatch by
    shifting both -ss and -to by the same amount, so "0.0" in this job
    still means "the true start of the file" to ffmpeg. Confirmed as a
    REAL bug, not a theoretical one: found while chasing an export that
    silently didn't grow after a real restore -- clip_540.mkv's video
    stream starts at PTS 4.506s (every current reference clip except
    full_game.mkv has a similar nonzero offset, 2.4-4.5s), so a span
    starting at job.start_s=0.0 with the OLD unshifted -ss 0 -to X
    request came out ~4.5s SHORTER than requested, a direct violation of
    this module's own "never less, only extra" guarantee. Verified fixed
    directly: the same span before the fix measured 115.690s against a
    120.155s request; after shifting both -ss/-to by 4.506s it measured
    120.195s (extra, as documented, never short)."""
    base = ["ffmpeg", "-y",
            "-ss", f"{job.start_s + job.start_offset}",
            "-to", f"{job.end_s + job.start_offset}",
            "-i", job.source_path]
    if not reencode:
        return base + ["-c", "copy", "-avoid_negative_ts", "make_zero",
                       str(out_path)]
    w, h, fps = target
    scale_pad = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    return base + [
        "-vf", scale_pad, "-r", f"{fps}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-c:a", "aac", str(out_path)]


def build_concat_list(clip_paths) -> str:
    """Content of the ffmpeg concat-demuxer list file for these clips, in
    order. Quoting follows ffmpeg's concat format (single-quoted paths,
    literal single quotes escaped as '\\'')."""
    lines = []
    for p in clip_paths:
        escaped = str(p).replace("'", r"'\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def build_concat_cmd(list_path, out_path) -> list:
    return ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_path), "-c", "copy", str(out_path)]


def compute_output_offsets(manifest: dict, placements: list) -> dict:
    """Maps every KEPT manifest segment's id to its real
    (output_start_s, output_end_s) position in the actually-rendered
    output, given each job's REAL measured placement (see run_stitch) --
    not a value re-derived from nominal segment durations, which is
    exactly the assumption that was proven wrong (see README's skip-ahead
    retraction writeup): merge_overlapping_spans can pull several kept
    segments into one physical rendered span, and stream-copy keyframe-
    snap can shift a span's real start earlier than its nominal one.

    A kept segment always lies fully inside exactly one placement's job
    range: merge_overlapping_spans only ever WIDENS a job's range to
    include more original spans, never splits one apart, and
    kept_spans_by_file already merges any segments that were literally
    adjacent before plan_stitch ever runs."""
    offsets = {}
    for seg in manifest["segments"]:
        if seg["status"] != "kept":
            continue
        for p in placements:
            if (p.job.source_file_index == seg["source_file_index"]
                    and p.job.start_s <= seg["start_s"]
                    and seg["end_s"] <= p.job.end_s):
                rel_start = seg["start_s"] - p.snapped_source_local_start
                rel_end = seg["end_s"] - p.snapped_source_local_start
                offsets[seg["id"]] = (p.output_start_s + rel_start,
                                      p.output_start_s + rel_end)
                break
    return offsets


def run_stitch(manifest: dict, source_dir, output_path, work_dir=None,
              prober=probe_video_params, keyframe_prober=get_keyframe_times,
              duration_prober=probe_duration,
              runner=None, on_stage=None) -> StitchResult:
    """Execute a stitch plan: extract every kept span, then concat them
    into `output_path`. `work_dir` holds intermediate per-span clips
    (a temp dir is used and cleaned up if not given). `runner` defaults
    to actually invoking ffmpeg via subprocess; tests can inject a fake
    to check commands without running real video I/O -- when doing so,
    also inject a fake `duration_prober` (real ffprobe has nothing to
    measure if `runner` never actually wrote the extracted files).
    `on_stage`, if given, is called with a human-readable stage name
    before extraction and again before the final concat (added for the
    backend's progress reporting; scripts/stitch.py doesn't pass one).

    Also computes where every kept manifest segment REALLY landed in the
    output -- see compute_output_offsets -- this is what fixed the
    skip-ahead output-time mapping bug (see README). Two different
    sources of truth, deliberately not conflated: each job's real
    ANCHOR (where its content actually starts, on the stream-copy path)
    is predicted_seek_start against the source's own real keyframes --
    reused, not re-derived, because a stream-copy start MUST land on a
    real keyframe, so this is a hard constraint, not a soft guess (its
    one known residual risk, the MKV cue-index quirk documented above,
    still applies here same as everywhere else it's used). Each job's
    real DURATION (how far the cursor advances for the next job) is
    measured directly from the extracted file instead
    (`duration_prober`), NOT derived from that anchor, because an
    initial version that back-derived the anchor from measured duration
    (`job.end_s - real_duration`) was caught by this fix's own test
    suite to be wrong: a stream-copied span's trailing frame near its
    `-to` cutoff can be reordered/duplicated by B-frame remuxing in a way
    that inflates measured duration by about one frame without moving
    where the content actually starts, corrupting exactly the number
    this whole fix depends on getting right."""
    import tempfile

    if runner is None:
        def runner(cmd):
            subprocess.run(cmd, check=True, capture_output=True)

    plan = plan_stitch(manifest, source_dir, prober=prober,
                      keyframe_prober=keyframe_prober)
    if not plan.jobs:
        raise ValueError("manifest has no kept segments to stitch")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    def _do_work(work_dir):
        # must be absolute: the concat demuxer resolves each list entry's
        # relative path against the LIST FILE's own directory, not the
        # process cwd, so a relative work_dir would make ffmpeg look for
        # e.g. work_dir/work_dir/span_0001.mp4 and fail to find it
        work_dir = Path(work_dir).resolve()
        if on_stage:
            on_stage("extracting kept segments")
        clip_paths = []
        placements = []
        cursor = 0.0
        keyframes_by_path = {}
        for job in plan.jobs:
            out = work_dir / job.clip_name
            cmd = build_extract_cmd(job, out, plan.reencode, plan.target)
            runner(cmd)
            clip_paths.append(out)

            if plan.reencode:
                # every frame is actually decoded and re-encoded starting
                # at the requested time -- no keyframe-snap slack
                snapped_source_local_start = job.start_s
            else:
                if job.source_path not in keyframes_by_path:
                    keyframes_by_path[job.source_path] = keyframe_prober(job.source_path)
                snapped_abs = predicted_seek_start(
                    job.start_s + job.start_offset,
                    keyframes_by_path[job.source_path], conservative=False)
                snapped_source_local_start = snapped_abs - job.start_offset

            placements.append(SpanPlacement(
                job=job, output_start_s=cursor,
                snapped_source_local_start=snapped_source_local_start))
            cursor += duration_prober(out)

        if on_stage:
            on_stage("stitching output")
        list_path = work_dir / "concat_list.txt"
        list_path.write_text(build_concat_list(clip_paths))
        runner(build_concat_cmd(list_path, output_path))
        return placements

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        placements = _do_work(work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="fmh_stitch_") as tmp:
            placements = _do_work(tmp)

    # sum of requested span lengths, not a re-probe of the rendered file:
    # a stream-copy export may include a little extra before a span's
    # start (keyframe snapping, see module docstring), so this is the
    # requested duration, not necessarily the exact rendered one
    duration = sum(job.end_s - job.start_s for job in plan.jobs)

    return StitchResult(
        output_path=str(output_path), span_count=len(plan.jobs),
        reencoded=plan.reencode, reencode_reason=plan.reencode_reason,
        output_duration_s=duration,
        segment_output_offsets=compute_output_offsets(manifest, placements))
