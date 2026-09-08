"""Durable job state: written to a JSON file at every stage transition,
mirroring pipeline.manifest's save/load-a-JSON-file pattern rather than
an in-memory dict. A real detect job takes tens of minutes and the
backend restarts constantly during Stage 7/8 development — in-memory-only
progress would silently vanish on every restart, which is a near-certain
failure mode here, not a simplification.

One job file per batch per job type: <batch_dir>/detect_job.json and
<batch_dir>/export_job.json. There is no separate lock file for the
single-job-at-a-time rule — find_active_job() scans the job files
directly, so there's nothing else that could drift out of sync with what
actually happened.

The lock is per-organization, not global: uploads_root holds
<org_id>/<batch_id> subdirectories (see backend/storage.py), and
find_active_job() only scans one org's subtree, so one organization's
processing run never blocks a different organization's.
"""

import json
import multiprocessing
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Real, current worst-case legitimate runtime this must never fire under
# (see docs/INVESTIGATION_LOG.md's RFDETRSmall-default entry): a full
# ~67.5min game's detect stage extrapolated at ~34min (down from ~45min
# pre-optimization, RFDETRSmall now shipped as the default) plus ~13min
# export, ~47min total -- flagged in that entry as an extrapolation, not
# yet directly re-measured against full_game.mkv post-flip. A 90min
# upload (backend/app.py's own MAX_UPLOAD_BYTES sizing target) scales
# roughly linearly with duration, landing a realistic worst case around
# 60-75 minutes. 3 hours gives real, generous headroom (2.5x+) over
# that worst case while still bounding a truly pathological hang (e.g. a
# corrupt file causing an infinite decode loop, unbounded by any real
# video's actual duration) to a finite wait. Overridable via
# FMH_PROCESSING_TIMEOUT_S for a deployment that needs to tune it
# (slower hardware, or games that legitimately run longer than 90min).
DEFAULT_PROCESSING_TIMEOUT_S = int(
    os.environ.get("FMH_PROCESSING_TIMEOUT_S", 3 * 60 * 60))

JOB_TYPES = ("detect", "export")
STATUSES = ("pending", "in_progress", "needs_order_confirmation",
           "completed", "failed", "interrupted")

# Statuses that mean "actively consuming CPU/memory, or about to" — what
# the single-job-at-a-time lock and the startup sweep both check for.
# needs_order_confirmation is deliberately excluded: it's only waiting on
# a human decision, costs nothing, and must survive a server restart
# rather than being discarded — a restart doesn't invalidate an
# unanswered question.
RUNNING_STATUSES = ("pending", "in_progress")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def job_path(batch_dir, job_type: str) -> Path:
    assert job_type in JOB_TYPES, job_type
    return Path(batch_dir) / f"{job_type}_job.json"


def create_job(batch_dir, batch_id: str, job_type: str,
              status: str = "pending", **extra) -> dict:
    job = {
        "job_id": new_id(), "batch_id": batch_id, "type": job_type,
        "status": status, "stage": None,
        "started_at": _now(), "updated_at": _now(),
        "suggested_order": None, "order_reason": None,
        "warnings": [], "error": None,
        "manifest_path": None, "output_path": None,
    }
    job.update(extra)
    save_job(batch_dir, job)
    return job


def save_job(batch_dir, job: dict) -> None:
    job["updated_at"] = _now()
    job_path(batch_dir, job["type"]).write_text(json.dumps(job, indent=2))


def load_job(batch_dir, job_type: str) -> dict | None:
    p = job_path(batch_dir, job_type)
    if not p.exists():
        return None
    return json.loads(p.read_text())


