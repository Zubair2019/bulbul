"""Frames: the units of data and control that flow through a voicecat pipeline.

Audio, transcripts and the bot's text travel as data frames, queued and processed in order.
System frames (speech started/stopped, interruption) skip the queues and reach every processor
immediately: that's what makes barge-in instant.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Frame:
    pass


@dataclass
class SystemFrame(Frame):
    """Handled immediately by each processor, ahead of anything queued."""


# ---------------------------------------------------------------- audio
@dataclass
class AudioIn(Frame):
    """Microphone audio from the client: PCM16 mono."""

    pcm: bytes
    rate: int = 16000


@dataclass
class AudioOut(Frame):
    """Speech for the client to play: PCM16 mono."""

    pcm: bytes
    rate: int = 24000


# ---------------------------------------------------------------- the user
@dataclass
class UserStartedSpeaking(SystemFrame):
    pass


@dataclass
class UserStoppedSpeaking(SystemFrame):
    pass


@dataclass
class Transcript(Frame):
    """What speech-to-text heard. `typed` = came as text from the client (keyboard or browser STT)."""

    text: str
    final: bool = True
    typed: bool = False


@dataclass
class UserTurn(Frame):
    """One complete thing the user said, ready for the brain."""

    text: str


# ---------------------------------------------------------------- the bot
@dataclass
class BotText(Frame):
    """Text the bot should say (streamed in chunks)."""

    text: str


@dataclass
class BotTurnEnd(Frame):
    """The bot finished this reply: speak whatever is still buffered."""


@dataclass
class Interruption(SystemFrame):
    """The user started talking over the bot: stop speaking now."""


# ---------------------------------------------------------------- client messages
@dataclass
class ClientEvent(Frame):
    """A JSON message for the client (state, captions, cards)."""

    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class ClientSystemEvent(SystemFrame):
    """A JSON message for the client that must not wait behind queued audio (e.g. "interrupt")."""

    data: dict[str, Any] = field(default_factory=dict)
