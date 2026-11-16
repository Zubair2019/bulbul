"""End-to-end graph and API tests with a scripted fake model router (no API keys needed)."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import httpx
import pytest

from app.main import app, build_services
from app.schemas import KV, AgentResult, PlanItem, Question, SupervisorDecision
from app.settings import Settings


class FakeRouter:
    """Returns scripted structured outputs per agent, in order, and streams a fixed reply."""

    def __init__(self, script: dict[str, list]):
        self.script = {k: list(v) for k, v in script.items()}
        self.calls: list[tuple[str, list]] = []

    async def structured(self, agent, messages, schema, on_fallback=None):
        self.calls.append((agent, messages))
        if agent == "memory" and agent not in self.script:
            return schema()  # nothing to remember unless a test scripts it
        return self.script[agent].pop(0)

    async def stream_text(self, agent, messages, on_fallback=None):
        self.calls.append((agent, messages))
        for part in ["Here ", "you ", "go."]:
            yield part


async def make_client(router: FakeRouter):
    stack = AsyncExitStack()
    app.state.services = await build_services(Settings(database_url=""), stack, router=router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    return client, stack


async def post_sse(client, url, body) -> list[dict]:
    events = []
    async with client.stream("POST", url, json=body) as r:
        assert r.status_code == 200, await r.aread()
        async for line in r.aiter_lines():
            if line.startswith("data: "):
                events.append(json.loads(line[6:]))
    return events


def types(events):
    return [e["type"] for e in events]


async def test_simple_chat_responds_directly():
    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond")]})
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "hi"})
        assert types(ev)[0] == "run_start"
        assert "".join(e["text"] for e in ev if e["type"] == "token") == "Here you go."
        assert ev[-1] == {"type": "run_end", "status": "done"}

        msgs = (await client.get(f"/api/threads/{t['id']}/messages")).json()
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        threads = (await client.get("/api/threads")).json()
        assert threads[0]["title"] == "hi"


async def test_missing_required_inputs_force_clarify_then_delegate():
    # The model tries to delegate to chef without servings/diet: code must force a clarify.
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="List ingredients",
                               inputs=[KV(key="dish", value="palak paneer")]),
            SupervisorDecision(action="delegate", delegate_to="chef", goal="List ingredients",
                               inputs=[KV(key="dish", value="palak paneer"), KV(key="servings", value="4")]),
            SupervisorDecision(action="respond"),
        ],
        "chef": [AgentResult(status="done", summary="Ingredients listed", details="- spinach\n- paneer")],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "I want to make palak paneer"})
        intr = next(e for e in ev if e["type"] == "interrupt")
        assert intr["kind"] == "clarify"
        assert {q["id"] for q in intr["questions"]} == {"servings", "dietary_restrictions"}
        assert ev[-1]["status"] == "interrupted"

        state = (await client.get(f"/api/threads/{t['id']}/state")).json()
        assert state["pending"]["kind"] == "clarify"

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume",
                             {"value": {"answers": {"servings": "4", "dietary_restrictions": "None"}}})
        assert "agent_start" in types(ev2) and "agent_end" in types(ev2)
        assert ev2[-1]["status"] == "done"

        # The dietary answer was remembered and filled in from memory for the chef call.
        chef_call = next(m for a, m in router.calls if a == "chef")
        assert "dietary_restrictions: None" in chef_call[-1].content

        # Nothing pending anymore -> resume is rejected.
        r = await client.post(f"/api/threads/{t['id']}/runs/resume", json={"value": {"approved": True}})
        assert r.status_code == 409


async def test_read_only_multi_step_plan_runs_without_a_plan_card():
    # Nothing in this plan acts in the world, so there's no "OK to go ahead?" card: it just runs.
    full = [KV(key="dish", value="dal"), KV(key="servings", value="2"), KV(key="dietary_restrictions", value="None")]
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="confirm_plan", plan=[
                PlanItem(agent="chef", goal="List ingredients", inputs=full),
                PlanItem(agent="chef", goal="Write recipe", inputs=full)]),
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Write recipe", inputs=full),
            SupervisorDecision(action="respond"),
        ],
        "chef": [AgentResult(status="done", summary="ingredients"), AgentResult(status="done", summary="recipe")],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "Help me cook dal for 2, no dietary restrictions"})
        assert "interrupt" not in types(ev)
        assert [e["goal"] for e in ev if e["type"] == "agent_start"] == ["List ingredients", "Write recipe"]
        assert ev[-1]["status"] == "done"


async def test_agent_needs_input_becomes_clarify_and_side_effects_need_approval():
    full = [KV(key="dish", value="dal"), KV(key="servings", value="2"), KV(key="dietary_restrictions", value="None")]
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Plan dinner", inputs=full),
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Plan dinner with lentils", inputs=full),
            SupervisorDecision(action="respond"),
        ],
        "chef": [
            AgentResult(status="needs_input", summary="Which lentils?",
                        questions=[Question(id="lentil", text="Which lentils?", options=["Toor", "Masoor"])]),
            AgentResult(status="done", summary="Planned", proposed_actions=[
                {"kind": "place_order", "description": "Order 500g masoor dal"}]),
        ],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "plan dal dinner for 2, no restrictions"})
        assert next(e for e in ev if e["type"] == "interrupt")["questions"][0]["id"] == "lentil"

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"answers": {"lentil": "Masoor"}}})
        intr = next(e for e in ev2 if e["type"] == "interrupt")
        assert intr["kind"] == "approve" and intr["actions"][0]["kind"] == "place_order"

        ev3 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": False}})
        assert ev3[-1]["status"] == "done"


async def test_invented_input_values_are_not_trusted():
    # User never said how many servings; the model makes one up. Code must ask instead,
    # and a follow-up delegation that drops the dish must inherit it from the earlier step.
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe", inputs=[
                KV(key="dish", value="chana masala"), KV(key="servings", value="2"),
                KV(key="dietary_restrictions", value="None")]),
            # After the answers: a read-only two-step plan (re-sent by the model before each step).
            # The second step drops the dish, which it must inherit from the first.
            *[SupervisorDecision(action="confirm_plan", plan=[
                PlanItem(agent="chef", goal="Recipe", inputs=[
                    KV(key="dish", value="chana masala"), KV(key="servings", value="4"),
                    KV(key="dietary_restrictions", value="None")]),
                PlanItem(agent="chef", goal="Shopping list", inputs=[])])] * 2,
            SupervisorDecision(action="respond"),
        ],
        "chef": [AgentResult(status="done", summary="recipe"), AgentResult(status="done", summary="list")],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "I want to make chana masala tonight"})
        intr = next(e for e in ev if e["type"] == "interrupt")
        assert {q["id"] for q in intr["questions"]} == {"servings", "dietary_restrictions"}  # dish was grounded

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume",
                             {"value": {"answers": {"servings": "4", "dietary_restrictions": "None"}}})
        assert types(ev2).count("agent_start") == 2 and ev2[-1]["status"] == "done"
        second_chef_call = [m for a, m in router.calls if a == "chef"][1]
        assert "dish: chana masala" in second_chef_call[-1].content
        assert "servings: 4" in second_chef_call[-1].content


@pytest.mark.parametrize("bad", ["not-a-uuid", "00000000-0000-0000-0000-000000000000"])
async def test_unknown_thread_404(bad):
    client, stack = await make_client(FakeRouter({}))
    async with stack, client:
        assert (await client.get(f"/api/threads/{bad}/messages")).status_code == 404


async def test_model_picker_endpoints_and_run_override(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    seen: list = []

    class Spy(FakeRouter):
        async def structured(self, agent, messages, schema, on_fallback=None):
            from app.llm.router import picked_model_id
            if agent == "supervisor":
                seen.append(picked_model_id())
            return await super().structured(agent, messages, schema, on_fallback)

    router = Spy({"supervisor": [SupervisorDecision(action="respond")] * 2})
    client, stack = await make_client(router)
    async with stack, client:
        models = {m["id"]: m for m in (await client.get("/api/models")).json()}
        assert models["gemini-3.5-flash-lite"]["available"] is True
        assert models["gpt-5-mini"]["available"] is False

        t = (await client.post("/api/threads", json={})).json()
        url = f"/api/threads/{t['id']}/runs"
        assert (await client.post(url, json={"message": "hi", "model": "nope"})).status_code == 400
        assert (await client.post(url, json={"message": "hi", "model": "gpt-5-mini"})).status_code == 400

        await post_sse(client, url, {"message": "hi", "model": "gemini-3.5-flash-lite"})
        await post_sse(client, url, {"message": "hi again"})
        assert seen == ["gemini-3.5-flash-lite", None]


def test_picked_model_goes_first_and_chain_stays_as_fallback(monkeypatch):
    from app.llm import router as router_mod
    from app.llm.config import load_models_config
    from app.settings import get_settings

    monkeypatch.setenv("GOOGLE_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    r = router_mod.ModelRouter(load_models_config(get_settings().models_config))
    auto = [e.label for e in r._chain("supervisor")]

    monkeypatch.setattr(router_mod, "picked_model_id", lambda: "claude-opus-5-5")
    picked = [e.label for e in r._chain("supervisor")]
    assert picked[0] == "anthropic:claude-opus-5-5"
    assert picked[1:] == [lab for lab in auto if lab != "anthropic:claude-opus-5-5"]

    monkeypatch.setattr(router_mod, "picked_model_id", lambda: "grok-4")  # no xAI key -> ignored
    assert [e.label for e in r._chain("supervisor")] == auto


async def test_single_step_plan_without_inputs_is_retried_not_asked_again():
    # Replays a real trace: after the user answered, the model proposed a 1-step plan with no
    # inputs, and Bulbul wrongly asked "Which dish?". Now: one corrective retry, and the user's
    # clarify answers fill what the retry leaves out.
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="clarify", questions=[
                Question(id="servings", text="How many servings?"),
                Question(id="dietary_restrictions", text="Any dietary restrictions?")]),
            SupervisorDecision(action="confirm_plan", plan=[PlanItem(agent="chef", goal="Recipe for palak paneer")]),
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe for palak paneer",
                               inputs=[KV(key="dish", value="palak paneer")]),
            SupervisorDecision(action="respond"),
        ],
        "chef": [AgentResult(status="done", summary="recipe")],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "I want to make palak paneer tonight"})
        assert next(e for e in ev if e["type"] == "interrupt")["kind"] == "clarify"

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume",
                             {"value": {"answers": {"servings": "2", "dietary_restrictions": "None"}}})
        assert "interrupt" not in types(ev2), "must not ask again"
        assert types(ev2).count("agent_start") == 1 and ev2[-1]["status"] == "done"

        sup_calls = [m for a, m in router.calls if a == "supervisor"]
        assert "Correction:" in sup_calls[2][-1].content  # the retry carried the correction note
        chef_brief = next(m for a, m in router.calls if a == "chef")[-1].content
        assert "dish: palak paneer" in chef_brief and "servings: 2" in chef_brief


async def test_single_step_plan_with_inputs_delegates_without_retry():
    router = FakeRouter({
        "supervisor": [
            SupervisorDecision(action="confirm_plan", plan=[PlanItem(agent="chef", goal="Dal recipe", inputs=[
                KV(key="dish", value="dal"), KV(key="servings", value="2"),
                KV(key="dietary_restrictions", value="None")])]),
            SupervisorDecision(action="respond"),
        ],
        "chef": [AgentResult(status="done", summary="recipe")],
    })
    client, stack = await make_client(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "dal for 2 people, no restrictions"})
        assert "interrupt" not in types(ev) and types(ev).count("agent_start") == 1
        # No retry needed, and no second decision after the one step: the reply follows directly.
        assert len([a for a, _ in router.calls if a == "supervisor"]) == 1
