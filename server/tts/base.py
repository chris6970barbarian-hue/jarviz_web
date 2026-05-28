from __future__ import annotations

from typing import AsyncIterator, Protocol


class TTSProvider(Protocol):
    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield 16-bit mono PCM chunks at the device's TTS sample rate
        (24 kHz). Chunks may be of arbitrary length; the audio module will
        slice them into 60 ms Opus frames."""
        ...
