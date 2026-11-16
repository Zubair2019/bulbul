"""Per-connection state shared by the processors of one voice session."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

SendJson = Callable[[dict[str, Any]], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


@dataclass
class Session:
    send_json: SendJson
    send_bytes: SendBytes
    chat_id: str | None = None
    stt_name: str = ""
    tts_name: str = ""
    bot_speaking: bool = False   # audio (or browser speech) is playing on the client right now
    brain_busy: bool = False     # a Bulbul request is in flight
    state: str = "listening"     # listening | thinking | speaking (what the client shows)
    log_lines: list[str] = field(default_factory=list)

    async def set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            await self.send_json({"type": "state", "state": state})

    async def settle_state(self) -> None:
        """After speech ends: thinking if Bulbul is still working, else listening."""
        if not self.bot_speaking:
            await self.set_state("thinking" if self.brain_busy else "listening")


class RecordingSession(Session):
    """A session that records what would be sent to the client (tests)."""

    def __init__(self, **kw: Any):
        self.sent_json: list[dict] = []
        self.sent_audio: list[bytes] = []

        async def sj(data: dict) -> None:
            self.sent_json.append(json.loads(json.dumps(data, default=str)))

        async def sb(data: bytes) -> None:
            self.sent_audio.append(data)

        super().__init__(send_json=sj, send_bytes=sb, **kw)
