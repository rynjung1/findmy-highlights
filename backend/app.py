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

import errno
import json
import logging
import mimetypes
import os
import re
import shutil
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

import cv2
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from pipeline.calibration import (build_calibration, grab_preview_frame,
                                  probe_frame_size, save_calibration)
from pipeline.manifest import (VALID_STATUS, load_manifest, save_manifest,
                               set_status)
from pipeline.multifile import order_infos, probe_file, resolve_order
from pipeline.review import review_priority_key
from pipeline.run import DEFAULT_CACHE_DIR

from backend import demo, jobs, storage
from backend.pipeline_runner import run_detect_then_export_job, run_export_job

# Tier 1 review queue (see pipeline/review.py): off by default, and only
# ever turned on via this explicit environment variable, deliberately not
# a hardcoded real path -- process_video's own training_data_dir param
# already defaults to None/off so scripts/regression.py's many repeated
# runs never pollute the label store, but scripts/smoke_api.py drives
# THIS real app over HTTP, indistinguishable at the API layer from a real
# user upload. An env-var opt-in means nobody -- including a developer
# running smoke_api.py against their own dev server -- collects training
# data by accident; a real deployment that wants this has to set it
# explicitly.
#
# load_dotenv() reads .env (gitignored, project-root, one real local
# setting: FMH_TRAINING_DATA_DIR=training_data) if present, and never
# overrides an already-exported shell value (load_dotenv's own default).
# Added because the explicit-opt-in design above worked exactly as
# intended and nobody ever actually re-typed the env-var prefix on a real
# run: training_data/reviews/ sat at zero real labels through the entire
# Tier 1 buildout. The opt-in is still real and still explicit -- it's
# just made once, in a file, instead of re-decided (or forgotten) every
# time a terminal is opened.
load_dotenv()
DEFAULT_TRAINING_DATA_DIR = os.environ.get("FMH_TRAINING_DATA_DIR")

# CORS: unset (default) means no cross-origin access is granted at all --
# safe for the local dev setup (frontend/vite.config.js proxies same-origin,
# so dev never needed CORS in the first place) but a real deployment with
# the frontend on a different domain (e.g. a static host) MUST set this
# explicitly, comma-separated, e.g.
# "https://findmy-highlights.vercel.app,https://findmy-highlights.example.com".
# Deliberately not defaulted to "*" -- this API accepts file uploads and
# serves back real user video, a wildcard origin would let any third-party
# page make authenticated-by-cookie-free-but-still-real requests against a
# visitor's own running job.
def _cors_origins() -> list[str]:
    raw = os.environ.get("FMH_CORS_ORIGINS", "")
    return [o.strip() for o in raw.split(",") if o.strip()]


VALID_REVIEW_LABELS = ("downtime", "real_action")
# Every real review id this app ever writes is "<prefix>_<uuid4 hex>" (see
# pipeline.review.CANDIDATE_KIND_TO_ID_PREFIX) -- an allowlist match
# against that exact shape, not a blocklist against '..'/'/', for the
# same reason get_source's own filename check below is an allowlist: a
# blocklist can be bypassed (e.g. percent-encoding) in a way a strict
# allowlist can't.
_REVIEW_ID_RE = re.compile(r"^[a-z0-9_]+$")

# Videos this app can plausibly work with. Rejected at upload time so an
# obviously-wrong file type never gets written to disk in the first
# place; a file that passes this but still can't actually be decoded is
# already handled gracefully further downstream by the pipeline's
# existing corrupt-file handling, not re-validated here.
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}

# For the unhandled-exception handler below -- real tracebacks still go
# somewhere (this process's own stderr/log, exactly where they already
# went via Starlette's default handler), just never into the HTTP
# response body a caller receives.
logger = logging.getLogger("findmy_highlights.backend")


