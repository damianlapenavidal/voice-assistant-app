"""Derive OpenAI server_vad settings from device calibration metrics."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VadSettings:
    """Turn-detection parameters computed from ambient calibration.

    ``eagerness`` drives OpenAI's semantic VAD: how quickly the model decides
    the child has finished their turn. ``threshold``/``silence_ms``/
    ``prefix_padding_ms`` are retained calibration signals (logged for
    diagnostics) but no longer bound turn detection to raw silence timers.
    """

    eagerness: str
    threshold: float
    silence_ms: int
    prefix_padding_ms: int


def derive_vad_settings(
    *,
    noise_floor: float,
    user_speech_peak: float,
) -> VadSettings:
    """Map Pi RMS calibration to OpenAI semantic_vad parameters.

    Semantic VAD lets the model judge whether the child is actually done
    speaking rather than ending the turn on a fixed silence timer, so a pause
    to think no longer cuts the child off. We stay patient by default
    (``eagerness="low"``) and only grow more eager to respond in loud rooms,
    where a long wait risks background chatter extending the turn.
    """
    peak = max(user_speech_peak, noise_floor + 1.0)
    noise_ratio = noise_floor / peak

    threshold = 0.45 + noise_ratio * 0.35
    threshold = max(0.35, min(0.85, threshold))

    if noise_floor > 800:
        silence_ms = 900
        eagerness = "medium"
    elif noise_floor > 500:
        silence_ms = 750
        eagerness = "low"
    else:
        silence_ms = 650
        eagerness = "low"

    return VadSettings(
        eagerness=eagerness,
        threshold=round(threshold, 2),
        silence_ms=silence_ms,
        prefix_padding_ms=300,
    )
