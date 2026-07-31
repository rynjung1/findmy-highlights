"""Unit tests for Stage 5 stitching: reencode-vs-copy decision, target
resolution/fps selection, ffmpeg command construction, and the span-job
plan built from a manifest. Most of this is pure logic — no real ffmpeg
or video files needed; ffmpeg invocation is checked via an injected fake
runner/prober. A final section runs real ffmpeg against tiny synthetic
clips (same pattern as tests/test_multifile.py's real-ffprobe smoke
test) to catch bugs the fake-runner tests structurally can't, such as
path-resolution issues that only surface when ffmpeg actually resolves
the concat list file from disk."""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.manifest import build_manifest, build_multi_file_manifest
from pipeline.stitch import (VideoParams, build_concat_cmd, build_concat_list,
                             build_extract_cmd, choose_target_params,
                             get_keyframe_times, merge_overlapping_spans,
                             needs_reencode, plan_stitch, predicted_seek_start,
                             probe_video_params, run_stitch)


def vp(path="a.mp4", codec="h264", w=1920, h=1080, fps=30.0, rotation=0,
      start_offset=0.0):
    return VideoParams(path=path, codec_name=codec, width=w, height=h,
                       fps=fps, rotation=rotation, start_offset=start_offset)


def no_keyframes(path):
    """Fake keyframe_prober for tests using fake (nonexistent) video
    paths: an empty keyframe list makes merge_overlapping_spans a no-op
    (predicted_seek_start falls back to the requested time unchanged),
    without ever shelling out to real ffprobe."""
    return []


# ---- needs_reencode ----

def test_single_file_never_needs_reencode():
    assert needs_reencode([vp()]) == (False, None)


def test_identical_params_no_reencode():
    assert needs_reencode([vp(path="a"), vp(path="b")]) == (False, None)


def test_mismatched_resolution_triggers_reencode():
    reencode, reason = needs_reencode([vp(path="a", w=1920, h=1080),
                                        vp(path="b", w=1280, h=720)])
    assert reencode
    assert "resolution" in reason


def test_mismatched_fps_triggers_reencode():
    reencode, reason = needs_reencode([vp(path="a", fps=30.0),
                                        vp(path="b", fps=60.0)])
    assert reencode
    assert "frame rate" in reason


def test_mismatched_codec_triggers_reencode():
    reencode, reason = needs_reencode([vp(path="a", codec="h264"),
                                        vp(path="b", codec="hevc")])
    assert reencode
    assert "codec" in reason


def test_mismatched_rotation_triggers_reencode():
    reencode, reason = needs_reencode([vp(path="a", rotation=0),
                                        vp(path="b", rotation=90)])
    assert reencode
    assert "orientation" in reason


def test_multiple_mismatches_all_named_in_reason():
    reencode, reason = needs_reencode([
        vp(path="a", w=1920, h=1080, fps=30.0),
        vp(path="b", w=1280, h=720, fps=60.0)])
    assert reencode
    assert "resolution" in reason and "frame rate" in reason


# ---- choose_target_params ----

def test_target_picks_largest_dims_and_fps():
    target = choose_target_params([vp(path="a", w=1280, h=720, fps=30.0),
                                    vp(path="b", w=1920, h=1080, fps=60.0)])
    assert target == (1920, 1080, 60.0)


def test_target_accounts_for_rotated_orientation():
    # a 1080x1920 stream rotated 90 degrees DISPLAYS as 1920x1080 —
    # target selection must not treat it as the smaller portrait frame
    target = choose_target_params([
        vp(path="a", w=1080, h=1920, fps=30.0, rotation=90),
        vp(path="b", w=1280, h=720, fps=30.0)])
    assert target == (1920, 1080, 30.0)


# ---- plan_stitch ----

def two_file_manifest():
    return build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(2.0, 10.0), (15.0, 20.0)]},
        {"source_file": "part2.mp4", "duration": 40.0,
         "kept_segments": [(0.0, 5.0)]},
    ])


def test_plan_stitch_jobs_follow_file_and_span_order(tmp_path):
    m = two_file_manifest()
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)),
                       keyframe_prober=no_keyframes)
    assert [(j.start_s, j.end_s) for j in plan.jobs] == [
        (2.0, 10.0), (15.0, 20.0), (0.0, 5.0)]
    assert plan.jobs[0].source_path.endswith("part1.mp4")
    assert plan.jobs[2].source_path.endswith("part2.mp4")


