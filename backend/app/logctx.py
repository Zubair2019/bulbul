"""Chat/run IDs on every log line, so one ID finds everything about a conversation.

    INFO [chat=2534de8d run=91a0c3f2] app.graph.builder: supervisor decision (raw): ...

`chat` is the first 8 characters of the chat (thread) UUID, the same short ID the app shows next
to "Details"; `run` is one request within that chat. Grep for either.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar

_chat: ContextVar[str] = ContextVar("bulbul_chat", default="-")
_run: ContextVar[str] = ContextVar("bulbul_run", default="-")

FORMAT = "%(levelname)s [chat=%(chat)s run=%(run)s] %(name)s: %(message)s"


def short(uuid_str: str | None) -> str:
    return (uuid_str or "-")[:8]


class _ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.chat = short(_chat.get())
        record.run = short(_run.get())
        return True


def install() -> None:
    """Add the chat/run fields to the root logger's handlers (call once, after basicConfig)."""
    for handler in logging.getLogger().handlers:
        if not any(isinstance(f, _ContextFilter) for f in handler.filters):
            handler.addFilter(_ContextFilter())
            handler.setFormatter(logging.Formatter(FORMAT))


@contextmanager
def bind(*, chat: str | None = None, run: str | None = None):
    """Tag log lines inside this block (and tasks started from it) with the chat and/or run."""
    tokens = []
    if chat is not None:
        tokens.append((_chat, _chat.set(chat)))
    if run is not None:
        tokens.append((_run, _run.set(run)))
    try:
        yield
    finally:
        for var, tok in reversed(tokens):
            try:
                var.reset(tok)
            except ValueError:  # closed from another context (e.g. client disconnect): harmless
                pass
