"""Unit tests: VAD + barge-in, turn taking, TTS text handling, provider choice, the mounted router."""

from __future__ import annotations

import json

import numpy as np

from tests.conftest import FakeBulbul, wait_for
from voicecat.audio import float_to_pcm, resample
from voicecat.frames import AudioIn, Interruption, Transcript, UserStartedSpeaking, UserStoppedSpeaking, UserTurn
from voicecat.pipeline import Pipeline, Processor
from voicecat.providers import availability, choose
from voicecat.session import RecordingSession
from voicecat.tts import TTSProcessor, speakable
from voicecat.turns import TurnProcessor, meaningful
from voicecat.vad import EnergyVAD, VADProcessor


class Collect(Processor):
    def __init__(self):
        super().__init__()
        self.frames = []

    async def queue_frame(self, frame, direction=None):
        self.frames.append(frame)


def tone(secs: float, amp: float = 0.3) -> bytes:
    t = np.arange(int(16000 * secs)) / 16000
    return float_to_pcm(amp * np.sin(2 * np.pi * 220 * t))


def quiet(secs: float) -> bytes:
    return float_to_pcm(np.random.default_rng(0).normal(0, 0.002, int(16000 * secs)))


async def _run_vad(chunks: list[bytes], bot_speaking: bool):
    vad, sink = VADProcessor(EnergyVAD(), threshold=0.6, start_secs=0.2, stop_secs=0.5, interrupt_secs=0.45), Collect()
    pipe = Pipeline([vad, sink])
    session = RecordingSession(bot_speaking=bot_speaking)
    await pipe.start(session)
    for c in chunks:
        for i in range(0, len(c), 640):  # 20 ms chunks, like the browser sends
            await vad.process(AudioIn(c[i:i + 640]), None)
    await pipe.stop()
    return [type(f).__name__ for f in sink.frames if not isinstance(f, AudioIn)]


async def test_vad_finds_speech_and_its_end():
    kinds = await _run_vad([quiet(1.0), tone(0.6), quiet(1.0)], bot_speaking=False)
    assert kinds.count("UserStartedSpeaking") == 1 and kinds.count("UserStoppedSpeaking") == 1
    assert "Interruption" not in kinds


async def test_vad_barge_in_only_when_bot_speaks_and_speech_lasts():
    assert "Interruption" in await _run_vad([quiet(1.0), tone(0.8), quiet(0.6)], bot_speaking=True)
    # A short sound (a cough / a bump in the road) doesn't stop Bulbul.
    assert "Interruption" not in await _run_vad([quiet(1.0), tone(0.3), quiet(0.6)], bot_speaking=True)


async def test_turns_wait_for_the_whole_thought():
    turns, sink = TurnProcessor(settle_secs=0.05, final_wait_secs=0.3), Collect()
    pipe = Pipeline([turns, sink])
    await pipe.start(RecordingSession())
    await turns.queue_frame(UserStartedSpeaking())
    await turns.queue_frame(Transcript("I want to make", final=True))
    await turns.queue_frame(Transcript("chicken biryani", final=True))
    await turns.queue_frame(UserStoppedSpeaking())
    await wait_for(lambda: any(isinstance(f, UserTurn) for f in sink.frames))
    await pipe.stop()
    assert [f.text for f in sink.frames if isinstance(f, UserTurn)] == ["I want to make chicken biryani"]
    assert not meaningful("umm uh") and meaningful("yes")


async def test_tts_speaks_by_sentence_and_like_a_person():
    class Voice:
        rate = 24000

        def __init__(self):
            self.said = []

        async def synthesize(self, text):
            self.said.append(text)
            yield b"\x00\x00" * 10

    voice = Voice()
    tts, sink = TTSProcessor(voice), Collect()
    pipe = Pipeline([tts, sink])
    await pipe.start(RecordingSession())
    from voicecat.frames import BotText, BotTurnEnd

    for chunk in ["Hi. Your **cart** has 2", " items for ₹1,424. It weighs 1.5 kg", " in total."]:
        await tts.process(BotText(chunk), None)
    await tts.process(BotTurnEnd(), None)
    await pipe.stop()
    assert voice.said == ["Hi. Your cart has 2 items for 1,424 rupees.", "It weighs 1.5 kg in total."]
    assert speakable("- **Milk** 🥛 [link](http://x)") == "Milk link"