def test_plan_stitch_clip_names_sequential_and_unique(tmp_path):
    m = two_file_manifest()
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)),
                       keyframe_prober=no_keyframes)
    names = [j.clip_name for j in plan.jobs]
    assert len(names) == len(set(names))


def test_plan_stitch_no_reencode_when_params_match(tmp_path):
    m = two_file_manifest()
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)),
                       keyframe_prober=no_keyframes)
    assert plan.reencode is False
    assert plan.target is None


def test_plan_stitch_reencode_when_params_mismatch(tmp_path):
    m = two_file_manifest()

    def prober(path):
        if "part1" in str(path):
            return vp(path=str(path), w=1920, h=1080, fps=30.0)
        return vp(path=str(path), w=1280, h=720, fps=60.0)

    plan = plan_stitch(m, tmp_path, prober=prober, keyframe_prober=no_keyframes)
    assert plan.reencode is True
    assert plan.target == (1920, 1080, 60.0)
    assert "resolution" in plan.reencode_reason


def test_plan_stitch_probes_only_files_with_kept_spans(tmp_path):
    # part2 has no kept segments at all -> should never be probed, since
    # it contributes nothing to the output and might not even be a valid
    # video (e.g. corrupt/unsupported file that was entirely cut)
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0,
         "kept_segments": [(2.0, 10.0)]},
        {"source_file": "part2.mp4", "duration": 40.0, "kept_segments": []},
    ])
    probed = []

    def prober(path):
        probed.append(str(path))
        return vp(path=str(path))

    plan_stitch(m, tmp_path, prober=prober, keyframe_prober=no_keyframes)
    assert len(probed) == 1
    assert "part1.mp4" in probed[0]


def test_plan_stitch_propagates_per_file_start_offset(tmp_path):
    # regression test for a real bug: every job from a given file must
    # carry THAT file's own start_offset (probed once per contributing
    # file), not a shared/default value -- a multi-file batch can easily
    # mix a file with a nonzero start_time and one without
    m = two_file_manifest()

    def prober(path):
        if "part1" in str(path):
            return vp(path=str(path), start_offset=4.506)
        return vp(path=str(path), start_offset=0.0)

    plan = plan_stitch(m, tmp_path, prober=prober, keyframe_prober=no_keyframes)
    part1_jobs = [j for j in plan.jobs if "part1" in j.source_path]
    part2_jobs = [j for j in plan.jobs if "part2" in j.source_path]
    assert all(j.start_offset == 4.506 for j in part1_jobs)
    assert all(j.start_offset == 0.0 for j in part2_jobs)


def test_plan_stitch_empty_manifest_yields_no_jobs(tmp_path):
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0, "kept_segments": []}])
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)))
    assert plan.jobs == []
    assert plan.reencode is False


# ---- predicted_seek_start / merge_overlapping_spans ----
# Regression coverage for a real bug: extending each span's own real end
# (the start_offset fix) can push it past the NEXT span's own
# keyframe-snapped start when the real gap between them is smaller than
# the source's GOP -- both spans then independently decode the same real
# content, which plays twice after concat. Confirmed on a real clip_300.mkv
# batch (90 of 144 frames at the start of one span byte-identical to
# frames at the end of the previous span) before any of this was written.

def test_predicted_seek_start_snaps_to_nearest_keyframe_at_or_before():
    # conservative=False: the raw, mathematically-nearest keyframe, no
    # extra safety step-back
    keyframes = [0.0, 6.0, 12.0, 18.0]
    assert predicted_seek_start(10.0, keyframes, conservative=False) == 6.0
    assert predicted_seek_start(12.0, keyframes, conservative=False) == 12.0  # exact match
    assert predicted_seek_start(17.999, keyframes, conservative=False) == 12.0


def test_predicted_seek_start_conservative_steps_back_one_more_keyframe():
    # conservative=True (the default) -- regression test for a real bug:
    # the non-conservative version missed a real duplicate-frame case on
    # full_game.mkv because ffmpeg's actual MKV cue-index seek landed one
    # keyframe earlier than the mathematically-nearest one (a real,
    # already-measured characteristic of this file, not hypothetical --
    # see this module's own docstring). Reproduces the exact real
    # keyframe spacing around the missed case (6.006s GOP).
    keyframes = [1129.128, 1135.134, 1141.14, 1147.146, 1153.152, 1159.158]
    assert predicted_seek_start(1147.246, keyframes) == 1141.14  # one step back
    assert predicted_seek_start(1147.246, keyframes, conservative=False) == 1147.146


