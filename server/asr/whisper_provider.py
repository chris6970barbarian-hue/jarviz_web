"""faster-whisper backed ASR. Lazy-loads the model on first use because the
download + warmup is several seconds we don't want blocking server boot."""

from __future__ import annotations

import asyncio
import logging
import threading

import numpy as np

from ..audio import pcm_rms
from ..config import settings
from .base import ASRProvider

log = logging.getLogger("jarviz.asr")

# Anything quieter than this is treated as no speech. The INMP441 mic floor
# at room noise sits around 200-400; real voice peaks 3000-15000.
_MIN_RMS = 250.0


class FasterWhisperASR(ASRProvider):
    def __init__(self) -> None:
        self._model = None
        # `_ready` is set exactly once after `_model` is fully constructed.
        # Readers wait on it (no lock-free attribute read), which removes
        # the double-checked-locking footgun for future maintainers — even
        # though CPython's GIL makes the bare read currently safe.
        self._ready = threading.Event()
        self._lock = threading.Lock()
        # Serialize calls into `model.transcribe()`. faster-whisper's
        # underlying CTranslate2 Generator is NOT thread-safe with one
        # model instance; without this, concurrent sessions would
        # eventually corrupt the model's internal state. Permits N
        # concurrent transcribes — leave at 1 unless you've separately
        # built a pool of model replicas.
        parallelism = max(1, settings.JARVIZ_ASR_PARALLELISM)
        self._asr_sem = threading.Semaphore(parallelism)

    def _ensure_model(self):
        if self._ready.is_set():
            return self._model
        with self._lock:
            if self._ready.is_set():
                return self._model
            from faster_whisper import WhisperModel

            log.info(
                "Loading faster-whisper model=%s device=%s compute=%s",
                settings.JARVIZ_WHISPER_MODEL,
                settings.JARVIZ_WHISPER_DEVICE,
                settings.JARVIZ_WHISPER_COMPUTE_TYPE,
            )
            model = WhisperModel(
                settings.JARVIZ_WHISPER_MODEL,
                device=settings.JARVIZ_WHISPER_DEVICE,
                compute_type=settings.JARVIZ_WHISPER_COMPUTE_TYPE,
            )
            self._model = model
            self._ready.set()
            return model

    def _transcribe_sync(self, pcm: bytes, sample_rate: int) -> str:
        rms = pcm_rms(pcm)
        if rms < _MIN_RMS:
            log.debug("ASR skipping silent buffer (rms=%.1f)", rms)
            return ""
        model = self._ensure_model()
        # faster-whisper accepts a numpy float32 array in [-1, 1].
        f32 = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        # Serialize the inference call — see __init__ for why. Using `with`
        # so an exception in `transcribe()` still releases the semaphore.
        # `segments` is a generator that lazy-decodes; we have to fully
        # materialize it INSIDE the critical section, since iterating later
        # would re-enter the same un-thread-safe code without the guard.
        with self._asr_sem:
            segments, _info = model.transcribe(
                f32,
                language="en",
                beam_size=1,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            text = " ".join(seg.text.strip() for seg in segments if seg.text).strip()
        log.info("ASR (%dms, rms=%.1f) -> %r", len(pcm) * 1000 // (sample_rate * 2), rms, text)
        return text

    async def transcribe(self, pcm_s16le: bytes, sample_rate: int) -> str:
        return await asyncio.to_thread(self._transcribe_sync, pcm_s16le, sample_rate)
