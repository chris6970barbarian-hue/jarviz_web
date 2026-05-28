from __future__ import annotations

from typing import Protocol


class ASRProvider(Protocol):
    async def transcribe(self, pcm_s16le: bytes, sample_rate: int) -> str:
        """Transcribe a chunk of mono PCM. Returns the recognized text
        (empty string if nothing was heard)."""
        ...
