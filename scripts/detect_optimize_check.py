"""Real check of rfdetr's own model.optimize_for_inference() -- surfaced by
a warning in scripts/detect_profile.py's real output ("Model is not
optimized for inference... For full GPU throughput (e.g. ~8x on T4 via
FP16 Tensor Cores), call model.optimize_for_inference(...)"). That ~8x
number is explicitly a CUDA/T4 claim in the library's own docstring --
this project runs on Apple Silicon (MPS), so it's tested here for real
rather than assumed to transfer.

Tests three variants against the SAME real sampled frames from a real
reference clip, loaded once for a fair comparison:
  1. optimize_for_inference(compile=False) -- export() only, still fp32,
     no numeric change expected (zero accuracy risk).
  2. optimize_for_inference(compile=True, batch_size=1) -- JIT-traced,
     still fp32 (zero accuracy risk, only trace/fusion speedup).
  3. optimize_for_inference(compile=True, batch_size=1, dtype=fp16) --
     traced AND lower precision -- real numeric risk, checked directly
     against the baseline's boxes with an explicit, looser tolerance
     appropriate to fp16 (not assumed identical).

Usage:
    venv/bin/python scripts/detect_optimize_check.py [clip_name]
"""

import sys
import time
from pathlib import Path

import cv2
import torch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.detection import DetectionConfig, _resolve_device

CLIPS_DIR = ROOT / "reference_clips"


def sample_frames(clip_path, cfg):
    from PIL import Image
    cap = cv2.VideoCapture(str(clip_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    step = max(1, round(fps / cfg.sample_fps))
    out = []
    idx = 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if idx % step != 0:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        if not ok:
            break
        t = idx / fps
        idx += 1
        out.append((t, Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))))
    cap.release()
    return out


def extract_person_boxes(det, person_class_id):
    return [det.xyxy[i].tolist() for i in range(len(det.class_id))
            if det.class_id[i] == person_class_id]


def run_and_time(model, frames, cfg, label):
    t0 = time.time()
    boxes = []
    for t, img in frames:
        det = model.predict(img, threshold=cfg.threshold)
        boxes.append(extract_person_boxes(det, cfg.person_class_id))
    elapsed = time.time() - t0
    print(f"{label:<45} {elapsed:>8.2f}s total  {1000*elapsed/len(frames):>8.1f}ms/frame")
    return boxes, elapsed


def compare(baseline_boxes, boxes, frames, tol, label):
    n_mismatch = 0
    max_diff = 0.0
    for i, (bb, ob) in enumerate(zip(baseline_boxes, boxes)):
        if len(bb) != len(ob):
            n_mismatch += 1
            continue
        for box_b, box_o in zip(sorted(bb), sorted(ob)):
            diff = max(abs(a - b) for a, b in zip(box_b, box_o))
            max_diff = max(max_diff, diff)
    verdict = "MATCH" if n_mismatch == 0 and max_diff <= tol else "DIFFERS"
    print(f"  vs baseline: {n_mismatch}/{len(frames)} box-count mismatches, "
         f"max coord diff {max_diff:.3f}px (tolerance {tol}px) -> {verdict}")
    return n_mismatch, max_diff


def main():
    clip_name = sys.argv[1] if len(sys.argv) > 1 else "clip_300.mkv"
    clip_path = CLIPS_DIR / clip_name
    cfg = DetectionConfig()
    device = _resolve_device(cfg.device)

    from rfdetr import RFDETRBase

    print(f"Loading and sampling {clip_name} once for a fair comparison...")
    model = RFDETRBase(device=device, resolution=cfg.resolution)
    frames = sample_frames(clip_path, cfg)
    print(f"{len(frames)} sampled frames, device={device}\n")

    baseline_boxes, baseline_time = run_and_time(model, frames, cfg, "baseline (unoptimized, current pipeline)")

    print()
    t0 = time.time()
    model.optimize_for_inference(compile=False)
    print(f"optimize_for_inference(compile=False) setup: {time.time()-t0:.2f}s")
    boxes_a, time_a = run_and_time(model, frames, cfg, "compile=False (export only, fp32)")
    compare(baseline_boxes, boxes_a, frames, tol=0.01, label="compile=False")
    print(f"  speedup vs baseline: {baseline_time/time_a:.2f}x\n")
    model.remove_optimized_model()

    t0 = time.time()
    model.optimize_for_inference(compile=True, batch_size=1)
    print(f"optimize_for_inference(compile=True, batch_size=1) setup: {time.time()-t0:.2f}s")
    boxes_b, time_b = run_and_time(model, frames, cfg, "compile=True, batch_size=1 (traced, fp32)")
    compare(baseline_boxes, boxes_b, frames, tol=0.01, label="compile=True fp32")
    print(f"  speedup vs baseline: {baseline_time/time_b:.2f}x\n")
    model.remove_optimized_model()

    t0 = time.time()
    model.optimize_for_inference(compile=True, batch_size=1, dtype=torch.float16)
    print(f"optimize_for_inference(compile=True, batch_size=1, dtype=fp16) setup: {time.time()-t0:.2f}s")
    boxes_c, time_c = run_and_time(model, frames, cfg, "compile=True, batch_size=1, fp16")
    # fp16 has ~3 decimal digits of precision; a few px of drift on a
    # 1120px-resolution input is a real, expected fp16 rounding effect,
    # not evidence of a bug -- tolerance set explicitly, not silently
    # widened to hide a real problem.
    compare(baseline_boxes, boxes_c, frames, tol=3.0, label="compile=True fp16")
    print(f"  speedup vs baseline: {baseline_time/time_c:.2f}x\n")
    model.remove_optimized_model()

    print("=== summary ===")
    print(f"baseline:              {baseline_time:.2f}s")
    print(f"compile=False fp32:    {time_a:.2f}s  ({baseline_time/time_a:.2f}x)")
    print(f"compile=True fp32:     {time_b:.2f}s  ({baseline_time/time_b:.2f}x)")
    print(f"compile=True fp16:     {time_c:.2f}s  ({baseline_time/time_c:.2f}x)")


if __name__ == "__main__":
    main()
