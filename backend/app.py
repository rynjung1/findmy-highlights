"""FastAPI backend wrapping the existing pipeline: upload, trigger
processing, poll progress, read/update the manifest, and re-export. No
pipeline logic lives here — every endpoint is a thin HTTP wrapper around
pipeline/ and backend/pipeline_runner.py, matching the project rule that
pipeline.run.process_video() and pipeline.stitch.run_stitch() stay the
single implementations of their respective jobs.

Single-job-at-a-time (v1, deliberate): this is a local single-user app
and detection is CPU/memory-heavy (~1GB RSS, near-full CPU on the
full-length checkpoint) — running two jobs at once would only make both
slower, not add real throughput. A second trigger while anything else is
pending/in_progress anywhere gets a clean 409 rather than silently
starting and contending for resources. See backend/jobs.py for the
active-job scan this relies on instead of a separate lock file.

Nothing is ever auto-deleted from uploads/ in v1 (see README's Known
limitations) — that's a deliberate scope cut, not an oversight.
"""

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import cv2
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from pipeline.calibration import (build_calibration, grab_preview_frame,
                                  probe_frame_size, save_calibration)
from pipeline.manifest import (VALID_STATUS, load_manifest, save_manifest,
                               set_status)
from pipeline.multifile import order_infos, probe_file, resolve_order

from backend import jobs, storage
from backend.pipeline_runner import run_detect_then_export_job, run_export_job

# Videos this app can plausibly work with. Rejected at upload time so an
# obviously-wrong file type never gets written to disk in the first
# place; a file that passes this but still can't actually be decoded is
# already handled gracefully further downstream by the pipeline's
# existing corrupt-file handling, not re-validated here.
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


class ProcessBody(BaseModel):
    order: list[str] | None = None
    # Calibration is checked at trigger time, not left to surface only in
    # a completed job's warnings field — a real run takes tens of
    # minutes, so discovering "oh, that was uncalibrated" after the fact
    # is expensive. Missing calibration is a 400 unless this is set,
    # which makes running without plate-occupancy signals an explicit,
    # visible choice instead of a silent default.
    allow_uncalibrated: bool = False


class OrderBody(BaseModel):
    order: list[str]


class SegmentStatusBody(BaseModel):
    status: str


