"""Integration tests for the FastAPI backend (backend/app.py). Every
test gets its own app instance pointed at a fresh tmp_path uploads root
(backend.app.create_app is a factory for exactly this reason) with a
SYNCHRONOUS run_in_background, so job dispatch runs inline and tests
don't race a real background thread.

The real pipeline (process_video, run_stitch) is monkeypatched to fast
fakes for most tests — a real detect job takes tens of minutes and needs
model weights; that's covered by the manual full-length checkpoint and
the existing e2e tests, not here. The multi-file ORDERING tests use real
tiny ffmpeg-generated clips (same lavfi pattern as test_multifile.py),
since backend.app deliberately reuses pipeline.multifile's real
order_infos/probe_file rather than reimplementing ordering — that's the
thing worth verifying for real.
"""

import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient

import backend.pipeline_runner as pipeline_runner
from backend import storage
from backend.app import create_app
from pipeline.manifest import build_manifest, save_manifest


def make_app(tmp_path, training_data_dir=None):
    app = create_app(uploads_root=tmp_path / "uploads",
                     run_in_background=lambda fn, *args: fn(*args),
                     training_data_dir=training_data_dir)
    return app


def upload(client, names_and_content):
    files = [("files", (name, content, "video/x-matroska"))
            for name, content in names_and_content]
    r = client.post("/batches", files=files)
    assert r.status_code == 200, r.text
    return r.json()["batch_id"]


def post_process(client, batch_id, **body_overrides):
    """POST /process with allow_uncalibrated=true by default — most
    tests here aren't exercising the calibration gate itself and would
    otherwise all need this repeated by hand. Tests that ARE about the
    gate call client.post(...) directly instead."""
    body = {"allow_uncalibrated": True}
    body.update(body_overrides)
    return client.post(f"/batches/{batch_id}/process", json=body)


def fake_process_video_factory(segments_by_file=None, default_segments=((1.0, 3.0),),
                               calls=None, hard_cut_windows_by_file=None):
    """Builds a fake replacing pipeline_runner.process_video: instant, no
    real video I/O or model inference, but it still calls on_stage (so
    job stage transitions are exercised) and still does the real
    "warn if zone is None" check the real process_video does, so tests
    that care whether calibration actually reached this call can tell —
    a fake that silently drops the zone argument would make a broken
    calibration wiring pass its test by accident. `calls`, if given, is
    a list this appends (filename, zone) to for direct inspection.
    `hard_cut_windows_by_file`, if given, lets a test simulate a real
    hard cut having happened inside a kept segment (default: none, same
    as a clip with nothing to hard-cut)."""
    class FakeMotion:
        times = __import__("numpy").array([0.0, 1.0, 2.0, 3.0, 4.0])
        scores = __import__("numpy").array([0.0, 1.0, 1.0, 1.0, 0.0])

    def fake(path, zone, motion_only=False, cache_dir=None, warn=None,
            on_stage=None, training_data_dir=None,
            training_data_source_info=None):
        if calls is not None:
            calls.append((Path(path).name, zone))
        if on_stage:
            on_stage("analyzing motion")
            on_stage("running player detection")
            on_stage("extending and padding segments")
        if zone is None and warn is not None:
            warn(f"no calibration for {path}; plate-occupancy signals disabled")
        segs = (segments_by_file or {}).get(Path(path).name, default_segments)
        hard_cuts = (hard_cut_windows_by_file or {}).get(Path(path).name, [])
        return list(segs), [], 5.0, FakeMotion(), list(hard_cuts)
    return fake


def fake_run_stitch(manifest, source_dir, output_path, work_dir=None,
                    prober=None, runner=None, on_stage=None):
    if on_stage:
        on_stage("extracting kept segments")
        on_stage("stitching output")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"fake output video")
    from pipeline.stitch import StitchResult
    # empty by default -- most tests here aren't exercising the
    # output-offset wiring itself; see
    # test_export_persists_real_output_offsets_onto_manifest below for a
    # variant that returns a real one
    return StitchResult(output_path=str(output_path), span_count=1,
                        reencoded=False, reencode_reason=None,
                        output_duration_s=2.0, segment_output_offsets={})


@pytest.fixture(autouse=True)
def fake_pipeline(monkeypatch):
    # resolve_zone is NOT faked here — it's cheap, pure, file-based logic
    # (reads calibration.json if present), so it runs for real in every
    # test. That's what makes test_calibration_is_actually_picked_up_by_
    # detection below a real check of the calibration endpoint's wiring,
    # not something a fake could accidentally make pass regardless.
    monkeypatch.setattr(pipeline_runner, "process_video",
                        fake_process_video_factory())
    monkeypatch.setattr(pipeline_runner, "run_stitch", fake_run_stitch)


@pytest.fixture
def process_video_calls(monkeypatch):
    """Opt-in fixture for tests that need to inspect exactly what zone
    process_video was called with, rather than just whether the job
    finished. Overrides the autouse fake with a call-recording one."""
    calls = []
    monkeypatch.setattr(pipeline_runner, "process_video",
                        fake_process_video_factory(calls=calls))
    return calls


