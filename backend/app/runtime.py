"""Runs the graph for one request and converts it into SSE events.

Event contract (mirrored in frontend/src/lib/events.ts):
  run_start      {run_id}
  plan_update    {plan: PlanStep[]}
  agent_start    {step_id, agent, goal}
  agent_end      {step_id, agent, status, summary}
  token          {text}
  suggestions    {items: string[]}   quick replies for the reply being streamed
  attachment     {title, body}       long content (e.g. a recipe) shown as a collapsible card
  model_fallback {agent, from, to, reason, reset?}
  interrupt      {kind: clarify|confirm_plan|approve, ...payload}
  error          {message}
  run_end        {status: done|interrupted|cancelled|failed}
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from langchain_core.messages import AIMessage

from app import logctx, tracing
from app.db.repo import Repo
from app.settings import Settings

log = logging.getLogger(__name__)

_cancel_flags: dict[str, asyncio.Event] = {}


def cancel_run(run_id: str) -> bool:
    flag = _cancel_flags.get(run_id)
    if flag:
        flag.set()
    return flag is not None


def sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str, ensure_ascii=False)}\n\n"


def run_config(settings: Settings, user_id: str, thread_id: str, run_id: str, model: str | None = None) -> dict:
    return {
        # `model`: catalog id the user picked in the UI (None = each agent's configured chain).
        "configurable": {"thread_id": thread_id, "user_id": user_id, "model": model},
        "callbacks": tracing.callbacks(),  # user/session/tags are set by tracing.chat_turn
        "metadata": {"run_id": run_id, "picked_model": model or "auto"},
        "run_name": tracing.GRAPH_RUN_NAME,
        "recursion_limit": 100,
    }


def _interrupt_text(payload: dict) -> str:
    kind = payload.get("kind")
    if kind == "clarify":
        return "\n".join(q["text"] for q in payload.get("questions", []))
    if kind == "confirm_plan":
        return "\n".join(f"{i + 1}. {p['agent']}: {p['goal']}" for i, p in enumerate(payload.get("plan", [])))
    if kind == "approve":
        return "\n".join(a["description"] for a in payload.get("actions", []))
    return ""


async def stream_run(
    graph,
    repo: Repo,
    settings: Settings,
    user_id: str,
    thread_id: str,
    graph_input: Any,
    model: str | None = None,
    *,
    kind: str = "message",
    user_text: str = "",
) -> AsyncIterator[str]:
    """`kind`/`user_text` describe the turn for tracing: "message" + the text, or "resume" + the answer."""
    run_id = await repo.start_run(thread_id)
    flag = _cancel_flags[run_id] = asyncio.Event()
    with logctx.bind(chat=thread_id, run=run_id), \
            tracing.chat_turn(user_id=user_id, thread_id=thread_id, run_id=run_id, kind=kind, user_text=user_text,
                              model=model, release=settings.app_version) as turn:
        log.info("run started (%s): %s", kind, (user_text or "")[:80])
        if turn.trace_id:
            await repo.set_run_trace(run_id, turn.trace_id)
        frames = _run_graph(graph, repo, settings, user_id, thread_id, graph_input, model, run_id, flag, turn)
        try:
            async for frame in frames:
                yield frame
        except GeneratorExit:  # the browser went away mid-run
            turn.finish(None, outcome="client_disconnected")
            raise
        finally:
            await frames.aclose()


async def _run_graph(graph, repo, settings, user_id, thread_id, graph_input, model, run_id, flag, turn):
    status, error = "done", None
    meta = {"trace_id": turn.trace_id} if turn.trace_id else {}
    yield sse({"type": "run_start", "run_id": run_id, **meta})

    stream = graph.astream(
        graph_input, run_config(settings, user_id, thread_id, run_id, model), stream_mode=["custom", "updates"]
    )
    try:
        async for mode, chunk in stream:
            if flag.is_set():
                status = "cancelled"
                break
            if mode == "custom":
                yield sse(chunk)
                continue
            # mode == "updates": {node_name: update} or {"__interrupt__": (Interrupt, ...)}
            if "__interrupt__" in chunk:
                payload = dict(chunk["__interrupt__"][0].value)
                status = "interrupted"
                text = _interrupt_text(payload)
                turn.finish(text, outcome=f"asked:{payload['kind']}")
                await repo.add_message(thread_id, "assistant", text, kind=payload["kind"], meta={**payload, **meta})
                yield sse({"type": "interrupt", **payload})
            elif "respond" in chunk:
                msgs = (chunk["respond"] or {}).get("messages", [])
                final = next((m for m in reversed(msgs) if isinstance(m, AIMessage)), None)
                if final is not None:
                    text = final.content if isinstance(final.content, str) else str(final.content)
                    turn.finish(text, outcome="replied")
                    extra = {k: chunk["respond"][k] for k in ("suggestions", "attachments") if chunk["respond"].get(k)}
                    await repo.add_message(thread_id, "assistant", text, meta={**meta, **extra} or None)
    except Exception as exc:  # noqa: BLE001
        log.exception("Run %s failed", run_id)
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        turn.fail(error)
        turn.finish("Something went wrong while handling that.", outcome="failed")
        await repo.add_message(thread_id, "assistant", "Something went wrong while handling that.", kind="error",
                               meta={"error": error})
        yield sse({"type": "error", "message": error})
    finally:
        # Close the graph stream explicitly when we stop early (cancel / disconnect), so LangGraph
        # ends its runs now and their trace spans are exported instead of left open.
        await stream.aclose()
        _cancel_flags.pop(run_id, None)
        if status == "cancelled":
            turn.finish(None, outcome="cancelled")
        await repo.end_run(run_id, status, error)

    yield sse({"type": "run_end", "status": status})
