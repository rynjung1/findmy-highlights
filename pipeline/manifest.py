"""The manifest: the single source of truth tying detection, the Edit Log,
and (re-)export together.

A manifest lists EVERY span of the source video exactly once: the detected
action segments with status "kept", and the gaps between them (the dead
time the pipeline removed) with status "cut". The Edit Log shows the cut
entries; restoring one flips its status to "kept" and the output is always
regenerated from the current kept set — video files are never mutated.

Timestamps are stored both as "HH:MM:SS.mmm" strings (the spec format,
human-readable) and as float seconds (lossless for tooling). The `origin`
field records how an entry came to be ("detected" / "gap"); a future
manual-cut feature can add its own origin without schema changes.
"""

import json
from pathlib import Path

MANIFEST_VERSION = 1
VALID_STATUS = ("kept", "cut")


def fmt_ts(seconds: float) -> str:
    m, s = divmod(round(float(seconds), 3), 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{int(m):02d}:{s:06.3f}"


def parse_ts(ts: str) -> float:
    h, m, s = ts.split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def build_manifest(source_file: str, duration: float, kept_segments,
                   score_fn=None) -> dict:
    """Build a manifest from final kept segments. Gaps become cut entries.

    score_fn(start_s, end_s) -> float, optional, supplies detection_score
    per span (e.g. peak smoothed motion); defaults to 0.0 for gaps and 1.0
    for kept spans if not given.
    """
    spans = []
    cursor = 0.0
    for a, b in sorted(kept_segments):
        a, b = max(0.0, float(a)), min(float(duration), float(b))
        if a > cursor:
            spans.append((cursor, a, "cut"))
        spans.append((a, b, "kept"))
        cursor = b
    if cursor < duration:
        spans.append((cursor, float(duration), "cut"))

    segments = []
    for i, (a, b, status) in enumerate(spans, start=1):
        if score_fn is not None:
            score = round(float(score_fn(a, b)), 3)
        else:
            score = 1.0 if status == "kept" else 0.0
        segments.append({
            "id": f"seg_{i:03d}",
            "source_file": source_file,
            "start": fmt_ts(a), "end": fmt_ts(b),
            "start_s": round(a, 3), "end_s": round(b, 3),
            "detection_score": score,
            "status": status,
            "origin": "detected" if status == "kept" else "gap",
        })
    return {
        "version": MANIFEST_VERSION,
        "source_files": [source_file],
        "duration_s": round(float(duration), 3),
        "segments": segments,
    }


def save_manifest(manifest: dict, path) -> None:
    Path(path).write_text(json.dumps(manifest, indent=2))


def load_manifest(path) -> dict:
    m = json.loads(Path(path).read_text())
    if m.get("version") != MANIFEST_VERSION:
        raise ValueError(f"unsupported manifest version: {m.get('version')}")
    return m


def set_status(manifest: dict, seg_id: str, status: str) -> dict:
    """Flip a segment's kept/cut status (the Edit Log restore action)."""
    if status not in VALID_STATUS:
        raise ValueError(f"invalid status {status!r}, expected one of "
                         f"{VALID_STATUS}")
    for seg in manifest["segments"]:
        if seg["id"] == seg_id:
            seg["status"] = status
            return seg
    raise KeyError(f"no segment with id {seg_id!r}")


def kept_spans(manifest: dict):
    """Current kept (start_s, end_s) spans, sorted, adjacent ones merged —
    this is what export renders."""
    spans = sorted((s["start_s"], s["end_s"])
                   for s in manifest["segments"] if s["status"] == "kept")
    return merge_adjacent(spans)


def merge_adjacent(spans, eps: float = 1e-6):
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= eps:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(s) for s in merged]
