"""Multi-file ordering and gap detection (Stage 4).

Files are ordered by capture-time metadata (ffprobe's creation_time tag)
first. That signal is trusted only when it's unambiguous — present on
every file, and far enough apart that measurement/precision noise
couldn't flip two files' order. When it isn't (missing metadata, or two
files whose timestamps are implausibly close for footage that's normally
tens of minutes long), ordering is NOT guessed: the caller must get an
explicit confirmation from the user before processing, per the project
rule that file ordering falls back to asking, not assuming.

A real time gap between files (recording stopped, phone ran out of
storage) is detected from the same metadata and reported so downstream
code and the UI know about it — but per the Stage 3/4 boundary decision,
NOTHING in the detection pipeline (play extension, at-bat state) is ever
allowed to reason across a file boundary regardless of gap size. Gap
detection here is informational, not a gate on pipeline behavior.
"""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# Two files' creation_time values closer together than this are treated
# as ambiguous rather than trusted, even if both are present. Real game
# footage files are normally tens of minutes long; a gap this small
# between START times only happens from bad/copied metadata (e.g. a
# file-copy tool overwriting creation_time) or two cameras recording
# concurrently — neither case should be silently ordered.
AMBIGUITY_THRESHOLD_S = 5.0


@dataclass
class FileInfo:
    path: str
    width: int
    height: int
    fps: float
    duration_s: float
    creation_time: datetime | None  # None if missing/unparseable


class AmbiguousOrderError(Exception):
    """Raised when automatic ordering can't be trusted and no explicit
    order was given. Carries enough detail for a caller (CLI, future API)
    to present a real, actionable "confirm or reorder" prompt — this must
    never be a dead end, per the project rule that ordering falls back to
    asking the user, not assuming."""

    def __init__(self, reason: str, suggested_order: list[str]):
        self.reason = reason
        self.suggested_order = suggested_order
        super().__init__(
            f"file order is ambiguous: {reason}; "
            f"suggested order: {', '.join(suggested_order)}")


@dataclass
class OrderResult:
    ordered_paths: list[str]         # best-effort order (may be ambiguous)
    ambiguous: bool
    reason: str | None = None        # human-readable cause, if ambiguous
    gaps_s: list[float | None] = field(default_factory=list)  # gap BEFORE
                                      # each file; None for the first file
                                      # or when either endpoint is unknown
    mismatched_resolution: bool = False
    mismatched_fps: bool = False


def _parse_probe_sections(out: str) -> tuple[dict, dict]:
    """Split ffprobe's [STREAM]/[FORMAT]-delimited default output into two
    separate key/value dicts. Sections must be parsed separately — a
    stream's own `duration` is frequently "N/A" for container formats that
    only record duration at the format (whole-file) level (observed on
    this project's own .mkv reference clips), so blindly taking whichever
    `duration=` line appears first silently produces 0.0 on those files."""
    stream, fmt, current = {}, {}, None
    for line in out.splitlines():
        line = line.strip()
        if line == "[STREAM]":
            current = stream
        elif line == "[FORMAT]":
            current = fmt
        elif line in ("[/STREAM]", "[/FORMAT]"):
            current = None
        elif "=" in line and current is not None:
            k, v = line.split("=", 1)
            current.setdefault(k.removeprefix("TAG:"), v)
    return stream, fmt


