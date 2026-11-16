"""Text-to-speech: a sentence-by-sentence processor plus pluggable providers.

The brain streams text; the processor cuts it into sentences so speech starts after the first
sentence instead of after the whole reply. On an Interruption the sentence being synthesized is
cancelled and buffered text is dropped.

Providers return PCM16 mono at `rate`:
- cartesia (best feel), elevenlabs (alternative cloud), kokoro (free, local)
- "browser": no audio from the server; the client speaks the text itself (free, zero setup).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncIterator

import httpx

from voicecat.audio import float_to_pcm
from voicecat.frames import AudioOut, BotText, BotTurnEnd, ClientEvent, UserStartedSpeaking, UserStoppedSpeaking
from voicecat.pipeline import Direction, Processor

log = logging.getLogger(__name__)

_SENTENCE_END = re.compile(r"(.+?[.!?])(\s+|$)", re.S)
_MD = [(re.compile(r"\[([^\]]+)\]\([^)]+\)"), r"\1"),   # links -> text
       (re.compile(r"[*_`#>|]+"), ""),                   # emphasis, headings, tables
       (re.compile(r"^\s*[-•]\s+", re.M), ""),            # bullets
       (re.compile(r"₹\s?([\d,]+(?:\.\d+)?)"), r"\1 rupees")]
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿️]")


def speakable(text: str) -> str:
    """Make chat text sound right when spoken (no markdown, prices in words)."""
    for pattern, repl in _MD:
        text = pattern.sub(repl, text)
    return re.sub(r"[ \t]+", " ", _EMOJI.sub("", text)).strip()


# ---------------------------------------------------------------- providers
class CartesiaTTS:
    """Cartesia Sonic over HTTP, streamed raw PCM. Env: CARTESIA_API_KEY; needs a voice id."""

    def __init__(self, api_key: str, voice: str, model: str = "sonic-2", version: str = "2024-06-10",
                 language: str = "en", rate: int = 24000, http: httpx.AsyncClient | None = None):
        if not voice:
            raise ValueError("cartesia needs a voice id (tts.cartesia.voice in voice.yaml)")
        self.api_key, self.voice, self.model, self.version, self.language = api_key, voice, model, version, language
        self.rate = rate
        self._http = http or httpx.AsyncClient(timeout=30)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        body = {"model_id": self.model, "transcript": text, "language": self.language,
                "voice": {"mode": "id", "id": self.voice},
                "output_format": {"container": "raw", "encoding": "pcm_s16le", "sample_rate": self.rate}}
        headers = {"X-API-Key": self.api_key, "Cartesia-Version": self.version}
        async with self._http.stream("POST", "https://api.cartesia.ai/tts/bytes", json=body, headers=headers) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes(4800):
                yield chunk


class ConnectionWarmer:
    """Keeps an HTTPS connection to a speech API open while a reply is on its way.

    Deepgram closes idle connections after ~5-8 s, and opening a fresh TLS connection adds
    ~0.5-0.7 s before the first audio (measured: 0.75-1.0 s cold vs 0.30-0.45 s warm). Between
    turns the connection is always idle longer than that, so without this every reply starts cold.
    While warm, a tiny unauthenticated GET (answered 401, free) is sent every few seconds.
    """

    def __init__(self, http: httpx.AsyncClient, url: str, every_secs: float = 4.0):
        self._http, self._url, self._every = http, url, every_secs
        self._until = 0.0
        self._task: asyncio.Task | None = None

    def hold(self, secs: float) -> None:
        loop = asyncio.get_running_loop()
        self._until = max(self._until, loop.time() + secs)
        if self._task is None or self._task.done():
            self._task = asyncio.ensure_future(self._run())

    def stop(self) -> None:
        self._until = 0.0
        if self._task and not self._task.done():
            self._task.cancel()

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while loop.time() < self._until:
            try:
                r = await self._http.get(self._url)
                await r.aread()
            except Exception as exc:  # noqa: BLE001 - best effort; the real request retries anyway
                log.debug("TTS warm-up failed: %s", exc)
            await asyncio.sleep(self._every)


class DeepgramTTS:
    """Deepgram Aura streaming TTS (raw linear16, 24 kHz). Env: DEEPGRAM_API_KEY (same key as STT)."""

    def __init__(self, api_key: str, model: str = "aura-2-thalia-en", rate: int = 24000,
                 url: str = "https://api.deepgram.com/v1/speak", http: httpx.AsyncClient | None = None):
        self.api_key, self.model, self.rate, self.url = api_key, model, rate, url
        self._http = http or httpx.AsyncClient(timeout=30, limits=httpx.Limits(keepalive_expiry=30))
        self._warmer = ConnectionWarmer(self._http, url)

    def keep_warm(self, secs: float) -> None:
        self._warmer.hold(secs)

    def stop_warm(self) -> None:
        self._warmer.stop()

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        params = {"model": self.model, "encoding": "linear16", "sample_rate": self.rate, "container": "none"}
        async with self._http.stream("POST", self.url, params=params, json={"text": text},
                                     headers={"Authorization": f"Token {self.api_key}"}) as r:
            if r.status_code >= 400:
                detail = (await r.aread()).decode(errors="replace")[:200]
                raise RuntimeError(f"Deepgram TTS {r.status_code}: {detail}")
            async for chunk in r.aiter_bytes(4800):
                yield chunk


class ElevenLabsTTS:
    """ElevenLabs streaming TTS (pcm_24000). Env: ELEVENLABS_API_KEY."""

    def __init__(self, api_key: str, voice_id: str = "21m00Tcm4TlvDq8ikWAM", model: str = "eleven_flash_v2_5",
                 rate: int = 24000, http: httpx.AsyncClient | None = None):
        self.api_key, self.voice_id, self.model, self.rate = api_key, voice_id, model, rate
        self._http = http or httpx.AsyncClient(timeout=30)

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice_id}/stream?output_format=pcm_{self.rate}"
        async with self._http.stream("POST", url, json={"text": text, "model_id": self.model},
                                     headers={"xi-api-key": self.api_key}) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes(4800):
                yield chunk


class KokoroTTS:
    """Kokoro (free, local, ONNX). Needs kokoro-onnx and its model + voices files."""

    def __init__(self, model_path: str, voices_path: str, voice: str = "af_heart", speed: float = 1.0,
                 lang: str = "en-us"):
        from kokoro_onnx import Kokoro  # optional dependency

        self._kokoro = Kokoro(model_path, voices_path)
        self.voice, self.speed, self.lang = voice, speed, lang
        self.rate = 24000

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        samples, rate = await asyncio.to_thread(self._kokoro.create, text, voice=self.voice, speed=self.speed,
                                                lang=self.lang)
        self.rate = rate
        yield float_to_pcm(samples)


# ---------------------------------------------------------------- processor
class TTSProcessor(Processor):
    interruptible = True  # stop mid-sentence when the user talks over us

    def __init__(self, provider=None, min_chars: int = 12, warm_secs: float = 20.0):
        super().__init__()
        self.provider = provider          # None = the client speaks ("browser")
        self.min_chars = min_chars
        self.warm_secs = warm_secs        # keep the speech connection open this long after the user talks
        self._buffer = ""

    async def on_interrupt(self) -> None:
        self._buffer = ""

    async def on_stop(self) -> None:
        if hasattr(self.provider, "stop_warm"):
            self.provider.stop_warm()

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, (UserStartedSpeaking, UserStoppedSpeaking)) and hasattr(self.provider, "keep_warm"):
            # A reply follows a few seconds after the user stops: have the connection ready.
            self.provider.keep_warm(self.warm_secs)
        if isinstance(frame, BotText):
            self._buffer += frame.text
            for sentence in self._take_sentences():
                await self._say(sentence)
            return
        if isinstance(frame, BotTurnEnd):
            rest, self._buffer = self._buffer.strip(), ""
            if rest:
                await self._say(rest)
            await self.push(frame, direction)
            return
        await self.push(frame, direction)

    def _take_sentences(self) -> list[str]:
        """Complete sentences from the buffer. A sentence counts as complete only once the
        terminator is followed by whitespace ("1.5 kg" and a still-streaming "Mr." stay put);
        very short ones ("Hi.") are joined to the next so the voice doesn't sound choppy."""
        out, pending = [], ""
        while True:
            m = _SENTENCE_END.match(self._buffer)
            if not m or not m.group(2):
                break
            piece = pending + m.group(1)
            self._buffer = self._buffer[m.end():]
            if len(piece.strip()) < self.min_chars:
                pending = piece + " "
                continue
            out.append(piece.strip())
            pending = ""
        if pending:
            self._buffer = pending + self._buffer
        return out

    async def _say(self, text: str) -> None:
        spoken = speakable(text)
        if not spoken:
            return
        await self.push(ClientEvent({"type": "bot_text", "text": spoken}))
        if self.provider is None:
            await self.push(ClientEvent({"type": "speak", "text": spoken}))
            return
        for attempt in (1, 2):
            started = False
            try:
                async for pcm in self.provider.synthesize(spoken):
                    started = True
                    await self.push(AudioOut(pcm=pcm, rate=self.provider.rate))
                return
            except asyncio.CancelledError:
                raise
            except httpx.TransportError as exc:
                # Cloud voices close idle keep-alive connections after a few seconds; the next
                # sentence then hits a dead one. Retry once on a fresh connection.
                if attempt == 1 and not started:
                    log.info("TTS connection dropped (%s); retrying", exc)
                    continue
                failure = exc
            except Exception as exc:  # noqa: BLE001
                failure = exc
            log.warning("TTS failed: %s", failure)  # say it on screen rather than go silent
            await self.push(ClientEvent({"type": "error", "message": f"Speech failed: {failure}"}))
            return


