"""Processors and pipelines.

    transport-in -> VAD -> STT -> turns -> brain -> TTS -> transport-out

Each processor has its own queue and task. Data frames are queued; system frames are handled
right away, so an Interruption overtakes everything. On an Interruption a processor drops its
queued frames, and if it is `interruptible` (TTS) cancels the work in progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from enum import Enum
from typing import TYPE_CHECKING

from voicecat.frames import Frame, Interruption, SystemFrame

if TYPE_CHECKING:
    from voicecat.session import Session

log = logging.getLogger(__name__)


class Direction(Enum):
    DOWN = 1
    UP = 2


class Processor:
    interruptible = False  # cancel in-progress work on interruption (only output-side processors)

    def __init__(self) -> None:
        self.next: Processor | None = None
        self.prev: Processor | None = None
        self.session: Session | None = None
        self._queue: asyncio.Queue[tuple[Frame, Direction]] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    @property
    def name(self) -> str:
        return type(self).__name__

    # ------------------------------------------------------------ lifecycle
    async def start(self, session: Session) -> None:
        self.session = session
        self._task = asyncio.create_task(self._loop(), name=f"voicecat:{self.name}")
        await self.on_start()

    async def stop(self) -> None:
        await self.on_stop()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def on_start(self) -> None: ...

    async def on_stop(self) -> None: ...

    async def on_interrupt(self) -> None: ...

    # ------------------------------------------------------------ frames
    async def queue_frame(self, frame: Frame, direction: Direction = Direction.DOWN) -> None:
        if isinstance(frame, SystemFrame):
            if isinstance(frame, Interruption):
                await self._interrupt()
            await self.process(frame, direction)
        else:
            self._queue.put_nowait((frame, direction))

    async def process(self, frame: Frame, direction: Direction) -> None:
        """Override to handle frames; by default pass everything along."""
        await self.push(frame, direction)

    async def push(self, frame: Frame, direction: Direction = Direction.DOWN) -> None:
        target = self.next if direction == Direction.DOWN else self.prev
        if target is not None:
            await target.queue_frame(frame, direction)

    async def _loop(self) -> None:
        while True:
            frame, direction = await self._queue.get()
            try:
                await self.process(frame, direction)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad frame must not kill the session
                log.exception("%s failed on %s", self.name, type(frame).__name__)

    async def _interrupt(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
        if self.interruptible and self._task and self._task is not asyncio.current_task():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
            self._task = asyncio.create_task(self._loop(), name=f"voicecat:{self.name}")
        await self.on_interrupt()


class Pipeline:
    def __init__(self, processors: list[Processor]):
        self.processors = processors
        for a, b in zip(processors, processors[1:]):
            a.next, b.prev = b, a

    @property
    def head(self) -> Processor:
        return self.processors[0]

    async def start(self, session: Session) -> None:
        for p in self.processors:
            await p.start(session)

    async def stop(self) -> None:
        for p in reversed(self.processors):
            await p.stop()