# ---- upload ----

def test_upload_creates_batch_with_files_on_disk(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"fake video bytes")])
        assert (tmp_path / "uploads" / batch_id / "clip.mkv").read_bytes() \
            == b"fake video bytes"


def test_upload_no_files_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[])
        assert r.status_code in (400, 422)  # FastAPI 422s an empty required list


def test_upload_unsupported_extension_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("notes.txt", b"hello", "text/plain"))])
        assert r.status_code == 400
        assert "unsupported file type" in r.json()["detail"]


def test_upload_rejects_whole_batch_if_any_file_is_bad(tmp_path):
    # a mix of one good + one bad file must reject atomically -- no
    # partial write of the good file left behind
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("good.mkv", b"video bytes", "video/x-matroska")),
            ("files", ("bad.txt", b"not a video", "text/plain"))])
        assert r.status_code == 400
        # no batch directory should exist at all
        assert list((tmp_path / "uploads").glob("*")) == [] if \
            (tmp_path / "uploads").exists() else True


def test_upload_extension_check_is_case_insensitive(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("CLIP.MP4", b"video bytes", "video/mp4"))])
        assert r.status_code == 200


def test_upload_traversal_filename_would_have_escaped_pre_fix(tmp_path):
    """Proves the vulnerability the next few tests guard against is
    real, not hypothetical -- the exact join upload_batch used to do
    (bdir / filename, with no validation) actually resolves outside the
    batch's own directory for this filename, the same join the pre-fix
    code would have written through. This is what makes the 400s below
    a real fix, not a check against an already-harmless string."""
    bdir = storage.batch_dir(tmp_path / "uploads", "some_batch_id")
    escaped = (bdir / "../../evil.mkv").resolve()
    assert not str(escaped).startswith(str(bdir.resolve()))


def test_upload_rejects_path_traversal_filename_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("../../evil.mkv", b"video bytes", "video/x-matroska"))])
        assert r.status_code == 400
        assert "unsafe filename" in r.json()["detail"]
        # nothing was written outside the uploads root
        assert not (tmp_path / "evil.mkv").exists()
        assert not (tmp_path.parent / "evil.mkv").exists()


def test_upload_rejects_backslash_filename_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("..\\evil.mkv", b"video bytes", "video/x-matroska"))])
        assert r.status_code == 400
        assert "unsafe filename" in r.json()["detail"]


def test_upload_rejects_bare_dotdot_segment_400(tmp_path):
    # a single ".." component needs no slash at all to climb one
    # directory when joined with Path(bdir) / name
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("..", b"video bytes", "video/x-matroska"))])
        assert r.status_code == 400
        assert "unsafe filename" in r.json()["detail"]


def test_upload_rejects_traversal_batch_atomically(tmp_path):
    # same atomic-rejection guarantee as the bad-extension case: one
    # unsafe filename in a multi-file batch must block the whole batch,
    # not just skip the bad file
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches", files=[
            ("files", ("good.mkv", b"video bytes", "video/x-matroska")),
            ("files", ("../evil.mkv", b"video bytes", "video/x-matroska"))])
        assert r.status_code == 400
        assert list((tmp_path / "uploads").glob("*")) == [] if \
            (tmp_path / "uploads").exists() else True


# ---- preview frame ----

def test_preview_jpg_matches_video_native_resolution(tmp_path):
    """The load-bearing invariant the frontend's coordinate-scaling math
    depends on: the JPEG's own pixel dimensions (what the browser's
    naturalWidth/naturalHeight report) must exactly equal what
    probe_frame_size() (and therefore build_calibration()) uses. If the
    backend ever resized the preview, clicks would silently scale wrong
    without either side raising an error."""
    from pipeline.calibration import probe_frame_size
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)  # 64x48

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.get(f"/batches/{batch_id}/preview.jpg")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"

        import cv2
        import numpy as np
        decoded = cv2.imdecode(np.frombuffer(r.content, np.uint8),
                               cv2.IMREAD_COLOR)
        jpeg_h, jpeg_w = decoded.shape[:2]

        native_w, native_h = probe_frame_size(tmp_path / "uploads" / batch_id / "clip.mp4")
        assert (jpeg_w, jpeg_h) == (native_w, native_h)


def test_preview_jpg_unknown_batch_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/batches/does-not-exist/preview.jpg")
        assert r.status_code == 404


def test_preview_jpg_uses_a_frame_past_the_start(tmp_path):
    """Confirms the fixed-offset choice actually took effect: a 3-second
    clip whose first ~1s is red and the rest is blue must show blue in
    the preview (target offset clamped down for the short clip, but
    still past the opening red segment), not the red opening frame."""
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error",
        "-f", "lavfi", "-i", "color=c=red:s=64x48:d=1:r=5",
        "-f", "lavfi", "-i", "color=c=blue:s=64x48:d=2:r=5",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
        "-y", str(clip)], check=True)

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.get(f"/batches/{batch_id}/preview.jpg")
        assert r.status_code == 200

        import cv2
        import numpy as np
        decoded = cv2.imdecode(np.frombuffer(r.content, np.uint8),
                               cv2.IMREAD_COLOR)
        b, g, r_channel = decoded[24, 32].tolist()  # center pixel, BGR
        assert b > 100 and r_channel < 100, (
            f"expected the blue portion of the clip, got BGR=({b},{g},{r_channel})"
            " -- preview frame wasn't actually offset past the start")


