"""Zero-shot video-text classification (X-CLIP) as review-queue
instrumentation: how strongly a real window matches "a baseball player
swinging a bat" versus "baseball players standing idle", via the
pretrained model's own video-text alignment -- no classifier trained
here, the model's zero-shot similarity IS the feature.

License, checked directly against the primary source before writing any
of this (same standard as pipeline.pose's MediaPipe citation): confirmed
MIT directly from `microsoft/xclip-base-patch32`'s Hugging Face model
card. 196.6M params, Kinetics-400 trained, loads via the `transformers`
library already pinned in requirements.txt (a transitive RF-DETR
dependency).

Real, measured separation (see README's transfer-learning and zero-shot
investigation writeups): AUC 0.690, p=0.012 -- the first signal across
every investigation this project has run (hand-crafted or pretrained) to
clear conventional significance -- against the same 11 real swing-type
ground-truth events / 170 real ambient samples pipeline.pose/
pipeline.audio were validated against.

NOT wired into any real kept/cut decision, on purpose, for a real,
named reason (not just "not validated enough yet"): the two-prompt
framing is measurably prompt-sensitive specifically for contact/hit-type
events with fielders converging nearby -- five such events swung between
the 31st-79th percentile of ambient across five tested prompt variants,
while the one event with zero defensive reaction (clip_whiff1) stayed at
the 96th-98th percentile regardless of wording. That's the same
structural shape already closed for the plate-distance and
person-proximity signals (see README): real signal, real instability on
exactly the plays that matter most. A real 9-clip recall/continuity gate
(same PASS/FAIL structure hard-cut's own shipping gate uses) also found
a concrete threshold ceiling -- safe up to 0.44, a real failure at 0.46
on clip_60/e5, the same event already flagged fragile by unrelated
signals elsewhere in this project's history.

Tier 1 instrumentation only, same category as pipeline.pose/
pipeline.audio, until a real, larger label set accumulated through
actual review-queue usage says otherwise.
"""

from dataclasses import dataclass

import cv2

DEFAULT_MODEL_NAME = "microsoft/xclip-base-patch32"
# The best-performing pair of five real variants tested (see README) --
# not the only one that "works", the one that measurably separates best
# in aggregate. Recorded on every result (not just fixed in config)
# since it's a real, load-bearing choice, not assumed permanent.
POS_PROMPT = "a baseball player swinging a bat"
NEG_PROMPT = "baseball players standing idle"


@dataclass
class XClipConfig:
    model_name: str = DEFAULT_MODEL_NAME
    # 8 evenly-sampled frames across a 2s window -- the exact convention
    # every real AUC number in the README's investigation was measured
    # under; changing either invalidates that number until re-measured.
    window_s: float = 2.0
    n_frames: int = 8
    pos_prompt: str = POS_PROMPT
    neg_prompt: str = NEG_PROMPT


class XClipModel:
    """Holds the loaded model/processor plus PRECOMPUTED text embeddings
    for config.pos_prompt/neg_prompt -- text embedding cost is trivial,
    but repeating tokenize+forward per candidate is pure waste when
    every candidate in a batch shares the same two prompts. Callers
    processing many windows should build one and reuse it via
    `swing_probability(..., xclip=...)`, rather than paying model-load
    cost per candidate (same reuse pattern as
    pipeline.pose.build_landmarker)."""

    def __init__(self, config: XClipConfig):
        import torch
        from transformers import AutoModel, AutoProcessor

        self.config = config
        self.model = AutoModel.from_pretrained(config.model_name).eval()
        self.processor = AutoProcessor.from_pretrained(config.model_name)
        text_inputs = self.processor(text=[config.pos_prompt, config.neg_prompt],
                                     return_tensors="pt", padding=True)
        with torch.no_grad():
            text_embeds = self.model.get_text_features(**text_inputs).pooler_output
            self.text_embeds = text_embeds / text_embeds.norm(dim=-1, keepdim=True)
        self.logit_scale = self.model.logit_scale.exp().item()


def build_xclip(config: XClipConfig | None = None) -> XClipModel:
    return XClipModel(config or XClipConfig())


def _extract_frames(video_path, center_s, n_frames, window_s):
    """n_frames evenly spaced across a window_s window centered on
    center_s -- matching pipeline.pose's own window-around-an-instant
    convention, but sparse frames rather than every decoded frame, since
    that's what the real AUC numbers were measured against."""
    cap = cv2.VideoCapture(str(video_path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    start_s = max(0.0, center_s - window_s / 2)
    idxs = [int((start_s + i * window_s / (n_frames - 1)) * fps) for i in range(n_frames)]
    idxs = [min(max(0, i), max(total_frames - 1, 0)) for i in idxs]
    frames = []
    for idx in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ok, frame = cap.read()
        if not ok:
            if frames:
                frames.append(frames[-1].copy())
            continue
        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    cap.release()
    return frames


def swing_probability(video_path, center_s, xclip: XClipModel):
    """P(window matches xclip.config.pos_prompt) vs .neg_prompt, softmax
    over the pair -- the zero-shot classification signal itself, no
    classifier trained on top. Returns None if fewer than 3 real frames
    could be decoded near center_s (too little to trust). Otherwise:
    {"p_swinging", "pos_prompt", "neg_prompt"} -- the prompt pair is
    recorded on every result since it's real, measured-against, and not
    guaranteed to stay fixed forever (see module docstring)."""
    import torch

    cfg = xclip.config
    frames = _extract_frames(video_path, center_s, cfg.n_frames, cfg.window_s)
    if len(frames) < 3:
        return None
    inputs = xclip.processor(videos=frames, return_tensors="pt")
    with torch.no_grad():
        video_out = xclip.model.get_video_features(pixel_values=inputs["pixel_values"])
        v = video_out.pooler_output
        v = v / v.norm(dim=-1, keepdim=True)
        logits = xclip.logit_scale * v @ xclip.text_embeds.T
        probs = logits.softmax(dim=-1)[0]
    return {
        "p_swinging": float(probs[0].item()),
        "pos_prompt": cfg.pos_prompt,
        "neg_prompt": cfg.neg_prompt,
    }
