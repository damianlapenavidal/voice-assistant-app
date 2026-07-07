"""Audio utility functions for format conversion and processing."""

from __future__ import annotations

import base64
import math
import struct

SAMPLE_RATE = 24000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes per sample (16-bit)
CHUNK_DURATION_MS = 200
CHUNK_SAMPLES = SAMPLE_RATE * CHUNK_DURATION_MS // 1000

# 100 ms @ 24 kHz mono PCM16 — keeps base64 PLAY_AUDIO frames well under 1 MB.
PLAY_AUDIO_CHUNK_BYTES = 4800

MIN_MEANINGFUL_USER_CHARS = 2
OPENING_NUDGE_WAIT_SEC = 60
# Calibration ("say hello") watchdog: re-prompt every minute, give up after 5.
CALIBRATION_REPEAT_SEC = 60
CALIBRATION_TIMEOUT_SEC = 300
CALIBRATION_PROMPT_PHRASES = (
    "say hello to start",
    "say hello",
    "hello to start",
)
CALIBRATION_HELLO_WORDS = frozenset({
    "hello",
    "hi",
    "hey",
    "hiya",
    "howdy",
})
MIN_CALIBRATION_HELLO_BYTES = PLAY_AUDIO_CHUNK_BYTES


def pcm16_to_base64(pcm_bytes: bytes) -> str:
    """Encode raw PCM16 bytes to a base64 string."""
    return base64.b64encode(pcm_bytes).decode("ascii")


def base64_to_pcm16(b64_string: str) -> bytes:
    """Decode a base64 string to raw PCM16 bytes."""
    return base64.b64decode(b64_string)


def generate_silence(duration_ms: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Generate silent PCM16 audio (all-zero samples)."""
    num_samples = sample_rate * duration_ms // 1000
    return b"\x00\x00" * num_samples


def likely_echo_transcript(user_text: str, assistant_text: str) -> bool:
    """Return True when a user transcript likely echoes recent assistant speech."""
    if not user_text or not assistant_text:
        return False
    user_norm = user_text.lower().strip()
    assistant_norm = assistant_text.lower().strip()
    if not user_norm:
        return False
    if user_norm in assistant_norm or assistant_norm in user_norm:
        return True
    user_words = set(user_norm.split())
    assistant_words = set(assistant_norm.split())
    if not user_words:
        return False
    overlap = len(user_words & assistant_words) / len(user_words)
    return overlap > 0.6


def compute_recovery_ms(playback_duration_ms: int) -> int:
    """Adaptive post-playback mic recovery before accepting a user turn."""
    return min(2000, max(400, int(playback_duration_ms * 0.05)))


def is_meaningful_user_text(
    text: str,
    min_chars: int = MIN_MEANINGFUL_USER_CHARS,
) -> bool:
    """Return True when a user transcript is long enough to arm the conversation."""
    return len(text.strip()) >= min_chars


def likely_calibration_prompt_transcript(text: str) -> bool:
    """Return True when a user transcript matches the Pi calibration prompt."""
    norm = text.lower().strip().rstrip(".!?")
    if not norm:
        return False
    return any(
        norm == phrase or norm.startswith(phrase + " ")
        for phrase in CALIBRATION_PROMPT_PHRASES
    )


def is_valid_calibration_hello_transcript(text: str) -> bool:
    """Return True when a transcript is an acceptable calibration hello."""
    if likely_calibration_prompt_transcript(text):
        return False
    norm = text.lower().strip().rstrip(".!?,")
    if not norm:
        return False
    words = [word.rstrip(".!?,") for word in norm.split()]
    if not words or len(words) > 2:
        return False
    return all(word in CALIBRATION_HELLO_WORDS for word in words)


def pcm16_chunk_rms(pcm: bytes) -> float:
    """Root-mean-square energy for a little-endian PCM16 mono chunk."""
    if len(pcm) < SAMPLE_WIDTH:
        return 0.0
    usable = pcm[: len(pcm) // SAMPLE_WIDTH * SAMPLE_WIDTH]
    samples = struct.unpack(f"<{len(usable) // SAMPLE_WIDTH}h", usable)
    if not samples:
        return 0.0
    mean_sq = sum(sample * sample for sample in samples) / len(samples)
    return mean_sq**0.5


def trim_calibration_hello_audio(
    pcm: bytes,
    *,
    speech_threshold: float,
    noise_floor: float,
    frame_ms: int = 20,
    prefix_ms: int = 300,
    hang_frames: int = 3,
) -> bytes:
    """Drop leading calibration prompt audio before the user's hello."""
    if not pcm:
        return pcm

    threshold = max(speech_threshold, noise_floor + 80.0)
    frame_bytes = max(SAMPLE_WIDTH, SAMPLE_RATE * frame_ms // 1000 * SAMPLE_WIDTH)
    prefix_bytes = SAMPLE_RATE * prefix_ms // 1000 * SAMPLE_WIDTH

    speech_run = 0
    speech_start = 0
    for offset in range(0, len(pcm), frame_bytes):
        chunk = pcm[offset : offset + frame_bytes]
        if pcm16_chunk_rms(chunk) >= threshold:
            speech_run += 1
            if speech_run >= hang_frames:
                speech_start = max(0, offset - (hang_frames - 1) * frame_bytes)
                break
        else:
            speech_run = 0

    if speech_start == 0 and speech_run < hang_frames:
        return pcm

    trimmed = pcm[max(0, speech_start - prefix_bytes) :]
    return trimmed if len(trimmed) >= MIN_CALIBRATION_HELLO_BYTES else pcm


def generate_test_tone(
    duration_ms: int,
    frequency: int = 440,
    sample_rate: int = SAMPLE_RATE,
) -> bytes:
    """Generate a sine-wave test tone as PCM16 little-endian mono."""
    num_samples = sample_rate * duration_ms // 1000
    samples: list[bytes] = []
    for i in range(num_samples):
        value = int(32767 * math.sin(2 * math.pi * frequency * i / sample_rate))
        samples.append(struct.pack("<h", value))
    return b"".join(samples)