# ---- calibration ----

def test_get_calibration_before_set_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/calibration")
        assert r.status_code == 404


def test_set_calibration_from_coordinates_against_real_video(tmp_path):
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)  # 64x48, from write_clip's default

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "32", "y": "24"})
        assert r.status_code == 200
        cal = r.json()
        assert cal["frame_size"] == [64, 48]
        assert cal["plate_xy"] == [32.0, 24.0]
        assert cal["zone_radius_px"] == pytest.approx(0.26 * 48, abs=0.1)
        assert cal["created_from"] == "clip.mp4"

        got = client.get(f"/batches/{batch_id}/calibration")
        assert got.status_code == 200
        assert got.json() == cal


def test_set_calibration_with_explicit_radius(tmp_path):
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "10", "y": "10", "radius": "5"})
        assert r.status_code == 200
        assert r.json()["zone_radius_px"] == 5.0


def test_set_calibration_by_uploading_existing_file(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        existing = json.dumps({
            "frame_size": [1920, 1080], "plate_xy": [900.0, 700.0],
            "zone_radius_px": 280.0, "created_from": "other_camera.mkv"})
        r = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": ("calibration.json", existing,
                                        "application/json")})
        assert r.status_code == 200
        assert r.json()["plate_xy"] == [900.0, 700.0]

        got = client.get(f"/batches/{batch_id}/calibration")
        assert got.json()["plate_xy"] == [900.0, 700.0]


def test_set_calibration_file_and_coordinates_together_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(
            f"/batches/{batch_id}/calibration",
            data={"x": "1", "y": "1"},
            files={"calibration_file": ("c.json", "{}", "application/json")})
        assert r.status_code == 400


def test_set_calibration_neither_file_nor_coordinates_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(f"/batches/{batch_id}/calibration")
        assert r.status_code == 400


def test_set_calibration_invalid_json_file_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": ("c.json", "not json",
                                        "application/json")})
        assert r.status_code == 400


def test_set_calibration_file_missing_fields_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": ("c.json", json.dumps({"foo": 1}),
                                        "application/json")})
        assert r.status_code == 400


def test_no_calibration_means_process_video_gets_a_none_zone(tmp_path, process_video_calls):
    """The degraded-path baseline this fix addresses: given an explicit
    allow_uncalibrated=true opt-in, process_video must receive zone=None
    and the resulting warning must be visible in the job — this is
    exactly what the real smoke test surfaced before calibration was
    wired through. (Without the opt-in, this is now a 400 at trigger
    time — see the calibration-gate tests below.)"""
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = post_process(client, batch_id)
        job = r.json()
        assert job["status"] == "completed"
        assert process_video_calls == [("clip.mkv", None)]
        assert job["warnings"], "expected a no-calibration warning"


def test_calibration_is_actually_picked_up_by_detection(tmp_path, process_video_calls):
    """The point of this endpoint: once set, process_video must be
    called with a REAL zone object (checked directly, not inferred from
    the warnings list being empty — a fake that ignores zone entirely
    would make that inference false), and no "no calibration" warning."""
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        cal_resp = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": (
                "c.json", json.dumps({
                    "frame_size": [1920, 1080],
                    "plate_xy": [900.0, 700.0],
                    "zone_radius_px": 280.0}),
                "application/json")})
        assert cal_resp.status_code == 200

        r = post_process(client, batch_id)
        job = r.json()
        assert job["status"] == "completed"

        assert len(process_video_calls) == 1
        name, zone = process_video_calls[0]
        assert name == "clip.mkv"
        assert zone is not None, "process_video was called with zone=None"
        assert zone.center_xy == (900.0, 700.0)
        assert zone.radius_px == 280.0
        assert not job["warnings"], (
            f"expected no calibration warning once set, got: {job['warnings']}")


# ---- calibration: malformed requests ----

def test_set_calibration_unknown_batch_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches/does-not-exist/calibration",
                        data={"x": "1", "y": "1"})
        assert r.status_code == 404


def test_set_calibration_out_of_bounds_coordinates_400(tmp_path):
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)  # 64x48

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "9999", "y": "24"})
        assert r.status_code == 400
        assert "outside this video's frame" in r.json()["detail"]

        r2 = client.post(f"/batches/{batch_id}/calibration",
                         data={"x": "-5", "y": "24"})
        assert r2.status_code == 400


def test_set_calibration_negative_radius_400(tmp_path):
    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "10", "y": "10", "radius": "-5"})
        assert r.status_code == 400


