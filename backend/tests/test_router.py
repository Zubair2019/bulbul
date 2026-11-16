"""Model router latency guards: quota cooldown and hedged (duplicate) requests."""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from app.llm.config import ModelsConfig
from app.llm.router import AllModelsFailed, ModelRouter


class QuotaError(Exception):
    def __str__(self):
        return "Error calling model 'gemini-3.5-flash' (RESOURCE_EXHAUSTED): 429 RESOURCE_EXHAUSTED."


class FakeModel:
    """Plays one scripted behaviour per call: ("ok", delay, text) or ("quota",)."""

    def __init__(self, script):
        self.script, self.calls = list(script), []

    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages, config=None):
        self.calls.append(config)
        step = self.script.pop(0)
        if step[0] == "quota":
            raise QuotaError()
        await asyncio.sleep(step[1])
        return AIMessage(step[2])

    async def astream(self, messages, config=None):
        self.calls.append(config)
        step = self.script.pop(0)
        if step[0] == "quota":
            raise QuotaError()
        await asyncio.sleep(step[1])
        for word in step[2].split(" "):
            yield AIMessageChunk(content=word + " ")


def make_router(monkeypatch, models: dict[str, FakeModel], hedge: float | None = None, cooldown: float = 600):
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    cfg = ModelsConfig.model_validate({
        "providers": {"google": {"api_key_env": "GOOGLE_API_KEY"}},
        "chains": {"c": [{"provider": "google", "model": "flash"},
                         {"provider": "google", "model": "lite", "hedge_after_s": hedge}]},
        "agents": {"general": {"chain": "c"}},
        "policy": {"quota_cooldown_s": cooldown},
    })
    router = ModelRouter(cfg)
    monkeypatch.setattr(router, "_model", lambda entry: models[entry.model])
    return router


async def test_a_model_out_of_quota_is_skipped_until_its_cooldown_ends(monkeypatch):
    flash = FakeModel([("quota",), ("ok", 0, "flash is back")])
    lite = FakeModel([("ok", 0, "lite 1"), ("ok", 0, "lite 2")])
    router = make_router(monkeypatch, {"flash": flash, "lite": lite})
    msgs = [HumanMessage("hi")]

    assert (await router.chat("shopping", msgs, [])).content == "lite 1"
    assert (await router.chat("shopping", msgs, [])).content == "lite 2"
    assert len(flash.calls) == 1  # the second call didn't pay for the same refusal again

    router._cooldown["google:flash"] = 0  # cooldown over: it's tried first again
    assert (await router.chat("shopping", msgs, [])).content == "flash is back"


async def test_everything_cooling_down_still_gets_tried(monkeypatch):
    flash = FakeModel([("quota",), ("ok", 0, "flash")])
    lite = FakeModel([("quota",)])
    router = make_router(monkeypatch, {"flash": flash, "lite": lite})
    with pytest.raises(AllModelsFailed):
        await router.chat("shopping", [HumanMessage("hi")], [])
    assert (await router.chat("shopping", [HumanMessage("hi")], [])).content == "flash"


async def test_a_slow_call_is_hedged_and_the_faster_answer_wins(monkeypatch):
    lite = FakeModel([("ok", 5, "stuck"), ("ok", 0.01, "hedge")])
    router = make_router(monkeypatch, {"flash": FakeModel([("quota",)]), "lite": lite}, hedge=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()
    reply = await router.chat("shopping", [HumanMessage("hi")], [])
    assert reply.content == "hedge" and loop.time() - started < 1
    assert [c["metadata"].get("hedge", False) for c in lite.calls] == [False, True]


async def test_a_fast_call_is_not_hedged(monkeypatch):
    lite = FakeModel([("ok", 0, "quick")])
    router = make_router(monkeypatch, {"flash": FakeModel([("quota",)]), "lite": lite}, hedge=1)
    assert (await router.chat("shopping", [HumanMessage("hi")], [])).content == "quick"
    assert len(lite.calls) == 1


async def test_a_stream_that_has_not_started_is_hedged(monkeypatch):
    lite = FakeModel([("ok", 5, "slow words"), ("ok", 0.01, "fast words here")])
    router = make_router(monkeypatch, {"flash": FakeModel([("quota",)]), "lite": lite}, hedge=0.05)
    text = "".join([t async for t in router.stream_text("responder", [HumanMessage("hi")])])
    assert text == "fast words here "


def test_the_cooldown_follows_the_providers_retry_time():
    from app.llm.router import _retry_after

    assert _retry_after(Exception("... per day limit exceeded on Free Tier.\nPlease retry in 18h45m50.2s.")) == \
        18 * 3600 + 45 * 60 + 50.2
    assert _retry_after(Exception("per minute limit exceeded. Please retry in 35.82593124s.")) == 35.82593124
    assert _retry_after(QuotaError()) is None  # no hint: the configured cooldown applies
