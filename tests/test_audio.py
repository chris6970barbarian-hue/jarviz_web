"""Opus wire-format + PCM helpers. Offline (needs libopus via DYLD path).

The TTS encoder is fixed at 24 kHz / 60 ms / mono to match the firmware's
decoder. These tests pin the framing contract and prove an end-to-end
encode -> decode round-trip at 24 kHz.
"""

from __future__ import annotations

import ctypes
from ctypes import POINTER, byref, c_int, c_int16, c_uint8

import numpy as np
import pytest

from server import audio
from server.audio import (
    CHANNELS,
    FRAME_DURATION_MS,
    MIC_FRAME_SAMPLES,
    OpusTtsEncoder,
    TTS_FRAME_SAMPLES,
    TTS_SAMPLE_RATE,
    pcm_resample,
    pcm_rms,
    silence_pcm,
)

BYTES_PER_FRAME = TTS_FRAME_SAMPLES * 2


def test_frame_constants():
    assert FRAME_DURATION_MS == 60
    assert TTS_FRAME_SAMPLES == 1440   # 24000 * 60 / 1000
    assert MIC_FRAME_SAMPLES == 960    # 16000 * 60 / 1000


def test_encode_one_full_frame_is_one_packet():
    enc = OpusTtsEncoder()
    pkts = enc.encode_pcm(b"\x00\x00" * TTS_FRAME_SAMPLES)
    assert len(pkts) == 1
    assert len(pkts[0]) > 0


def test_encode_two_frames_is_two_packets():
    enc = OpusTtsEncoder()
    pkts = enc.encode_pcm(b"\x01\x00" * (TTS_FRAME_SAMPLES * 2))
    assert len(pkts) == 2


def test_encode_partial_frame_is_padded_to_one_packet():
    enc = OpusTtsEncoder()
    # Half a frame of PCM — encoder pads internally, must not drop audio.
    pkts = enc.encode_pcm(b"\x02\x00" * (TTS_FRAME_SAMPLES // 2))
    assert len(pkts) == 1


def _make_24k_decoder():
    err = c_int(0)
    dec = audio._libopus.opus_decoder_create(TTS_SAMPLE_RATE, CHANNELS, byref(err))
    assert err.value == 0 and dec
    return dec


def test_encode_decode_roundtrip_24k():
    """Encode a sine tone, decode it back at 24 kHz, and confirm the decoded
    frame is the right length and carries real signal energy."""
    enc = OpusTtsEncoder()
    t = np.arange(TTS_FRAME_SAMPLES, dtype=np.float32) / TTS_SAMPLE_RATE
    tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
    pkt = enc.encode_pcm(tone.tobytes())[0]

    dec = _make_24k_decoder()
    try:
        out = (c_int16 * TTS_FRAME_SAMPLES)()
        in_buf = (c_uint8 * len(pkt)).from_buffer_copy(pkt)
        n = audio._libopus.opus_decode(dec, in_buf, len(pkt), out, TTS_FRAME_SAMPLES, 0)
        assert n == TTS_FRAME_SAMPLES
        decoded = np.frombuffer(bytes(ctypes.string_at(out, n * 2)), dtype=np.int16)
    finally:
        audio._libopus.opus_decoder_destroy(dec)

    # Opus is lossy, but a 440 Hz tone must survive with substantial energy.
    assert pcm_rms(decoded.tobytes()) > 1000


def test_pcm_resample_identity_and_ratio():
    pcm = (np.arange(1000, dtype=np.int16)).tobytes()
    assert pcm_resample(pcm, 24000, 24000) == pcm           # identity fast-path
    down = pcm_resample(pcm, 24000, 16000)
    assert abs(len(down) // 2 - 1000 * 16000 // 24000) <= 2  # ~2/3 length
    assert pcm_resample(b"", 24000, 16000) == b""


def test_pcm_rms_silence_is_zero_and_signal_positive():
    assert pcm_rms(b"") == 0.0
    assert pcm_rms(silence_pcm(60, TTS_SAMPLE_RATE)) == 0.0
    loud = (np.full(480, 10000, dtype=np.int16)).tobytes()
    assert pcm_rms(loud) == pytest.approx(10000, rel=0.01)


def test_silence_pcm_length():
    pcm = silence_pcm(60, TTS_SAMPLE_RATE)
    assert len(pcm) == (TTS_SAMPLE_RATE * 60 // 1000) * 2
