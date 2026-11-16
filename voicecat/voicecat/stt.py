"""Speech-to-text: processors plus pluggable providers.

- Streaming providers (Deepgram) get audio continuously and return interim + final text.
- Segmented providers (local Whisper) transcribe each utterance after the user stops speaking.
- "browser": the client's own speech recognition sends text; the server only runs VAD on audio
  (for barge-in).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode

from voicecat.audio import pcm_to_float
from voicecat.frames import AudioIn, Transcript, UserStartedSpeaking, UserStoppedSpeaking
from voicecat.pipeline import Direction, Processor

log = logging.getLogger(__name__)

OnText = Callable[[str, bool], Awaitable[None]]


# ---------------------------------------------------------------- providers
class DeepgramSTT:
    """Deepgram live transcription over a websocket (best latency). Env: DEEPGRAM_API_KEY."""

    kind = "streaming"

    def __init__(self, api_key: str, model: str = "nova-3", language: str = "en", endpointing_ms: int = 300,
                 url: str = "wss://api.deepgram.com/v1/listen", keyterms: list[str] | None = None):
        self.api_key, self.model, self.language, self.endpointing_ms = api_key, model, language, endpointing_ms
        self.url = url
        self.keyterms = keyterms or []  # words to expect ("Instamart", "cart"…): fewer mishearings
        self._ws = None
        self._reader: asyncio.Task | None = None

    async def start(self, on_text: OnText) -> None:
        import websockets

        params = [("model", self.model), ("language", self.language), ("encoding", "linear16"),
                  ("sample_rate", 16000), ("channels", 1), ("interim_results", "true"),
                  ("smart_format", "true"), ("endpointing", self.endpointing_ms)]
        params += [("keyterm", t) for t in self.keyterms]
        query = urlencode(params)
        self._ws = await websockets.connect(f"{self.url}?{query}",
                                            additional_headers={"Authorization": f"Token {self.api_key}"})

        async def read() -> None:
            async for message in self._ws:
                data = json.loads(message)
                if data.get("type") != "Results":
                    continue
                alt = (data.get("channel", {}).get("alternatives") or [{}])[0]
                text = alt.get("transcript", "")
                if text:
                    await on_text(text, bool(data.get("is_final")))

        self._reader = asyncio.create_task(read())

    async def send(self, pcm: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm)

    async def finalize(self) -> None:
        """Our voice detection says the user stopped: return the final text now instead of
        waiting for Deepgram's own end-of-speech detection (seconds, with road noise)."""
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "Finalize"}))

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
        if self._reader:
            self._reader.cancel()


class WhisperSTT:
    """Local Whisper via faster-whisper (free, private; slower on CPU). Model downloads on first use."""

    kind = "segmented"

    def __init__(self, model: str = "base.en", device: str = "cpu", compute_type: str = "int8", language: str = "en",
                 download_root: str | None = None, vocabulary: str = ""):
        from faster_whisper import WhisperModel  # optional dependency

        self.language = language
        self.vocabulary = vocabulary  # words to expect ("Swiggy, Instamart, cart…"): fewer mishearings
        self._model = WhisperModel(model, device=device, compute_type=compute_type, download_root=download_root)

    async def transcribe(self, pcm: bytes) -> str:
        audio = pcm_to_float(pcm)

        def run() -> str:
            segments, _ = self._model.transcribe(audio, language=self.language, beam_size=1,
                                                 initial_prompt=self.vocabulary or None)
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.to_thread(run)


# ---------------------------------------------------------------- processors
class StreamingSTTProcessor(Processor):
    def __init__(self, provider):
        super().__init__()
        self.provider = provider

    async def on_start(self) -> None:
        async def on_text(text: str, final: bool) -> None:
            await self.push(Transcript(text=text, final=final))

        await self.provider.start(on_text)

    async def on_stop(self) -> None:
        await self.provider.close()

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, AudioIn):
            await self.provider.send(frame.pcm)
            return
        if isinstance(frame, UserStoppedSpeaking) and hasattr(self.provider, "finalize"):
            await self.provider.finalize()
        await self.push(frame, direction)


class SegmentedSTTProcessor(Processor):
    """Buffers each utterance (with a little audio from just before speech started) and
    transcribes it when the user stops."""

    def __init__(self, provider, preroll_secs: float = 0.4):
        super().__init__()
        self.provider = provider
        self._preroll_bytes = int(preroll_secs * 16000) * 2
        self._ring = b""
        self._utterance: bytearray | None = None

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, AudioIn):
            if self._utterance is not None:
                self._utterance.extend(frame.pcm)
            else:
                self._ring = (self._ring + frame.pcm)[-self._preroll_bytes:]
            return
        if isinstance(frame, UserStartedSpeaking):
            self._utterance = bytearray(self._ring)
        elif isinstance(frame, UserStoppedSpeaking) and self._utterance is not None:
            audio, self._utterance = bytes(self._utterance), None
            asyncio.create_task(self._transcribe(audio))
        await self.push(frame, direction)

    async def _transcribe(self, audio: bytes) -> None:
        try:
            text = await self.provider.transcribe(audio)
        except Exception:  # noqa: BLE001
            log.exception("transcription failed")
            return
        if text:
            await self.push(Transcript(text=text, final=True))


class ClientSTTProcessor(Processor):
    """Text comes from the client (browser speech recognition); audio is only used for VAD."""

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, AudioIn):
            return
        await self.push(frame, direction)


_SHARED: dict[tuple, WhisperSTT] = {}


def shared_whisper(cfg: dict) -> WhisperSTT:
    """One Whisper model per process, shared by all sessions (loading takes seconds)."""
    key = (cfg.get("model", "base.en"), cfg.get("device", "cpu"), cfg.get("compute_type", "int8"),
           cfg.get("language", "en"), cfg.get("download_root"), cfg.get("vocabulary", ""))
    if key not in _SHARED:
        _SHARED[key] = WhisperSTT(model=key[0], device=key[1], compute_type=key[2], language=key[3],
                                  download_root=key[4], vocabulary=key[5])
    return _SHARED[key]


def build_stt(name: str, cfg: dict) -> Processor:
    if name == "deepgram":
        return StreamingSTTProcessor(DeepgramSTT(os.environ[cfg.get("api_key_env", "DEEPGRAM_API_KEY")],
                                                 model=cfg.get("model", "nova-3"), language=cfg.get("language", "en"),
                                                 endpointing_ms=int(cfg.get("endpointing_ms", 300)),
                                                 keyterms=list(cfg.get("keyterms") or [])))
    if name == "whisper":
        return SegmentedSTTProcessor(shared_whisper(cfg))
    if name == "browser":
        return ClientSTTProcessor()
    raise ValueError(f"unknown STT provider {name!r}")