_SHARED: dict[tuple, KokoroTTS] = {}


def shared_kokoro(cfg: dict) -> KokoroTTS:
    """One Kokoro model per process, shared by all sessions."""
    key = (cfg["model_path"], cfg["voices_path"], cfg.get("voice", "af_heart"), float(cfg.get("speed", 1.0)),
           cfg.get("lang", "en-us"))
    if key not in _SHARED:
        _SHARED[key] = KokoroTTS(key[0], key[1], voice=key[2], speed=key[3], lang=key[4])
    return _SHARED[key]


def build_tts(name: str, cfg: dict) -> TTSProcessor:
    if name == "deepgram":
        return TTSProcessor(DeepgramTTS(os.environ[cfg.get("api_key_env", "DEEPGRAM_API_KEY")],
                                        model=cfg.get("model", "aura-2-thalia-en")))
    if name == "cartesia":
        return TTSProcessor(CartesiaTTS(os.environ[cfg.get("api_key_env", "CARTESIA_API_KEY")], cfg.get("voice", ""),
                                        model=cfg.get("model", "sonic-2"), version=cfg.get("version", "2024-06-10"),
                                        language=cfg.get("language", "en")))
    if name == "elevenlabs":
        return TTSProcessor(ElevenLabsTTS(os.environ[cfg.get("api_key_env", "ELEVENLABS_API_KEY")],
                                          voice_id=cfg.get("voice_id", "21m00Tcm4TlvDq8ikWAM"),
                                          model=cfg.get("model", "eleven_flash_v2_5")))
    if name == "kokoro":
        return TTSProcessor(shared_kokoro(cfg))
    if name == "browser":
        return TTSProcessor(None)
    raise ValueError(f"unknown TTS provider {name!r}")
