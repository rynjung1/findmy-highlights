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
"""

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


def plan_stitch(manifest: dict, source_dir, prober=probe_video_params) -> StitchPlan:
    """Pure planning logic: resolve source paths, probe them, decide
    stream-copy vs. re-encode, and build the ordered list of span
    extraction jobs. `prober` is injectable so this is testable without
    real video files or ffmpeg."""
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
        for start_s, end_s in f["spans"]:
            seq += 1
            jobs.append(SpanJob(
                source_path=str(src_path), start_s=start_s, end_s=end_s,
                clip_name=f"span_{seq:04d}.mp4", start_offset=start_offset))

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


def run_stitch(manifest: dict, source_dir, output_path, work_dir=None,
              prober=probe_video_params, runner=None,
              on_stage=None) -> StitchResult:
    """Execute a stitch plan: extract every kept span, then concat them
    into `output_path`. `work_dir` holds intermediate per-span clips
    (a temp dir is used and cleaned up if not given). `runner` defaults
    to actually invoking ffmpeg via subprocess; tests can inject a fake
    to check commands without running real video I/O. `on_stage`, if
    given, is called with a human-readable stage name before extraction
    and again before the final concat (added for the backend's progress
    reporting; scripts/stitch.py doesn't pass one)."""
    import tempfile

    if runner is None:
        def runner(cmd):
            subprocess.run(cmd, check=True, capture_output=True)

    plan = plan_stitch(manifest, source_dir, prober=prober)
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
        for job in plan.jobs:
            out = work_dir / job.clip_name
            cmd = build_extract_cmd(job, out, plan.reencode, plan.target)
            runner(cmd)
            clip_paths.append(out)

        if on_stage:
            on_stage("stitching output")
        list_path = work_dir / "concat_list.txt"
        list_path.write_text(build_concat_list(clip_paths))
        runner(build_concat_cmd(list_path, output_path))

    if work_dir is not None:
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        _do_work(work_dir)
    else:
        with tempfile.TemporaryDirectory(prefix="fmh_stitch_") as tmp:
            _do_work(tmp)

    # sum of requested span lengths, not a re-probe of the rendered file:
    # a stream-copy export may include a little extra before a span's
    # start (keyframe snapping, see module docstring), so this is the
    # requested duration, not necessarily the exact rendered one
    duration = sum(job.end_s - job.start_s for job in plan.jobs)

    return StitchResult(
        output_path=str(output_path), span_count=len(plan.jobs),
        reencoded=plan.reencode, reencode_reason=plan.reencode_reason,
        output_duration_s=duration)
