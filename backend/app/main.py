"""FastAPI app: threads, messages, and streaming runs over SSE."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from app.agents.registry import load_registry
from app import logctx, memory, tracing
from app.integrations.crypto import TokenCipher
from app.integrations.swiggy import SwiggyIntegration
from app.db.repo import MemoryRepo, PostgresRepo, Repo
from app.graph.builder import build_graph, load_persona
from app.llm.config import load_models_config
from app.llm.router import ModelRouter, Router
from app.runtime import cancel_run, run_config, stream_run
from app.settings import BACKEND_DIR, Settings, get_settings
from app.tracing import init_tracing, shutdown_tracing

load_dotenv(BACKEND_DIR.parent / ".env")  # provider SDKs read their API keys from os.environ
load_dotenv(BACKEND_DIR / ".env")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logctx.install()  # every line gets [chat=... run=...]
log = logging.getLogger("bulbul")


async def build_services(
    settings: Settings, stack: AsyncExitStack, router: Router | None = None, swiggy_client_factory=None
) -> dict[str, Any]:
    models_cfg = load_models_config(settings.models_config)
    router = router or ModelRouter(models_cfg)

    if settings.database_url:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from langgraph.store.postgres.aio import AsyncPostgresStore
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        pool = await stack.enter_async_context(
            AsyncConnectionPool(
                conninfo=settings.database_url,
                max_size=10,
                open=False,
                kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            )
        )
        checkpointer, store = AsyncPostgresSaver(pool), AsyncPostgresStore(pool)
        await checkpointer.setup()
        await store.setup()
        repo: Repo = PostgresRepo(pool)
        storage = "postgres"
    else:
        from langgraph.checkpoint.memory import InMemorySaver
        from langgraph.store.memory import InMemoryStore

        log.warning("DATABASE_URL not set: using in-memory storage (data is lost on restart)")
        checkpointer, store, repo, storage = InMemorySaver(), InMemoryStore(), MemoryRepo(), "memory"

    await repo.init(settings.dev_user_id)
    swiggy = SwiggyIntegration(
        base_url=settings.swiggy_base_url,
        redirect_uri=f"{settings.public_url.rstrip('/')}/api/integrations/swiggy/callback",
        repo=repo, cipher=TokenCipher(settings.bulbul_secret_key), client_factory=swiggy_client_factory,
    )
    integrations = {"swiggy": swiggy}
    registry = load_registry(settings.agents_dir, router, integrations)
    graph = build_graph(router, registry, settings.max_supervisor_steps, checkpointer=checkpointer, store=store,
                        persona=load_persona(settings.persona_file))
    return {"graph": graph, "repo": repo, "registry": registry, "models": models_cfg, "storage": storage,
            "integrations": integrations, "store": store}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    init_tracing(settings, release=settings.app_version)  # after load_dotenv above
    warm = None
    if settings.voice_enabled:
        from voicecat.api import warm_up

        warm = asyncio.create_task(warm_up())  # load speech models without delaying startup
    try:
        async with AsyncExitStack() as stack:
            app.state.services = await build_services(settings, stack)
            yield
    finally:
        if warm and not warm.done():
            warm.cancel()
        shutdown_tracing()  # flush queued traces


app = FastAPI(title="Bulbul API", version=get_settings().app_version, lifespan=lifespan)


def _mount_voice(app: FastAPI, settings: Settings) -> None:
    """Live voice (voicecat): ws /api/voice/ws, GET /api/voice/status. The code lives in
    ../voicecat; it talks to Bulbul only through this HTTP API, like the web app."""
    if not settings.voice_enabled:
        return
    from voicecat.api import voice_router

    url = settings.voice_bulbul_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
    app.include_router(voice_router(bulbul_url=url), prefix="/api/voice")


_mount_voice(app, get_settings())
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)


_CHAT_IN_PATH = re.compile(r"^/api/threads/([0-9a-fA-F-]{36})")


@app.middleware("http")
async def tag_logs_with_chat(request: Request, call_next):
    """Requests about a chat tag their log lines with its ID (runs add their run ID too)."""
    m = _CHAT_IN_PATH.match(request.url.path)
    with logctx.bind(chat=m.group(1) if m else None):
        return await call_next(request)


# ---------------------------------------------------------------- dependencies
def services(request: Request) -> dict[str, Any]:
    return request.app.state.services


def current_user(settings: Settings = Depends(get_settings)) -> str:
    """Phase 1: single dev user. Replace with real auth (JWT/session) later; nothing else changes."""
    return settings.dev_user_id


async def owned_thread(thread_id: str, user: str = Depends(current_user), svc=Depends(services)) -> dict:
    thread = await svc["repo"].get_thread(user, thread_id)
    if thread is None:
        raise HTTPException(404, "Thread not found")
    return thread


def _pending_interrupt(snapshot) -> dict | None:
    for task in getattr(snapshot, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            return dict(intr.value)
    return None


def _sse_response(gen) -> StreamingResponse:
    return StreamingResponse(
        gen, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


# ---------------------------------------------------------------- routes
class CreateThread(BaseModel):
    title: str | None = None


class SendMessage(BaseModel):
    message: str = Field(min_length=1, max_length=20000)
    model: str | None = None  # catalog id from GET /api/models; None = Auto
    voice: bool = False       # the reply will be spoken (voicecat): write for listening, e.g. while driving


def _checked_model(model: str | None, svc: dict) -> str | None:
    if not model:
        return None
    cfg = svc["models"]
    entry = cfg.catalog_model(model)
    if entry is None:
        raise HTTPException(400, f"Unknown model '{model}'")
    if not cfg.provider_available(entry.provider):
        raise HTTPException(400, f"Model '{model}' is unavailable: no API key for {entry.provider}")
    return model


class Resume(BaseModel):
    # clarify:      {"answers": {"servings": "4", "_free_text": "..."}}
    # confirm_plan: {"approved": true, "feedback": "..."}
    # approve:      {"approved": false}
    value: dict
    model: str | None = None


@app.get("/api/health")
async def health(svc=Depends(services)):
    cfg = svc["models"]
    return {
        "status": "ok",
        "storage": svc["storage"],
        "providers": {p: cfg.provider_available(p) for p in cfg.providers},
        "agents": list(svc["registry"].agents),
    }


@app.get("/api/models")
async def list_models(svc=Depends(services)):
    """Models for the UI's picker. `available` is false when the provider has no API key."""
    cfg = svc["models"]
    return [
        {"id": m.id, "name": m.name, "description": m.description, "provider": m.provider,
         "available": cfg.provider_available(m.provider)}
        for m in cfg.catalog
    ]


