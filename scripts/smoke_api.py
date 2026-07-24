"""Basic end-to-end smoke test for the backend API against a REAL running
server (not FastAPI's in-process TestClient) — the Phase 6 spec's "basic
test showing the API working via a simple script or API client (not the
UI yet)".

Usage:
    ./venv/bin/uvicorn backend.app:app --port 8420 &
    ./venv/bin/python scripts/smoke_api.py reference_clips/clip_60.mkv --base-url http://127.0.0.1:8420

Uploads a real video, triggers real detection (the actual pipeline, not
a fake — this is intentionally slow, proportional to the video length),
polls progress to completion, reads the manifest, restores one cut
segment, re-exports, and confirms the output file exists.
"""

import argparse
import sys
import time
from pathlib import Path

import httpx


def poll_job(client, batch_id, job_type, interval=2.0, timeout=1800):
    start = time.monotonic()
    last_stage = None
    while True:
        r = client.get(f"/batches/{batch_id}/jobs/{job_type}")
        r.raise_for_status()
        job = r.json()
        if job["stage"] != last_stage:
            print(f"  [{job_type}] stage: {job['stage']} (status={job['status']})")
            last_stage = job["stage"]
        if job["status"] in ("completed", "failed", "interrupted"):
            return job
        if time.monotonic() - start > timeout:
            raise TimeoutError(f"{job_type} job did not finish within {timeout}s")
        time.sleep(interval)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--base-url", default="http://127.0.0.1:8420")
    args = ap.parse_args()

    video = Path(args.video)
    with httpx.Client(base_url=args.base_url, timeout=60.0) as client:
        print(f"uploading {video.name}...")
        with open(video, "rb") as f:
            r = client.post("/batches", files=[("files", (video.name, f, "video/x-matroska"))])
        r.raise_for_status()
        batch_id = r.json()["batch_id"]
        print(f"batch_id={batch_id}")

        calib_path = video.parent / "calibration.json"
        if calib_path.exists():
            print(f"uploading calibration from {calib_path}...")
            with open(calib_path, "rb") as f:
                r = client.post(f"/batches/{batch_id}/calibration",
                               files={"calibration_file": ("calibration.json", f, "application/json")})
            r.raise_for_status()
            print(f"  calibration set: {r.json()}")
        else:
            print(f"no calibration.json found next to {video} — "
                 f"detection will run with plate-occupancy disabled")

        print("triggering detection...")
        r = client.post(f"/batches/{batch_id}/process")
        r.raise_for_status()
        job = r.json()
        if job["status"] == "needs_order_confirmation":
            sys.exit("unexpected: single-file upload should never need order confirmation")

        job = poll_job(client, batch_id, "detect")
        if job["status"] != "completed":
            sys.exit(f"detect job did not complete: {job}")
        if job["warnings"]:
            print(f"  warnings: {job['warnings']}")
        print("detect completed.")

        r = client.get(f"/batches/{batch_id}/manifest")
        r.raise_for_status()
        manifest = r.json()
        kept = [s for s in manifest["segments"] if s["status"] == "kept"]
        cut = [s for s in manifest["segments"] if s["status"] == "cut"]
        print(f"manifest: {len(kept)} kept, {len(cut)} cut segments")
        assert kept, "expected at least one kept segment on a real clip"

        if cut:
            target = cut[0]
            print(f"restoring cut segment {target['id']} "
                 f"({target['start']}-{target['end']})...")
            r = client.patch(
                f"/batches/{batch_id}/manifest/segments/{target['id']}",
                json={"status": "kept"})
            r.raise_for_status()
            assert r.json()["status"] == "kept"
            print("restore confirmed via PATCH response.")

        print("triggering export...")
        r = client.post(f"/batches/{batch_id}/export")
        r.raise_for_status()
        job = poll_job(client, batch_id, "export")
        if job["status"] != "completed":
            sys.exit(f"export job did not complete: {job}")

        output_path = Path(job["output_path"])
        assert output_path.exists(), f"output file missing: {output_path}"
        size_mb = output_path.stat().st_size / 1024 / 1024
        print(f"export completed: {output_path} ({size_mb:.1f} MB)")

        print("\nSMOKE TEST PASSED")


if __name__ == "__main__":
    main()
