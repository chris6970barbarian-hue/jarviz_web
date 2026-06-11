"""Edge-TTS produces 24 kHz mono MP3 by default for Neural voices. We pull
the MP3 stream, decode to PCM via numpy/ffmpeg-less pure-Python decode, then
hand 24 kHz s16le to the encoder.

To avoid bringing in a heavy decoder, we use Edge-TTS's `Communicate` in
pcm-streaming mode by setting `output_format` -- but the official edge-tts
package returns mp3 chunks. We decode with `audioread` if available; fallback
to `pydub` (ffmpeg). For a docker-only deployment ffmpeg is trivially present.

Simpler: `edge_tts.Communicate.stream()` yields events that include audio
chunks. We collect everything, then use `ffmpeg` via subprocess to decode to
24 kHz s16le. Docker image installs ffmpeg.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import AsyncIterator

import edge_tts

from ..audio import TTS_SAMPLE_RATE
from ..config import settings
from .base import TTSProvider

log = logging.getLogger("jarviz.tts")


def _resolve_ffmpeg() -> str:
    """Return the ffmpeg binary path. Prefers system ffmpeg; falls back to
    the static binary bundled in `imageio-ffmpeg` so the user doesn't have
    to install ffmpeg separately."""
    sys_ffmpeg = shutil.which("ffmpeg")
    if sys_ffmpeg:
        return sys_ffmpeg
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        log.info("Using bundled ffmpeg from imageio-ffmpeg: %s", path)
        return path
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "No ffmpeg binary available. Install one (winget/scoop/apt) "
            "or `pip install imageio-ffmpeg`."
        ) from e


class EdgeTTS(TTSProvider):
    def __init__(self) -> None:
        self._ffmpeg = _resolve_ffmpeg()
        self._voice = settings.JARVIZ_TTS_VOICE
        self._rate = settings.JARVIZ_TTS_RATE

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        if not text.strip():
            return
        # Edge-TTS streams mp3 chunks; we pipe them through ffmpeg into a
        # 24 kHz mono s16le PCM stream and yield as we read.
        comm = edge_tts.Communicate(text=text, voice=self._voice, rate=self._rate)

        # stderr=DEVNULL: previously this was PIPE and we only drained it on
        # non-zero exit. For long TTS runs ffmpeg's stderr pipe could fill
        # (~64 KB on Windows) and block writes, hanging the subprocess. We
        # don't display the stderr usefully — a failure already shows up as
        # an early-cut PCM stream and a non-zero return code in the warning
        # log below.
        ff = await asyncio.create_subprocess_exec(
            self._ffmpeg,
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-f",
            "s16le",
            "-acodec",
            "pcm_s16le",
            "-ac",
            "1",
            "-ar",
            str(TTS_SAMPLE_RATE),
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        # Captures a real (non-cancellation) failure of the Edge-TTS stream so
        # the consumer can surface it. Without this, an Edge CDN error / network
        # drop / NoAudioReceived raised inside comm.stream() was silently
        # swallowed: ffmpeg saw a clean EOF, synthesize() yielded zero PCM and
        # "succeeded", and the device just played silence with no log or metric.
        feeder_error: list[BaseException] = []

        async def feed_ffmpeg() -> None:
            try:
                async for chunk in comm.stream():
                    if chunk["type"] == "audio":
                        try:
                            ff.stdin.write(chunk["data"])
                            await ff.stdin.drain()
                        except (BrokenPipeError, ConnectionResetError):
                            # ffmpeg already exited (e.g. consumer cancelled)
                            return
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                # Edge upstream failure (403/throttle/NoAudioReceived/network).
                feeder_error.append(e)
            finally:
                try:
                    ff.stdin.close()
                except Exception:
                    pass

        feeder = asyncio.create_task(feed_ffmpeg())
        produced = 0
        try:
            chunk_size = 4096
            while True:
                pcm = await ff.stdout.read(chunk_size)
                if not pcm:
                    break
                produced += len(pcm)
                yield pcm
        finally:
            # If the consumer cancelled or raised mid-stream, cancel the
            # feeder so it stops writing to a stdin nobody is reading, and
            # make sure the subprocess actually exits — `await ff.wait()`
            # alone hangs if ffmpeg is still blocked on stdin.
            if not feeder.done():
                feeder.cancel()
            try:
                await feeder
            except asyncio.CancelledError:
                pass
            except Exception as e:  # noqa: BLE001
                feeder_error.append(e)
            # Surface an upstream Edge-TTS failure loudly. We don't raise (the
            # session-level TTS handler already sends a clean `tts stop`), but
            # an operator must be able to tell "the user asked for silence" from
            # "Edge-TTS broke": the former produces audio, the latter logs here.
            if feeder_error:
                log.warning(
                    "Edge-TTS stream failed (produced %d PCM bytes before failure): %s",
                    produced, feeder_error[0],
                )
            if ff.returncode is None:
                try:
                    ff.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(ff.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    try:
                        ff.kill()
                    except ProcessLookupError:
                        pass
                    await ff.wait()
            if ff.returncode not in (0, None):
                log.warning("ffmpeg exited %s (stderr suppressed)", ff.returncode)