def test_set_calibration_file_negative_radius_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": ("c.json", json.dumps({
                "plate_xy": [10.0, 10.0], "zone_radius_px": -1.0}),
                "application/json")})
        assert r.status_code == 400


def test_set_calibration_file_malformed_plate_xy_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(
            f"/batches/{batch_id}/calibration",
            files={"calibration_file": ("c.json", json.dumps({
                "plate_xy": [10.0], "zone_radius_px": 5.0}),
                "application/json")})
        assert r.status_code == 400


def test_set_calibration_after_detect_completed_409(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = post_process(client, batch_id)
        assert r.json()["status"] == "completed"

        r2 = client.post(f"/batches/{batch_id}/calibration",
                         data={"x": "1", "y": "1"})
        assert r2.status_code == 409
        assert "already completed" in r2.json()["detail"]


def test_set_calibration_while_detect_in_progress_409(tmp_path):
    # same 409 path as "after completed" but for a job caught mid-run —
    # written directly rather than raced against a real thread
    from backend import jobs

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        bdir = tmp_path / "uploads" / batch_id
        jobs.create_job(bdir, batch_id, "detect", status="in_progress")

        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "1", "y": "1"})
        assert r.status_code == 409
        assert "in_progress" in r.json()["detail"]


def test_set_calibration_still_allowed_after_failed_detect(tmp_path):
    from backend import jobs

    app = make_app(tmp_path)
    clip = tmp_path / "src.mp4"
    write_clip(clip, seconds=1, fps=5)

    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mp4", clip.read_bytes())])
        bdir = tmp_path / "uploads" / batch_id
        jobs.create_job(bdir, batch_id, "detect", status="failed")

        r = client.post(f"/batches/{batch_id}/calibration",
                        data={"x": "10", "y": "10"})
        assert r.status_code == 200


# ---- trigger-processing: calibration gate ----

def test_process_without_calibration_is_blocked_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(f"/batches/{batch_id}/process")
        assert r.status_code == 400
        assert "no calibration set" in r.json()["detail"]
        # confirms this is a real gate, not just a documented convention:
        # no job was ever created for the rejected attempt
        assert client.get(f"/batches/{batch_id}/jobs/detect").status_code == 404


def test_process_allow_uncalibrated_opt_in_proceeds(tmp_path, process_video_calls):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(f"/batches/{batch_id}/process",
                        json={"allow_uncalibrated": True})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"
        assert process_video_calls == [("clip.mkv", None)]


