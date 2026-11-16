"""Model router: builds chat models from config and runs them with ordered fallbacks.

Agents never talk to a provider SDK directly. They call:
  - `structured(agent, messages, Schema)`  -> validated pydantic object
  - `stream_text(agent, messages)`         -> async iterator of text chunks

Both walk the agent's chain from models.yaml. If the run's config carries
`configurable.model` (the model the user picked in the UI), that catalog model is
tried first and the agent's chain follows it as the fallback. A model is skipped on any error,
timeout, or (for structured calls) output that fails schema validation.

Two latency guards:
  - A model that answers "quota exhausted" (429) is skipped until the provider says the quota
    resets (else for `policy.quota_cooldown_s`), so each call doesn't pay for the same refusal.
  - An entry with `hedge_after_s` gets the same request sent a second time if the first
    hasn't answered by then (for streams: hasn't sent its first text); the faster one wins.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol, TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration
from langchain_core.runnables import RunnableSequence
from pydantic import BaseModel

from app.llm.config import ModelEntry, ModelsConfig
from app.tracing import generation_name


def picked_model_id() -> str | None:
    """The model the user picked for the current graph run, if any."""
    try:
        from langgraph.config import get_config

        return (get_config().get("configurable") or {}).get("model")
    except RuntimeError:  # called outside a graph run
        return None

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
R = TypeVar("R")

# Called when the router moves from one model to the next.
FallbackHook = Callable[[dict[str, Any]], None]


class AllModelsFailed(RuntimeError):
    pass


class Router(Protocol):
    async def structured(
        self, agent: str, messages: list[BaseMessage], schema: type[T], on_fallback: FallbackHook | None = None
    ) -> T: ...

    def stream_text(
        self, agent: str, messages: list[BaseMessage], on_fallback: FallbackHook | None = None
    ) -> AsyncIterator[str]: ...

    async def chat(
        self, agent: str, messages: list[BaseMessage], tools: list, on_fallback: FallbackHook | None = None
    ) -> AIMessage: ...


def build_chat_model(entry: ModelEntry, retries: int) -> BaseChatModel:
    """Instantiate a LangChain chat model for one config entry (imports are lazy)."""
    p = dict(entry.params)
    if entry.provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        return ChatGoogleGenerativeAI(model=entry.model, timeout=entry.timeout_s, max_retries=retries, **p)
    if entry.provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(model=entry.model, timeout=entry.timeout_s, max_retries=retries, **p)
    if entry.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        p.setdefault("max_tokens", 16000)
        return ChatAnthropic(model=entry.model, timeout=entry.timeout_s, max_retries=retries, **p)
    if entry.provider == "xai":
        from langchain_xai import ChatXAI

        return ChatXAI(model=entry.model, timeout=entry.timeout_s, max_retries=retries, **p)
    raise ValueError(f"Unsupported provider {entry.provider}")


def _text_of(chunk: Any) -> str:
    content = getattr(chunk, "content", "")
    if isinstance(content, str):
        return content
    # Some providers return a list of content blocks; keep only text.
    parts = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _is_quota_error(exc: BaseException) -> bool:
    """True for "quota exhausted / rate limited" refusals (HTTP 429), which repeat until reset."""
    text = f"{type(exc).__name__}: {exc}"
    return "RESOURCE_EXHAUSTED" in text or "RateLimit" in text or "429" in text.split("{", 1)[0]


_RETRY_IN = re.compile(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", re.I)


def _retry_after(exc: BaseException) -> float | None:
    """Seconds until a quota resets, when the provider says ("Please retry in 18h45m50s" for
    Gemini's daily cap, "35.8s" for its per-minute one)."""
    m = _RETRY_IN.search(str(exc))
    if not m or not any(m.groups()):
        return None
    h, mins, s = (float(g) if g else 0.0 for g in m.groups())
    return h * 3600 + mins * 60 + s


async def _hedged(make: Callable[[bool], Awaitable[R]], after: float | None,
                  discard: Callable[[R], None] | None = None) -> R:
    """Run `make(False)`. If it hasn't finished after `after` seconds, also run `make(True)` (the
    hedge) and return whichever succeeds first; the other is cancelled. Raises only if both fail.

    Model latency has a long tail (Flash-Lite: ~1.3 s median, 6-11 s at p90 under load), and a
    second identical request usually beats a stuck first one.
    """
    first = asyncio.ensure_future(make(False))
    tasks = {first}
    try:
        if not after:
            return await first
        done, _ = await asyncio.wait(tasks, timeout=after)
        if done:
            return first.result()
        log.info("No answer after %.1fs: sending the same request again (hedge)", after)
        tasks.add(asyncio.ensure_future(make(True)))
        pending: set = set(tasks)
        error: BaseException | None = None
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            winners = [t for t in done if not t.cancelled() and t.exception() is None]
            if winners:
                for extra in winners[1:]:
                    if discard:
                        discard(extra.result())
                return winners[0].result()
            error = error or next(t.exception() for t in done if not t.cancelled())
        raise error or RuntimeError("hedged call failed")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()


async def _first_text(stream: AsyncIterator[str]) -> tuple[AsyncIterator[str], str | None]:
    """Wait for a stream's first text; returns the stream (to keep reading) and that text."""
    async for text in stream:
        return stream, text
    return stream, None


def _close_stream(opened: tuple[AsyncIterator[str], str | None]) -> None:
    aclose = getattr(opened[0], "aclose", None)
    if aclose:
        asyncio.ensure_future(aclose())


class ModelRouter:
    def __init__(self, config: ModelsConfig):
        self.config = config
        self._cache: dict[str, BaseChatModel] = {}
        self._cooldown: dict[str, float] = {}  # model label -> monotonic time it may be tried again

    def _model(self, entry: ModelEntry) -> BaseChatModel:
        key = f"{entry.label}|{sorted(entry.params.items())}|{entry.timeout_s}"
        if key not in self._cache:
            self._cache[key] = build_chat_model(entry, self.config.policy.retries_per_model)
        return self._cache[key]

    def _chain(self, agent: str) -> list[ModelEntry]:
        chain = self.config.chain_for(agent)
        picked = picked_model_id()
        choice = self.config.catalog_model(picked) if picked else None
        if choice and self.config.provider_available(choice.provider):
            chain = [choice, *(e for e in chain if e.label != choice.label)]
        if not chain:
            raise AllModelsFailed(
                f"No usable models for agent '{agent}'. Set at least one provider API key in .env."
            )
        now = time.monotonic()
        live = [e for e in chain if self._cooldown.get(e.label, 0) <= now]
        # Everything is cooling down: try anyway rather than fail outright, soonest reset first.
        return live or sorted(chain, key=lambda e: self._cooldown.get(e.label, 0))

    def _failed(self, entry: ModelEntry, agent: str, exc: BaseException) -> None:
        log.warning("Model %s failed for %s: %s", entry.label, agent, exc)
        if _is_quota_error(exc):
            secs = _retry_after(exc) or self.config.policy.quota_cooldown_s
            self._cooldown[entry.label] = time.monotonic() + secs
            log.warning("Model %s is out of quota: skipping it for %.0fs", entry.label, secs)

    @staticmethod
    def _attempt_config(agent: str, chain: list[ModelEntry], i: int, hedge: bool = False) -> dict:
        """Run name + metadata for one model attempt (shown on the Langfuse generation)."""
        meta: dict[str, Any] = {"agent": agent, "model_attempt": i + 1, "model_chain": [e.label for e in chain]}
        if i:
            meta["fallback_from"] = chain[i - 1].label
        if hedge:
            meta["hedge"] = True
        return {"run_name": generation_name(agent), "metadata": meta}

    async def _invoke_structured(self, entry: ModelEntry, messages, schema, config: dict):
        runnable = self._model(entry).with_structured_output(schema)
        steps = runnable.steps if isinstance(runnable, RunnableSequence) else []
        parser = steps[-1] if len(steps) == 2 else None
        if parser is not None and hasattr(parser, "parse_result"):
            # Call the model step directly so the generation carries our run name, then parse
            # without a separate (noisy) parser run. Same result as invoking the sequence.
            msg = await steps[0].ainvoke(messages, config=config)
            return parser.parse_result([ChatGeneration(message=msg)])
        return await runnable.ainvoke(messages, config=config)

    async def structured(self, agent, messages, schema, on_fallback=None):
        errors: list[str] = []
        chain = self._chain(agent)
        for i, entry in enumerate(chain):

            async def attempt(hedge: bool, entry=entry, i=i):
                result = await self._invoke_structured(
                    entry, messages, schema, self._attempt_config(agent, chain, i, hedge))
                if isinstance(result, dict):
                    result = schema.model_validate(result)
                if not isinstance(result, schema):
                    raise ValueError(f"invalid structured output: {result!r}")
                return result

            try:
                return await _hedged(attempt, entry.hedge_after_s)
            except Exception as exc:  # noqa: BLE001 - any failure moves to the next model
                errors.append(f"{entry.label}: {type(exc).__name__}: {exc}")
                self._failed(entry, agent, exc)
                if on_fallback and i + 1 < len(chain):
                    on_fallback({"from": entry.label, "to": chain[i + 1].label, "reason": type(exc).__name__})
        raise AllModelsFailed("; ".join(errors))

    async def chat(self, agent, messages, tools, on_fallback=None) -> AIMessage:
        """One tool-calling turn (the model may answer or request tool calls), with fallbacks."""
        errors: list[str] = []
        chain = self._chain(agent)
        for i, entry in enumerate(chain):
            model = self._model(entry).bind_tools(tools) if tools else self._model(entry)

            def attempt(hedge: bool, model=model, i=i):
                return model.ainvoke(messages, config=self._attempt_config(agent, chain, i, hedge))

            try:
                return await _hedged(attempt, entry.hedge_after_s)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry.label}: {type(exc).__name__}: {exc}")
                self._failed(entry, agent, exc)
                if on_fallback and i + 1 < len(chain):
                    on_fallback({"from": entry.label, "to": chain[i + 1].label, "reason": type(exc).__name__})
        raise AllModelsFailed("; ".join(errors))

    async def _texts(self, entry: ModelEntry, messages, config: dict) -> AsyncIterator[str]:
        async for chunk in self._model(entry).astream(messages, config=config):
            text = _text_of(chunk)
            if text:
                yield text

    async def stream_text(self, agent, messages, on_fallback=None):
        errors: list[str] = []
        chain = self._chain(agent)
        for i, entry in enumerate(chain):
            started = False
            try:
                # The hedge races on the first text: whichever stream starts talking first wins.
                stream, first = await _hedged(
                    lambda hedge, entry=entry, i=i: _first_text(
                        self._texts(entry, messages, self._attempt_config(agent, chain, i, hedge))),
                    entry.hedge_after_s, discard=_close_stream)
                if first is not None:
                    started = True
                    yield first
                    async for text in stream:
                        yield text
                return
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{entry.label}: {type(exc).__name__}: {exc}")
                log.warning("Model %s failed mid-stream=%s for %s: %s", entry.label, started, agent, exc)
                self._failed(entry, agent, exc)
                if on_fallback and i + 1 < len(chain):
                    # reset=True tells the UI to discard the partial text it already showed.
                    on_fallback(
                        {"from": entry.label, "to": chain[i + 1].label, "reason": type(exc).__name__, "reset": started}
                    )
        raise AllModelsFailed("; ".join(errors))
