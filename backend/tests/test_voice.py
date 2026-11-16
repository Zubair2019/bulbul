"""Live voice (voicecat) inside Bulbul: mounted at /api/voice, and a spoken turn reaches the graph
as a voice request whose reply streams back to the voice client (and so to the chat UI)."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.main import app, build_services
from app.schemas import SupervisorDecision
from app.settings import Settings
from tests.test_graph_flow import FakeRouter


async def test_voice_is_mounted_in_the_bulbul_api():
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        r = await client.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["stt"] and body["tts"] and "providers" in body


def test_a_spoken_turn_runs_through_bulbul_and_streams_back():
    from voicecat.api import voice_router
    from voicecat.bulbul_client import BulbulClient

    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond")]})
    stack = AsyncExitStack()

    def bulbul_client(headers: dict) -> BulbulClient:
        # The real Bulbul app, in-process (the backend itself uses loopback HTTP so replies stream).
        return BulbulClient("http://test", http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)))

    host = FastAPI()
    host.include_router(voice_router(client_factory=bulbul_client), prefix="/api/voice")
    with TestClient(host) as tc:
        async def setup() -> None:
            app.state.services = await build_services(Settings(database_url=""), stack, router=router)

        tc.portal.call(setup)
        try:
            with tc.websocket_connect("/api/voice/ws?stt=browser&tts=browser") as ws:
                ready = json.loads(ws.receive_text())
                assert ready["type"] == "ready" and ready["chat_id"]
                ws.send_text(json.dumps({"type": "text", "text": "what can you do?"}))
                seen: list[dict] = []
                while not any(e.get("type") == "bot_turn_end" for e in seen if e.get("type") != "state") or \
                        not any(e.get("type") == "bulbul" and e["event"]["type"] == "run_end" for e in seen):
                    seen.append(json.loads(ws.receive_text()))
        finally:
            tc.portal.call(stack.aclose)

    assert {"type": "run", "text": "what can you do?", "answer": False} in seen
    bulbul_types = [e["event"]["type"] for e in seen if e["type"] == "bulbul"]
    assert bulbul_types[0] == "run_start" and "token" in bulbul_types and bulbul_types[-1] == "run_end"
    assert any(e["type"] == "speak" and "Here you go." in e["text"] for e in seen)  # spoken back
    assert any(a == "voice_responder" for a, _ in router.calls)  # Bulbul knew it was a voice request