def run_with_timeout(fn, batch_dir, job: dict, args: tuple,
                     timeout_s: int | None = None) -> None:
    """Runs fn(batch_dir, job, *args) -- one of backend.pipeline_runner's
    real job functions -- in a genuinely separate OS process, not just a
    thread: a Python thread can't be forcibly stopped, and the real
    threat this defends against (a pathological or corrupt file causing
    cv2/ffmpeg decoding to hang indefinitely) needs an actual kill, not
    just "give up watching it."

    Releasing the single-job lock is what "on timeout, fail cleanly and
    don't leave the service stuck" actually means here: find_active_job
    only ever reads job status files, never checks whether a real
    process is still alive, so once every RUNNING job file for this
    batch reads "failed", a subsequent job can start immediately --
    independent of whether the killed process's own OS resources have
    fully unwound yet.

    Sweeps every JOB_TYPE for this batch on timeout, not just the one
    `job` passed in at dispatch time -- real, found live (not
    theoretical): run_detect_then_export_job can finish detect INSIDE
    the timeout window and chain straight into creating a real, separate
    export job, which was still genuinely in_progress when the timeout
    fired. Failing only the original detect job left that export job
    file dangling at in_progress forever, a permanent lock leak
    find_active_job would report as active indefinitely -- confirmed via
    a real live run (FMH_PROCESSING_TIMEOUT_S=3 against the real demo
    pipeline) before this sweep was added, and confirmed fixed by the
    same live scenario afterward.

    fn's own real per-model-instance-per-call cost (see
    pipeline/detection.py's detect_persons, which already constructs a
    fresh model on every call, no cross-call cached instance) means
    running it in a fresh process pays no NEW per-job model-reload cost
    beyond what already happened in-thread before this change -- the
    real added cost is one-time Python/ML-library import overhead per
    job, not per-frame work.
    """
    if timeout_s is None:
        timeout_s = DEFAULT_PROCESSING_TIMEOUT_S
    process = multiprocessing.Process(target=fn, args=(batch_dir, job, *args))
    process.start()
    process.join(timeout_s)
    if not process.is_alive():
        return  # finished (successfully or not) within the timeout --
                # fn's own save_job calls already recorded the real outcome

    process.terminate()  # SIGTERM first: a real chance to exit cleanly
    process.join(5)
    if process.is_alive():
        process.kill()  # SIGKILL: it ignored SIGTERM
        process.join()

    error = f"processing timed out after {timeout_s}s and was stopped"
    for job_type in JOB_TYPES:
        current = load_job(batch_dir, job_type)
        if current and current["status"] in RUNNING_STATUSES:
            current["status"] = "failed"
            current["stage"] = None
            current["error"] = error
            save_job(batch_dir, current)


def find_active_job(uploads_root, org_id: str) -> dict | None:
    """First job anywhere under uploads_root/org_id that's
    pending/in_progress — the single source of truth for the
    single-job-at-a-time lock. Scoped per-org (not global): one
    organization's processing run must not block a different
    organization's, now that uploads_root holds more than one org's
    batches (uploads_root/<org_id>/<batch_id>)."""
    org_root = Path(uploads_root) / org_id
    if not org_root.exists():
        return None
    for batch_dir in sorted(p for p in org_root.iterdir() if p.is_dir()):
        for job_type in JOB_TYPES:
            job = load_job(batch_dir, job_type)
            if job and job["status"] in RUNNING_STATUSES:
                return job
    return None


def sweep_interrupted_jobs(uploads_root) -> list:
    """Startup sweep: a job still marked pending/in_progress from before
    this process started didn't keep running while the server was down —
    it's stale. Mark it interrupted instead of leaving it to silently
    report "in progress" forever. Returns the jobs that were swept.

    Unlike find_active_job (scoped to one org's lock), this runs once at
    startup and must catch every org's interrupted jobs, so it walks two
    levels: uploads_root/<org_id>/<batch_id>."""
    uploads_root = Path(uploads_root)
    swept = []
    if not uploads_root.exists():
        return swept
    for org_dir in sorted(p for p in uploads_root.iterdir() if p.is_dir()):
        for batch_dir in sorted(p for p in org_dir.iterdir() if p.is_dir()):
            for job_type in JOB_TYPES:
                job = load_job(batch_dir, job_type)
                if job and job["status"] in RUNNING_STATUSES:
                    job["status"] = "interrupted"
                    job["error"] = (job.get("error") or
                                    "server restarted while this job was running")
                    save_job(batch_dir, job)
                    swept.append(job)
    return swept
