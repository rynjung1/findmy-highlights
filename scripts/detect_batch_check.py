"""Real check: does batching frames into a single model.predict() call
(instead of the current one-at-a-time loop in pipeline.detection) give a
real wall-clock speedup, AND does it produce identical detections to the
existing sequential path? Both questions answered on the same real
reference clip, not assumed from either direction -- a batched forward
pass through RF-DETR's stacked-tensor path (confirmed real in
rfdetr/detr.py: torch.stack + a single model call, not a hidden
per-image loop) SHOULD be numerically equivalent per-image, but "should"
isn't "confirmed measured" per this project's own standing rule.

Usage:
    venv/bin/python scripts/detect_batch_check.py [clip_name] [batch_size]
"""

import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.detection import DetectionConfig, _resolve_device

CLIPS_DIR = ROOT / "reference_clips"


def sample_frames(clip_path, cfg):
    """Same sampling logic as detect_persons -- returns [(t, PIL.Image), ...]."""
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
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        out.append((t, img))
    cap.release()
    return out


def extract_person_boxes(det, person_class_id):
    return [det.xyxy[i].tolist() for i in range(len(det.class_id))
            if det.class_id[i] == person_class_id]


def main():
    clip_name = sys.argv[1] if len(sys.argv) > 1 else "clip_300.mkv"
    batch_size = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    clip_path = CLIPS_DIR / clip_name
    cfg = DetectionConfig()

    from rfdetr import RFDETRBase
    model = RFDETRBase(device=_resolve_device(cfg.device), resolution=cfg.resolution)

    frames = sample_frames(clip_path, cfg)
    print(f"{clip_name}: {len(frames)} sampled frames, batch_size={batch_size}\n")

    # --- sequential baseline (the exact current pipeline.detection path) ---
    t0 = time.time()
    seq_boxes = []
    for t, img in frames:
        det = model.predict(img, threshold=cfg.threshold)
        seq_boxes.append(extract_person_boxes(det, cfg.person_class_id))
    seq_time = time.time() - t0
    print(f"sequential (current): {seq_time:.2f}s total, "
         f"{1000 * seq_time / len(frames):.1f}ms/frame")

    # --- batched ---
    t0 = time.time()
    batch_boxes = []
    imgs = [img for _, img in frames]
    for i in range(0, len(imgs), batch_size):
        chunk = imgs[i:i + batch_size]
        dets = model.predict(chunk, threshold=cfg.threshold)
        if not isinstance(dets, list):
            dets = [dets]
        for det in dets:
            batch_boxes.append(extract_person_boxes(det, cfg.person_class_id))
    batch_time = time.time() - t0
    print(f"batched (size={batch_size}): {batch_time:.2f}s total, "
         f"{1000 * batch_time / len(frames):.1f}ms/frame")
    print(f"speedup: {seq_time / batch_time:.2f}x\n")

    # --- correctness: identical detections per frame? ---
    assert len(seq_boxes) == len(batch_boxes), \
        f"sample count mismatch: {len(seq_boxes)} vs {len(batch_boxes)}"

    n_mismatch = 0
    max_coord_diff = 0.0
    for i, (sb, bb) in enumerate(zip(seq_boxes, batch_boxes)):
        if len(sb) != len(bb):
            n_mismatch += 1
            print(f"  MISMATCH at sample {i} (t={frames[i][0]:.1f}s): "
                 f"seq={len(sb)} boxes, batch={len(bb)} boxes")
            continue
        # order should match (both iterate the model's own detection order);
        # compare coordinates with a real float tolerance, not exact equality,
        # since batched vs unbatched matmul can differ in the last bits.
        for box_s, box_b in zip(sorted(sb), sorted(bb)):
            diff = max(abs(a - b) for a, b in zip(box_s, box_b))
            max_coord_diff = max(max_coord_diff, diff)

    print(f"box-count mismatches: {n_mismatch}/{len(frames)} samples")
    print(f"max per-coordinate difference (px) across all matched boxes: {max_coord_diff:.4f}")
    print(f"\nVERDICT: {'IDENTICAL' if n_mismatch == 0 and max_coord_diff < 0.5 else 'DIFFERENT'} "
         f"detections between sequential and batched (real reference clip: {clip_name})")


if __name__ == "__main__":
    main()
