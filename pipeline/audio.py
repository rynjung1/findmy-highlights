"""Audio onset/attack-rate feature extraction: how fast a real sound's
amplitude envelope rises to its local peak, near a candidate instant.

Built fresh for this project (no external model, no license concerns
beyond ffmpeg's own decode, already relied on everywhere else in this
codebase). A genuinely different feature than the two already closed in
the committed Tier 2 audio investigation (see README): amplitude alone
(a miss's glove-pop and a hit's bat-crack are comparably LOUD) and
spectral ratio (the noise floor drifts across a real game enough that no
fixed frequency-band cutoff holds). Attack sharpness is a different
axis: a bat-ball contact is a genuinely fast transient regardless of its
absolute loudness or spectral content, where footsteps, talking, wind,
or a ball caught in a glove tend to rise more gradually.

Tier 1 investigative instrumentation for the review queue (see
pipeline/review.py), not a cutting signal on its own -- see the README's
Task 2 pose+audio writeup for the real, measured separation before any
decision about wiring this into anything that affects what gets
kept/cut.
"""

import subprocess
from dataclasses import dataclass

import numpy as np


@dataclass
class AudioConfig:
    sample_rate: int = 16000
    hop_ms: float = 5.0
    # Half-window searched around the candidate instant for the local
    # peak -- matches pipeline.pose's own window_s default, since both
    # features look at the same short clip a reviewer would watch.
    window_s: float = 1.0
    # Rise time is measured from the peak back to where the envelope
    # first drops below this fraction of the peak -- a sharp transient
    # (real contact) has a SHORT rise time; a gradual swell (talking,
    # wind, footsteps) has a longer one.
    rise_threshold_frac: float = 0.3


def load_audio(video_path, sample_rate: int = 16000) -> np.ndarray:
    """Mono float PCM in [-1, 1], decoded directly from the video's own
    audio track via ffmpeg (the same real-decode approach this project
    already uses everywhere else, e.g. pipeline.stitch's ffmpeg
    commands) -- no separate audio-extraction dependency needed."""
    cmd = ["ffmpeg", "-v", "error", "-i", str(video_path), "-f", "s16le",
          "-ac", "1", "-ar", str(sample_rate), "-"]
    out = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float64) / 32768.0


def envelope(audio: np.ndarray, sample_rate: int = 16000, hop_ms: float = 5.0):
    """RMS amplitude envelope, one value per hop_ms window -- a coarse
    enough time resolution (5ms default) to smooth over individual
    sample noise while still resolving a real attack transient (typical
    bat-ball contact rises over single-digit to low-tens of ms)."""
    hop = max(1, int(sample_rate * hop_ms / 1000))
    n = len(audio) // hop
    if n == 0:
        return np.array([]), np.array([])
    env = np.array([np.sqrt(np.mean(audio[i * hop:(i + 1) * hop] ** 2))
                    for i in range(n)])
    times = np.arange(n) * hop_ms / 1000.0
    return times, env


def onset_rise_time(env_times, env, center_s: float,
                    config: AudioConfig | None = None):
    """The local peak nearest center_s within config.window_s, and how
    fast the envelope rose to it (rise_time_s). Returns None if fewer
    than 5 envelope samples fall in the search window (too little data
    to trust a peak/rise-time reading)."""
    cfg = config or AudioConfig()
    env_times = np.asarray(env_times, dtype=float)
    env = np.asarray(env, dtype=float)
    idx = (env_times >= center_s - cfg.window_s) & (env_times <= center_s + cfg.window_s)
    tt, ee = env_times[idx], env[idx]
    if len(tt) < 5:
        return None
    peak_i = int(np.argmax(ee))
    peak = float(ee[peak_i])
    peak_t = float(tt[peak_i])
    thresh = cfg.rise_threshold_frac * peak
    j = peak_i
    while j > 0 and ee[j] > thresh:
        j -= 1
    rise_time_s = peak_t - float(tt[j])
    return {"peak_amplitude": peak, "peak_t": peak_t, "rise_time_s": rise_time_s}


def onset_features(video_path, center_s: float, config: AudioConfig | None = None,
                   envelope_cache: dict | None = None):
    """Convenience entry point: decode+envelope the whole file once (via
    envelope_cache, keyed by str(video_path)) and reuse it across every
    candidate from the same source -- re-decoding a video's full audio
    track per candidate would be wasteful when a real batch produces
    several candidates from the same file, same reasoning as
    pipeline.pose's landmarker reuse. Returns None exactly when
    onset_rise_time would (too little data near center_s)."""
    cfg = config or AudioConfig()
    cache = envelope_cache if envelope_cache is not None else {}
    key = str(video_path)
    if key not in cache:
        audio = load_audio(video_path, cfg.sample_rate)
        cache[key] = envelope(audio, cfg.sample_rate, cfg.hop_ms)
    env_times, env = cache[key]
    return onset_rise_time(env_times, env, center_s, cfg)
