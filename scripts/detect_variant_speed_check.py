"""Quick real speed check of smaller RF-DETR variants (Nano, Small,
Medium) against the current RFDETRBase, on the SAME real sampled frames
from a real reference clip -- answers "is there even a meaningful speed
difference" BEFORE paying for the expensive full 9-clip regression
suite (scripts/regression.py) to check accuracy. If a variant isn't
meaningfully faster, there's no point risking accuracy to get it.

Usage:
    venv/bin/python scripts/detect_variant_speed_check.py [clip_name]
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


def main():
    clip_name = sys.argv[1] if len(sys.argv) > 1 else "clip_300.mkv"
    clip_path = CLIPS_DIR / clip_name
    cfg = DetectionConfig()
    device = _resolve_device(cfg.device)

    import rfdetr

    frames = sample_frames(clip_path, cfg)
    print(f"{clip_name}: {len(frames)} sampled frames, resolution={cfg.resolution}, device={device}\n")

    variants = [
        ("RFDETRBase (current)", rfdetr.RFDETRBase),
        ("RFDETRMedium", rfdetr.RFDETRMedium),
        ("RFDETRSmall", rfdetr.RFDETRSmall),
        ("RFDETRNano", rfdetr.RFDETRNano),
    ]

    results = {}
    for name, cls in variants:
        print(f"--- {name} ---")
        t0 = time.time()
        try:
            model = cls(device=device, resolution=cfg.resolution)
        except Exception as e:
            print(f"  FAILED to load at resolution={cfg.resolution}: {e}\n")
            continue
        load_s = time.time() - t0
        print(f"  load: {load_s:.2f}s")

        t0 = time.time()
        boxes_per_sample = []
        total_persons = 0
        min_box_h = float("inf")
        for t, img in frames:
            det = model.predict(img, threshold=cfg.threshold)
            b = extract_person_boxes(det, cfg.person_class_id)
            boxes_per_sample.append(b)
            total_persons += len(b)
            for x1, y1, x2, y2 in b:
                min_box_h = min(min_box_h, y2 - y1)
        elapsed = time.time() - t0
        results[name] = (elapsed, boxes_per_sample, total_persons, min_box_h)
        print(f"  inference: {elapsed:.2f}s total, {1000*elapsed/len(frames):.1f}ms/frame")
        print(f"  total person detections across all frames: {total_persons}")
        print(f"  smallest detected person box height (px): "
             f"{min_box_h:.1f}" if min_box_h != float('inf') else "  no persons detected at all\n")
        print()

    base_time = results.get("RFDETRBase (current)", (None,))[0]
    base_total = results.get("RFDETRBase (current)", (None, None, None))[2]
    print("=== summary (relative to RFDETRBase) ===")
    for name, (elapsed, _, total_persons, min_h) in results.items():
        speedup = base_time / elapsed if base_time else float("nan")
        person_delta = total_persons - base_total if base_total is not None else None
        print(f"{name:<25} {elapsed:>8.2f}s  {speedup:>5.2f}x speedup  "
             f"total_persons={total_persons} (delta {person_delta:+d})  "
             f"min_box_h={min_h:.1f}px" if person_delta is not None else
             f"{name:<25} {elapsed:>8.2f}s")


if __name__ == "__main__":
    main()