def test_predicted_seek_start_conservative_never_goes_before_first_keyframe():
    keyframes = [4.266, 10.272, 16.278]
    assert predicted_seek_start(10.272, keyframes) == 4.266  # exact match, steps back
    assert predicted_seek_start(4.266, keyframes) == 4.266  # already at the first one


def test_predicted_seek_start_before_first_keyframe_clamps_to_it():
    keyframes = [4.266, 10.272, 16.278]
    assert predicted_seek_start(1.0, keyframes) == 4.266


def test_predicted_seek_start_empty_keyframes_returns_unchanged():
    assert predicted_seek_start(12.0, []) == 12.0


def test_merge_overlapping_spans_merges_when_gap_smaller_than_gop():
    # gap between spans is 3.36s; the second span's -ss (47.25+4.266=51.516)
    # snaps back to keyframe 46.308 -- BEFORE the first span's own real end
    # (43.892+4.266=48.158) -- a real overlap, must merge
    keyframes = [4.266 + 6.006 * n for n in range(20)]  # matches clip_300's real GOP
    spans = [(3.873, 43.892), (47.250, 80.010)]
    merged = merge_overlapping_spans(spans, start_offset=4.266, keyframes=keyframes)
    assert merged == [(3.873, 80.010)]


def test_merge_overlapping_spans_does_not_merge_when_gap_larger_than_gop():
    # gap of 10s, larger than the 6.006s GOP -- no overlap, stays separate
    keyframes = [4.266 + 6.006 * n for n in range(20)]
    spans = [(3.873, 40.0), (50.0, 80.0)]
    merged = merge_overlapping_spans(spans, start_offset=4.266, keyframes=keyframes)
    assert merged == [(3.873, 40.0), (50.0, 80.0)]


def test_merge_overlapping_spans_catches_gap_between_one_and_two_gops():
    # regression test for a real bug: a gap BETWEEN one and two real GOPs
    # (here 4.465s, GOP 6.006s) is exactly the case the non-conservative
    # prediction gets wrong on a file with the documented MKV cue-index
    # seek quirk (see predicted_seek_start's docstring) -- confirmed on
    # real full_game.mkv footage: extracting this exact pair in isolation
    # showed 80 duplicate frames before this fix.
    keyframes = [1129.128, 1135.134, 1141.14, 1147.146, 1153.152, 1159.158]
    spans = [(1136.776, 1142.781), (1147.246, 1159.152)]
    merged = merge_overlapping_spans(spans, start_offset=0.0, keyframes=keyframes)
    assert merged == [(1136.776, 1159.152)]


def test_merge_overlapping_spans_cascades_across_three_spans():
    keyframes = [6.0 * n for n in range(20)]
    # each gap is 1s, well under the 6s GOP -- all three should merge into one
    spans = [(1.0, 10.0), (11.0, 20.0), (21.0, 30.0)]
    merged = merge_overlapping_spans(spans, start_offset=0.0, keyframes=keyframes)
    assert merged == [(1.0, 30.0)]


def test_merge_overlapping_spans_empty_keyframes_is_noop():
    # no keyframe data available -> can't predict overlap, don't merge
    # (predicted_seek_start falls back to the raw requested start, so
    # merging only happens if spans already literally touch/overlap)
    spans = [(3.873, 43.892), (47.250, 80.010)]
    merged = merge_overlapping_spans(spans, start_offset=4.266, keyframes=[])
    assert merged == [(3.873, 43.892), (47.250, 80.010)]


def test_merge_overlapping_spans_single_span_unchanged():
    assert merge_overlapping_spans([(1.0, 5.0)], 0.0, [0.0, 3.0, 6.0]) == [(1.0, 5.0)]


def test_merge_overlapping_spans_empty_input():
    assert merge_overlapping_spans([], 0.0, [0.0, 3.0]) == []


def test_plan_stitch_merges_overlapping_spans_on_copy_path_not_reencode(tmp_path):
    # the merge must only ever run on the stream-copy path -- the
    # re-encode path decodes every frame exactly and has no keyframe-snap
    # behavior for it to guard against
    m = build_manifest("g.mp4", 100.0, [(3.873, 43.892), (47.250, 80.010)])
    keyframes = [4.266 + 6.006 * n for n in range(20)]

    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p), start_offset=4.266),
                       keyframe_prober=lambda p: keyframes)
    assert [(j.start_s, j.end_s) for j in plan.jobs] == [(3.873, 80.010)]