def test_process_with_calibration_set_proceeds_without_flag(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        client.post(f"/batches/{batch_id}/calibration",
                   files={"calibration_file": ("c.json", json.dumps({
                       "plate_xy": [10.0, 10.0], "zone_radius_px": 5.0}),
                       "application/json")})
        r = client.post(f"/batches/{batch_id}/process")
        assert r.status_code == 200
        assert r.json()["status"] == "completed"


# ---- trigger-processing: basic + malformed cases ----

def test_process_unknown_batch_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.post("/batches/does-not-exist/process")
        assert r.status_code == 404


def test_process_single_file_completes_with_manifest(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = post_process(client, batch_id)
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "completed"
        assert job["manifest_path"] is not None

        m = client.get(f"/batches/{batch_id}/manifest")
        assert m.status_code == 200
        assert any(s["status"] == "kept" for s in m.json()["segments"])


def test_hard_cut_windows_from_process_video_reach_the_manifest_as_origin(
        tmp_path, monkeypatch):
    """Wiring test: process_video's new 5th return value (hard_cut_windows)
    must actually reach build_manifest, so a real hard-cut shows up as
    origin="hard_cut" in the manifest the Edit Log reads -- not silently
    dropped anywhere along backend/pipeline_runner.py's path."""
    # default_segments=(2.0, 8.0) with a hard cut at (4.0, 5.0) inside it --
    # after real hard-cutting this would be two kept pieces around a
    # hard_cut gap, but the fake here supplies the ALREADY-cut segments
    # (matching what real process_video would return: hard-cutting is
    # pipeline/run.py's own last step, already applied before this
    # function's caller ever sees the segment list) plus the window that
    # was cut, exactly like the real return contract.
    monkeypatch.setattr(pipeline_runner, "process_video",
                        fake_process_video_factory(
                            default_segments=((2.0, 4.0), (5.0, 8.0)),
                            hard_cut_windows_by_file={"clip.mkv": [(4.0, 5.0)]}))

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = post_process(client, batch_id)
        assert r.status_code == 200

        m = client.get(f"/batches/{batch_id}/manifest").json()
        hard_cut_seg = next(
            (s for s in m["segments"] if s["origin"] == "hard_cut"), None)
        assert hard_cut_seg is not None, (
            f"no hard_cut-origin segment in manifest: {m['segments']}")
        assert hard_cut_seg["start_s"] == pytest.approx(4.0)
        assert hard_cut_seg["end_s"] == pytest.approx(5.0)
        assert hard_cut_seg["status"] == "cut"


def test_process_triggered_twice_on_same_batch_409(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r1 = post_process(client, batch_id)
        assert r1.status_code == 200
        r2 = post_process(client, batch_id)
        assert r2.status_code == 409
        assert "already triggered" in r2.json()["detail"]


def test_process_rejected_while_another_batch_is_active(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_a = upload(client, [("clip.mkv", b"x")])
        batch_b = upload(client, [("clip2.mkv", b"y")])

        # simulate batch A's job still running (don't actually run it —
        # write the state directly so this test isn't racing a thread)
        from backend import jobs
        bdir_a = tmp_path / "uploads" / batch_a
        jobs.create_job(bdir_a, batch_a, "detect", status="in_progress")

        r = post_process(client, batch_b)
        assert r.status_code == 409
        assert "another job is already running" in r.json()["detail"]
        assert batch_a in r.json()["detail"]


# ---- ambiguous multi-file order (real ffmpeg-generated clips) ----

def write_clip(path, creation_time=None, seconds=1, fps=5):
    cmd = ["ffmpeg", "-v", "error", "-f", "lavfi",
          "-i", f"color=c=blue:s=64x48:d={seconds}:r={fps}"]
    if creation_time:
        cmd += ["-metadata", f"creation_time={creation_time.isoformat()}Z"]
    cmd += ["-y", str(path)]
    subprocess.run(cmd, check=True)


def test_ambiguous_order_returns_needs_confirmation(tmp_path):
    app = make_app(tmp_path)
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    write_clip(clip_a)  # no creation_time on either -> ambiguous
    write_clip(clip_b)

    with TestClient(app) as client:
        batch_id = upload(client, [
            ("a.mp4", clip_a.read_bytes()), ("b.mp4", clip_b.read_bytes())])
        r = post_process(client, batch_id)
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "needs_order_confirmation"
        assert set(job["suggested_order"]) == {"a.mp4", "b.mp4"}
        assert job["order_reason"]


def test_needs_order_confirmation_does_not_block_other_batch(tmp_path):
    app = make_app(tmp_path)
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    write_clip(clip_a)
    write_clip(clip_b)

    with TestClient(app) as client:
        stuck_batch = upload(client, [
            ("a.mp4", clip_a.read_bytes()), ("b.mp4", clip_b.read_bytes())])
        r1 = post_process(client, stuck_batch)
        assert r1.json()["status"] == "needs_order_confirmation"

        other_batch = upload(client, [("clip.mkv", b"x")])
        r2 = post_process(client, other_batch)
        assert r2.status_code == 200
        assert r2.json()["status"] == "completed"


def test_confirm_order_starts_processing(tmp_path):
    app = make_app(tmp_path)
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    write_clip(clip_a)
    write_clip(clip_b)

    with TestClient(app) as client:
        batch_id = upload(client, [
            ("a.mp4", clip_a.read_bytes()), ("b.mp4", clip_b.read_bytes())])
        post_process(client, batch_id)

        r = client.post(f"/batches/{batch_id}/order",
                        json={"order": ["a.mp4", "b.mp4"]})
        assert r.status_code == 200
        assert r.json()["status"] == "completed"

        m = client.get(f"/batches/{batch_id}/manifest")
        assert m.status_code == 200
        assert m.json()["source_files"] == ["a.mp4", "b.mp4"]


def test_confirm_order_mismatched_files_400(tmp_path):
    app = make_app(tmp_path)
    clip_a = tmp_path / "a.mp4"
    clip_b = tmp_path / "b.mp4"
    write_clip(clip_a)
    write_clip(clip_b)

    with TestClient(app) as client:
        batch_id = upload(client, [
            ("a.mp4", clip_a.read_bytes()), ("b.mp4", clip_b.read_bytes())])
        post_process(client, batch_id)

        r = client.post(f"/batches/{batch_id}/order",
                        json={"order": ["a.mp4", "c.mp4"]})
        assert r.status_code == 400


def test_confirm_order_without_pending_confirmation_409(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(f"/batches/{batch_id}/order", json={"order": ["clip.mkv"]})
        assert r.status_code == 409


# ---- jobs ----

def test_get_job_unknown_type_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/jobs/bogus")
        assert r.status_code == 404


def test_get_job_none_yet_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/jobs/detect")
        assert r.status_code == 404


# ---- manifest read/update, including required malformed-request cases ----

def test_get_manifest_before_ready_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/manifest")
        assert r.status_code == 404


def test_update_manifest_before_manifest_exists_404(tmp_path):
    # explicitly requested malformed-request case
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.patch(f"/batches/{batch_id}/manifest/segments/seg_001",
                         json={"status": "kept"})
        assert r.status_code == 404


def test_update_manifest_unknown_segment_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)
        r = client.patch(f"/batches/{batch_id}/manifest/segments/seg_999",
                         json={"status": "kept"})
        assert r.status_code == 404


def test_update_manifest_invalid_status_400(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)
        m = client.get(f"/batches/{batch_id}/manifest").json()
        seg_id = m["segments"][0]["id"]
        r = client.patch(f"/batches/{batch_id}/manifest/segments/{seg_id}",
                         json={"status": "bogus"})
        assert r.status_code == 400


def test_update_manifest_flips_status_and_persists(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)
        m = client.get(f"/batches/{batch_id}/manifest").json()
        cut = next(s for s in m["segments"] if s["status"] == "cut")

        r = client.patch(f"/batches/{batch_id}/manifest/segments/{cut['id']}",
                         json={"status": "kept"})
        assert r.status_code == 200
        assert r.json()["status"] == "kept"

        reloaded = client.get(f"/batches/{batch_id}/manifest").json()
        seg = next(s for s in reloaded["segments"] if s["id"] == cut["id"])
        assert seg["status"] == "kept"


# ---- export ----

def test_export_before_manifest_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.post(f"/batches/{batch_id}/export")
        assert r.status_code == 404


def test_auto_chain_export_runs_automatically_after_detect(tmp_path):
    """The point of the Stage 7 auto-chain: a single trigger-processing
    call ends with a completed export, no separate POST /export needed."""
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        detect_job = post_process(client, batch_id).json()
        assert detect_job["status"] == "completed"

        export_job = client.get(f"/batches/{batch_id}/jobs/export").json()
        assert export_job["status"] == "completed"
        assert export_job["output_path"] is not None
        assert Path(export_job["output_path"]).exists()


def test_auto_chain_skips_export_when_detect_fails(tmp_path, monkeypatch):
    def failing_process_video(*a, **kw):
        raise RuntimeError("boom")
    monkeypatch.setattr(pipeline_runner, "process_video", failing_process_video)

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        detect_job = post_process(client, batch_id).json()
        assert detect_job["status"] == "failed"
        assert client.get(f"/batches/{batch_id}/jobs/export").status_code == 404


def test_export_can_be_retriggered_after_completion(tmp_path):
    # unlike detect, export is safe (and meant) to re-trigger once idle —
    # this is what a future restore-then-re-export flow will rely on
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)  # auto-chain already exported once

        r = client.post(f"/batches/{batch_id}/export")
        assert r.status_code == 200
        job = r.json()
        assert job["status"] == "completed"
        assert Path(job["output_path"]).exists()


def test_export_persists_real_output_offsets_onto_manifest(tmp_path, monkeypatch):
    """Wiring test for the skip-ahead output-time mapping fix (see
    README's retraction writeup): run_export_job must take
    StitchResult.segment_output_offsets and write it onto the saved
    manifest via apply_output_offsets, for every export -- not just the
    first one, since a restore/cut-again re-export is exactly when a
    segment's real rendered position can change. Uses a fake_run_stitch
    variant returning a real (nonzero) offset for one segment, since the
    default autouse fake returns {} and wouldn't catch a broken wiring
    (e.g. the result being computed but never applied/saved)."""
    def fake_run_stitch_with_offsets(manifest, source_dir, output_path,
                                     work_dir=None, prober=None,
                                     runner=None, on_stage=None):
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"fake output video")
        from pipeline.stitch import StitchResult
        kept = next(s for s in manifest["segments"] if s["status"] == "kept")
        return StitchResult(
            output_path=str(output_path), span_count=1, reencoded=False,
            reencode_reason=None, output_duration_s=2.0,
            segment_output_offsets={kept["id"]: (0.25, 2.25)})

    monkeypatch.setattr(pipeline_runner, "run_stitch",
                        fake_run_stitch_with_offsets)

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)

        m = client.get(f"/batches/{batch_id}/manifest").json()
        kept = next(s for s in m["segments"] if s["status"] == "kept")
        assert kept["output_start_s"] == pytest.approx(0.25)
        assert kept["output_end_s"] == pytest.approx(2.25)

        # re-export (e.g. after a restore/cut-again) must refresh it too,
        # not just leave the value from the first export in place
        def fake_run_stitch_second_export(manifest, source_dir, output_path,
                                          work_dir=None, prober=None,
                                          runner=None, on_stage=None):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"fake output video 2")
            from pipeline.stitch import StitchResult
            kept = next(s for s in manifest["segments"] if s["status"] == "kept")
            return StitchResult(
                output_path=str(output_path), span_count=1, reencoded=False,
                reencode_reason=None, output_duration_s=2.0,
                segment_output_offsets={kept["id"]: (1.0, 3.0)})

        monkeypatch.setattr(pipeline_runner, "run_stitch",
                            fake_run_stitch_second_export)
        r = client.post(f"/batches/{batch_id}/export")
        assert r.status_code == 200

        m2 = client.get(f"/batches/{batch_id}/manifest").json()
        kept2 = next(s for s in m2["segments"] if s["status"] == "kept")
        assert kept2["output_start_s"] == pytest.approx(1.0)
        assert kept2["output_end_s"] == pytest.approx(3.0)


