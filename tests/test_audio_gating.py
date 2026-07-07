"""Tests for Pi-side calibration gating logic."""

from __future__ import annotations

import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "device"))

from audio_gating import AudioGating, CalibrationStep, CHUNK_BYTES


def _pcm_at_level(rms: float) -> bytes:
    """Build a PCM16 chunk with approximately the requested RMS."""
    amplitude = int(min(30000, max(100, rms * 4)))
    sample = struct.pack("<h", amplitude)
    return sample * (CHUNK_BYTES // 2)


class TestCalibrationGating:
    def test_quiet_phase_then_prompt(self) -> None:
        gating = AudioGating(quiet_sec=0.1, speak_sec=1.0)
        gating.start_calibration()
        quiet = _pcm_at_level(50)

        step = None
        for _ in range(20):
            step = gating.process_calibration_chunk(quiet)
            if step is not None:
                break

        assert step == CalibrationStep.PLAY_PROMPT
        assert gating.is_waiting_for_prompt

    def test_timeout_without_speech_does_not_complete(self) -> None:
        gating = AudioGating(quiet_sec=0.1, speak_sec=0.2)
        gating.start_calibration()

        for _ in range(20):
            if gating.process_calibration_chunk(_pcm_at_level(50)) == CalibrationStep.PLAY_PROMPT:
                break

        gating.begin_speak_phase()
        step = None
        for _ in range(30):
            step = gating.process_calibration_chunk(_pcm_at_level(50))
            if step is not None:
                break

        assert step == CalibrationStep.SPEECH_TIMEOUT
        assert gating.is_calibrating

    def test_speech_completes_calibration(self) -> None:
        gating = AudioGating(quiet_sec=0.1, speak_sec=5.0)
        gating.start_calibration()

        for _ in range(20):
            if gating.process_calibration_chunk(_pcm_at_level(50)) == CalibrationStep.PLAY_PROMPT:
                break

        gating.begin_speak_phase()
        loud = _pcm_at_level(1500)
        quiet = _pcm_at_level(40)

        step = None
        for _ in range(15):
            gating.process_calibration_chunk(loud)
        for _ in range(60):
            step = gating.process_calibration_chunk(quiet)
            if step is not None:
                break

        assert step == CalibrationStep.COMPLETE
        assert not gating.is_calibrating
        assert gating.user_speech_peak > gating.noise_floor + 50
