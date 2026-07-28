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

Multi-file (Stage 4): `start_s`/`end_s` are always LOCAL to a segment's own
`source_file` (matching the spec's example, which shows plain timestamps
per source file, not one global concatenated clock). Every segment also
carries `source_file_index`, an explicit integer position into the
manifest's `source_files` list — the single source of truth for file
order. A reader must never re-derive order by sorting filenames or
re-probing timestamps; `source_files` + `source_file_index` already say
it exactly. Critically, spans from different files are NEVER merged into
one span even if their local timestamps look numerically adjacent — they
are different pieces of video that need independent seeking, and per the
Stage 3/4 boundary decision, nothing about detection reasons across a
file boundary either.
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


def _spans_with_gaps(kept_segments, duration):
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
    return spans


def build_manifest(source_file: str, duration: float, kept_segments,
                   score_fn=None) -> dict:
    """Build a single-file manifest from final kept segments. Gaps become
    cut entries. For multiple files, use build_multi_file_manifest.

    score_fn(start_s, end_s) -> float, optional, supplies detection_score
    per span (e.g. peak smoothed motion); defaults to 0.0 for gaps and 1.0
    for kept spans if not given.
    """
    segments = _segments_for_file(source_file, 0, duration, kept_segments,
                                  score_fn, start_id=1)
    return {
        "version": MANIFEST_VERSION,
        "source_files": [source_file],
        "duration_s": round(float(duration), 3),
        "segments": segments,
    }


def _segments_for_file(source_file, source_file_index, duration,
                       kept_segments, score_fn, start_id):
    segments = []
    for i, (a, b, status) in enumerate(
            _spans_with_gaps(kept_segments, duration), start=start_id):
        if score_fn is not None:
            score = round(float(score_fn(a, b)), 3)
        else:
            score = 1.0 if status == "kept" else 0.0
        segments.append({
            "id": f"seg_{i:03d}",
            "source_file": source_file,
            "source_file_index": source_file_index,
            "start": fmt_ts(a), "end": fmt_ts(b),
            "start_s": round(a, 3), "end_s": round(b, 3),
            "detection_score": score,
            "status": status,
            "origin": "detected" if status == "kept" else "gap",
        })
    return segments


def build_multi_file_manifest(files) -> dict:
    """Build a manifest spanning multiple source files.

    `files` is an ordered list of dicts, one per file, IN THE ALREADY
    -DETERMINED processing order (e.g. from pipeline.multifile.order_files
    — this function does not re-derive or verify order, callers must have
    resolved any ambiguity first):
        {"source_file": str, "duration": float, "kept_segments": [...],
         "score_fn": callable or None (optional)}

    Each file's segments are local to that file (see module docstring);
    `source_file_index` is each file's position in this list, which also
    becomes the manifest's `source_files` order — the locked-in record of
    processing order.
    """
    segments = []
    next_id = 1
    for idx, f in enumerate(files):
        file_segments = _segments_for_file(
            f["source_file"], idx, f["duration"], f["kept_segments"],
            f.get("score_fn"), start_id=next_id)
        segments.extend(file_segments)
        next_id += len(file_segments)
    return {
        "version": MANIFEST_VERSION,
        "source_files": [f["source_file"] for f in files],
        # sum of each file's OWN duration — footage content, not a
        # continuous timeline; real gaps between files are not counted
        # here (they aren't footage), see pipeline.multifile for those
        "duration_s": round(sum(float(f["duration"]) for f in files), 3),
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
    """Current kept (start_s, end_s) spans for a SINGLE-FILE manifest,
    sorted, adjacent ones merged — this is what export renders.

    For a multi-file manifest, use kept_spans_by_file instead: bare
    (start_s, end_s) pairs are only unambiguous when every segment shares
    one source file, and merging across files would silently splice two
    different videos' timestamps into one span.
    """
    spans = sorted((s["start_s"], s["end_s"])
                   for s in manifest["segments"] if s["status"] == "kept")
    return merge_adjacent(spans)


def kept_spans_by_file(manifest: dict):
    """Current kept spans grouped by file, in source_files order. Returns
    a list of {"source_file", "source_file_index", "spans"} dicts; spans
    are merged only WITHIN each file, never across the boundary between
    two files, even if their local timestamps would look adjacent."""
    by_index = {}
    for s in manifest["segments"]:
        if s["status"] != "kept":
            continue
        by_index.setdefault(s["source_file_index"], []).append(
            (s["start_s"], s["end_s"]))
    out = []
    for idx, name in enumerate(manifest["source_files"]):
        spans = merge_adjacent(sorted(by_index.get(idx, [])))
        out.append({"source_file": name, "source_file_index": idx,
                    "spans": spans})
    return out


def merge_adjacent(spans, eps: float = 1e-6):
    merged = []
    for a, b in spans:
        if merged and a - merged[-1][1] <= eps:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(s) for s in merged]