def test_export_rejected_while_still_in_progress(tmp_path):
    # a genuinely-running export must still be protected — simulated
    # directly rather than racing a real thread
    from backend import jobs

    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)

        bdir = tmp_path / "uploads" / batch_id
        existing = jobs.load_job(bdir, "export")
        existing["status"] = "in_progress"
        jobs.save_job(bdir, existing)

        r = client.post(f"/batches/{batch_id}/export")
        assert r.status_code == 409


# ---- output file serving ----

def test_get_output_before_export_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/output")
        assert r.status_code == 404


def test_get_output_serves_the_exported_file(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)  # auto-chain exports too

        r = client.get(f"/batches/{batch_id}/output")
        assert r.status_code == 200
        assert r.headers["content-type"] == "video/mp4"
        assert r.content == b"fake output video"  # from fake_run_stitch


def test_get_output_unknown_batch_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/batches/does-not-exist/output")
        assert r.status_code == 404


# ---- source file serving (Edit Log cut-segment preview) ----

def test_get_source_serves_original_upload(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"original bytes")])
        r = client.get(f"/batches/{batch_id}/source/clip.mkv")
        assert r.status_code == 200
        assert r.content == b"original bytes"


def test_get_source_unknown_batch_404(tmp_path):
    app = make_app(tmp_path)
    with TestClient(app) as client:
        r = client.get("/batches/does-not-exist/source/clip.mkv")
        assert r.status_code == 404