@app.get("/api/memory")
async def list_memory(user: str = Depends(current_user), svc=Depends(services)):
    """What Bulbul remembers about the user (learned from what they said)."""
    return [{k: f[k] for k in ("key", "text", "category", "updated_at")} for f in await memory.load(svc["store"], user)]


@app.delete("/api/memory/{key}")
async def forget_memory(key: str, user: str = Depends(current_user), svc=Depends(services)):
    if not await memory.forget(svc["store"], user, key):
        raise HTTPException(404, "Nothing remembered under that key")
    return {"forgotten": key}


@app.get("/api/integrations")
async def list_integrations(user: str = Depends(current_user), svc=Depends(services)):
    return [await i.status(user) for i in svc["integrations"].values()]


def _integration(svc: dict, name: str):
    integ = svc["integrations"].get(name)
    if integ is None or not integ.enabled:
        raise HTTPException(404, f"Integration '{name}' is not configured")
    return integ


@app.post("/api/integrations/{name}/connect")
async def connect_integration(name: str, user: str = Depends(current_user), svc=Depends(services)):
    """Start sign-in: the browser should navigate to the returned URL."""
    integ = _integration(svc, name)
    try:
        return {"url": await integ.start_auth(user)}
    except Exception as exc:  # noqa: BLE001
        log.warning("%s connect failed: %s", name, exc)
        raise HTTPException(502, f"Could not reach {name}: {exc}") from exc


@app.get("/api/integrations/{name}/callback")
async def integration_callback(name: str, request: Request, settings: Settings = Depends(get_settings),
                               svc=Depends(services)):
    """OAuth redirect target. Exchanges the code, then sends the browser back to the app."""
    integ = _integration(svc, name)
    back = settings.public_url.rstrip("/")
    q = request.query_params
    if q.get("error"):
        return RedirectResponse(f"{back}/?integration={name}&status=error&message={q.get('error')}", status_code=302)
    try:
        await integ.finish_auth(q.get("state", ""), q.get("code", ""))
    except Exception as exc:  # noqa: BLE001
        log.warning("%s callback failed: %s", name, exc)
        from urllib.parse import quote

        return RedirectResponse(f"{back}/?integration={name}&status=error&message={quote(str(exc)[:200])}",
                                status_code=302)
    return RedirectResponse(f"{back}/?integration={name}&status=connected", status_code=302)


@app.delete("/api/integrations/{name}")
async def disconnect_integration(name: str, user: str = Depends(current_user), svc=Depends(services)):
    integ = _integration(svc, name)
    await integ.disconnect(user)
    return await integ.status(user)


@app.get("/api/threads")
async def list_threads(user: str = Depends(current_user), svc=Depends(services)):
    return await svc["repo"].list_threads(user)


@app.post("/api/threads", status_code=201)
async def create_thread(body: CreateThread, user: str = Depends(current_user), svc=Depends(services)):
    return await svc["repo"].create_thread(user, body.title or "New chat")


@app.get("/api/threads/{thread_id}/messages")
async def list_messages(thread: dict = Depends(owned_thread), svc=Depends(services)):
    return await svc["repo"].list_messages(thread["id"])


