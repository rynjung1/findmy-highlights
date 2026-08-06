"""INVESTIGATION ONLY -- not wired into any real decision.

Runs Qwen2-VL-2B-Instruct (see scripts/local_vlm_feasibility_check.py)
against the specific required real events that have broken every other
gating/model attempt this project has tried tonight: clip_base1,
clip_base4, clip_foul1 (the three the multi-feature logistic-regression
model misclassified as downtime -- see
scripts/multi_feature_review_model.py), clip_60/e5 and clip_540/e4 (the
events named fragile in the X-CLIP threshold investigation), and
clip_whiff1 (the clean swing-and-miss demo clip, a real sanity check).

4 frames extracted from a 3s window centered on each event's real
window midpoint -- same duration convention as the review-queue's own
self-contained clips (see pipeline/review.py), for a fair, consistent
comparison rather than a bespoke window per clip.

Usage:
    python scripts/vlm_recall_risk_check.py --prompt-mode original
    python scripts/vlm_recall_risk_check.py --prompt-mode reasoning_first
"""

import argparse
import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.local_vlm_feasibility_check import PROMPTS, classify

ROOT = Path(__file__).resolve().parent.parent
GROUND_TRUTH_DIR = ROOT / "tests" / "ground_truth"
CLIPS_DIR = ROOT / "reference_clips"

N_FRAMES = 4
WINDOW_S = 3.0  # matches the review-queue's own self-contained clip duration

# (clip_stem, event_id) -- explicit, not "every required event", since
# the user named these specific ones as the real recall-risk targets.
TARGETS = [
    ("clip_base1", "e1"),
    ("clip_base4", "e1"),
    ("clip_foul1", "e1"),
    ("clip_60", "e5"),
    ("clip_540", "e4"),
    ("clip_whiff1", "e1"),
]


def extract_frames_from_window(clip_path, center_s, n_frames=N_FRAMES, window_s=WINDOW_S):
    from PIL import Image
    cap = cv2.VideoCapture(str(clip_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_s = max(0.0, center_s - window_s / 2)
    frame_idxs = [int((start_s + i * window_s / (n_frames - 1)) * fps) for i in range(n_frames)]
    frame_idxs = [min(max(0, i), max(total_frames - 1, 0)) for i in frame_idxs]
    frames = []
    for idx in frame_idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            continue
        h, w = frame.shape[:2]
        scale = 640 / w
        frame = cv2.resize(frame, (640, int(h * scale)))
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        frames.append(Image.fromarray(frame))
    cap.release()
    return frames


def load_targets():
    rows = []
    for clip_stem, event_id in TARGETS:
        truth = json.loads((GROUND_TRUTH_DIR / f"{clip_stem}.json").read_text())
        clip_path = CLIPS_DIR / truth["clip"]
        event = next(e for e in truth["events"] if e["id"] == event_id)
        center_s = sum(event["window"]) / 2.0
        rows.append({
            "clip_stem": clip_stem, "clip_path": clip_path, "event_id": event_id,
            "type": event["type"], "window": event["window"], "center_s": center_s,
            "required": event.get("required", False),
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--prompt-mode", choices=list(PROMPTS.keys()), default="original")
    ap.add_argument("--max-new-tokens", type=int, default=150)
    args = ap.parse_args()

    import torch
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"using device: {device}, prompt-mode: {args.prompt_mode}")

    t0 = time.time()
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        "Qwen/Qwen2-VL-2B-Instruct", torch_dtype=torch.float16, low_cpu_mem_usage=True)
    model = model.to(device)
    processor = AutoProcessor.from_pretrained("Qwen/Qwen2-VL-2B-Instruct")
    print(f"model+processor load: {time.time() - t0:.1f}s\n")

    prompt = PROMPTS[args.prompt_mode]
    targets = load_targets()
    any_miss = False
    for t in targets:
        if not t["clip_path"].exists():
            print(f"[skip] {t['clip_stem']}: {t['clip_path']} not found")
            continue
        images = extract_frames_from_window(t["clip_path"], t["center_s"])
        messages = [{
            "role": "user",
            "content": [{"type": "image", "image": img} for img in images]
                       + [{"type": "text", "text": prompt}],
        }]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=images, return_tensors="pt").to(device)

        t0 = time.time()
        with torch.no_grad():
            out_ids = model.generate(**inputs, max_new_tokens=args.max_new_tokens, do_sample=False)
        dt = time.time() - t0

        trimmed = out_ids[:, inputs["input_ids"].shape[1]:]
        response = processor.batch_decode(trimmed, skip_special_tokens=True)[0].strip()
        predicted = classify(response)
        # every target here is a real REQUIRED event -- the correct
        # answer is always ACTIVE_SWING; a DOWNTIME prediction is a real
        # recall-risk miss, the exact thing this check exists to catch.
        flag = ""
        if predicted == "DOWNTIME":
            flag = "  *** WOULD MISCLASSIFY AS DOWNTIME (real recall risk) ***"
            any_miss = True
        print(f"{t['clip_stem']}/{t['event_id']} ({t['type']}) window={t['window']} "
             f"center_s={t['center_s']:.1f} -> {predicted}  ({dt:.1f}s){flag}")
        print(f"  response: {response!r}\n")

    print("REAL RECALL RISK FOUND" if any_miss else
         "No recall risk found: every named target scores as ACTIVE_SWING.")


if __name__ == "__main__":
    main()
