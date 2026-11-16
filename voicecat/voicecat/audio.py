"""Small PCM helpers (16-bit little-endian mono)."""

from __future__ import annotations

import numpy as np


def pcm_to_float(pcm: bytes) -> np.ndarray:
    return np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0


def float_to_pcm(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resampling; plenty for speech."""
    if src_rate == dst_rate or not pcm:
        return pcm
    x = pcm_to_float(pcm)
    n_out = int(round(len(x) * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    positions = np.linspace(0, len(x) - 1, n_out)
    return float_to_pcm(np.interp(positions, np.arange(len(x)), x))


def duration_secs(pcm: bytes, rate: int) -> float:
    return len(pcm) / 2 / rate


def rms_db(samples: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(samples)))) if len(samples) else 0.0
    return 20 * np.log10(max(rms, 1e-6))
