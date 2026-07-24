"""Unit tests for backend.storage: chunked upload streaming. The
requirement (real files here run 1GB+) is that content is never read in
one whole-body .read() call — verified directly against the read calls
made, not just that the final bytes happen to match."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import storage


class _RecordingFile:
    """Fake upload file object: records every .read(n) call's argument
    and fails if .read() is ever called without an explicit size, which
    is exactly the whole-body-buffering mistake this test guards
    against."""

    def __init__(self, content: bytes):
        self._data = content
        self._pos = 0
        self.read_sizes = []

    def read(self, size=None):
        if size is None:
            raise AssertionError(
                "save_upload must never call .read() with no size — "
                "that buffers the whole file in memory")
        self.read_sizes.append(size)
        chunk = self._data[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk


def test_save_upload_reads_in_fixed_size_chunks(tmp_path):
    content = b"x" * (storage.CHUNK_SIZE * 3 + 123)  # not an even multiple
    f = _RecordingFile(content)
    dest = tmp_path / "video.mkv"

    written = storage.save_upload(dest, f)

    assert written == len(content)
    assert dest.read_bytes() == content
    # every read call requested exactly CHUNK_SIZE (the last one may
    # return less, but it must still ask for CHUNK_SIZE, never "the rest")
    assert all(size == storage.CHUNK_SIZE for size in f.read_sizes)
    assert len(f.read_sizes) == 5  # 3 full chunks + 1 partial(123B) + final empty read


def test_save_upload_creates_parent_dirs(tmp_path):
    f = _RecordingFile(b"hello")
    dest = tmp_path / "batch123" / "video.mkv"
    storage.save_upload(dest, f)
    assert dest.exists()
    assert dest.read_bytes() == b"hello"


def test_save_upload_empty_file(tmp_path):
    f = _RecordingFile(b"")
    dest = tmp_path / "empty.mkv"
    written = storage.save_upload(dest, f)
    assert written == 0
    assert dest.exists()
    assert dest.read_bytes() == b""
