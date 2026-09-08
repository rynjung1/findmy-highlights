"""Unit tests for backend.jobs: the durable (JSON-file-per-job) progress
state, the single-job-at-a-time active scan, and the startup interrupt
sweep. Pure filesystem logic, no HTTP layer, no real pipeline."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jobs


def test_create_and_load_job_round_trip(tmp_path):
    job = jobs.create_job(tmp_path, "batch1", "detect")
    loaded = jobs.load_job(tmp_path, "detect")
    assert loaded["job_id"] == job["job_id"]
    assert loaded["batch_id"] == "batch1"
    assert loaded["status"] == "pending"


def test_load_job_missing_returns_none(tmp_path):
    assert jobs.load_job(tmp_path, "detect") is None


def test_save_job_persists_stage_updates(tmp_path):
    job = jobs.create_job(tmp_path, "batch1", "detect")
    job["stage"] = "analyzing motion"
    jobs.save_job(tmp_path, job)
    reloaded = jobs.load_job(tmp_path, "detect")
    assert reloaded["stage"] == "analyzing motion"


def test_find_active_job_none_when_nothing_running(tmp_path):
    assert jobs.find_active_job(tmp_path, "orgA") is None


def test_find_active_job_finds_pending(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect", status="pending")
    active = jobs.find_active_job(tmp_path, "orgA")
    assert active is not None
    assert active["batch_id"] == "batchA"


def test_find_active_job_finds_in_progress(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect", status="in_progress")
    active = jobs.find_active_job(tmp_path, "orgA")
    assert active["status"] == "in_progress"


def test_find_active_job_ignores_needs_order_confirmation(tmp_path):
    # a batch waiting on a human decision costs nothing and must never
    # block a different batch from processing
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect",
                    status="needs_order_confirmation")
    assert jobs.find_active_job(tmp_path, "orgA") is None


def test_find_active_job_ignores_completed_and_failed(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect", status="completed")
    assert jobs.find_active_job(tmp_path, "orgA") is None


def test_find_active_job_is_scoped_per_org(tmp_path):
    # the single-job lock is per-organization, not global: org B running
    # a job must never block org A from starting its own (see
    # backend/jobs.py's module docstring for why this changed)
    bdir_a = tmp_path / "orgA" / "batchA"
    bdir_a.mkdir(parents=True)
    bdir_b = tmp_path / "orgB" / "batchB"
    bdir_b.mkdir(parents=True)
    jobs.create_job(bdir_b, "batchB", "detect", status="in_progress")

    assert jobs.find_active_job(tmp_path, "orgA") is None
    active_b = jobs.find_active_job(tmp_path, "orgB")
    assert active_b is not None
    assert active_b["batch_id"] == "batchB"


def test_sweep_interrupted_marks_pending_and_in_progress(tmp_path):
    bdir1 = tmp_path / "orgA" / "batchA"
    bdir1.mkdir(parents=True)
    bdir2 = tmp_path / "orgA" / "batchB"
    bdir2.mkdir(parents=True)
    jobs.create_job(bdir1, "batchA", "detect", status="pending")
    jobs.create_job(bdir2, "batchB", "detect", status="in_progress")

    swept = jobs.sweep_interrupted_jobs(tmp_path)

    assert len(swept) == 2
    assert jobs.load_job(bdir1, "detect")["status"] == "interrupted"
    assert jobs.load_job(bdir2, "detect")["status"] == "interrupted"
    for job in (jobs.load_job(bdir1, "detect"), jobs.load_job(bdir2, "detect")):
        assert job["error"]  # explains why, not just silently flipped


def test_sweep_interrupted_covers_every_org(tmp_path):
    # the startup sweep, unlike find_active_job, must catch a stale job
    # in ANY org, not just one
    bdir_a = tmp_path / "orgA" / "batchA"
    bdir_a.mkdir(parents=True)
    bdir_b = tmp_path / "orgB" / "batchB"
    bdir_b.mkdir(parents=True)
    jobs.create_job(bdir_a, "batchA", "detect", status="in_progress")
    jobs.create_job(bdir_b, "batchB", "detect", status="pending")

    swept = jobs.sweep_interrupted_jobs(tmp_path)

    assert len(swept) == 2
    assert jobs.load_job(bdir_a, "detect")["status"] == "interrupted"
    assert jobs.load_job(bdir_b, "detect")["status"] == "interrupted"


def test_sweep_interrupted_leaves_needs_order_confirmation_alone(tmp_path):
    # a restart must not discard an unanswered question the user hasn't
    # gotten to yet
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect",
                    status="needs_order_confirmation")
    jobs.sweep_interrupted_jobs(tmp_path)
    assert jobs.load_job(bdir, "detect")["status"] == "needs_order_confirmation"


def test_sweep_interrupted_leaves_completed_and_failed_alone(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    jobs.create_job(bdir, "batchA", "detect", status="completed")
    jobs.create_job(bdir, "batchA", "export", status="failed")
    jobs.sweep_interrupted_jobs(tmp_path)
    assert jobs.load_job(bdir, "detect")["status"] == "completed"
    assert jobs.load_job(bdir, "export")["status"] == "failed"


def test_sweep_on_empty_uploads_root_is_a_noop(tmp_path):
    missing = tmp_path / "does_not_exist"
    assert jobs.sweep_interrupted_jobs(missing) == []
    assert jobs.find_active_job(missing, "orgA") is None
