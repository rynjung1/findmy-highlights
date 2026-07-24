"""Stitch a manifest's kept segments into one finished output video.

Usage:
    python scripts/stitch.py manifest.json --input-dir reference_clips \\
        --output out/highlights.mp4

`manifest.json` is whatever scripts/detect.py or scripts/detect_multi.py
wrote with --manifest. `--input-dir` is the directory containing the
original source video file(s) named in the manifest's `source_files`
(the manifest stores filenames only, not full paths, so this script
needs to be told where to find them).

Concat-demuxer stream copy is used when every source file that
contributes a kept span shares the same codec/resolution/fps/orientation;
otherwise every span is re-encoded to a common target (largest resolution
and fps among the inputs, so nothing is downscaled) and this is reported,
not done silently. See pipeline/stitch.py for the full explanation.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.manifest import load_manifest
from pipeline.stitch import run_stitch


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", help="manifest JSON written by detect.py/detect_multi.py")
    ap.add_argument("--input-dir", required=True,
                    help="directory containing the manifest's source video file(s)")
    ap.add_argument("--output", required=True, metavar="PATH",
                    help="path to write the final stitched video")
    args = ap.parse_args()

    manifest = load_manifest(args.manifest)

    result = run_stitch(manifest, args.input_dir, args.output)

    print(f"stitched {result.span_count} span(s) -> {result.output_path}",
         file=sys.stderr)
    print(f"requested output duration: {result.output_duration_s:.1f}s",
         file=sys.stderr)
    if result.reencoded:
        print(f"note: inputs required a re-encode ({result.reencode_reason})",
             file=sys.stderr)
    else:
        print("stream-copied (no re-encode needed)", file=sys.stderr)


if __name__ == "__main__":
    main()
