"""Unit tests for pipeline.audio: envelope computation and onset
rise-time, plus (gated on real reference footage) a real smoke test."""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import reference_clips_dir
from pipeline.audio import AudioConfig, envelope, onset_features, onset_rise_time


# ---- envelope ----

def test_envelope_shape_matches_hop():
    sr = 16000
    audio = np.zeros(sr)  # 1 second of silence
    times, env = envelope(audio, sample_rate=sr, hop_ms=5.0)
    assert len(times) == len(env)
    assert len(env) == pytest.approx(200, abs=1)  # 1000ms / 5ms


def test_envelope_empty_audio():
    times, env = envelope(np.array([]), sample_rate=16000, hop_ms=5.0)
    assert len(times) == 0 and len(env) == 0


def test_envelope_reflects_real_amplitude():
    sr = 16000
    quiet = np.full(sr, 0.01)
    loud = np.full(sr, 0.5)
    _, env_quiet = envelope(quiet, sr)
    _, env_loud = envelope(loud, sr)
    assert env_loud.mean() > env_quiet.mean()


# ---- onset_rise_time ----

def _synthetic_envelope(sr_hop_ms=5.0, duration_s=4.0):
    n = int(duration_s * 1000 / sr_hop_ms)
    return np.arange(n) * sr_hop_ms / 1000.0


def test_onset_rise_time_sharp_transient_has_short_rise_time():
    times = _synthetic_envelope()
    env = np.full(len(times), 0.05)
    # a sharp spike right at t=2.0: jumps from baseline to peak in one hop
    peak_idx = int(np.argmin(np.abs(times - 2.0)))
    env[peak_idx] = 1.0
    result = onset_rise_time(times, env, center_s=2.0, config=AudioConfig())
    assert result is not None
    assert result["rise_time_s"] < 0.02  # one hop (5ms), effectively instant


def test_onset_rise_time_gradual_swell_has_longer_rise_time():
    times = _synthetic_envelope()
    env = np.full(len(times), 0.05)
    # a gradual ramp from t=1.5 to t=2.0 up to a peak, then back down --
    # models a slow swell (talking, wind) rather than a sharp contact
    ramp_idx = (times >= 1.5) & (times <= 2.0)
    ramp_t = times[ramp_idx]
    env[ramp_idx] = 0.05 + (ramp_t - 1.5) / 0.5 * 0.95
    result = onset_rise_time(times, env, center_s=2.0, config=AudioConfig())
    assert result is not None
    assert result["rise_time_s"] > 0.3  # a real, much longer rise


def test_onset_rise_time_sharp_vs_gradual_are_directionally_separated():
    times = _synthetic_envelope()

    sharp = np.full(len(times), 0.05)
    sharp[int(np.argmin(np.abs(times - 2.0)))] = 1.0
    sharp_result = onset_rise_time(times, sharp, center_s=2.0)

    gradual = np.full(len(times), 0.05)
    ramp_idx = (times >= 1.5) & (times <= 2.0)
    ramp_t = times[ramp_idx]
    gradual[ramp_idx] = 0.05 + (ramp_t - 1.5) / 0.5 * 0.95
    gradual_result = onset_rise_time(times, gradual, center_s=2.0)

    assert sharp_result["rise_time_s"] < gradual_result["rise_time_s"]


def test_onset_rise_time_none_when_too_little_data():
    times = np.array([1.0, 1.1])
    env = np.array([0.1, 0.2])
    assert onset_rise_time(times, env, center_s=1.0) is None


# ---- onset_features caching ----

def test_onset_features_uses_and_populates_cache():
    times = _synthetic_envelope()
    env = np.full(len(times), 0.05)
    env[int(np.argmin(np.abs(times - 2.0)))] = 1.0
    cache = {"fake_video.mp4": (times, env)}
    # a bogus path that would fail if load_audio were actually invoked --
    # proves onset_features used the pre-seeded cache instead of decoding
    result = onset_features("fake_video.mp4", center_s=2.0, envelope_cache=cache)
    assert result is not None
    assert result["rise_time_s"] < 0.02
    assert "fake_video.mp4" in cache  # unchanged, not re-decoded


# ---- real ffmpeg smoke test ----
# Deliberately NOT a swing-vs-ambient assertion -- see test_pose.py's
# matching smoke test for why a single real pair proves nothing at
# scale; the real separation claim belongs to the scaled validation
# study (README's Task 2 pose+audio writeup), not a unit test.

def test_onset_features_real_smoke():
    ref = reference_clips_dir() / "clip_base3.mkv"
    if not ref.exists():
        pytest.skip("reference_clips/clip_base3.mkv not available")

    cache = {}
    result = onset_features(str(ref), center_s=12.0, envelope_cache=cache)
    assert result is not None
    assert result["rise_time_s"] >= 0
    assert result["peak_amplitude"] >= 0
    assert str(ref) in cache
