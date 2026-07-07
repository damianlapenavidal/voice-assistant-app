"""Tests for dynamic VAD derivation from calibration metrics."""

from __future__ import annotations

from voice_assistant.audio.vad import derive_vad_settings


class TestDeriveVadSettings:
    def test_quiet_room_gets_lower_threshold(self) -> None:
        quiet = derive_vad_settings(noise_floor=200.0, user_speech_peak=900.0)
        noisy = derive_vad_settings(noise_floor=900.0, user_speech_peak=1200.0)
        assert quiet.threshold < noisy.threshold

    def test_threshold_stays_in_valid_range(self) -> None:
        settings = derive_vad_settings(noise_floor=50.0, user_speech_peak=2000.0)
        assert 0.35 <= settings.threshold <= 0.85

    def test_noisy_room_gets_longer_silence_window(self) -> None:
        quiet = derive_vad_settings(noise_floor=200.0, user_speech_peak=800.0)
        noisy = derive_vad_settings(noise_floor=900.0, user_speech_peak=1200.0)
        assert noisy.silence_ms > quiet.silence_ms

    def test_prefix_padding_is_stable(self) -> None:
        settings = derive_vad_settings(noise_floor=400.0, user_speech_peak=700.0)
        assert settings.prefix_padding_ms == 300