# ---- build_extract_cmd ----

def test_extract_cmd_copy_path_uses_stream_copy():
    from pipeline.stitch import SpanJob
    job = SpanJob(source_path="in.mp4", start_s=1.5, end_s=4.0,
                 clip_name="span_0001.mp4")
    cmd = build_extract_cmd(job, "out/span_0001.mp4", reencode=False)
    assert "-c" in cmd and "copy" in cmd
    assert "-ss" in cmd and "1.5" in cmd
    assert "-to" in cmd and "4.0" in cmd
    assert "-vf" not in cmd


def test_extract_cmd_shifts_ss_and_to_by_start_offset():
    # regression test for a real bug (see pipeline/stitch.py's module
    # docstring and build_extract_cmd's docstring): a source file whose
    # video stream doesn't start at PTS 0 needs BOTH -ss and -to shifted
    # by that offset, or a span starting at start_s=0.0 comes out
    # SHORTER than requested -- a direct violation of "never less, only
    # extra". Confirmed on a real reference clip below; this is the pure,
    # deterministic check that the shift is applied correctly.
    from pipeline.stitch import SpanJob
    job = SpanJob(source_path="in.mp4", start_s=0.0, end_s=120.155,
                 clip_name="span_0001.mp4", start_offset=4.506)
    cmd = build_extract_cmd(job, "out/span_0001.mp4", reencode=False)
    ss = cmd[cmd.index("-ss") + 1]
    to = cmd[cmd.index("-to") + 1]
    assert float(ss) == pytest.approx(4.506)
    assert float(to) == pytest.approx(124.661)


def test_extract_cmd_zero_offset_is_unaffected():
    # default start_offset=0.0 -> byte-identical to pre-fix behavior
    from pipeline.stitch import SpanJob
    job = SpanJob(source_path="in.mp4", start_s=1.5, end_s=4.0,
                 clip_name="span_0001.mp4")
    cmd = build_extract_cmd(job, "out/span_0001.mp4", reencode=False)
    assert "1.5" in cmd and "4.0" in cmd


def test_extract_cmd_reencode_path_scales_and_pads():
    from pipeline.stitch import SpanJob
    job = SpanJob(source_path="in.mp4", start_s=1.5, end_s=4.0,
                 clip_name="span_0001.mp4")
    cmd = build_extract_cmd(job, "out/span_0001.mp4", reencode=True,
                            target=(1920, 1080, 30.0))
    assert "-vf" in cmd
    vf = cmd[cmd.index("-vf") + 1]
    assert "scale=1920:1080" in vf
    assert "pad=1920:1080" in vf
    assert "-r" in cmd and "30.0" in cmd
    assert "libx264" in cmd


# ---- build_concat_list / build_concat_cmd ----

def test_concat_list_format():
    content = build_concat_list(["/a/b.mp4", "/a/c.mp4"])
    assert content == "file '/a/b.mp4'\nfile '/a/c.mp4'\n"


def test_concat_list_escapes_single_quotes():
    content = build_concat_list(["/a/it's a clip.mp4"])
    assert content == "file '/a/it'\\''s a clip.mp4'\n"


def test_concat_cmd_uses_demuxer_and_stream_copy():
    cmd = build_concat_cmd("list.txt", "out.mp4")
    assert cmd[:4] == ["ffmpeg", "-y", "-f", "concat"]
    assert "-c" in cmd and "copy" in cmd
    assert "list.txt" in cmd and "out.mp4" in cmd


# ---- run_stitch orchestration (fake runner, no real ffmpeg) ----

def test_run_stitch_invokes_one_extract_per_span_plus_one_concat(tmp_path):
    m = two_file_manifest()
    calls = []

    def fake_runner(cmd):
        calls.append(cmd)

    out = tmp_path / "out" / "final.mp4"
    result = run_stitch(m, tmp_path, out, work_dir=tmp_path / "work",
                        prober=lambda p: vp(path=str(p)),
                        keyframe_prober=no_keyframes, runner=fake_runner)

    # 3 kept spans across the two files -> 3 extract calls + 1 concat call
    assert len(calls) == 4
    assert calls[-1][:4] == ["ffmpeg", "-y", "-f", "concat"]
    assert result.span_count == 3
    assert result.reencoded is False
    assert result.output_duration_s == pytest.approx(8.0 + 5.0 + 5.0)
    assert out.parent.exists()  # output dir created even though ffmpeg is faked