@app.get("/api/threads/{thread_id}/state")
async def thread_state(thread: dict = Depends(owned_thread), user: str = Depends(current_user), svc=Depends(services)):
    snapshot = await svc["graph"].aget_state({"configurable": {"thread_id": thread["id"], "user_id": user}})
    values = snapshot.values or {}
    return {"pending": _pending_interrupt(snapshot), "plan": values.get("plan", [])}


@app.get("/api/threads/{thread_id}/debug")
async def debug_thread(thread: dict = Depends(owned_thread), user: str = Depends(current_user), svc=Depends(services)):
    """Everything about one chat in one place: messages, runs (status, error, Langfuse links),
    the conversation state the graph holds right now, and what Bulbul remembers about the user."""
    snapshot = await svc["graph"].aget_state({"configurable": {"thread_id": thread["id"], "user_id": user}})
    values = snapshot.values or {}
    keys = ("request_text", "step_count", "plan", "assumptions", "answers", "offer", "accepted_offer",
            "held_cart", "cart_edit", "cart_notice", "suggestions")
    runs = await svc["repo"].list_runs(thread["id"])
    for r in runs:
        r["short_id"] = logctx.short(r["id"])
        r["trace_url"] = tracing.trace_url(r.get("trace_id"))
    last_trace = next((r["trace_id"] for r in reversed(runs) if r.get("trace_id")), None)
    return {
        "chat_id": thread["id"],
        "short_id": logctx.short(thread["id"]),
        "title": thread["title"],
        "created_at": thread["created_at"],
        "how_to_debug": f"grep the backend logs for chat={logctx.short(thread['id'])}",
        "links": {"langfuse_session": tracing.session_url(thread["id"], last_trace)},
        "runs": runs,
        "state": {**{k: values.get(k) for k in keys},
                  "pending": _pending_interrupt(snapshot), "next": list(snapshot.next or []),
                  "graph_messages": len(values.get("messages", []))},
        "messages": await svc["repo"].list_messages(thread["id"]),
        "memory": await memory.load(svc["store"], user),
    }


@app.post("/api/threads/{thread_id}/runs")
async def start_run(
    body: SendMessage,
    thread: dict = Depends(owned_thread),
    user: str = Depends(current_user),
    settings: Settings = Depends(get_settings),
    svc=Depends(services),
):
    repo = svc["repo"]
    model = _checked_model(body.model, svc)
    if thread["title"] == "New chat":
        await repo.rename_thread(thread["id"], body.message.strip().splitlines()[0][:60])
    await repo.add_message(thread["id"], "user", body.message)
    graph_input = {
        "messages": [HumanMessage(body.message)],
        # Per-request scratch state is reset; the conversation history is kept.
        "plan": [], "plan_confirmed": False, "step_count": 0, "assumptions": [], "note": None,
        "decision": None, "current_step": None, "answers": {}, "pause_for_user": False,
        "request_text": body.message, "voice": body.voice, "blocked": None, "accepted_offer": None, "suggestions": [], "attachments": [],
        "held_cart": None, "cart_edit": None, "cart_notice": None,
        # "offer" is NOT reset: it carries Bulbul's last offer into this request (see load_context).
    }
    return _sse_response(stream_run(svc["graph"], repo, settings, user, thread["id"], graph_input, model,
                                     kind="message", user_text=body.message))


@app.post("/api/threads/{thread_id}/runs/resume")
async def resume_run(
    body: Resume,
    thread: dict = Depends(owned_thread),
    user: str = Depends(current_user),
    settings: Settings = Depends(get_settings),
    svc=Depends(services),
):
    graph, repo = svc["graph"], svc["repo"]
    model = _checked_model(body.model, svc)
    snapshot = await graph.aget_state(run_config(settings, user, thread["id"], "state"))
    pending = _pending_interrupt(snapshot)
    if pending is None:
        raise HTTPException(409, "Nothing is waiting for an answer in this thread")
    answer_text = _describe_answer(pending["kind"], body.value)
    await repo.add_message(thread["id"], "user", answer_text, kind="answer",
                           meta={"kind": pending["kind"], "value": body.value})
    await repo.record_response(thread["id"], None, pending["kind"], body.value)
    return _sse_response(stream_run(graph, repo, settings, user, thread["id"], Command(resume=body.value), model,
                                     kind="resume", user_text=answer_text))


@app.post("/api/runs/{run_id}/cancel")
async def cancel(run_id: str, user: str = Depends(current_user)):
    return {"cancelled": cancel_run(run_id)}


def _describe_answer(kind: str, value: dict) -> str:
    if kind == "clarify":
        answers = value.get("answers", {})
        return "\n".join(f"{k}: {v}" if k != "_free_text" else str(v) for k, v in answers.items() if str(v).strip())
    if value.get("approved") and isinstance(value.get("items"), list):
        n = len(value["items"])
        return f"Approved {n} item{'' if n == 1 else 's'} for the cart"
    if value.get("approved"):
        return "Approved" + (f" — {value['feedback']}" if value.get("feedback") else "")
    return "Declined" + (f" — {value['feedback']}" if value.get("feedback") else "")
