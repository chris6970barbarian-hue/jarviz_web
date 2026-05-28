"""Opus encode/decode helpers tied to the firmware's audio params.

Wire format (websocket protocol version 1): each binary WS frame carries one
raw Opus packet. No Ogg container, no length prefix.

Mic side (device -> server):
    16 kHz mono, 60 ms per frame -> 960 PCM samples per packet.

TTS side (server -> device):
    24 kHz mono, 60 ms per frame -> 1440 PCM samples per packet.

We bind to libopus directly via ctypes. The DLL is located by trying:
  1. PyOgg's bundled `opus.dll` (Windows pip wheel ships this; zero-config).
  2. Standard system names (libopus.so.0 on Linux, etc).

Picking PyOgg as a binary host instead of writing our own opuslib loader
keeps the prototype Windows-friendly without forcing the user to install
libopus separately.
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import wave
from ctypes import POINTER, byref, c_int, c_int16, c_int32, c_uint8
from io import BytesIO

import numpy as np

# These match the firmware's hello/hello-ack contract. Don't change them
# without updating both ends.
MIC_SAMPLE_RATE = 16000
TTS_SAMPLE_RATE = 24000
FRAME_DURATION_MS = 60
CHANNELS = 1

MIC_FRAME_SAMPLES = MIC_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 960
TTS_FRAME_SAMPLES = TTS_SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 1440

# Generous output buffer per Opus packet (Opus rarely produces >1275 B per frame).
_MAX_PACKET_BYTES = 4000

OPUS_APPLICATION_VOIP = 2048
OPUS_OK = 0


def _load_libopus() -> ctypes.CDLL:
    # First try the DLL bundled inside PyOgg's wheel — saves the user a
    # manual libopus install on Windows.
    try:
        import pyogg
        pyogg_dir = os.path.dirname(pyogg.__file__)
        if sys.platform == "win32":
            candidate = os.path.join(pyogg_dir, "opus.dll")
            if os.path.exists(candidate):
                return ctypes.CDLL(candidate)
        else:
            for name in ("libopus.so.0", "libopus.so", "libopus.0.dylib", "libopus.dylib"):
                p = os.path.join(pyogg_dir, name)
                if os.path.exists(p):
                    return ctypes.CDLL(p)
    except Exception:
        pass

    # Fall back to system-installed libopus.
    candidates = (
        ("opus.dll", "libopus-0.dll", "libopus.dll")
        if sys.platform == "win32"
        else ("libopus.so.0", "libopus.so", "libopus.0.dylib", "libopus.dylib")
    )
    for name in candidates:
        try:
            return ctypes.CDLL(name)
        except OSError:
            continue
    raise RuntimeError(
        "libopus not found. On Linux: install libopus0. On Windows: pip install PyOgg."
    )


_libopus = _load_libopus()

_libopus.opus_encoder_create.restype = ctypes.c_void_p
_libopus.opus_encoder_create.argtypes = [c_int32, c_int, c_int, POINTER(c_int)]

_libopus.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
_libopus.opus_encoder_destroy.restype = None

_libopus.opus_encode.restype = c_int32
_libopus.opus_encode.argtypes = [
    ctypes.c_void_p,
    POINTER(c_int16),
    c_int,
    POINTER(c_uint8),
    c_int32,
]

_libopus.opus_decoder_create.restype = ctypes.c_void_p
_libopus.opus_decoder_create.argtypes = [c_int32, c_int, POINTER(c_int)]

_libopus.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
_libopus.opus_decoder_destroy.restype = None

_libopus.opus_decode.restype = c_int
_libopus.opus_decode.argtypes = [
    ctypes.c_void_p,
    POINTER(c_uint8),
    c_int32,
    POINTER(c_int16),
    c_int,
    c_int,
]

_libopus.opus_strerror.restype = ctypes.c_char_p
_libopus.opus_strerror.argtypes = [c_int]


def _opus_strerror(code: int) -> str:
    msg = _libopus.opus_strerror(code) or b""
    return msg.decode("utf-8", errors="replace")


class OpusMicDecoder:
    """Decodes mic Opus packets coming from the device into 16-bit PCM."""

    def __init__(self) -> None:
        err = c_int(0)
        self._dec = _libopus.opus_decoder_create(MIC_SAMPLE_RATE, CHANNELS, byref(err))
        if err.value != OPUS_OK or not self._dec:
            raise RuntimeError(f"opus_decoder_create failed: {_opus_strerror(err.value)}")

    def __del__(self) -> None:
        try:
            if getattr(self, "_dec", None):
                _libopus.opus_decoder_destroy(self._dec)
                self._dec = None
        except Exception:
            pass

    def decode(self, packet: bytes) -> bytes:
        in_buf = (c_uint8 * len(packet)).from_buffer_copy(packet)
        out_buf = (c_int16 * MIC_FRAME_SAMPLES)()
        n = _libopus.opus_decode(self._dec, in_buf, len(packet), out_buf, MIC_FRAME_SAMPLES, 0)
        if n < 0:
            raise RuntimeError(f"opus_decode failed: {_opus_strerror(n)}")
        return bytes(ctypes.string_at(out_buf, n * CHANNELS * 2))


class OpusTtsEncoder:
    """Encodes 24 kHz s16le PCM into 60 ms Opus packets for the device.

    The decoder on the device is fixed at 24 kHz / 60 ms, so we feed it
    full-frame chunks. `encode_pcm` slices the input into frames; any
    trailing partial frame is zero-padded so we never drop audio.
    """

    def __init__(self) -> None:
        err = c_int(0)
        self._enc = _libopus.opus_encoder_create(
            TTS_SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_VOIP, byref(err)
        )
        if err.value != OPUS_OK or not self._enc:
            raise RuntimeError(f"opus_encoder_create failed: {_opus_strerror(err.value)}")

    def __del__(self) -> None:
        try:
            if getattr(self, "_enc", None):
                _libopus.opus_encoder_destroy(self._enc)
                self._enc = None
        except Exception:
            pass

    def encode_pcm(self, pcm_s16le: bytes) -> list[bytes]:
        bytes_per_frame = TTS_FRAME_SAMPLES * 2  # 1440 samples * 2 bytes = 2880
        out: list[bytes] = []
        for off in range(0, len(pcm_s16le), bytes_per_frame):
            chunk = pcm_s16le[off : off + bytes_per_frame]
            if len(chunk) < bytes_per_frame:
                chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))
            in_buf = (c_int16 * TTS_FRAME_SAMPLES).from_buffer_copy(chunk)
            out_buf = (c_uint8 * _MAX_PACKET_BYTES)()
            n = _libopus.opus_encode(
                self._enc, in_buf, TTS_FRAME_SAMPLES, out_buf, _MAX_PACKET_BYTES
            )
            if n < 0:
                raise RuntimeError(f"opus_encode failed: {_opus_strerror(n)}")
            out.append(bytes(ctypes.string_at(out_buf, n)))
        return out


def pcm_resample(pcm_s16le: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear resampler. Good enough for TTS that's already band-limited."""
    if src_rate == dst_rate:
        return pcm_s16le
    samples = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return b""
    n_out = int(round(samples.size * dst_rate / src_rate))
    if n_out <= 0:
        return b""
    x_src = np.linspace(0, 1, samples.size, endpoint=False, dtype=np.float32)
    x_dst = np.linspace(0, 1, n_out, endpoint=False, dtype=np.float32)
    out = np.interp(x_dst, x_src, samples).astype(np.int16)
    return out.tobytes()


def pcm_to_wav_bytes(pcm_s16le: bytes, sample_rate: int) -> bytes:
    buf = BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm_s16le)
    return buf.getvalue()


def pcm_rms(pcm_s16le: bytes) -> float:
    if not pcm_s16le:
        return 0.0
    samples = np.frombuffer(pcm_s16le, dtype=np.int16).astype(np.float32)
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(samples * samples)))


def silence_pcm(duration_ms: int, sample_rate: int) -> bytes:
    n = sample_rate * duration_ms // 1000
    return struct.pack("<" + "h" * n, *([0] * n))