def test_run_stitch_reports_reencode_reason(tmp_path):
    m = two_file_manifest()

    def prober(path):
        if "part1" in str(path):
            return vp(path=str(path), w=1920, h=1080)
        return vp(path=str(path), w=1280, h=720)

    result = run_stitch(m, tmp_path, tmp_path / "out.mp4",
                        work_dir=tmp_path / "work", prober=prober,
                        runner=lambda cmd: None)
    assert result.reencoded is True
    assert "resolution" in result.reencode_reason


def test_run_stitch_raises_on_no_kept_segments(tmp_path):
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0, "kept_segments": []}])
    with pytest.raises(ValueError):
        run_stitch(m, tmp_path, tmp_path / "out.mp4",
                  prober=lambda p: vp(path=str(p)), runner=lambda cmd: None)


# ---- real ffmpeg smoke tests (needs ffmpeg on PATH, no reference clips
# needed — tiny synthetic clips generated on the fly). These exist because
# the fake-runner tests above only check which commands WOULD be built;
# they can't catch a bug in how ffmpeg actually resolves those commands
# against the real filesystem, which is exactly the class of bug found
# manually while validating this stage: a relative `work_dir` produced a
# concat list whose relative entries the concat demuxer re-resolved
# against the LIST FILE's own directory (not the process cwd), doubling
# the path and failing outright. run_stitch now resolves work_dir to an
# absolute path before writing the list file specifically to prevent this. ----

def write_tiny_video(path, seconds=5, fps=10, w=64, h=48, gop=5):
    # small GOP (keyframe every `gop` frames) so stream-copy trims have a
    # short, predictable keyframe-snap window to assert against, instead
    # of one keyframe covering the whole tiny clip
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=size={w}x{h}:rate={fps}:duration={seconds}",
         "-g", str(gop), "-c:v", "libx264", "-pix_fmt", "yuv420p",
         "-y", str(path)], check=True)


def probe_real_duration_s(path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True).stdout.strip()
    return float(out)


def test_stream_copy_output_duration_covers_all_nonadjacent_kept_spans(tmp_path):
    """The core Stage 5 correctness check: a manifest with several
    non-contiguous kept spans (real cut gaps between them, not just a
    file-boundary split) must produce one output whose rendered duration
    covers every requested span. Stream-copy trims can't cut mid-GOP, so
    the rendered duration is allowed to be >= the requested sum (extra
    footage before a span start, per the priority rule) but must never
    be shorter.

    IMPORTANT SCOPE NOTE: the upper-bound assertion below only proves
    slack stays roughly proportional to GOP size AT THIS CLIP'S OWN SMALL
    GOP (0.5s) — it does not, and cannot, bound how much slack a real
    file with a larger GOP will show, since that's a property of the
    source file's own encoding, not of this module's logic. Manual
    validation against a real 67.5-minute MKV file (6.006s GOP) found
    slack up to ~1.03 GOPs per span (6.17s), and for a small fraction of
    spans ffmpeg's seek landed on the keyframe before the nearest one
    rather than the nearest — see pipeline/stitch.py's module docstring
    and the README's Known Limitations for the verified real-world
    numbers. This test's job is to confirm the MECHANISM (never shorter,
    slack roughly bounded, not unbounded drift), not to stand in for that
    real-file validation."""
    video = tmp_path / "src.mp4"
    write_tiny_video(video, seconds=5, fps=10, gop=5)  # keyframe every 0.5s

    kept_segments = [(0.6, 1.4), (2.5, 3.0), (4.0, 4.8)]
    requested_total = sum(b - a for a, b in kept_segments)
    m = build_manifest("src.mp4", 5.0, kept_segments)

    out = tmp_path / "out.mp4"
    result = run_stitch(m, tmp_path, out, work_dir=tmp_path / "work")

    assert result.reencoded is False
    assert result.span_count == 3
    assert out.exists()

    actual = probe_real_duration_s(out)
    assert actual >= requested_total - 0.05, (
        f"rendered output ({actual:.2f}s) is SHORTER than the requested "
        f"kept spans ({requested_total:.2f}s) — real content was lost")
    # generous margin (2 GOPs/span) since real footage has shown ffmpeg's
    # seek can land a full extra keyframe back, not just the nearest one
    assert actual <= requested_total + 3 * (2 * 0.5) + 0.2, (
        f"rendered output ({actual:.2f}s) is far longer than requested "
        f"({requested_total:.2f}s) — more than keyframe-snap slack "
        f"explains")


