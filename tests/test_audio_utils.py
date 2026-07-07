"""Tests for audio utility functions."""

from voice_assistant.audio.utils import (
    CHANNELS,
    CHUNK_DURATION_MS,
    CHUNK_SAMPLES,
    PLAY_AUDIO_CHUNK_BYTES,
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    base64_to_pcm16,
    compute_recovery_ms,
    generate_silence,
    generate_test_tone,
    is_meaningful_user_text,
    likely_calibration_prompt_transcript,
    likely_echo_transcript,
    pcm16_to_base64,
    trim_calibration_hello_audio,
)


class TestPcm16Base64Roundtrip:
    """pcm16_to_base64 and base64_to_pcm16 should be inverses."""

    def test_roundtrip_empty(self) -> None:
        original = b""
        assert base64_to_pcm16(pcm16_to_base64(original)) == original

    def test_roundtrip_small(self) -> None:
        original = b"\x01\x02\x03\x04"
        assert base64_to_pcm16(pcm16_to_base64(original)) == original

    def test_roundtrip_silence(self) -> None:
        silence = generate_silence(100)
        assert base64_to_pcm16(pcm16_to_base64(silence)) == silence

    def test_roundtrip_tone(self) -> None:
        tone = generate_test_tone(100)
        assert base64_to_pcm16(pcm16_to_base64(tone)) == tone


class TestGenerateSilence:
    """generate_silence produces the correct number of zero-valued bytes."""

    def test_silence_100ms(self) -> None:
        silence = generate_silence(100)
        expected_samples = SAMPLE_RATE * 100 // 1000  # 2400
        expected_bytes = expected_samples * SAMPLE_WIDTH
        assert len(silence) == expected_bytes

    def test_silence_1000ms(self) -> None:
        silence = generate_silence(1000)
        expected_bytes = SAMPLE_RATE * SAMPLE_WIDTH  # 48000
        assert len(silence) == expected_bytes

    def test_silence_is_zeros(self) -> None:
        silence = generate_silence(50)
        assert all(b == 0 for b in silence)

    def test_silence_custom_sample_rate(self) -> None:
        silence = generate_silence(100, sample_rate=16000)
        expected = 16000 * 100 // 1000 * SAMPLE_WIDTH
        assert len(silence) == expected


class TestGenerateTestTone:
    """generate_test_tone produces a valid PCM16 sine-wave buffer."""

    def test_tone_correct_length(self) -> None:
        tone = generate_test_tone(200)
        expected_samples = SAMPLE_RATE * 200 // 1000
        assert len(tone) == expected_samples * SAMPLE_WIDTH

    def test_tone_not_all_zeros(self) -> None:
        tone = generate_test_tone(100)
        assert any(b != 0 for b in tone)

    def test_tone_has_waveform_data(self) -> None:
        tone = generate_test_tone(100, frequency=440)
        assert len(tone) > 0
        max_val = max(
            abs(int.from_bytes(tone[i : i + 2], "little", signed=True))
            for i in range(0, len(tone), 2)
        )
        assert max_val > 1000  # a 440Hz tone should have significant amplitude

    def test_tone_custom_frequency(self) -> None:
        tone_low = generate_test_tone(100, frequency=220)
        tone_high = generate_test_tone(100, frequency=880)
        assert len(tone_low) == len(tone_high)

    def test_tone_custom_sample_rate(self) -> None:
        tone = generate_test_tone(100, sample_rate=16000)
        expected = 16000 * 100 // 1000 * SAMPLE_WIDTH
        assert len(tone) == expected


class TestAudioConstants:
    """Module-level constants must match the OpenAI Realtime API spec."""

    def test_sample_rate(self) -> None:
        assert SAMPLE_RATE == 24000

    def test_channels(self) -> None:
        assert CHANNELS == 1

    def test_sample_width(self) -> None:
        assert SAMPLE_WIDTH == 2

    def test_chunk_duration(self) -> None:
        assert CHUNK_DURATION_MS == 200

    def test_chunk_samples(self) -> None:
        assert CHUNK_SAMPLES == SAMPLE_RATE * CHUNK_DURATION_MS // 1000

    def test_play_audio_chunk_bytes(self) -> None:
        assert PLAY_AUDIO_CHUNK_BYTES == 4800


class TestEchoHelpers:
    def test_likely_echo_detects_overlap(self) -> None:
        assert likely_echo_transcript(
            "the weather today",
            "Sure, the weather today is sunny and warm.",
        )

    def test_likely_echo_rejects_unrelated(self) -> None:
        assert not likely_echo_transcript("hello", "the capital of France is Paris")

    def test_compute_recovery_ms_bounds(self) -> None:
        assert compute_recovery_ms(0) == 400
        assert compute_recovery_ms(100_000) == 2000
        assert compute_recovery_ms(10_000) == 500


class TestMeaningfulUserText:
    def test_rejects_empty_and_whitespace(self) -> None:
        assert not is_meaningful_user_text("")
        assert not is_meaningful_user_text(" ")
        assert not is_meaningful_user_text("  \t  ")

    def test_rejects_single_character(self) -> None:
        assert not is_meaningful_user_text("a")

    def test_accepts_two_or_more_characters(self) -> None:
        assert is_meaningful_user_text("hi")
        assert is_meaningful_user_text(" ok ")


class TestCalibrationHelloTrim:
    def _loud_chunk(self, amplitude: int = 8000) -> bytes:
        from struct import pack

        sample = pack("<h", amplitude)
        return sample * (PLAY_AUDIO_CHUNK_BYTES // SAMPLE_WIDTH)

    def test_trim_drops_leading_quiet_prompt(self) -> None:
        quiet = b"\x00\x00" * (PLAY_AUDIO_CHUNK_BYTES * 5)
        speech = self._loud_chunk()
        pcm = quiet + speech + speech

        trimmed = trim_calibration_hello_audio(
            pcm,
            speech_threshold=400.0,
            noise_floor=200.0,
        )

        assert len(trimmed) < len(pcm)
        assert trimmed.endswith(speech + speech)

    def test_likely_calibration_prompt_transcript(self) -> None:
        assert likely_calibration_prompt_transcript("Say hello to start.")
        assert likely_calibration_prompt_transcript("say hello")
        assert not likely_calibration_prompt_transcript("Hello there")
        assert not likely_calibration_prompt_transcript("Hello.")

    def test_valid_calibration_hello_transcript(self) -> None:
        from voice_assistant.audio.utils import is_valid_calibration_hello_transcript

        assert is_valid_calibration_hello_transcript("Hello.")
        assert is_valid_calibration_hello_transcript("hi")
        assert not is_valid_calibration_hello_transcript("Say hello to start.")
        assert not is_valid_calibration_hello_transcript("What can we do?")
        assert not is_valid_calibration_hello_transcript("good morning")
