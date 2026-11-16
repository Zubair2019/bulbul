"""Cloud providers speak each API's protocol correctly (checked against local fakes, no keys)."""

from __future__ import annotations

import asyncio
import json

import httpx
import websockets

from voicecat.stt import DeepgramSTT
from voicecat.tts import CartesiaTTS, DeepgramTTS, ElevenLabsTTS


async def test_deepgram_streams_audio_and_reads_transcripts():
    got = {"audio": b"", "headers": None, "path": None}

    async def fake_deepgram(ws):
        got["headers"] = dict(ws.request.headers)
        got["path"] = ws.request.path
        async for msg in ws:
            if isinstance(msg, bytes):
                got["audio"] += msg
                if len(got["audio"]) >= 1280:
                    for text, final in (("add six", False), ("add six eggs", True)):
                        await ws.send(json.dumps({"type": "Results", "is_final": final,
                                                  "channel": {"alternatives": [{"transcript": text}]}}))
            elif json.loads(msg).get("type") == "CloseStream":
                break

    async with websockets.serve(fake_deepgram, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stt = DeepgramSTT("dg-key", model="nova-3", language="en-IN", url=f"ws://127.0.0.1:{port}/v1/listen")
        heard = []

        async def on_text(text, final):
            heard.append((text, final))

        await stt.start(on_text)
        await stt.send(b"\x01\x00" * 320)
        await stt.send(b"\x01\x00" * 320)
        for _ in range(100):
            if len(heard) == 2:
                break
            await asyncio.sleep(0.02)
        await stt.close()

    assert got["headers"]["authorization"] == "Token dg-key"
    for part in ("model=nova-3", "language=en-IN", "encoding=linear16", "sample_rate=16000", "interim_results=true"):
        assert part in got["path"]
    assert got["audio"] == b"\x01\x00" * 640
    assert heard == [("add six", False), ("add six eggs", True)]


async def test_cartesia_requests_raw_pcm_for_the_chosen_voice():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x01" * 2400)

    tts = CartesiaTTS("ca-key", voice="voice-123", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    audio = b"".join([c async for c in tts.synthesize("About fifteen hundred rupees.")])
    assert seen["url"] == "https://api.cartesia.ai/tts/bytes"
    assert seen["headers"]["x-api-key"] == "ca-key" and seen["headers"]["cartesia-version"]
    assert seen["body"]["voice"] == {"mode": "id", "id": "voice-123"}
    assert seen["body"]["output_format"] == {"container": "raw", "encoding": "pcm_s16le", "sample_rate": 24000}
    assert seen["body"]["transcript"] == "About fifteen hundred rupees."
    assert len(audio) == 4800 and tts.rate == 24000


async def test_elevenlabs_streams_pcm_24k():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = str(request.url), request.headers, json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x01" * 100)

    tts = ElevenLabsTTS("el-key", voice_id="v9", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    audio = b"".join([c async for c in tts.synthesize("Shall I place it?")])
    assert seen["url"] == "https://api.elevenlabs.io/v1/text-to-speech/v9/stream?output_format=pcm_24000"
    assert seen["headers"]["xi-api-key"] == "el-key"
    assert seen["body"] == {"text": "Shall I place it?", "model_id": "eleven_flash_v2_5"}
    assert len(audio) == 200


async def test_deepgram_voice_requests_raw_pcm_24k_with_the_stt_key():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"], seen["headers"], seen["body"] = request.url, request.headers, json.loads(request.content)
        return httpx.Response(200, content=b"\x00\x01" * 100)

    tts = DeepgramTTS("dg-key", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    audio = b"".join([c async for c in tts.synthesize("Shall I place it?")])
    assert seen["url"].path == "/v1/speak"
    assert dict(seen["url"].params) == {"model": "aura-2-thalia-en", "encoding": "linear16", "sample_rate": "24000",
                                        "container": "none"}
    assert seen["headers"]["authorization"] == "Token dg-key"
    assert seen["body"] == {"text": "Shall I place it?"}
    assert len(audio) == 200 and tts.rate == 24000


async def test_a_cloud_failure_is_said_on_screen_not_silent():
    from voicecat.frames import BotText, BotTurnEnd
    from voicecat.pipeline import Pipeline, Processor
    from voicecat.session import RecordingSession
    from voicecat.tts import TTSProcessor

    class Sink(Processor):
        def __init__(self):
            super().__init__()
            self.frames = []

        async def queue_frame(self, frame, direction=None):
            self.frames.append(frame)

    def down(request):
        return httpx.Response(401, text="bad key")

    tts = TTSProcessor(CartesiaTTS("wrong", voice="v", http=httpx.AsyncClient(transport=httpx.MockTransport(down))))
    sink = Sink()
    pipe = Pipeline([tts, sink])
    await pipe.start(RecordingSession())
    await tts.process(BotText("Hello there, Asha."), None)
    await tts.process(BotTurnEnd(), None)
    await pipe.stop()
    errors = [f.data for f in sink.frames if getattr(f, "data", {}).get("type") == "error"]
    assert errors and "Speech failed" in errors[0]["message"]


async def test_deepgram_gets_keyterms_and_finalizes_when_the_user_stops():
    """Our VAD hears the end of speech first: Deepgram is told to finalize right away."""
    from voicecat.frames import UserStoppedSpeaking
    from voicecat.session import RecordingSession
    from voicecat.stt import StreamingSTTProcessor

    got = {"path": None, "finalize": False}

    async def fake_deepgram(ws):
        got["path"] = ws.request.path
        async for msg in ws:
            if isinstance(msg, str) and json.loads(msg).get("type") == "Finalize":
                got["finalize"] = True
                await ws.send(json.dumps({"type": "Results", "is_final": True, "from_finalize": True,
                                          "channel": {"alternatives": [{"transcript": "what's in my cart"}]}}))
            elif isinstance(msg, str) and json.loads(msg).get("type") == "CloseStream":
                break

    async with websockets.serve(fake_deepgram, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        stt = DeepgramSTT("k", url=f"ws://127.0.0.1:{port}/v1/listen", keyterms=["Instamart", "cart"])
        proc = StreamingSTTProcessor(stt)
        out = []

        async def capture(frame, direction=None):
            out.append(frame)

        proc.push = capture  # type: ignore[method-assign]
        proc.session = RecordingSession()
        await proc.on_start()
        await proc.process(UserStoppedSpeaking(), None)
        for _ in range(100):
            if any(getattr(f, "final", False) for f in out):
                break
            await asyncio.sleep(0.02)
        await proc.on_stop()

    assert "keyterm=Instamart" in got["path"] and "keyterm=cart" in got["path"]
    assert got["finalize"]
    assert any(getattr(f, "text", "") == "what's in my cart" and f.final for f in out)


async def test_a_dropped_voice_connection_is_retried_once():
    """Seen live: Deepgram closed an idle keep-alive connection and a sentence went unsaid."""
    from voicecat.frames import AudioOut
    from voicecat.tts import TTSProcessor

    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
        return httpx.Response(200, content=b"\x00\x01" * 100)

    tts = TTSProcessor(DeepgramTTS("k", http=httpx.AsyncClient(transport=httpx.MockTransport(flaky))))
    out = []

    async def capture(frame, direction=None):
        out.append(frame)

    tts.push = capture  # type: ignore[method-assign]
    await tts._say("Your cart has twenty one items.")
    assert calls["n"] == 2
    assert sum(len(f.pcm) for f in out if isinstance(f, AudioOut)) == 200
    assert not any(getattr(f, "data", {}).get("type") == "error" for f in out)


async def test_the_voice_connection_is_kept_warm_while_a_reply_is_on_its_way():
    """Deepgram drops idle connections after ~5 s and a cold one costs ~0.6 s before the first audio.
    When the user speaks, the connection is kept open (cheap unauthenticated pings) until the
    reply; nothing is pinged when nobody talks, and the API key never goes on a ping."""
    from voicecat.frames import UserStoppedSpeaking
    from voicecat.tts import TTSProcessor

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.headers.get("authorization")))
        return httpx.Response(401 if request.method == "GET" else 200, content=b"\x00\x01" * 10)

    provider = DeepgramTTS("dg-key", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    provider._warmer._every = 0.02
    tts = TTSProcessor(provider, warm_secs=0.1)
    out = []

    async def capture(frame, direction=None):
        out.append(frame)

    tts.push = capture  # type: ignore[method-assign]
    await asyncio.sleep(0.05)
    assert seen == []  # nobody spoke: no pings

    await tts.process(UserStoppedSpeaking(), None)
    await asyncio.sleep(0.2)
    pings = [s for s in seen if s[0] == "GET"]
    assert 2 <= len(pings) <= 8 and all(auth is None for _, auth in pings)
    assert any(isinstance(f, UserStoppedSpeaking) for f in out)  # the frame still travels on

    count = len(seen)
    await asyncio.sleep(0.1)
    assert len(seen) == count  # stops on its own after warm_secs
    await tts.on_stop()