def test_run_stitch_with_relative_work_dir_string(tmp_path, monkeypatch):
    """Regression test for the actual bug found during manual Stage 5
    validation: passing a RELATIVE work_dir (a plain string like "work",
    not tmp_path / "work" which pytest always makes absolute) used to
    make the concat demuxer look for a doubled path
    (work/work/span_0001.mp4) and fail, because relative entries in the
    concat list are resolved against the list file's own directory, not
    the process cwd. run_stitch must resolve work_dir to absolute
    internally so this works regardless of what the caller passes."""
    video = tmp_path / "src.mp4"
    write_tiny_video(video, seconds=3, fps=10, gop=5)
    m = build_manifest("src.mp4", 3.0, [(0.5, 1.5)])

    monkeypatch.chdir(tmp_path)
    result = run_stitch(m, ".", "out.mp4", work_dir="work")  # both relative

    assert Path("out.mp4").exists()
    assert result.span_count == 1


def test_reencode_path_normalizes_mismatched_inputs_and_plays(tmp_path):
    """Real end-to-end coverage for the re-encode fallback (previously
    only exercised via the fake-runner unit tests above): two source
    files with different resolution AND fps must actually get normalized
    to one common target and produce a single playable output."""
    part1 = tmp_path / "part1.mp4"
    part2 = tmp_path / "part2.mp4"
    write_tiny_video(part1, seconds=3, fps=10, w=96, h=64, gop=5)
    write_tiny_video(part2, seconds=3, fps=5, w=64, h=48, gop=5)

    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 3.0,
         "kept_segments": [(0.5, 1.5)]},
        {"source_file": "part2.mp4", "duration": 3.0,
         "kept_segments": [(1.0, 2.0)]},
    ])

    out = tmp_path / "out.mp4"
    result = run_stitch(m, tmp_path, out, work_dir=tmp_path / "work")

    assert result.reencoded is True
    assert "resolution" in result.reencode_reason
    assert "frame rate" in result.reencode_reason
    assert out.exists()

    rendered = probe_video_params(out)
    assert (rendered.width, rendered.height) == (96, 64)  # larger of the two
    assert rendered.fps == pytest.approx(10.0)  # higher of the two

    # frame-accurate trim (every frame is decoded) -> no keyframe-snap
    # slack, unlike the stream-copy path above
    actual = probe_real_duration_s(out)
    assert actual == pytest.approx(2.0, abs=0.2)


def test_probe_video_params_reads_real_nonzero_start_offset():
    """probe_video_params's ffprobe query gained start_time alongside the
    existing codec/width/height/fps/rotation fields -- confirm the extra
    field didn't break parsing of the others, and that a real file's
    genuinely nonzero start_time is read correctly, not silently
    defaulted to 0.0."""
    clip = Path(__file__).parent.parent / "reference_clips" / "clip_540.mkv"
    if not clip.exists():
        pytest.skip("reference clip not available for start_offset probe check")

    info = probe_video_params(clip)
    assert info.codec_name == "h264"
    assert info.width == 1920 and info.height == 1080
    assert info.start_offset == pytest.approx(4.506, abs=0.01)


def test_stream_copy_span_from_zero_not_shortened_by_real_stream_start_offset(tmp_path):
    """Regression test for a real bug, not a theoretical one -- found
    while chasing why a restored gap didn't make an exported output any
    longer. Every current reference clip except full_game.mkv has a
    nonzero video-stream start_time (2.4-4.5s, likely recording-device
    encoder delay) -- a real container property the synthetic
    write_tiny_video() clips above never reproduce (confirmed directly:
    a plain lavfi-encoded synthetic clip measures start_time=0.0), which
    is exactly why this class of bug survived until now. Before the fix,
    a span requested as (0.0, 30.0) against clip_540.mkv (start_time
    4.506s) rendered at ~25.5s -- SHORTER than requested, a direct
    violation of this module's own "never less, only extra" guarantee.
    Uses a real reference clip specifically because reproducing the exact
    mechanism synthetically (tried: -itsoffset) produces a DIFFERENT,
    unrelated symptom, not the same bug -- an honest synthetic stand-in
    for this one isn't available, so this test skips gracefully if the
    clip isn't present rather than pretending a fake substitute proves
    the same thing (same pattern as tests/test_veto_e2e.py's real-clip
    dependency)."""
    clip = Path(__file__).parent.parent / "reference_clips" / "clip_540.mkv"
    if not clip.exists():
        pytest.skip("reference clip not available for real start-offset check")

    requested = 30.0
    m = build_manifest(clip.name, 190.0, [(0.0, requested)])
    out = tmp_path / "out.mp4"
    result = run_stitch(m, clip.parent, out, work_dir=tmp_path / "work")

    assert result.reencoded is False
    actual = probe_real_duration_s(out)
    assert actual >= requested - 0.5, (
        f"rendered output ({actual:.2f}s) is SHORTER than the requested "
        f"{requested:.2f}s span starting at 0.0 -- real content was lost, "
        f"the video stream's own nonzero start_time was not corrected for")


