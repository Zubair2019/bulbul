"""voicecat as a mountable API: one WebSocket per voice session, plus a status endpoint.

voicecat has no server of its own. An app mounts the router, e.g. Bulbul at /api/voice:

    app.include_router(voice_router(bulbul_url="http://127.0.0.1:8000"), prefix="/api/voice")

    ws://host/api/voice/ws?chat=<bulbul chat id>&stt=<provider>&tts=<provider>

Client -> server: binary PCM16 mono 16 kHz (mic, ~20 ms chunks) and JSON:
    {"type": "text", "text": "..."}        typed input, or the browser's own speech recognition
    {"type": "speaking", "value": bool}    browser speech synthesis started / stopped
Server -> client: binary PCM16 mono 24 kHz (Bulbul's voice) and JSON events:
    ready, state, transcript, user_turn, run (a Bulbul request starts), bulbul (each Bulbul event,
    so the chat UI can show the reply and cards), bot_text, speak (browser TTS), interrupt, card,
    queued, error, bot_turn_end
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from voicecat.brain import BulbulBrain
from voicecat.frames import AudioIn, Transcript
from voicecat.bulbul_client import BulbulClient
from voicecat.pipeline import Pipeline
from voicecat.providers import choose, load_config
from voicecat.session import Session
from voicecat.stt import build_stt, shared_whisper
from voicecat.transport import TransportOut
from voicecat.tts import build_tts, shared_kokoro
from voicecat.turns import TurnProcessor
from voicecat.vad import EnergyVAD, SileroVAD, VADProcessor

log = logging.getLogger("voicecat")

# Headers passed on to Bulbul, so voice requests act as the same signed-in user.
_FORWARD = ("cookie", "authorization")

ClientFactory = Callable[[dict[str, str]], BulbulClient]


async def warm_up() -> None:
    """Load the local speech models once and run them on a tiny input, so the first
    conversation doesn't pay the model-loading and first-inference cost."""
    cfg = load_config()
    c = choose(cfg)
    try:
        if c.stt == "whisper":
            await shared_whisper(cfg["stt"]["whisper"]).transcribe(b"\x00\x00" * 16000)
        if c.tts == "kokoro":
            async for _ in shared_kokoro(cfg["tts"]["kokoro"]).synthesize("Ready."):
                pass
        log.info("voice warm-up done (stt=%s tts=%s)", c.stt, c.tts)
    except Exception as exc:  # noqa: BLE001
        log.warning("voice warm-up skipped: %s", exc)


def build_pipeline(cfg: dict, stt_name: str, tts_name: str, client: BulbulClient) -> tuple[Pipeline, str]:
    vcfg = cfg.get("vad") or {}
    analyzer = SileroVAD(vcfg["silero_model"]) if (
        vcfg.get("provider") == "silero" and SileroVAD.available(vcfg.get("silero_model"))) else EnergyVAD()
    tcfg = cfg.get("turns") or {}
    processors = [
        VADProcessor(analyzer, threshold=float(vcfg.get("threshold", 0.6)), start_secs=float(vcfg.get("start_secs", 0.2)),
                     stop_secs=float(vcfg.get("stop_secs", 0.8)), interrupt_secs=float(vcfg.get("interrupt_secs", 0.3))),
        build_stt(stt_name, (cfg.get("stt") or {}).get(stt_name) or {}),
        TurnProcessor(settle_secs=float(tcfg.get("settle_secs", 0.35)),
                      final_wait_secs=float(tcfg.get("final_wait_secs", 2.5))),
        BulbulBrain(client, fillers=cfg.get("fillers") or {}),
        build_tts(tts_name, (cfg.get("tts") or {}).get(tts_name) or {}),
        TransportOut(out_rate=24000),
    ]
    return Pipeline(processors), analyzer.name


def voice_router(bulbul_url: str | None = None, client_factory: ClientFactory | None = None) -> APIRouter:
    """`bulbul_url`: where Bulbul's HTTP API is (the host app itself, over loopback, so replies
    stream). `client_factory(headers)` replaces the client (tests)."""

    def make_client(headers: dict[str, str]) -> BulbulClient:
        if client_factory:
            return client_factory(headers)
        return BulbulClient(bulbul_url or "http://127.0.0.1:8000", headers=headers)

    router = APIRouter()

    @router.get("/status")
    async def status() -> dict:
        """Which speech providers a session would get, and why any is unavailable."""
        cfg = load_config()
        c = choose(cfg)
        return {"stt": c.stt, "tts": c.tts, "providers": c.report}

    @router.websocket("/ws")
    async def voice(ws: WebSocket, chat: str | None = None, stt: str | None = None, tts: str | None = None) -> None:
        await ws.accept()
        cfg = load_config()
        lock = asyncio.Lock()

        async def send_json(data: dict) -> None:
            async with lock:
                await ws.send_text(json.dumps(data, default=str))

        async def send_bytes(data: bytes) -> None:
            async with lock:
                await ws.send_bytes(data)

        client = make_client({k: v for k, v in ws.headers.items() if k.lower() in _FORWARD})
        pipeline: Pipeline | None = None
        try:
            choice = choose(cfg, stt, tts)
            chat_id = chat if chat and await client.chat_exists(chat) else await client.create_chat()
            session = Session(send_json=send_json, send_bytes=send_bytes, chat_id=chat_id)
            try:
                pipeline, vad_name = build_pipeline(cfg, choice.stt, choice.tts, client)
            except Exception as exc:  # noqa: BLE001 - a provider failed to load: fall back to the browser
                log.warning("voice provider setup failed (%s); using browser speech", exc)
                choice.stt, choice.tts = "browser", "browser"
                pipeline, vad_name = build_pipeline(cfg, "browser", "browser", client)
            session.stt_name, session.tts_name = choice.stt, choice.tts
            await pipeline.start(session)
            await send_json({"type": "ready", "chat_id": chat_id, "stt": choice.stt, "tts": choice.tts,
                             "vad": vad_name, "providers": choice.report, "out_rate": 24000})
            log.info("voice session started: chat=%s stt=%s tts=%s vad=%s", chat_id[:8], choice.stt, choice.tts, vad_name)
            head = pipeline.head
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if msg.get("bytes"):
                    await head.queue_frame(AudioIn(msg["bytes"]))
                elif msg.get("text"):
                    data = json.loads(msg["text"])
                    if data.get("type") == "text" and str(data.get("text", "")).strip():
                        await head.queue_frame(Transcript(text=data["text"].strip(), final=True, typed=True))
                    elif data.get("type") == "speaking":
                        session.bot_speaking = bool(data.get("value"))
                        if session.bot_speaking:
                            await session.set_state("speaking")
                        else:
                            await session.settle_state()
        except WebSocketDisconnect:
            pass
        except Exception as exc:  # noqa: BLE001
            log.exception("voice session failed")
            try:
                await send_json({"type": "error", "message": str(exc)})
            except Exception:  # noqa: BLE001
                pass
        finally:
            if pipeline:
                await pipeline.stop()
            await client.close()
            log.info("voice session ended")

    return router
