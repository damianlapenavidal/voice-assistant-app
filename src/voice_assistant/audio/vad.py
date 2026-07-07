"""Derive OpenAI server_vad settings from device calibration metrics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VadSettings:
    """Turn-detection parameters computed from ambient calibration."""

    threshold: float
    silence_ms: int
    prefix_padding_ms: int


def derive_vad_settings(
    *,
    noise_floor: float,
    user_speech_peak: float,
) -> VadSettings:
    """Map Pi RMS calibration to OpenAI server_vad parameters.

    Noisier rooms get a higher threshold and slightly longer silence window so
    background noise is less likely to trigger or extend a turn.
    """
    peak = max(user_speech_peak, noise_floor + 1.0)
    noise_ratio = noise_floor / peak

    threshold = 0.45 + noise_ratio * 0.35
    threshold = max(0.35, min(0.85, threshold))

    if noise_floor > 800:
        silence_ms = 900
    elif noise_floor > 500:
        silence_ms = 750
    else:
        silence_ms = 650

    return VadSettings(
        threshold=round(threshold, 2),
        silence_ms=silence_ms,
        prefix_padding_ms=300,
    )