def frame_hashes(video_path, start_s=None, duration_s=None):
    """Per-decoded-frame checksum, in order, via ffmpeg's own `framemd5`
    muxer -- used to detect exact-duplicate frames directly, the same
    method used to find and confirm the real duplicate-frame bug below.

    NOT implemented via seeking + re-encoding to PNG through a pipe: that
    approach was tried first and produces two DIFFERENT classes of false
    positive, both confirmed directly against real files, not assumed:
    (1) any mid-file `-ss` seek reliably produces a spurious duplicate at
    the seek point, reproduced by seeking into arbitrary, uninteresting
    points of the pristine, UNTOUCHED original clip_300.mkv -- an
    ffmpeg accurate-seek artifact, not a real duplicate; (2) checking near
    a real concat-demuxer join specifically produced spurious duplicates
    under the PNG-pipe method that framemd5 -- and a genuine from-t=0,
    no-seek decode -- both showed were not really there. `framemd5`
    checksums the actual decoded picture directly, has no seek-accuracy
    behavior to work around (this always decodes from the true start
    when start_s is None), and is also far cheaper than PNG encoding."""
    cmd = ["ffmpeg", "-v", "error"]
    if start_s is not None:
        cmd += ["-ss", str(start_s)]
    if duration_s is not None:
        cmd += ["-t", str(duration_s)]
    cmd += ["-i", str(video_path), "-an", "-f", "framemd5", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True, text=True)
    return [line.split(",")[-1].strip() for line in proc.stdout.splitlines()
           if not line.startswith("#")]


def test_no_duplicate_frames_across_a_real_close_splice_boundary(tmp_path):
    """Regression test for a real bug introduced BY the start_offset fix
    above: two adjacent kept spans, from a real clip_300.mkv batch, whose
    real gap (3.36s) is smaller than the file's own real GOP (6.006s).
    Before merge_overlapping_spans, extracting them independently caused
    ~90 of 144 frames at the start of the second span to be byte-identical
    duplicates of frames at the end of the first -- confirmed by direct
    frame-hash comparison against the real file before this fix existed.
    This confirms it's fixed: zero duplicate frames anywhere across the
    real splice boundary, checked directly on the real rendered output."""
    clip = Path(__file__).parent.parent / "reference_clips" / "clip_300.mkv"
    if not clip.exists():
        pytest.skip("reference clip not available for real splice-duplication check")

    # the exact two spans that reproduced the bug (see this module's and
    # pipeline/stitch.py's docstrings for the full derivation)
    m = build_manifest(clip.name, 190.0, [(3.873, 43.892), (47.250, 80.010)])
    out = tmp_path / "out.mp4"
    result = run_stitch(m, clip.parent, out, work_dir=tmp_path / "work")

    assert result.reencoded is False
    # the two spans must have been merged into one (real gap 3.36s <
    # real GOP 6.006s) -- if this ever regresses to 2 separate jobs again,
    # the duplicate-frame bug is back
    assert result.span_count == 1

    # scan every frame from the true start through past the join -- no
    # seeking, so there's no seek-accuracy behavior to account for
    hashes = frame_hashes(out, duration_s=48.0)
    seen = {}
    dups = []
    for i, h in enumerate(hashes):
        if h in seen:
            dups.append((seen[h], i))
        else:
            seen[h] = i
    assert dups == [], (
        f"found {len(dups)} exact-duplicate frame pair(s) near the real "
        f"splice boundary: {dups[:10]} -- real footage is playing twice")