def probe_file(path: str) -> FileInfo:
    """Read duration, resolution, fps, and creation_time via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=width,height,r_frame_rate,duration:"
         "stream_tags=creation_time:format=duration:format_tags=creation_time",
         "-of", "default=noprint_wrappers=0", str(path)],
        capture_output=True, text=True, check=True).stdout
    stream, fmt = _parse_probe_sections(out)

    width = int(stream.get("width", 0))
    height = int(stream.get("height", 0))
    num, den = (stream.get("r_frame_rate", "0/1")).split("/")
    fps = float(num) / float(den) if float(den) else 0.0

    # prefer format (whole-file) duration; fall back to the stream's own
    # duration only if the format-level one is absent or "N/A"
    duration_s = 0.0
    for src in (fmt, stream):
        raw = src.get("duration")
        if raw and raw != "N/A":
            duration_s = float(raw)
            break

    creation_time = None
    raw_ct = fmt.get("creation_time") or stream.get("creation_time")
    if raw_ct:
        try:
            creation_time = datetime.fromisoformat(raw_ct.replace("Z", "+00:00"))
        except ValueError:
            creation_time = None

    return FileInfo(path=str(path), width=width, height=height, fps=fps,
                    duration_s=duration_s, creation_time=creation_time)


def order_files(paths) -> OrderResult:
    """Probe each file and determine processing order. Thin I/O wrapper —
    see order_infos() for the actual (pure, unit-tested) ordering logic."""
    infos = [probe_file(p) for p in paths]
    return order_infos(infos)


def order_infos(infos: list[FileInfo]) -> OrderResult:
    """Determine processing order for already-probed files. Pure logic,
    no I/O — this is what's unit-tested.

    Trusts creation_time only if every file has it AND consecutive
    (sorted) files are separated by more than AMBIGUITY_THRESHOLD_S.
    Otherwise returns ambiguous=True with a best-effort fallback order
    (alphabetical by path) that the caller must have the user confirm
    before proceeding — never silently trusted.
    """
    if not infos:
        return OrderResult(ordered_paths=[], ambiguous=False)

    have_all_times = all(i.creation_time is not None for i in infos)
    reason = None
    ordered = infos

    if have_all_times:
        ordered = sorted(infos, key=lambda i: i.creation_time)
        # A pair is ambiguous if either: their start times are too close
        # together to trust (precision noise, copied metadata), OR the
        # next file's creation_time falls before the previous file's
        # footage would have finished recording — physically impossible
        # for one camera shooting sequentially, so it signals bad/stale
        # metadata rather than a real gap.
        too_close = too_overlapping = 0
        for a, b in zip(ordered, ordered[1:]):
            delta = (b.creation_time - a.creation_time).total_seconds()
            if delta < AMBIGUITY_THRESHOLD_S:
                too_close += 1
            elif delta - a.duration_s < 0:
                too_overlapping += 1
        if too_close or too_overlapping:
            parts = []
            if too_close:
                parts.append(f"{too_close} pair(s) with creation_time "
                            f"within {AMBIGUITY_THRESHOLD_S}s of each other")
            if too_overlapping:
                parts.append(f"{too_overlapping} pair(s) where the next "
                            f"file starts before the previous file's "
                            f"duration would have finished — implausible "
                            f"for sequential single-camera footage")
            reason = "; ".join(parts) + "; confirm order manually"
    else:
        missing = [i.path for i in infos if i.creation_time is None]
        reason = f"missing creation_time metadata on: {', '.join(missing)}"
        ordered = sorted(infos, key=lambda i: i.path)  # best-effort fallback

    gaps: list[float | None] = [None]
    for prev, cur in zip(ordered, ordered[1:]):
        if prev.creation_time is not None and cur.creation_time is not None:
            gap = (cur.creation_time - prev.creation_time).total_seconds() \
                - prev.duration_s
            gaps.append(gap)
        else:
            gaps.append(None)

    widths = {i.width for i in infos}
    heights = {i.height for i in infos}
    fpses = {round(i.fps, 2) for i in infos}

    return OrderResult(
        ordered_paths=[i.path for i in ordered],
        ambiguous=reason is not None,
        reason=reason,
        gaps_s=gaps,
        mismatched_resolution=len(widths) > 1 or len(heights) > 1,
        mismatched_fps=len(fpses) > 1,
    )


def resolve_order(paths, explicit_order: str | None,
                  result: OrderResult | None = None) -> list[str]:
    """Turn a set of input paths, an optional --order string, and an
    OrderResult into the final ordered path list — the actual "confirm or
    reorder" decision point, pulled out of the CLI so it's independently
    testable rather than only exercised by hand.

    - If `explicit_order` is given, it's used verbatim after checking it
      names exactly the given paths (ValueError otherwise) — this is the
      user's confirmation/reorder path, always available, regardless of
      whether automatic ordering succeeded or not.
    - Otherwise, automatic ordering is used if unambiguous.
    - Otherwise, raises AmbiguousOrderError (never silently guesses, but
      also never a dead end — the exception carries the suggested order
      the caller can hand back for confirmation).

    `result`, if given, reuses an already-computed OrderResult (paths must
    match); otherwise one is computed from `paths` via order_files.
    """
    if explicit_order:
        ordered = [p.strip() for p in explicit_order.split(",")]
        mismatch = set(ordered) ^ set(paths)
        if mismatch:
            raise ValueError(f"--order must list exactly the given files; "
                            f"mismatch: {sorted(mismatch)}")
        return ordered

    r = result if result is not None else order_files(paths)
    if r.ambiguous:
        raise AmbiguousOrderError(r.reason, r.ordered_paths)
    return r.ordered_paths
