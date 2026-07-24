"""Durable job state: written to a JSON file at every stage transition,
mirroring pipeline.manifest's save/load-a-JSON-file pattern rather than
an in-memory dict. A real detect job takes tens of minutes and the
backend restarts constantly during Phase 7/8 development — in-memory-only
progress would silently vanish on every restart, which is a near-certain
failure mode here, not a simplification.

One job file per batch per job type: <batch_dir>/detect_job.json and
<batch_dir>/export_job.json. There is no separate lock file for the
single-job-at-a-time rule — find_active_job() scans the job files
directly, so there's nothing else that could drift out of sync with what
actually happened.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

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


def find_active_job(uploads_root) -> dict | None:
    """First job anywhere under uploads_root that's pending/in_progress —
    the single source of truth for the global single-job-at-a-time lock."""
    uploads_root = Path(uploads_root)
    if not uploads_root.exists():
        return None
    for batch_dir in sorted(p for p in uploads_root.iterdir() if p.is_dir()):
        for job_type in JOB_TYPES:
            job = load_job(batch_dir, job_type)
            if job and job["status"] in RUNNING_STATUSES:
                return job
    return None


def sweep_interrupted_jobs(uploads_root) -> list:
    """Startup sweep: a job still marked pending/in_progress from before
    this process started didn't keep running while the server was down —
    it's stale. Mark it interrupted instead of leaving it to silently
    report "in progress" forever. Returns the jobs that were swept."""
    uploads_root = Path(uploads_root)
    swept = []
    if not uploads_root.exists():
        return swept
    for batch_dir in sorted(p for p in uploads_root.iterdir() if p.is_dir()):
        for job_type in JOB_TYPES:
            job = load_job(batch_dir, job_type)
            if job and job["status"] in RUNNING_STATUSES:
                job["status"] = "interrupted"
                job["error"] = (job.get("error") or
                                "server restarted while this job was running")
                save_job(batch_dir, job)
                swept.append(job)
    return swept
