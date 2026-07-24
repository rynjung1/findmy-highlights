"""Unit tests for Phase 5 stitching: reencode-vs-copy decision, target
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
                             needs_reencode, plan_stitch, probe_video_params,
                             run_stitch)


def vp(path="a.mp4", codec="h264", w=1920, h=1080, fps=30.0, rotation=0):
    return VideoParams(path=path, codec_name=codec, width=w, height=h,
                       fps=fps, rotation=rotation)


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
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)))
    assert [(j.start_s, j.end_s) for j in plan.jobs] == [
        (2.0, 10.0), (15.0, 20.0), (0.0, 5.0)]
    assert plan.jobs[0].source_path.endswith("part1.mp4")
    assert plan.jobs[2].source_path.endswith("part2.mp4")


def test_plan_stitch_clip_names_sequential_and_unique(tmp_path):
    m = two_file_manifest()
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)))
    names = [j.clip_name for j in plan.jobs]
    assert len(names) == len(set(names))


def test_plan_stitch_no_reencode_when_params_match(tmp_path):
    m = two_file_manifest()
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)))
    assert plan.reencode is False
    assert plan.target is None


def test_plan_stitch_reencode_when_params_mismatch(tmp_path):
    m = two_file_manifest()

    def prober(path):
        if "part1" in str(path):
            return vp(path=str(path), w=1920, h=1080, fps=30.0)
        return vp(path=str(path), w=1280, h=720, fps=60.0)

    plan = plan_stitch(m, tmp_path, prober=prober)
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

    plan_stitch(m, tmp_path, prober=prober)
    assert len(probed) == 1
    assert "part1.mp4" in probed[0]


def test_plan_stitch_empty_manifest_yields_no_jobs(tmp_path):
    m = build_multi_file_manifest([
        {"source_file": "part1.mp4", "duration": 30.0, "kept_segments": []}])
    plan = plan_stitch(m, tmp_path, prober=lambda p: vp(path=str(p)))
    assert plan.jobs == []
    assert plan.reencode is False


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
                        prober=lambda p: vp(path=str(p)), runner=fake_runner)

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
# manually while validating this phase: a relative `work_dir` produced a
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
    """The core Phase 5 correctness check: a manifest with several
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
    """Regression test for the actual bug found during manual Phase 5
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