async def test_tts_drops_buffered_text_on_interruption():
    tts = TTSProcessor(None)
    pipe = Pipeline([tts, Collect()])
    await pipe.start(RecordingSession())
    from voicecat.frames import BotText

    await tts.process(BotText("This sentence is not finished yet"), None)
    await tts.queue_frame(Interruption())
    assert tts._buffer == ""
    await pipe.stop()


def test_resample_keeps_duration():
    pcm = tone(0.5)
    assert abs(len(resample(pcm, 16000, 24000)) / 2 / 24000 - 0.5) < 0.001


def test_provider_choice_best_first_free_last(monkeypatch):
    cfg = {"stt": {"order": ["deepgram", "whisper", "browser"]},
           "tts": {"order": ["deepgram", "cartesia", "elevenlabs", "browser"], "cartesia": {"voice": "v1"}}}
    for k in ("DEEPGRAM_API_KEY", "CARTESIA_API_KEY", "ELEVENLABS_API_KEY", "VOICECAT_STT", "VOICECAT_TTS"):
        monkeypatch.delenv(k, raising=False)
    import voicecat.providers as providers

    real_has = providers._has
    monkeypatch.setattr(providers, "_has", lambda pkg: False if pkg in ("faster_whisper", "kokoro_onnx") else real_has(pkg))
    c = choose(cfg)
    assert (c.stt, c.tts) == ("browser", "browser")
    assert c.report["stt"]["deepgram"] == "set DEEPGRAM_API_KEY"

    monkeypatch.setenv("DEEPGRAM_API_KEY", "x")
    monkeypatch.setenv("ELEVENLABS_API_KEY", "x")
    c = choose(cfg)
    assert (c.stt, c.tts) == ("deepgram", "deepgram")  # one Deepgram key covers both
    assert choose(cfg, want_tts="browser").tts == "browser"         # a session can ask for the free voice
    assert availability("tts", "cartesia", {"tts": {"cartesia": {"voice": ""}}})[0] is False


def test_mounted_router_session_with_browser_speech():
    """The router mounted in a host app (as Bulbul does): typed text in, browser-speech and
    Bulbul events out, and the caller's cookie forwarded to Bulbul."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from voicecat.api import voice_router

    bulbul = FakeBulbul([[(0.01, {"type": "token", "text": "Hello Asha, how can I help? "})]])
    seen_headers: list[dict] = []

    def factory(headers: dict) -> object:
        seen_headers.append(headers)
        return bulbul.client()

    app = FastAPI()
    app.include_router(voice_router(client_factory=factory), prefix="/api/voice")
    with TestClient(app) as tc:
        assert tc.get("/api/voice/status").json()["stt"]
        with tc.websocket_connect("/api/voice/ws?stt=browser&tts=browser", headers={"cookie": "s=1"}) as ws:
            ready = json.loads(ws.receive_text())
            assert ready["type"] == "ready" and ready["chat_id"] == "chat-1"
            assert (ready["stt"], ready["tts"]) == ("browser", "browser")
            ws.send_text(json.dumps({"type": "text", "text": "hi bulbul"}))
            seen = []
            while not any(e.get("type") == "speak" for e in seen):
                seen.append(json.loads(ws.receive_text()))
    speak = next(e for e in seen if e["type"] == "speak")
    assert speak["text"] == "Hello Asha, how can I help?"
    assert bulbul.requests[0] == ("runs", {"message": "hi bulbul", "voice": True})
    run = next(e for e in seen if e["type"] == "run")
    assert run == {"type": "run", "text": "hi bulbul", "answer": False}
    assert any(e["type"] == "bulbul" and e["event"]["type"] == "token" for e in seen)
    assert seen_headers[0].get("cookie") == "s=1"
