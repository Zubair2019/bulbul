"""Langfuse tracing (optional: off unless LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY are set).

Shape of one trace, which is one chat turn (a new message, or the answer to a question card):

    chat-turn                  trace name; session = thread id, user = user id
    └─ handle-chat-turn        [agent] input = user text, output = reply or question asked
       └─ supervisor-graph     [chain] the LangGraph run (via the LangChain CallbackHandler)
          ├─ supervisor        [chain] node
          │  └─ decide-next-step         [generation]
          ├─ delegate          [chain] node
          │  └─ chef-agent               [agent] input = task brief, output = AgentResult
          │     └─ generate-chef-result  [generation] (one per model attempt; failures are ERROR)
          └─ respond           [chain] node
             └─ write-reply              [generation]

Turns of one conversation share a session, so the Sessions view replays the whole chat,
including the clarify / plan / approval round-trips.

Best-practice notes (https://langfuse.com/docs/observability/best-practices):
- Names are stable, verb-first, and never contain model names or ids; dynamic values go in metadata.
- Langfuse is configured once at startup after .env is loaded, and flushed on shutdown.
- Output-parser spans are dropped (pure noise, always leaves), and credential-like strings
  are masked out of every exported attribute.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.settings import Settings

log = logging.getLogger(__name__)

TRACE_NAME = "chat-turn"
ROOT_NAME = "handle-chat-turn"
GRAPH_RUN_NAME = "supervisor-graph"

# Generation names by agent (see `generation_name`).
_GENERATION_NAMES = {"supervisor": "decide-next-step", "responder": "write-reply", "memory": "update-memory"}

# Leaf spans that add nothing to a trace tree.
_NOISE_SPANS = {"PydanticOutputParser", "PydanticToolsParser", "JsonOutputParser", "JsonOutputKeyToolsParser"}

# Credential-looking strings that must never reach Langfuse, even if a user pastes one.
_SECRET_RE = re.compile(
    r"(sk-lf-[0-9a-f-]{20,}"            # Langfuse secret key
    r"|sk-ant-[A-Za-z0-9_\-]{20,}"      # Anthropic
    r"|sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"  # OpenAI
    r"|xai-[A-Za-z0-9]{20,}"            # xAI
    r"|AIza[0-9A-Za-z_\-]{30,}"         # Google
    r"|Bearer\s+[A-Za-z0-9._\-]{20,})"
)
REDACTED = "[REDACTED]"

_enabled = False


def generation_name(agent: str) -> str:
    return _GENERATION_NAMES.get(agent, f"generate-{agent}-result")


def agent_run_name(agent: str) -> str:
    # The LangChain handler types a chain as `agent` when its name contains "agent".
    return f"{agent}-agent"


def is_enabled() -> bool:
    return _enabled


# ---------------------------------------------------------------- client setup
def _should_export(span) -> bool:
    from langfuse.span_filter import is_default_export_span

    return is_default_export_span(span) and span.name not in _NOISE_SPANS


def _mask(text: str) -> str:
    return _SECRET_RE.sub(REDACTED, text)


def mask_otel_spans(*, params):
    """Export-stage masking: redact credential-like strings in any string attribute."""
    from langfuse.types import MaskOtelSpansResult, OtelSpanPatch

    patches = {}
    for ident, span in params.spans.items():
        changed: dict[str, Any] = {}
        for key, value in span.attributes.items():
            if isinstance(value, str) and _SECRET_RE.search(value):
                changed[key] = _mask(value)
            elif isinstance(value, (list, tuple)) and any(isinstance(v, str) and _SECRET_RE.search(v) for v in value):
                changed[key] = [_mask(v) if isinstance(v, str) else v for v in value]
        if changed:
            patches[ident] = OtelSpanPatch(set_attributes=changed)
    return MaskOtelSpansResult(span_patches=patches) if patches else None


def init_tracing(settings: Settings, release: str) -> bool:
    """Create the Langfuse client once. Must run after .env is loaded. Never raises."""
    global _enabled
    if not (settings.langfuse_public_key and settings.langfuse_secret_key):
        log.info("Langfuse tracing off (no LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY)")
        return False
    try:
        from langfuse import Langfuse

        Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url or None,  # None -> SDK default / LANGFUSE_BASE_URL
            environment=settings.langfuse_tracing_environment,
            release=release,
            should_export_span=_should_export,
            mask_otel_spans=mask_otel_spans,
        )
        _enabled = True
        log.info("Langfuse tracing on (environment=%s)", settings.langfuse_tracing_environment)
    except Exception as exc:  # noqa: BLE001 - tracing must never break the app
        log.warning("Langfuse tracing disabled: %s", exc)
        _enabled = False
    return _enabled


def shutdown_tracing() -> None:
    if not _enabled:
        return
    try:
        from langfuse import get_client

        get_client().shutdown()  # flushes queued spans
    except Exception as exc:  # noqa: BLE001
        log.warning("Langfuse shutdown failed: %s", exc)


def callbacks() -> list:
    """LangChain callbacks for one graph run (a fresh handler per run: safe under concurrency)."""
    if not _enabled:
        return []
    from langfuse.langchain import CallbackHandler

    return [CallbackHandler()]


def trace_url(trace_id: str | None) -> str | None:
    """Link to one turn in the Langfuse UI (None when tracing is off)."""
    if not (_enabled and trace_id):
        return None
    try:
        from langfuse import get_client

        return get_client().get_trace_url(trace_id=trace_id)
    except Exception:  # noqa: BLE001
        return None


def session_url(thread_id: str, any_trace_id: str | None) -> str | None:
    """Link to the whole chat (Langfuse session = chat ID), derived from one of its trace links."""
    url = trace_url(any_trace_id)
    return url.split("/traces/")[0] + f"/sessions/{thread_id}" if url else None


# ---------------------------------------------------------------- one chat turn
@dataclass
class Turn:
    trace_id: str | None = None
    _span: Any = None
    _output: Any = None
    _meta: dict = field(default_factory=dict)

    def finish(self, output: Any, **metadata: Any) -> None:
        """Record what the user saw at the end of the turn (reply, question, or error)."""
        self._output = output
        self._meta.update({k: v for k, v in metadata.items() if v is not None})

    def fail(self, message: str) -> None:
        if self._span is not None:
            self._span.update(level="ERROR", status_message=message)


@contextmanager
def chat_turn(
    *, user_id: str, thread_id: str, run_id: str, kind: str, user_text: str, model: str | None, release: str
) -> Iterator[Turn]:
    """Root observation for one turn. `kind` is "message" (new message) or "resume" (answering a card)."""
    turn = Turn()
    if not _enabled:
        yield turn
        return
    try:
        from langfuse import get_client, propagate_attributes

        # Propagation first, so the root observation itself carries user/session/tags too.
        prop = propagate_attributes(
            user_id=user_id,
            session_id=thread_id,
            trace_name=TRACE_NAME,
            version=release,
            tags=[kind, f"model:{model or 'auto'}"],
            metadata={"picked_model": model or "auto", "run_id": run_id},
        )
        prop.__enter__()
        cm = get_client().start_as_current_observation(
            as_type="agent", name=ROOT_NAME, input=user_text, metadata={"run_id": run_id, "kind": kind}
        )
        span = cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        log.warning("Langfuse: could not start trace: %s", exc)
        yield turn
        return

    turn._span, turn.trace_id = span, getattr(span, "trace_id", None)
    error: BaseException | None = None
    try:
        yield turn
    except BaseException as exc:
        error = exc
        raise
    finally:
        exc_info = (type(error), error, error.__traceback__) if error else (None, None, None)
        try:
            span.update(output=turn._output, metadata=turn._meta or None)
            cm.__exit__(*exc_info)
            prop.__exit__(*exc_info)
        except Exception as exc:  # noqa: BLE001
            log.debug("Langfuse: closing trace failed: %s", exc)
