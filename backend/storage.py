"""Upload storage: one directory per batch under uploads/, holding the
source video files plus (from backend.jobs) the job state and the
manifest — flat, inspectable JSON-and-files on disk, same approach as
the rest of this project, no database.
"""

import os
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Overridable via FMH_UPLOADS_ROOT so a deployment can point this at a
# mounted persistent volume (uploads/, manifests, job state, and
# output.mp4 are real user data, unlike the detection cache -- this one
# actually needs to survive a container restart/redeploy). create_app()'s
# own uploads_root param still wins for tests, which pass an isolated
# tmp_path regardless of this env var.
DEFAULT_UPLOADS_ROOT = Path(os.environ.get("FMH_UPLOADS_ROOT", str(ROOT / "uploads")))

# Uploaded files here run 1GB+ (a real 30-60+ minute recording). Reading
# in fixed-size chunks means the process never buffers a whole file in
# memory regardless of its size.
CHUNK_SIZE = 1024 * 1024


def new_batch_id() -> str:
    return uuid.uuid4().hex[:12]


def batch_dir(uploads_root, batch_id: str) -> Path:
    return Path(uploads_root) / batch_id


def save_upload(dest_path, file_obj) -> int:
    """Stream an uploaded file's contents to disk in CHUNK_SIZE pieces —
    never a single .read() of the whole (potentially 1GB+) body. Returns
    bytes written."""
    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(dest_path, "wb") as out:
        while True:
            chunk = file_obj.read(CHUNK_SIZE)
            if not chunk:
                break
            out.write(chunk)
            written += len(chunk)
    return written
