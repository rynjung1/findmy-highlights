"""Real per-component timing breakdown of pipeline.detection.detect_persons
on a real reference clip -- answers "where does the ~45min cold detect-
stage time actually go" instead of assuming it's all model inference.
Instruments the exact same code path detect_persons runs (frame grab/
retrieve via cv2, BGR->RGB + PIL conversion, model.predict(), box
post-processing), just with real timers around each step, no cache (a
cache hit would trivially report near-zero everywhere and defeat the
whole point).

Usage:
    venv/bin/python scripts/detect_profile.py [clip_name]
"""

import sys
import time
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.detection import DetectionConfig, _resolve_device

CLIPS_DIR = ROOT / "reference_clips"


def main():
    clip_name = sys.argv[1] if len(sys.argv) > 1 else "clip_300.mkv"
    clip_path = CLIPS_DIR / clip_name
    cfg = DetectionConfig()

    from PIL import Image
    from rfdetr import RFDETRBase

    t0 = time.time()
    model = RFDETRBase(device=_resolve_device(cfg.device), resolution=cfg.resolution)
    model_load_s = time.time() - t0
    print(f"model load: {model_load_s:.2f}s (device={_resolve_device(cfg.device)}, "
         f"resolution={cfg.resolution})")

    cap = cv2.VideoCapture(str(clip_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, round(fps / cfg.sample_fps))
    print(f"clip: {clip_name}  fps={fps:.2f}  n_frames={n_frames}  "
         f"step={step} (sampling every {step}th frame -> ~{n_frames // step} samples)")

    t_grab = t_convert = t_infer = t_post = 0.0
    n_samples = 0
    idx = 0
    while True:
        t0 = time.time()
        ok = cap.grab()
        if not ok:
            break
        if idx % step != 0:
            idx += 1
            continue
        ok, frame = cap.retrieve()
        t_grab += time.time() - t0
        if not ok:
            break
        idx += 1

        t0 = time.time()
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        t_convert += time.time() - t0

        t0 = time.time()
        det = model.predict(img, threshold=cfg.threshold)
        t_infer += time.time() - t0

        t0 = time.time()
        boxes = [det.xyxy[i].tolist() for i in range(len(det.class_id))
                 if det.class_id[i] == cfg.person_class_id]
        t_post += time.time() - t0

        n_samples += 1
    cap.release()

    total = t_grab + t_convert + t_infer + t_post
    print(f"\n{n_samples} samples processed\n")
    print(f"{'component':<30}{'seconds':>10}{'% of total':>12}{'ms/sample':>14}")
    for name, t in [("frame grab+retrieve (cv2)", t_grab),
                     ("BGR->RGB + PIL convert", t_convert),
                     ("model.predict() (inference)", t_infer),
                     ("box post-processing", t_post)]:
        pct = 100 * t / total if total else 0
        per = 1000 * t / n_samples if n_samples else 0
        print(f"{name:<30}{t:>10.2f}{pct:>11.1f}%{per:>13.1f}ms")
    print(f"{'TOTAL (excl. model load)':<30}{total:>10.2f}{'100.0':>11}%{1000*total/n_samples if n_samples else 0:>13.1f}ms")
    print(f"\nreal wall clock for this clip's detect loop: {total:.1f}s "
         f"(+ {model_load_s:.1f}s model load)")


if __name__ == "__main__":
    main()