def _default_run_in_background(fn, *args):
    """Real dispatch: a daemon thread, so a 30-60+ minute job doesn't
    block the event loop that serves progress-polling requests. Tests
    override app.state.run_in_background with a synchronous call so
    assertions don't race a real thread."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def create_app(uploads_root=None, run_in_background=None) -> FastAPI:
    """App factory, not a module-level singleton — tests point each app
    instance at its own tmp_path uploads root with a synchronous
    run_in_background, fully isolated from other tests and from a real
    uploads/ directory."""
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        jobs.sweep_interrupted_jobs(app.state.uploads_root)
        yield

    app = FastAPI(title="Find My Highlights", lifespan=_lifespan)
    app.state.uploads_root = (Path(uploads_root) if uploads_root is not None
                              else storage.DEFAULT_UPLOADS_ROOT)
    app.state.run_in_background = run_in_background or _default_run_in_background

    def _batch_dir(batch_id: str) -> Path:
        bdir = storage.batch_dir(app.state.uploads_root, batch_id)
        if not bdir.exists():
            raise HTTPException(404, f"no such batch: {batch_id}")
        return bdir

    def _batch_file_names(bdir: Path) -> list:
        return json.loads((bdir / "files.json").read_text())["files"]

    def _reject_if_busy(bdir: Path, batch_id: str, job_type: str,
                        block_if_completed: bool = True) -> None:
        """Blocks a second trigger while one is pending/in_progress
        anywhere, and (for detect, not export) also blocks re-triggering
        once already completed. Export is deliberately exempted from the
        completed-blocks-retrigger rule: re-stitching is idempotent
        against whatever the manifest currently says, so it's the
        correct way to regenerate the output after a Phase 8 restore —
        unlike detect, re-running it doesn't reprocess the source video,
        it only redoes the fast, cheap stitch step."""
        existing = jobs.load_job(bdir, job_type)
        if existing:
            blocked = {"pending", "in_progress"}
            if block_if_completed:
                blocked.add("completed")
            if existing["status"] in blocked:
                raise HTTPException(
                    409, f"{job_type} already triggered for this batch "
                         f"(status={existing['status']})")
        active = jobs.find_active_job(app.state.uploads_root)
        if active and active["batch_id"] != batch_id:
            raise HTTPException(
                409, f"another job is already running: "
                     f"batch_id={active['batch_id']} job_id={active['job_id']}")

    @app.post("/batches")
    def upload_batch(files: list[UploadFile] = File(...)):
        if not files:
            raise HTTPException(400, "no files provided")
        # validate every filename BEFORE writing anything to disk, so one
        # bad file in a multi-file batch doesn't leave partial writes of
        # the good ones behind
        bad = [f.filename for f in files
              if Path(f.filename).suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS]
        if bad:
            raise HTTPException(
                400, f"unsupported file type: {bad} — allowed extensions: "
                     f"{sorted(ALLOWED_VIDEO_EXTENSIONS)}")

        batch_id = storage.new_batch_id()
        bdir = storage.batch_dir(app.state.uploads_root, batch_id)
        names = []
        for f in files:
            storage.save_upload(bdir / f.filename, f.file)
            names.append(f.filename)
        (bdir / "files.json").write_text(json.dumps({"files": names}))
        return {"batch_id": batch_id, "files": names}

    @app.get("/batches/{batch_id}/preview.jpg")
    def get_preview(batch_id: str):
        bdir = _batch_dir(batch_id)
        names = _batch_file_names(bdir)
        try:
            frame = grab_preview_frame(bdir / names[0])
        except ValueError as e:
            raise HTTPException(400, str(e))
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            raise HTTPException(500, "failed to encode preview frame")
        return Response(content=buf.tobytes(), media_type="image/jpeg")

    @app.get("/batches/{batch_id}/calibration")
    def get_calibration(batch_id: str):
        bdir = _batch_dir(batch_id)
        p = bdir / "calibration.json"
        if not p.exists():
            raise HTTPException(404, "no calibration set for this batch")
        return json.loads(p.read_text())

    @app.post("/batches/{batch_id}/calibration")
    def set_calibration(batch_id: str,
                        calibration_file: UploadFile | None = File(None),
                        x: float | None = Form(None),
                        y: float | None = Form(None),
                        radius: float | None = Form(None)):
        """Sets the plate zone for every file in this batch (one
        calibration.json covers the whole batch, same shared-by-default
        rule pipeline.calibration.resolve_zone already applies). Two
        ways to provide it, matching the two real scripts/calibrate.py
        paths: upload an existing calibration.json to reuse (the "same
        camera setup, already calibrated" case), or give plate pixel
        coordinates to compute a fresh one against this batch's own
        first video (the API equivalent of `calibrate.py --set x,y` —
        there's no in-browser interactive click flow here, that's
        Phase 7/8's job to build against this endpoint).

        /process now requires this to have been called first, unless the
        caller explicitly passes allow_uncalibrated=true — see
        trigger_process. Setting calibration is rejected once the detect
        job has moved past the point where it would matter (pending,
        in_progress, or completed): it would either race an
        already-running job or silently do nothing for one that's
        already done, which is more confusing than a clear error telling
        the caller it's too late."""
        bdir = _batch_dir(batch_id)
        dest = bdir / "calibration.json"

        existing_detect = jobs.load_job(bdir, "detect")
        if existing_detect and existing_detect["status"] in (
                "pending", "in_progress", "completed"):
            raise HTTPException(
                409, f"cannot set calibration: this batch's detect job is "
                     f"already {existing_detect['status']} — calibration "
                     f"must be set before triggering processing")

        if calibration_file is not None:
            if x is not None or y is not None or radius is not None:
                raise HTTPException(
                    400, "provide either calibration_file or x/y "
                         "coordinates, not both")
            try:
                calibration = json.loads(calibration_file.file.read())
            except json.JSONDecodeError:
                raise HTTPException(400, "calibration_file is not valid JSON")
            missing = [f for f in ("plate_xy", "zone_radius_px")
                      if f not in calibration]
            if missing:
                raise HTTPException(
                    400, f"calibration_file missing required field(s): "
                         f"{missing}")
            plate_xy = calibration["plate_xy"]
            if not (isinstance(plate_xy, list) and len(plate_xy) == 2):
                raise HTTPException(
                    400, "calibration_file's plate_xy must be a 2-element "
                         "[x, y]")
            if calibration["zone_radius_px"] <= 0:
                raise HTTPException(
                    400, "calibration_file's zone_radius_px must be positive")
            save_calibration(dest, calibration)
            return calibration

        if x is None or y is None:
            raise HTTPException(
                400, "provide either calibration_file, or both x and y "
                     "plate pixel coordinates")
        if radius is not None and radius <= 0:
            raise HTTPException(
                400, f"radius must be positive, got {radius}")

        names = _batch_file_names(bdir)
        video_path = bdir / names[0]
        try:
            frame_size = probe_frame_size(video_path)
        except ValueError as e:
            raise HTTPException(400, str(e))

        w, h = frame_size
        if not (0 <= x <= w) or not (0 <= y <= h):
            raise HTTPException(
                400, f"plate coordinates ({x}, {y}) are outside this "
                     f"video's frame ({w}x{h})")

        calibration = build_calibration(frame_size, (x, y), radius,
                                        created_from=names[0])
        save_calibration(dest, calibration)
        return calibration

    @app.post("/batches/{batch_id}/process")
    def trigger_process(batch_id: str, body: ProcessBody | None = None):
        bdir = _batch_dir(batch_id)
        names = _batch_file_names(bdir)
        paths = [str(bdir / n) for n in names]

        _reject_if_busy(bdir, batch_id, "detect")

        allow_uncalibrated = body.allow_uncalibrated if body is not None else False
        if not (bdir / "calibration.json").exists() and not allow_uncalibrated:
            raise HTTPException(
                400, f"no calibration set for this batch — POST "
                     f"/batches/{batch_id}/calibration first, or pass "
                     f"allow_uncalibrated=true to proceed without "
                     f"plate-occupancy signals (this disables the at-bat "
                     f"boundary logic, not just a minor quality knob)")

        if body is not None and body.order:
            try:
                ordered = resolve_order(
                    paths, ",".join(str(bdir / n) for n in body.order))
            except ValueError as e:
                raise HTTPException(400, str(e))
            job = jobs.create_job(bdir, batch_id, "detect", status="pending")
            app.state.run_in_background(run_detect_then_export_job, bdir, job, ordered)
            return job

        if len(paths) == 1:
            job = jobs.create_job(bdir, batch_id, "detect", status="pending")
            app.state.run_in_background(run_detect_then_export_job, bdir, job, paths)
            return job

        infos = [probe_file(p) for p in paths]
        result = order_infos(infos)
        if result.ambiguous:
            # not an error: an expected, actionable intermediate state —
            # the client is meant to show this and call /order next, the
            # same "ask the user to confirm/reorder" fallback the CLI's
            # AmbiguousOrderError provides, shaped as data instead of an
            # exit code.
            return jobs.create_job(
                bdir, batch_id, "detect", status="needs_order_confirmation",
                suggested_order=[Path(p).name for p in result.ordered_paths],
                order_reason=result.reason)

        job = jobs.create_job(bdir, batch_id, "detect", status="pending")
        app.state.run_in_background(run_detect_then_export_job, bdir, job,
                                    result.ordered_paths)
        return job

    @app.post("/batches/{batch_id}/order")
    def confirm_order(batch_id: str, body: OrderBody):
        bdir = _batch_dir(batch_id)
        names = _batch_file_names(bdir)
        paths = [str(bdir / n) for n in names]

        existing = jobs.load_job(bdir, "detect")
        if not existing or existing["status"] != "needs_order_confirmation":
            raise HTTPException(
                409, "no order confirmation is pending for this batch")

        try:
            ordered = resolve_order(
                paths, ",".join(str(bdir / n) for n in body.order))
        except ValueError as e:
            raise HTTPException(400, str(e))

        existing["status"] = "pending"
        existing["suggested_order"] = None
        existing["order_reason"] = None
        jobs.save_job(bdir, existing)
        app.state.run_in_background(run_detect_then_export_job, bdir, existing, ordered)
        return existing

    @app.get("/batches/{batch_id}/jobs/{job_type}")
    def get_job(batch_id: str, job_type: str):
        if job_type not in jobs.JOB_TYPES:
            raise HTTPException(404, f"unknown job type: {job_type}")
        bdir = _batch_dir(batch_id)
        job = jobs.load_job(bdir, job_type)
        if job is None:
            raise HTTPException(404, f"no {job_type} job for this batch")
        return job

    @app.get("/batches/{batch_id}/manifest")
    def get_manifest(batch_id: str):
        bdir = _batch_dir(batch_id)
        p = bdir / "manifest.json"
        if not p.exists():
            raise HTTPException(404, "no manifest yet for this batch")
        return load_manifest(p)

    @app.patch("/batches/{batch_id}/manifest/segments/{segment_id}")
    def update_segment(batch_id: str, segment_id: str,
                       body: SegmentStatusBody):
        bdir = _batch_dir(batch_id)
        p = bdir / "manifest.json"
        if not p.exists():
            raise HTTPException(404, "no manifest yet for this batch")
        if body.status not in VALID_STATUS:
            raise HTTPException(
                400, f"invalid status {body.status!r}, expected one of "
                     f"{VALID_STATUS}")
        manifest = load_manifest(p)
        try:
            seg = set_status(manifest, segment_id, body.status)
        except KeyError:
            raise HTTPException(404, f"no segment with id {segment_id!r}")
        save_manifest(manifest, p)
        return seg

    @app.post("/batches/{batch_id}/export")
    def trigger_export(batch_id: str):
        bdir = _batch_dir(batch_id)
        if not (bdir / "manifest.json").exists():
            raise HTTPException(404, "no manifest yet for this batch")

        _reject_if_busy(bdir, batch_id, "export", block_if_completed=False)

        job = jobs.create_job(bdir, batch_id, "export", status="pending")
        app.state.run_in_background(run_export_job, bdir, job)
        return job

    @app.get("/batches/{batch_id}/output")
    def get_output(batch_id: str):
        bdir = _batch_dir(batch_id)
        p = bdir / "output.mp4"
        if not p.exists():
            raise HTTPException(404, "no exported output yet for this batch")
        # FileResponse handles Range requests itself (needed for a
        # <video> player to seek without downloading the whole file first)
        return FileResponse(p, media_type="video/mp4", filename="highlights.mp4")

    return app


app = create_app()
