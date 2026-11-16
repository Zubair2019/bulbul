"""Test doubles: a fake Bulbul API (streams SSE events with delays) and a fake voice."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable

import httpx
import pytest

from voicecat.brain import BulbulBrain
from voicecat.bulbul_client import BulbulClient
from voicecat.pipeline import Pipeline
from voicecat.session import RecordingSession
from voicecat.transport import TransportOut
from voicecat.tts import TTSProcessor
from voicecat.turns import TurnProcessor


def tokens(text: str, delay: float = 0.01) -> list[tuple[float, dict]]:
    return [(delay, {"type": "token", "text": w + " "}) for w in text.split()]


class FakeBulbul:
    """Each POST /runs or /runs/resume plays the next script: a list of (delay_secs, event)."""

    def __init__(self, scripts: list[list[tuple[float, dict]]], pending: dict | None = None):
        self.scripts = list(scripts)
        self.pending = pending
        self.requests: list[tuple[str, dict]] = []
        self.finished = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/api/threads":
            return httpx.Response(201, json={"id": "chat-1"})
        if path.endswith("/messages"):
            return httpx.Response(200, json=[])
        if path.endswith("/state"):
            return httpx.Response(200, json={"pending": self.pending, "plan": []})
        if path.endswith("/runs") or path.endswith("/runs/resume"):
            body = json.loads(request.content or b"{}")
            self.requests.append((path.rsplit("/", 1)[-1], body))
            script = self.scripts.pop(0) if self.scripts else []
            self.pending = None  # like Bulbul: a new request answers / replaces what it waited on

            async def stream():
                yield b'data: {"type": "run_start", "run_id": "r"}\n\n'
                for delay, ev in script:
                    await asyncio.sleep(delay)
                    if ev.get("type") == "interrupt":
                        self.pending = {k: v for k, v in ev.items() if k != "type"}
                    yield f"data: {json.dumps(ev)}\n\n".encode()
                yield b'data: {"type": "run_end", "status": "done"}\n\n'
                self.finished += 1

            return httpx.Response(200, content=stream(), headers={"content-type": "text/event-stream"})
        if "/cancel" in path:
            self.requests.append(("cancel", {}))
            return httpx.Response(200, json={"cancelled": True})
        return httpx.Response(404)

    def client(self) -> BulbulClient:
        return BulbulClient("http://bulbul.test", http=httpx.AsyncClient(transport=httpx.MockTransport(self.handler)))


class FakeVoice:
    """10 ms of audio per character, produced quickly (like a fast cloud TTS)."""

    rate = 24000

    def __init__(self):
        self.said: list[str] = []

    async def synthesize(self, text: str):
        self.said.append(text)
        total = len(text) * 480  # bytes: 240 samples (10 ms) x 2 bytes
        for i in range(0, total, 4800):
            await asyncio.sleep(0.001)
            yield b"\x00\x01" * (min(4800, total - i) // 2)


async def wait_for(pred: Callable[[], bool], timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not pred():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


class Harness:
    def __init__(self, bulbul: FakeBulbul, fillers: dict | None = None):
        self.bulbul = bulbul
        self.voice = FakeVoice()
        self.session = RecordingSession(chat_id="chat-1", stt_name="test", tts_name="test")
        self.pipeline = Pipeline([
            TurnProcessor(settle_secs=0.01, final_wait_secs=0.05),
            BulbulBrain(bulbul.client(), fillers=fillers or {"shopping": "Let me check Instamart.",
                                                           "default": "One moment."}),
            TTSProcessor(self.voice),
            TransportOut(out_rate=24000, tail_secs=0.05),
        ])

    async def __aenter__(self):
        await self.pipeline.start(self.session)
        return self

    async def __aexit__(self, *exc):
        await self.pipeline.stop()

    async def put(self, frame) -> None:
        await self.pipeline.head.queue_frame(frame)

    def events(self, kind: str) -> list[dict]:
        return [e for e in self.session.sent_json if e.get("type") == kind]

    def spoken(self) -> str:
        return " ".join(e["text"] for e in self.events("bot_text"))


@pytest.fixture
def harness():
    return Harness