def _is_unsafe_filename(name: str) -> bool:
    """A client-supplied filename is written directly as bdir / name
    (see upload_batch) with no renaming, so it must be a bare filename,
    not a path: reject any '/', '\\', '..' component, or anything
    os.path.basename() would strip, before it ever reaches the
    filesystem. Path traversal here would be a write-side vulnerability
    (escaping the batch's upload directory), worse than a read-side one."""
    return (not name
            or "/" in name
            or "\\" in name
            or ".." in name
            or os.path.basename(name) != name)


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


class ReviewLabelBody(BaseModel):
    label: str
    note: str | None = None


def _default_run_in_background(fn, *args):
    """Real dispatch: a daemon thread, so a 30-60+ minute job doesn't
    block the event loop that serves progress-polling requests. Tests
    override app.state.run_in_background with a synchronous call so
    assertions don't race a real thread."""
    t = threading.Thread(target=fn, args=args, daemon=True)
    t.start()
    return t


def create_app(uploads_root=None, run_in_background=None,
              training_data_dir=None) -> FastAPI:
    """App factory, not a module-level singleton — tests point each app
    instance at its own tmp_path uploads root with a synchronous
    run_in_background, fully isolated from other tests and from a real
    uploads/ directory.

    `training_data_dir`, if given (or if unset, from DEFAULT_TRAINING_DATA_DIR
    -- see that constant's docstring for why this is env-var-gated),
    opts every real detect job this app runs into the Tier 1 review
    queue."""
    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        jobs.sweep_interrupted_jobs(app.state.uploads_root)
        yield

    app = FastAPI(title="Find My Highlights", lifespan=_lifespan)
    origins = _cors_origins()
    if origins:
        app.add_middleware(
            CORSMiddleware, allow_origins=origins, allow_credentials=False,
            allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(request: Request, exc: Exception):
        """Defense in depth, not a fix for an active leak: FastAPI's own
        HTTPException handling (every deliberate `raise HTTPException(...)`
        in this file) is a SEPARATE, more specific registration and is
        unaffected by this -- Starlette's handler lookup matches the exact
        exception type before falling back to a registered ancestor-class
        handler like this one, so every existing {"detail": ...} error
        response is unchanged. This only ever catches a genuinely
        unhandled exception (e.g. the real FileNotFoundError the pre-fix
        path-traversal bug could trigger, see docs/INVESTIGATION_LOG.md)
        that would otherwise fall through to Starlette's own
        ServerErrorMiddleware default.

        That default, confirmed live before this handler existed, was
        already NOT leaking a traceback to the client -- with debug=False
        (this app's default, never overridden), it already returned a
        bare "Internal Server Error" with no file paths or stack frames
        in the response body; only this process's own log saw the real
        traceback, exactly as intended. So this handler isn't closing an
        observed client-facing leak -- it removes this app's reliance on
        that Starlette default (and on debug staying off) by making the
        safe behavior this app's own explicit code, in this app's own
        {"detail": ...} shape instead of Starlette's differently-shaped
        generic response, and it guarantees the same safe shape even if a
        future change (e.g. a debug flag flipped on for local
        troubleshooting and left on) would otherwise have exposed one.
        The real traceback is logged here explicitly so a genuine bug
        (like the one that motivated this) is still fully diagnosable
        from the server's own log, exactly as before.
        """
        logger.exception(
            "unhandled exception on %s %s", request.method, request.url.path,
            exc_info=exc,
        )
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    app.state.uploads_root = (Path(uploads_root) if uploads_root is not None
                              else storage.DEFAULT_UPLOADS_ROOT)
    app.state.run_in_background = run_in_background or _default_run_in_background
    app.state.training_data_dir = (
        training_data_dir if training_data_dir is not None
        else DEFAULT_TRAINING_DATA_DIR)

    def _batch_dir(batch_id: str) -> Path:
        """Resolves batch_id to a real directory under uploads_root -- the
        one function every real batch-scoped endpoint funnels through (11
        call sites), so this is the single, correct place to enforce
        containment rather than re-deriving the check per endpoint.

        Real, demonstrated vulnerability (see docs/INVESTIGATION_LOG.md's
        security review entry), fixed here: the previous version only
        checked existence, not containment. A batch_id of ".." (as a
        single URL path segment, e.g. `%2e%2e` -- FastAPI's default
        string path converter can't smuggle a literal "/" through this
        parameter, so multi-level traversal via encoded slashes is
        already blocked by Starlette's own routing, confirmed live) made
        `storage.batch_dir()` resolve to uploads_root's own parent
        directory, which very much exists -- letting every downstream
        batch-scoped endpoint read and write files ONE level outside
        uploads_root under attacker-chosen names (confirmed live: a
        planted file one level up was served back via GET .../output,
        and POST .../calibration wrote a real file to that same
        location). Fixed by resolving both paths to their real,
        symlink-free absolute form and requiring the batch directory to
        actually be uploads_root or a real descendant of it -- not just
        "some directory that happens to exist" -- before anything else
        touches it. A batch_id resolving to uploads_root itself (e.g. a
        literal ".") is also rejected: every real batch is a subdirectory
        of it, never the root itself.
        """
        root = app.state.uploads_root.resolve()
        bdir = (root / batch_id).resolve()
        if bdir == root or not bdir.is_relative_to(root):
            raise HTTPException(404, f"no such batch: {batch_id}")
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
        correct way to regenerate the output after a Stage 8 restore —
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
        unsafe = [f.filename for f in files if _is_unsafe_filename(f.filename)]
        if unsafe:
            raise HTTPException(400, f"unsafe filename: {unsafe}")

        bad = [f.filename for f in files
              if Path(f.filename).suffix.lower() not in ALLOWED_VIDEO_EXTENSIONS]
        if bad:
            raise HTTPException(
                400, f"unsupported file type: {bad} — allowed extensions: "
                     f"{sorted(ALLOWED_VIDEO_EXTENSIONS)}")

        batch_id = storage.new_batch_id()
        bdir = storage.batch_dir(app.state.uploads_root, batch_id)
        names = []
        try:
            for f in files:
                storage.save_upload(bdir / f.filename, f.file)
                names.append(f.filename)
            (bdir / "files.json").write_text(json.dumps({"files": names}))
        except OSError as e:
            # Real operational failure hit during actual use (see
            # docs/INVESTIGATION_LOG.md): an uncaught OSError here fell
            # through to Starlette's default 500 handler, which returns a
            # bare "Internal Server Error" with zero indication of what
            # happened -- not just unfriendly, actively misleading for a
            # non-technical deployment (looks identical to a real bug).
            # Clean up whatever partial batch dir this attempt left
            # behind so a retry doesn't find a half-written directory.
            shutil.rmtree(bdir, ignore_errors=True)
            if e.errno == errno.ENOSPC:
                raise HTTPException(
                    507, "Not enough disk space on the server to save this "
                         "upload. Free up space on the server, then try "
                         "again.") from e
            raise HTTPException(500, f"failed to save upload: {e}") from e
        return {"batch_id": batch_id, "files": names}

    @app.get("/health")
    def health():
        """Liveness check for a hosting platform's healthcheck (e.g.
        Railway's `deploy.healthcheckPath`, see railway.json) --
        deliberately cheap (no filesystem/model access), just confirms
        the process is up and serving requests."""
        return {"status": "ok"}

    @app.post("/demo/run")
    def run_demo():
        """Creates a fresh batch from the bundled demo clip
        (backend/demo.py), already calibrated, and immediately triggers
        real detect-then-export processing on it -- one call, no upload,
        no manual calibration step. Real pipeline the whole way through
        (see backend/demo.py's module docstring for what's actually
        pre-computed vs. run live); the response shape matches a normal
        /batches/{id}/process trigger plus the new batch_id, so the
        frontend can drive it with the exact same polling/rendering path
        a real upload uses.

        Respects the same single-job-at-a-time rule as a real upload
        (see module docstring) -- a demo run while something else is
        already processing gets the same 409 a second real upload would.
        Never opts into the Tier 1 review queue regardless of the
        server's own FMH_TRAINING_DATA_DIR setting: this is the same
        fixed clip every single time, so repeat demo runs would only
        ever mine exact duplicate candidates, not real new label data.
        """
        active = jobs.find_active_job(app.state.uploads_root)
        if active:
            raise HTTPException(
                409, f"another job is already running: "
                     f"batch_id={active['batch_id']} job_id={active['job_id']}")

        batch_id = storage.new_batch_id()
        bdir = storage.batch_dir(app.state.uploads_root, batch_id)
        bdir.mkdir(parents=True, exist_ok=True)
        demo.seed_demo_batch(bdir, DEFAULT_CACHE_DIR)
        (bdir / "files.json").write_text(
            json.dumps({"files": [demo.DEMO_VIDEO_NAME]}))

        job = jobs.create_job(bdir, batch_id, "detect", status="pending")
        app.state.run_in_background(
            run_detect_then_export_job, bdir, job,
            [str(bdir / demo.DEMO_VIDEO_NAME)], None)
        return {"batch_id": batch_id, **job}

    @app.get("/batches/{batch_id}/preview.jpg")
    def get_preview(batch_id: str, at_seconds: float | None = None):
        """`at_seconds`, if given, overrides grab_preview_frame's fixed
        20.0s default -- purely additive, existing callers (the real
        frontend calibration UI) omit it and are unaffected. Added for
        the multi-pass redundant calibration diagnostic, which needs
        genuinely different real frames of the same clip per pass."""
        bdir = _batch_dir(batch_id)
        names = _batch_file_names(bdir)
        try:
            if at_seconds is not None:
                frame = grab_preview_frame(bdir / names[0], at_seconds=at_seconds)
            else:
                frame = grab_preview_frame(bdir / names[0])
        except ValueError as e:
            raise HTTPException(400, str(e))
        ok, buf = cv2.imencode(".jpg", frame)
        if not ok:
            raise HTTPException(500, "failed to encode preview frame")
        return Response(content=buf.tobytes(), media_type="image/jpeg")

    @app.get("/diagnostics/calibration-multipass")
    def calibration_multipass_diagnostic():
        """Serves scripts/calibration_multipass_diagnostic.html same-origin
        so it can call the real /batches/{id}/calibration and preview.jpg
        endpoints without needing CORS enabled. Diagnostic-only tool for
        the over-determined multi-pass calibration investigation -- not
        linked from the real frontend, not part of the shipped user
        flow. Safe to delete once that investigation is done."""
        path = (Path(__file__).resolve().parent.parent / "scripts"
               / "calibration_multipass_diagnostic.html")
        if not path.exists():
            raise HTTPException(404, "diagnostic page not present")
        return FileResponse(path, media_type="text/html")

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
                        radius: float | None = Form(None),
                        first_x: float | None = Form(None),
                        first_y: float | None = Form(None),
                        first_radius: float | None = Form(None),
                        second_x: float | None = Form(None),
                        second_y: float | None = Form(None),
                        second_radius: float | None = Form(None),
                        third_x: float | None = Form(None),
                        third_y: float | None = Form(None),
                        third_radius: float | None = Form(None)):
        """Sets the plate zone for every file in this batch (one
        calibration.json covers the whole batch, same shared-by-default
        rule pipeline.calibration.resolve_zone already applies). Two
        ways to provide it, matching the two real scripts/calibrate.py
        paths: upload an existing calibration.json to reuse (the "same
        camera setup, already calibrated" case), or give plate pixel
        coordinates to compute a fresh one against this batch's own
        first video (the API equivalent of `calibrate.py --set x,y` —
        there's no in-browser interactive click flow here, that's
        Stage 7/8's job to build against this endpoint).

        Bases (pipeline.calibration.build_calibration's `bases` param,
        Stage 10 infrastructure that had never been surfaced through any
        real tool -- not the CLI, not this endpoint, not the frontend --
        until now; see README) are independently optional, both from the
        plate and from each other, matching resolve_base_zones()'s own
        "partial calibration is the expected common case" contract: a
        camera angle that only shows first base can submit only
        first_x/first_y. <name>_radius is optional per base and defaults
        to DEFAULT_BASE_RADIUS_PX (see that constant's docstring for why
        that's NOT the plate's radius). Only meaningful alongside the
        x/y coordinate path -- the calibration_file upload path already
        passes any `bases` key in the uploaded JSON through unvalidated
        and unchanged, since it always has.

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

        base_fields = {
            "first": (first_x, first_y, first_radius),
            "second": (second_x, second_y, second_radius),
            "third": (third_x, third_y, third_radius),
        }
        any_base_field_set = any(
            v is not None for fields in base_fields.values() for v in fields)

        if calibration_file is not None:
            if x is not None or y is not None or radius is not None or any_base_field_set:
                raise HTTPException(
                    400, "provide either calibration_file or x/y "
                         "coordinates (plate and/or base), not both")
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

        bases = {}
        for name, (bx, by, br) in base_fields.items():
            if bx is None and by is None and br is None:
                continue  # this base not being set at all is the common case
            if bx is None or by is None:
                raise HTTPException(
                    400, f"{name} base requires both {name}_x and {name}_y")
            if not (0 <= bx <= w) or not (0 <= by <= h):
                raise HTTPException(
                    400, f"{name} base coordinates ({bx}, {by}) are "
                         f"outside this video's frame ({w}x{h})")
            if br is not None and br <= 0:
                raise HTTPException(
                    400, f"{name}_radius must be positive, got {br}")
            bases[name] = (bx, by, br) if br is not None else (bx, by)

        calibration = build_calibration(frame_size, (x, y), radius,
                                        created_from=names[0], bases=bases)
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
            app.state.run_in_background(run_detect_then_export_job, bdir, job, ordered,
                                        app.state.training_data_dir)
            return job

        if len(paths) == 1:
            job = jobs.create_job(bdir, batch_id, "detect", status="pending")
            app.state.run_in_background(run_detect_then_export_job, bdir, job, paths,
                                        app.state.training_data_dir)
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
                                    result.ordered_paths, app.state.training_data_dir)
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
        app.state.run_in_background(run_detect_then_export_job, bdir, existing, ordered,
                                    app.state.training_data_dir)
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

    @app.get("/batches/{batch_id}/source/{filename}")
    def get_source(batch_id: str, filename: str):
        """Serves one of the batch's own original uploaded source files,
        for the Edit Log's cut-segment preview (seek within the source
        file rather than exporting a physical clip per candidate
        segment, per the project spec). `filename` is a client-supplied
        URL segment, unlike every other endpoint here, so it's validated
        against `files.json` -- the batch's own recorded file list, the
        same source of truth get_preview already trusts -- rather than
        joined onto bdir directly; a bare '..' needs no slash to escape
        one directory level, so this can't be a suffix/character
        blocklist. The resolved-parent check is defense in depth on top
        of that allowlist, not a substitute for it."""
        bdir = _batch_dir(batch_id)
        if filename not in _batch_file_names(bdir):
            raise HTTPException(404, f"no such source file in this batch: {filename}")
        p = (bdir / filename).resolve()
        if p.parent != bdir.resolve() or not p.exists():
            raise HTTPException(404, f"no such source file in this batch: {filename}")
        media_type, _ = mimetypes.guess_type(filename)
        return FileResponse(p, media_type=media_type or "application/octet-stream",
                            filename=filename)

    def _reviews_dir() -> Path:
        if not app.state.training_data_dir:
            raise HTTPException(
                404, "the review queue is not enabled on this server "
                     "(set FMH_TRAINING_DATA_DIR)")
        return Path(app.state.training_data_dir) / "reviews"

    def _safe_review_path(reviews_dir: Path, review_id: str, suffix: str) -> Path:
        if not _REVIEW_ID_RE.match(review_id):
            raise HTTPException(404, f"no such review record: {review_id}")
        p = (reviews_dir / f"{review_id}{suffix}").resolve()
        if p.parent != reviews_dir.resolve():
            raise HTTPException(404, f"no such review record: {review_id}")
        return p

    def _pending_reviews(reviews_dir: Path) -> list:
        """Every unlabeled record, ranked by pipeline.review.review_priority_key:
        real xclip/pipeline disagreement first (an independent signal
        pointing the opposite way from the pipeline's own decision is the
        most useful case to check), then the original lowest-margin-first
        order for everything else -- a control sample (margin=None) still
        sorts last within that fallback group, unchanged from before."""
        records = []
        if not reviews_dir.exists():
            return records
        for p in sorted(reviews_dir.glob("*.json")):
            try:
                record = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if record.get("label") is None:
                records.append(record)
        records.sort(key=review_priority_key)
        return records

    def _review_response(record: dict, remaining: int) -> dict:
        return {
            "id": record["id"],
            "candidate_type": record["candidate_type"],
            "pipeline_decision": record["pipeline_decision"],
            "window": record["window"],
            "margin": record["margin"],
            "features_at_label_time": record["features_at_label_time"],
            "source": record["source"],
            "created_at": record["created_at"],
            "clip_url": f"/review/{record['id']}/clip",
            # Total unlabeled records right now, INCLUDING this one -- so
            # a caller showing "N remaining" doesn't off-by-one it. Added
            # once a real mined batch (30-50+ candidates in one sitting,
            # not the normal 5/run trickle) made "how much is left" a
            # real question the UI had no answer to.
            "remaining": remaining,
        }

    @app.get("/review/next")
    def get_next_review():
        """Returns the lowest-margin unlabeled record (control samples
        last), or {"done": true} once the queue is empty -- a normal 200
        either way, since an empty queue isn't an error condition."""
        pending = _pending_reviews(_reviews_dir())
        if not pending:
            return {"done": True, "remaining": 0}
        return _review_response(pending[0], remaining=len(pending))

    @app.get("/review/{review_id}/clip")
    def get_review_clip(review_id: str):
        reviews_dir = _reviews_dir()
        p = _safe_review_path(reviews_dir, review_id, ".mp4")
        if not p.exists():
            raise HTTPException(404, f"no such review record: {review_id}")
        return FileResponse(p, media_type="video/mp4", filename=f"{review_id}.mp4")

    @app.post("/review/{review_id}/label")
    def label_review(review_id: str, body: ReviewLabelBody):
        """Writes the label and returns the next pending item (same
        shape as GET /review/next), so the frontend can label one after
        another without a round trip back to /next each time."""
        if body.label not in VALID_REVIEW_LABELS:
            raise HTTPException(
                400, f"invalid label {body.label!r}, expected one of "
                     f"{VALID_REVIEW_LABELS}")
        reviews_dir = _reviews_dir()
        p = _safe_review_path(reviews_dir, review_id, ".json")
        if not p.exists():
            raise HTTPException(404, f"no such review record: {review_id}")

        record = json.loads(p.read_text())
        record["label"] = body.label
        record["labeled_at"] = datetime.now(timezone.utc).isoformat()
        record["note"] = body.note
        p.write_text(json.dumps(record, indent=2))

        pending = _pending_reviews(reviews_dir)
        if not pending:
            return {"done": True, "remaining": 0}
        return _review_response(pending[0], remaining=len(pending))

    return app


app = create_app()