def test_get_source_filename_not_in_batch_404(tmp_path):
    # a real filename, just not one this batch actually has -- must not
    # fall back to trying to read it off disk anyway
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/source/other.mkv")
        assert r.status_code == 404


def test_get_source_rejects_traversal_filename_404(tmp_path):
    # a literal ".." never reaches the server for real -- httpx (like a
    # real browser) collapses it during URL normalization before the
    # request is even sent, so testing with a bare ".." would pass for
    # the wrong reason. %2e%2e is the actual adversarial case: it
    # survives client-side normalization (encoded dot-segments aren't
    # collapsed) and only decodes to ".." after Starlette's routing has
    # already matched it into `filename` -- this is what proves the
    # endpoint's own allowlist check is the real defense, not an
    # accident of how the test client builds URLs.
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        r = client.get(f"/batches/{batch_id}/source/%2e%2e")
        assert r.status_code == 404
        assert r.json()["detail"] == "no such source file in this batch: .."


def test_get_source_does_not_leak_a_same_named_file_outside_the_batch(tmp_path):
    # even if a file with the "right" name exists one directory up (as
    # it would for every other batch's uploads under the same uploads
    # root), the allowlist is per-batch (files.json), not just "does a
    # file with this name exist somewhere reachable from bdir"
    app = make_app(tmp_path)
    with TestClient(app) as client:
        batch_a = upload(client, [("shared_name.mkv", b"batch A bytes")])
        batch_b = upload(client, [("shared_name.mkv", b"batch B bytes")])
        r = client.get(f"/batches/{batch_a}/source/shared_name.mkv")
        assert r.content == b"batch A bytes"
        r = client.get(f"/batches/{batch_b}/source/shared_name.mkv")
        assert r.content == b"batch B bytes"


# ---- startup interrupt sweep ----

def test_startup_sweep_marks_stale_in_progress_job_interrupted(tmp_path):
    from backend import jobs

    uploads_root = tmp_path / "uploads"
    bdir = uploads_root / "stale-batch"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "stale-batch", "detect", status="in_progress")

    app = create_app(uploads_root=uploads_root,
                     run_in_background=lambda fn, *args: fn(*args))
    with TestClient(app):  # entering the context triggers the startup event
        pass

    reloaded = jobs.load_job(bdir, "detect")
    assert reloaded["status"] == "interrupted"
    assert reloaded["error"]


# ---- Tier 1 review queue endpoints ----

def write_review_record(training_data_dir, record_id, margin, label=None,
                        candidate_type="hard_cut_dip", pipeline_decision="cut",
                        write_clip=True):
    reviews_dir = Path(training_data_dir) / "reviews"
    reviews_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "id": record_id, "created_at": "2026-01-01T00:00:00+00:00",
        "source": {"video_path": "v.mp4", "source_file": "v.mp4"},
        "window": {"start_s": 1.0, "end_s": 2.0},
        "candidate_type": candidate_type, "pipeline_decision": pipeline_decision,
        "margin": margin, "features_at_label_time": {},
        "config_hash": "abc123", "label": label, "labeled_at": None, "note": None,
    }
    (reviews_dir / f"{record_id}.json").write_text(json.dumps(record))
    if write_clip:
        (reviews_dir / f"{record_id}.mp4").write_bytes(b"fake clip bytes")
    return record


def test_review_next_disabled_when_no_training_data_dir(tmp_path):
    # Explicit "" (not the default None) to force-disable regardless of a
    # real local .env's FMH_TRAINING_DATA_DIR -- None means "fall back to
    # DEFAULT_TRAINING_DATA_DIR", which is no longer reliably unset now
    # that a real .env exists at the project root (see README's
    # transfer-learning writeup for why that default was added).
    app = make_app(tmp_path, training_data_dir="")
    with TestClient(app) as client:
        r = client.get("/review/next")
    assert r.status_code == 404


