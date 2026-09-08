"""Unit tests for backend.jobs: the durable (JSON-file-per-job) progress
state, the single-job-at-a-time active scan, and the startup interrupt
sweep. Pure filesystem logic, no HTTP layer, no real pipeline."""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import jobs


# Real top-level functions, not closures/lambdas: multiprocessing's
# 'spawn' start method (macOS/Windows default) pickles a target by
# module+name reference and re-imports it in the child process, which
# only works for something importable at module scope.
def _finishes_quickly(batch_dir, job):
    job["status"] = "completed"
    jobs.save_job(batch_dir, job)


def _hangs_forever(batch_dir, job):
    # writes a real heartbeat file every 0.1s so a test can prove the
    # process was ACTUALLY killed (heartbeat stops advancing), not just
    # that the parent gave up waiting on it
    heartbeat = Path(batch_dir) / "heartbeat.txt"
    deadline = time.monotonic() + 100
    while time.monotonic() < deadline:
        heartbeat.write_text(str(time.monotonic()))
        time.sleep(0.1)


def _finishes_detect_then_hangs_in_export(batch_dir, job):
    # reproduces the real scenario found live: run_detect_then_export_job
    # finishing its detect phase INSIDE the timeout window, then chaining
    # into a real, separate export job that's still in_progress when the
    # process gets killed
    job["status"] = "completed"
    jobs.save_job(batch_dir, job)
    export_job = jobs.create_job(batch_dir, job["batch_id"], "export",
                                 status="in_progress")
    jobs.save_job(batch_dir, export_job)
    time.sleep(100)


def _hangs_forever_after_stage(batch_dir, job):
    # note: `stage` deliberately isn't used here -- run_with_timeout's
    # own failure path resets it to None (same as every other "job
    # failed" path in backend/pipeline_runner.py), so it wouldn't
    # demonstrate anything by surviving. `warnings` isn't touched by the
    # timeout handler, so it's the real, meaningful field to check.
    job["warnings"] = ["a real warning the child actually wrote"]
    jobs.save_job(batch_dir, job)
    time.sleep(100)


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


# ---- processing timeout (real subprocess, real kill) ----

def test_run_with_timeout_lets_a_quick_job_finish_normally(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    job = jobs.create_job(bdir, "batchA", "detect", status="pending")

    jobs.run_with_timeout(_finishes_quickly, bdir, job, (), timeout_s=30)

    result = jobs.load_job(bdir, "detect")
    assert result["status"] == "completed"  # fn's own outcome, not overwritten


def test_run_with_timeout_kills_a_hung_job_and_marks_it_failed(tmp_path):
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    job = jobs.create_job(bdir, "batchA", "detect", status="pending")

    start = time.monotonic()
    jobs.run_with_timeout(_hangs_forever, bdir, job, (), timeout_s=2)
    elapsed = time.monotonic() - start

    # real wall-clock proof: bounded by the 2s timeout (+ a few seconds'
    # grace for a clean SIGTERM exit), nowhere near the 100s _hangs_forever
    # itself asks for
    assert elapsed < 20

    result = jobs.load_job(bdir, "detect")
    assert result["status"] == "failed"
    assert "timed out" in result["error"]

    # real proof the process was actually killed, not just abandoned:
    # the heartbeat file _hangs_forever writes every 0.1s must have
    # genuinely stopped advancing, not still be ticking in the background
    heartbeat = bdir / "heartbeat.txt"
    assert heartbeat.exists()
    last_seen = heartbeat.read_text()
    time.sleep(1.0)
    assert heartbeat.read_text() == last_seen  # no further writes happened


def test_run_with_timeout_releases_the_lock_for_a_subsequent_job(tmp_path):
    bdir_a = tmp_path / "orgA" / "batchA"
    bdir_a.mkdir(parents=True)
    job_a = jobs.create_job(bdir_a, "batchA", "detect", status="pending")

    jobs.run_with_timeout(_hangs_forever, bdir_a, job_a, (), timeout_s=2)

    # the lock is real disk state (find_active_job only ever reads the
    # job status file) -- confirm it reads as free immediately after the
    # timeout, which is exactly what lets a subsequent job start
    assert jobs.find_active_job(tmp_path, "orgA") is None

    bdir_b = tmp_path / "orgA" / "batchB"
    bdir_b.mkdir(parents=True)
    job_b = jobs.create_job(bdir_b, "batchB", "detect", status="pending")
    active = jobs.find_active_job(tmp_path, "orgA")
    assert active is not None
    assert active["batch_id"] == "batchB"


def test_run_with_timeout_preserves_real_progress_the_child_already_wrote(tmp_path):
    # the parent must reload the job file rather than blindly overwriting
    # with its own stale copy -- a real stage the child recorded before
    # hanging must survive into the failure record, not be silently lost
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    job = jobs.create_job(bdir, "batchA", "detect", status="pending")

    jobs.run_with_timeout(_hangs_forever_after_stage, bdir, job, (), timeout_s=2)

    result = jobs.load_job(bdir, "detect")
    assert result["status"] == "failed"
    assert "timed out" in result["error"]
    # the real progress the child wrote before hanging must survive --
    # a naive implementation that writes back the caller's own stale
    # `job` dict (captured before dispatch, warnings still []) would
    # silently lose this
    assert result["warnings"] == ["a real warning the child actually wrote"]


def test_run_with_timeout_fails_a_chained_job_type_too(tmp_path):
    # regression test for a real bug found via a live server run
    # (FMH_PROCESSING_TIMEOUT_S=3 against the real demo pipeline):
    # run_detect_then_export_job can finish detect INSIDE the timeout
    # window and chain into creating a real, separate export job that's
    # still in_progress when the kill fires. A fix that only knows about
    # the ONE job_type passed in at dispatch time leaves that second job
    # file dangling at in_progress forever -- a permanent lock leak.
    bdir = tmp_path / "orgA" / "batchA"
    bdir.mkdir(parents=True)
    detect_job = jobs.create_job(bdir, "batchA", "detect", status="pending")

    jobs.run_with_timeout(_finishes_detect_then_hangs_in_export, bdir,
                          detect_job, (), timeout_s=2)

    # detect really did complete -- must NOT be clobbered back to failed
    assert jobs.load_job(bdir, "detect")["status"] == "completed"
    # the export job the child created and left running must be failed too
    export_result = jobs.load_job(bdir, "export")
    assert export_result["status"] == "failed"
    assert "timed out" in export_result["error"]

    # the real point: the lock must actually be free afterward
    assert jobs.find_active_job(tmp_path, "orgA") is None
