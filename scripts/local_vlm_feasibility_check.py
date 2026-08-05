"""INVESTIGATION ONLY -- not wired into any real decision.

Feasibility check for a free, local, open-weight vision-language model as
a scriptable, repeatable replacement for the manual Claude-agent vision
test in scripts/sustained_ambient_xclip_check.py's follow-up (see
README's "Free, local, open-weight VLM feasibility check" writeup).
That earlier test used a separate Claude agent instance to blind-classify
the same 16 real labeled boundary_crossing clips and scored 8/9 on the 9
real presence-without-action disagreement cases -- a real result, but not
a callable pipeline component, and the user didn't want ongoing paid-API
billing risk for something still investigation-only.

Qwen2-VL-2B-Instruct picked over moondream2/SmolVLM-Instruct (all three
confirmed Apache 2.0 directly from their HuggingFace model cards) because
it natively supports multi-image/video input -- closer to what the
blind-agent test actually did (reasoning jointly over 4 temporally-
ordered frames) than a single-image-oriented model.

Real gotcha found and fixed here: `device_map="mps"` (the documented way)
hangs indefinitely under real memory pressure (accelerate's dispatch
machinery, not the model itself) -- this script loads to CPU with
low_cpu_mem_usage=True first, then does a plain `.to(device)` transfer,
which is fast and reliable.

Usage:
    python scripts/local_vlm_feasibility_check.py
"""

import json
import sys
import time
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
REVIEWS_DIR = ROOT / "training_data" / "reviews"

MODEL_ID = "Qwen/Qwen2-VL-2B-Instruct"
N_FRAMES = 4

PROMPT = (
    "These 4 frames are shown in temporal order, spanning about 3 seconds "
    "of real baseball footage. Is a batter actively engaged in an at-bat/"
    "swing sequence (loading, swinging, making contact, or immediate "
    "post-contact follow-through/running) in this clip, or is this "
    "\"downtime\" (no batter actively swinging -- e.g. empty plate, "
    "someone walking with a bat, a batter just standing static in the box "
    "with no swing motion across all 4 frames, players walking between "
    "positions)? Answer with ACTIVE_SWING or DOWNTIME on the first line, "
    "then one sentence of visual justification."
)


def extract_frames(clip_path, n_frames=N_FRAMES):
    from PIL import Image
    cap = cv2.VideoCapture(str(clip_path))
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fracs = [0.05, 0.35, 0.65, 0.95][:n_frames]
    idxs = [min(max(0, int(n * f)), n - 1) for f in fracs]
    frames = []
    for idx in idxs:
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


def load_enter_labeled_records():
    records = []
    for f in sorted(REVIEWS_DIR.glob("bc_*.json")):
        d = json.loads(f.read_text())
        if d.get("label") is not None and d.get("pipeline_decision") == "enter":
            records.append(d)
    return records


def main():
    import torch
    from transformers import AutoProcessor, Qwen2VLForConditionalGeneration

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    print(f"using device: {device}")

    t0 = time.time()
    model = Qwen2VLForConditionalGeneration.from_pretrained(
        MODEL_ID, torch_dtype=torch.float16, low_cpu_mem_usage=True)
    model = model.to(device)
    processor = AutoProcessor.from_pretrained(MODEL_ID)
    print(f"model+processor load: {time.time() - t0:.1f}s")

    records = load_enter_labeled_records()
    print(f"{len(records)} real labeled enter-type boundary_crossing records\n")

    correct = 0
    infer_times = []
    for d in records:
        clip_path = REVIEWS_DIR / f"{d['id']}.mp4"
        if not clip_path.exists():
            print(f"[skip] {d['id']}: clip missing")
            continue
        images = extract_frames(clip_path)
        messages = [{
            "role": "user",
            "content": [{"type": "image", "image": img} for img in images]
                       + [{"type": "text", "text": PROMPT}],
        }]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = processor(text=[text], images=images, return_tensors="pt").to(device)

        t0 = time.time()
        with torch.no_grad():
            # the shipped generation_config.json defaults to do_sample=True
            # -- forced False here for reproducible, deterministic
            # classification output (verified this actually matters: an
            # earlier do_sample=True run and this run disagreed on one
            # record before this fix).
            out_ids = model.generate(**inputs, max_new_tokens=80, do_sample=False)
        t_infer = time.time() - t0
        infer_times.append(t_infer)

        trimmed = out_ids[:, inputs["input_ids"].shape[1]:]
        response = processor.batch_decode(trimmed, skip_special_tokens=True)[0].strip()
        predicted = "ACTIVE_SWING" if "ACTIVE_SWING" in response.upper() else "DOWNTIME"
        should_be = "ACTIVE_SWING" if d["label"] == "real_action" else "DOWNTIME"
        ok = predicted == should_be
        correct += ok
        print(f"{d['id']}: label={d['label']:<12} predicted={predicted:<12} "
             f"{'CORRECT' if ok else 'WRONG'}  ({t_infer:.1f}s)  {response}")

    print(f"\n{correct}/{len(records)} correct")
    if infer_times:
        print(f"mean inference time: {sum(infer_times)/len(infer_times):.1f}s "
             f"(min {min(infer_times):.1f}s, max {max(infer_times):.1f}s)")


if __name__ == "__main__":
    main()