def test_review_next_done_when_queue_empty(tmp_path):
    app = make_app(tmp_path, training_data_dir=tmp_path / "training_data")
    with TestClient(app) as client:
        r = client.get("/review/next")
    assert r.status_code == 200
    assert r.json() == {"done": True, "remaining": 0}


def test_review_next_returns_lowest_margin_first(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=5.0)
    write_review_record(td, "hc_bbb", margin=1.0)
    write_review_record(td, "hc_ccc", margin=3.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/next")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "hc_bbb"
    assert body["clip_url"] == "/review/hc_bbb/clip"
    # 3 unlabeled records total, this one included -- a real backlog
    # size, not just presence/absence of a next item (see
    # scripts/mine_review_candidates.py for why this started mattering:
    # a mined batch can be 30-50+, not the normal 5/run trickle).
    assert body["remaining"] == 3


def test_review_next_remaining_shrinks_as_records_are_labeled(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    write_review_record(td, "hc_bbb", margin=2.0)
    write_review_record(td, "hc_ccc", margin=3.0, label="downtime")  # already labeled
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/next")
    # only the 2 unlabeled records count -- an already-labeled one isn't
    # part of "how much is left"
    assert r.json()["remaining"] == 2


def test_review_next_skips_already_labeled_records(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0, label="downtime")
    write_review_record(td, "hc_bbb", margin=5.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/next")
    assert r.json()["id"] == "hc_bbb"


def test_review_next_control_samples_sort_last(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "ctl_aaa", margin=None, candidate_type="control",
                        pipeline_decision="kept")
    write_review_record(td, "hc_bbb", margin=100.0)  # a large but real margin
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/next")
    assert r.json()["id"] == "hc_bbb"


def test_review_clip_serves_the_real_file(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/hc_aaa/clip")
    assert r.status_code == 200
    assert r.content == b"fake clip bytes"


def test_review_clip_404_for_unknown_id(tmp_path):
    app = make_app(tmp_path, training_data_dir=tmp_path / "training_data")
    with TestClient(app) as client:
        r = client.get("/review/hc_nope/clip")
    assert r.status_code == 404


def test_review_clip_404_for_path_traversal_id(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_secret", margin=1.0)
    # a real secret file OUTSIDE reviews_dir that traversal would target
    (tmp_path / "outside.mp4").write_bytes(b"should never be served")
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.get("/review/..%2f..%2foutside/clip")
    assert r.status_code == 404


def test_review_label_writes_and_returns_next(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    write_review_record(td, "hc_bbb", margin=5.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.post("/review/hc_aaa/label", json={"label": "real_action"})
    assert r.status_code == 200
    assert r.json()["id"] == "hc_bbb"
    # hc_aaa just got labeled -- only hc_bbb is left unlabeled
    assert r.json()["remaining"] == 1

    record = json.loads((td / "reviews" / "hc_aaa.json").read_text())
    assert record["label"] == "real_action"
    assert record["labeled_at"] is not None


def test_review_label_with_note(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        client.post("/review/hc_aaa/label",
                    json={"label": "downtime", "note": "clearly a lull"})
    record = json.loads((td / "reviews" / "hc_aaa.json").read_text())
    assert record["note"] == "clearly a lull"


def test_review_label_done_when_that_was_the_last_one(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.post("/review/hc_aaa/label", json={"label": "downtime"})
    assert r.json() == {"done": True, "remaining": 0}


def test_review_label_rejects_invalid_label_value(tmp_path):
    td = tmp_path / "training_data"
    write_review_record(td, "hc_aaa", margin=1.0)
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        r = client.post("/review/hc_aaa/label", json={"label": "maybe"})
    assert r.status_code == 400
    record = json.loads((td / "reviews" / "hc_aaa.json").read_text())
    assert record["label"] is None  # rejected before writing anything


def test_review_label_404_for_unknown_id(tmp_path):
    app = make_app(tmp_path, training_data_dir=tmp_path / "training_data")
    with TestClient(app) as client:
        r = client.post("/review/hc_nope/label", json={"label": "downtime"})
    assert r.status_code == 404


def test_process_threads_training_data_dir_into_process_video(tmp_path, monkeypatch):
    seen = []

    def fake(path, zone, motion_only=False, cache_dir=None, warn=None,
            on_stage=None, training_data_dir=None, training_data_source_info=None):
        seen.append((training_data_dir, training_data_source_info))
        import numpy as np

        class FakeMotion:
            times = np.array([0.0])
            scores = np.array([0.0])
        return [(1.0, 3.0)], [], 5.0, FakeMotion(), []

    monkeypatch.setattr(pipeline_runner, "process_video", fake)
    monkeypatch.setattr(pipeline_runner, "run_stitch", fake_run_stitch)

    td = tmp_path / "training_data"
    app = make_app(tmp_path, training_data_dir=td)
    with TestClient(app) as client:
        batch_id = upload(client, [("clip.mkv", b"x")])
        post_process(client, batch_id)

    assert len(seen) == 1
    training_data_dir, source_info = seen[0]
    assert Path(training_data_dir) == td
    assert source_info == {"batch_id": batch_id}
