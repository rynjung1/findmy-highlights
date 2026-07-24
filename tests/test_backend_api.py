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
from backend.app import create_app
from pipeline.manifest import build_manifest, save_manifest


def make_app(tmp_path):
    app = create_app(uploads_root=tmp_path / "uploads",
                     run_in_background=lambda fn, *args: fn(*args))
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
                               calls=None):
    """Builds a fake replacing pipeline_runner.process_video: instant, no
    real video I/O or model inference, but it still calls on_stage (so
    job stage transitions are exercised) and still does the real
    "warn if zone is None" check the real process_video does, so tests
    that care whether calibration actually reached this call can tell —
    a fake that silently drops the zone argument would make a broken
    calibration wiring pass its test by accident. `calls`, if given, is
    a list this appends (filename, zone) to for direct inspection."""
    class FakeMotion:
        times = __import__("numpy").array([0.0, 1.0, 2.0, 3.0, 4.0])
        scores = __import__("numpy").array([0.0, 1.0, 1.0, 1.0, 0.0])

    def fake(path, zone, motion_only=False, cache_dir=None, warn=None,
            on_stage=None):
        if calls is not None:
            calls.append((Path(path).name, zone))
        if on_stage:
            on_stage("analyzing motion")
            on_stage("running player detection")
            on_stage("extending and padding segments")
        if zone is None and warn is not None:
            warn(f"no calibration for {path}; plate-occupancy signals disabled")
        segs = (segments_by_file or {}).get(Path(path).name, default_segments)
        return list(segs), [], 5.0, FakeMotion()
    return fake


def fake_run_stitch(manifest, source_dir, output_path, work_dir=None,
                    prober=None, runner=None, on_stage=None):
    if on_stage:
        on_stage("extracting kept segments")
        on_stage("stitching output")
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"fake output video")
    from pipeline.stitch import StitchResult
    return StitchResult(output_path=str(output_path), span_count=1,
                        reencoded=False, reencode_reason=None,
                        output_duration_s=2.0)


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
    """The point of the Phase 7 auto-chain: a single trigger-processing
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
