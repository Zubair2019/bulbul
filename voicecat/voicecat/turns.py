"""Turn taking: decide when the user has finished a thought and hand it to the brain.

A turn ends when the user has stopped speaking and speech-to-text has delivered its final text,
plus a short settle time (so "I want to make... biryani" isn't split in two). Text that arrives
typed (keyboard, or the browser's own speech recognition) is a complete turn on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

from voicecat.frames import ClientEvent, Transcript, UserStartedSpeaking, UserStoppedSpeaking, UserTurn
from voicecat.pipeline import Direction, Processor

_FILLERS = {"uh", "um", "hmm", "mm", "ah", "er", "erm", "uhh", "umm", "hm", "mhm"}


def meaningful(text: str) -> bool:
    words = re.findall(r"[a-zA-Z0-9']+", text.lower())
    return bool(words) and not set(words) <= _FILLERS


class TurnProcessor(Processor):
    def __init__(self, settle_secs: float = 0.35, final_wait_secs: float = 2.5):
        super().__init__()
        self.settle_secs = settle_secs        # after the last final transcript
        self.final_wait_secs = final_wait_secs  # max wait for STT after speech stops
        self._parts: list[str] = []
        self._user_speaking = False
        self._timer: asyncio.Task | None = None

    async def on_stop(self) -> None:
        self._cancel_timer()

    def _cancel_timer(self) -> None:
        if self._timer and not self._timer.done():
            self._timer.cancel()
        self._timer = None

    def _schedule(self, delay: float) -> None:
        self._cancel_timer()
        self._timer = asyncio.create_task(self._emit_after(delay))

    async def _emit_after(self, delay: float) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.sleep(delay)
            if not self._user_speaking:
                await self._emit()

    async def _emit(self) -> None:
        text = " ".join(p.strip() for p in self._parts if p.strip()).strip()
        self._parts = []
        if meaningful(text):
            await self.push(UserTurn(text))

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, UserStartedSpeaking):
            self._user_speaking = True
            self._cancel_timer()
        elif isinstance(frame, UserStoppedSpeaking):
            self._user_speaking = False
            self._schedule(self.settle_secs if self._parts else self.final_wait_secs)
        elif isinstance(frame, Transcript):
            if frame.typed:
                self._parts.append(frame.text)
                await self.push(ClientEvent({"type": "transcript", "text": frame.text, "final": True}))
                self._cancel_timer()
                await self._emit()
                return
            await self.push(ClientEvent({"type": "transcript", "text": frame.text, "final": frame.final}))
            if frame.final and frame.text.strip():
                self._parts.append(frame.text)
                if not self._user_speaking:
                    self._schedule(self.settle_secs)
            return
        await self.push(frame, direction)
