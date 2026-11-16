"""Output side of the WebSocket transport (the input side is the socket reader in api.py).

Sends speech as binary PCM16 mono at `out_rate` and everything else as JSON. Tracks whether the
bot is audibly speaking (from the duration of audio sent) so barge-in knows when it applies.
On an Interruption, queued audio is dropped here and the client is told to flush its buffer.
"""

from __future__ import annotations

import asyncio
import contextlib

from voicecat.audio import duration_secs, resample
from voicecat.frames import AudioOut, BotTurnEnd, ClientEvent, ClientSystemEvent, Interruption
from voicecat.pipeline import Direction, Processor


class TransportOut(Processor):
    def __init__(self, out_rate: int = 24000, tail_secs: float = 0.3):
        super().__init__()
        self.out_rate = out_rate
        self.tail_secs = tail_secs  # client jitter buffer / playback tail
        self._speaking_until = 0.0
        self._watcher: asyncio.Task | None = None

    async def on_stop(self) -> None:
        if self._watcher:
            self._watcher.cancel()

    async def process(self, frame, direction: Direction) -> None:
        s = self.session
        if isinstance(frame, AudioOut):
            pcm = resample(frame.pcm, frame.rate, self.out_rate)
            now = asyncio.get_running_loop().time()
            self._speaking_until = max(now, self._speaking_until) + duration_secs(pcm, self.out_rate)
            await s.send_bytes(pcm)
            if not s.bot_speaking:
                s.bot_speaking = True
                await s.set_state("speaking")
            if not self._watcher or self._watcher.done():
                self._watcher = asyncio.create_task(self._watch())
        elif isinstance(frame, Interruption):
            self._speaking_until = 0.0
            await s.send_json({"type": "interrupt"})
            s.bot_speaking = False
            await s.settle_state()
        elif isinstance(frame, (ClientEvent, ClientSystemEvent)):
            await s.send_json(frame.data)
        elif isinstance(frame, BotTurnEnd):
            await s.send_json({"type": "bot_turn_end"})
            # Everything for this reply has been sent; if nothing is playing, we're listening again
            # (if speech is playing, the watcher settles the state when it ends).
            await s.settle_state()

    async def _watch(self) -> None:
        loop = asyncio.get_running_loop()
        with contextlib.suppress(asyncio.CancelledError):
            while loop.time() < self._speaking_until + self.tail_secs:
                await asyncio.sleep(0.05)
            if self.session.bot_speaking and self.session.tts_name != "browser":
                self.session.bot_speaking = False
                await self.session.settle_state()
