"""The unified conversation: memory across chats, price check before the cart, cart edits by chat."""

from __future__ import annotations

from contextlib import AsyncExitStack

import httpx
import pytest
from langchain_core.messages import AIMessage

from app.main import app, build_services
from app.memory import Fact, MemoryUpdate, worth_reading
from app.schemas import KV, AgentResult, SupervisorDecision
from app.settings import Settings
from stubs import swiggy_instamart as stub
from tests.test_graph_flow import FakeRouter, post_sse, types
from tests.test_shopping import ToolFakeRouter, call, make

CHEF = AgentResult(status="done", summary="Biryani for 4", details="### Ingredients\n- Chicken: 750 g\n- Curd: 1 cup\n"
                   "- Basmati rice: 2 cups\n\n### Steps\n1. Marinate.\n2. Layer and cook.")
RECIPE_INPUTS = [KV(key="dish", value="chicken biryani"), KV(key="servings", value="4"),
                 KV(key="dietary_restrictions", value="None")]


@pytest.fixture(autouse=True)
def fresh_stub():
    stub._cart.update(addressId=None, items=[])
    stub._orders.clear()
    yield


def responder_system(router):
    return [m for a, m in router.calls if a == "responder"][-1][0].content


async def test_memory_is_learned_used_and_can_be_forgotten():
    router = FakeRouter({
        "supervisor": [SupervisorDecision(action="respond"), SupervisorDecision(action="respond")],
        "memory": [MemoryUpdate(remember=[
            Fact(key="name", text="Name is Asha.", category="identity"),
            Fact(key="craving_biryani", text="Craves Hyderabadi chicken biryani.", category="food")])],
    })
    stack = AsyncExitStack()
    app.state.services = await build_services(Settings(database_url=""), stack, router=router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs",
                       {"message": "I'm Asha and I'm craving Hyderabadi chicken biryani these days"})
        facts = {f["key"]: f["text"] for f in (await client.get("/api/memory")).json()}
        assert facts == {"name": "Name is Asha.", "craving_biryani": "Craves Hyderabadi chicken biryani."}

        # A brand-new chat: Bulbul still knows.
        t2 = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t2['id']}/runs", {"message": "How are you?"})
        assert "- Name is Asha." in responder_system(router)
        supervisor = [m for a, m in router.calls if a == "supervisor"][-1][0].content
        assert "Craves Hyderabadi chicken biryani." in supervisor

        assert (await client.delete("/api/memory/name")).status_code == 200
        assert [f["key"] for f in (await client.get("/api/memory")).json()] == ["craving_biryani"]


def test_memory_skips_messages_without_facts():
    assert not worth_reading("yes") and not worth_reading("ok thanks") and not worth_reading("")
    assert worth_reading("we are 5 at home") and worth_reading("I'm Asha")


def _buy_script(*extra_turns):
    return [
        AIMessage("", tool_calls=[call("get_addresses", {})]),
        AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "milk"}, 1),
                                  call("search_products", {"addressId": "addr-home", "query": "banana"}, 2)]),
        AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
            {"spinId": "milk-2", "skuId": "sku-milk-2", "quantity": 1},
            {"spinId": "banana-1", "skuId": "sku-banana-1", "quantity": 1}]})]),
        AIMessage("I propose milk and bananas."),
        *extra_turns,
    ]


async def test_cart_can_be_changed_by_chat_while_the_card_is_open():
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy milk and bananas",
                                          inputs=[KV(key="items", value="milk, bananas")])],
        "shopping:chat": _buy_script(
            # revision: "remove the bananas and add 6 eggs"
            AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "eggs"}, 3)]),
            AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
                {"spinId": "milk-2", "skuId": "sku-milk-2", "quantity": 1},
                {"spinId": "eggs-1", "skuId": "sku-eggs-1", "quantity": 1}]}, 4)]),
            AIMessage("Swapped bananas for eggs."),
        ),
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy milk and bananas on instamart"})
        first = next(e for e in ev if e["type"] == "interrupt")
        assert [i["name"] for i in first["actions"][0]["items"]] == ["Toned Milk", "Banana - Robusta"]

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume",
                             {"value": {"edit": "remove the bananas and add 6 eggs"}})
        card = next(e for e in ev2 if e["type"] == "interrupt")  # the card comes back, changed
        assert [i["name"] for i in card["actions"][0]["items"]] == ["Toned Milk", "Farm Eggs"]
        assert card["actions"][0]["items"][1]["image_url"].endswith("eggs.svg")
        assert stub._cart["items"] == []  # still nothing changed in the store

        ev3 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"edit": "looks good, go ahead"}})
        assert ev3[-1]["status"] == "done"
        assert stub._cart["items"] == [{"spinId": "milk-2", "quantity": 1}, {"spinId": "eggs-1", "quantity": 1}]


async def test_saying_no_on_the_card_changes_nothing():
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy milk and bananas",
                                          inputs=[KV(key="items", value="milk, bananas")]),
                       SupervisorDecision(action="respond")],
        "shopping:chat": _buy_script(),
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy milk and bananas"})
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"edit": "no, not now"}})
        assert ev[-1]["status"] == "done" and stub._cart["items"] == []


async def test_changes_before_seeing_the_cart_are_applied_to_the_card():
    # Recipe -> "I don't have anything" -> price check -> "skip the curd" -> card without curd.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe", inputs=RECIPE_INPUTS),
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Check the biryani ingredients",
                               inputs=[KV(key="items", value="Chicken 750 g, Curd 1 cup")]),
        ],
        "chef": [CHEF],
        "shopping:chat": [
            AIMessage("", tool_calls=[call("get_addresses", {})]),
            AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "chicken"}, 1),
                                      call("search_products", {"addressId": "addr-home", "query": "curd"}, 2)]),
            AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
                {"spinId": "chicken-2", "skuId": "sku-chicken-2", "quantity": 1},
                {"spinId": "curd-1", "skuId": "sku-curd-1", "quantity": 1}]})]),
            AIMessage("Everything is available."),
            # revision for "skip the curd"
            AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
                {"spinId": "chicken-2", "skuId": "sku-chicken-2", "quantity": 1}]}, 5)]),
            AIMessage("Removed the curd."),
        ],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        url = f"/api/threads/{t['id']}/runs"
        await post_sse(client, url, {"message": "Give me an Indian chicken biryani recipe for 4, no dietary restrictions"})
        held = await post_sse(client, url, {"message": "I don't have anything at home"})
        assert "interrupt" not in types(held)  # price check only
        assert "about ₹" in responder_system(router) and "2 items" in responder_system(router)

        ev = await post_sse(client, url, {"message": "skip the curd"})
        card = next(e for e in ev if e["type"] == "interrupt")
        assert [i["name"] for i in card["actions"][0]["items"]] == ["Chicken Curry Cut (with bone)"]


async def test_remembered_household_size_fills_servings_without_asking():
    # Earlier chat: "we're 4 at home". Now the model fills servings=4 from memory; that must be trusted.
    router = FakeRouter({
        "memory": [MemoryUpdate(remember=[
            Fact(key="household_size", text="The household has 4 people.", category="household"),
            Fact(key="diet", text="Has no dietary restrictions.", category="diet")])],
        "supervisor": [
            SupervisorDecision(action="respond"),
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Biryani recipe", inputs=[
                KV(key="dish", value="Hyderabadi chicken biryani"), KV(key="servings", value="4"),
                KV(key="dietary_restrictions", value="None")]),
            SupervisorDecision(action="respond"),
        ],
        "chef": [CHEF],
    })
    stack = AsyncExitStack()
    app.state.services = await build_services(Settings(database_url=""), stack, router=router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "We're 4 at home, no dietary restrictions"})
        t2 = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t2['id']}/runs", {"message": "let's make Hyderabadi chicken biryani"})
        assert "interrupt" not in types(ev)  # no "For how many people?"
        assert [e["agent"] for e in ev if e["type"] == "agent_start"] == ["chef"]


async def test_persona_comes_from_the_editable_file(tmp_path):
    persona = tmp_path / "persona.md"
    persona.write_text("<!-- editor note: not sent -->\nYou are Bulbul. You speak like a calm ship's captain.", encoding="utf-8")
    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond")]})
    stack = AsyncExitStack()
    app.state.services = await build_services(Settings(database_url="", persona_file=persona), stack, router=router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "hello"})
        supervisor = [m for a, m in router.calls if a == "supervisor"][-1][0].content
        assert "calm ship's captain" in responder_system(router)  # the reply's voice
        assert "calm ship's captain" in supervisor                # how questions are worded
        assert "editor note" not in responder_system(router)       # comments are for people only


def test_shipped_prompts_contain_no_scripted_lines():
    from pathlib import Path

    from app.graph import prompts

    shipped = prompts.SUPERVISOR_SYSTEM + prompts.RESPONDER_SYSTEM + \
        (Path(__file__).parents[1] / "config" / "persona.md").read_text(encoding="utf-8")
    for line in ("Cooking fixes a lot", "Still craving that", "Hyderabadi"):
        assert line not in shipped


async def test_a_change_that_fails_is_reported_on_the_card():
    # Seen live: with no model available, "skip the saffron" left the card unchanged with no word.
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy milk and bananas",
                                          inputs=[KV(key="items", value="milk, bananas")])],
        "shopping:chat": _buy_script(),  # nothing scripted for the revision -> the model call fails
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy milk and bananas"})
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"edit": "remove the bananas"}})
        card = next(e for e in ev if e["type"] == "interrupt")
        assert "couldn't make that change" in card["notice"] and "remove the bananas" in card["notice"]
        assert [i["name"] for i in card["actions"][0]["items"]] == ["Toned Milk", "Banana - Robusta"]  # unchanged


async def test_voice_requests_get_spoken_style_replies():
    router = FakeRouter({"supervisor": [SupervisorDecision(action="respond"), SupervisorDecision(action="respond")]})
    stack = AsyncExitStack()
    app.state.services = await build_services(Settings(database_url=""), stack, router=router)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "what's in my cart?", "voice": True})
        spoken = [m for a, m in router.calls if a == "voice_responder"][-1][0].content  # the fast model
        assert "VOICE MODE" in spoken and "driving" in spoken
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "thanks"})  # typed again
        assert "VOICE MODE" not in responder_system(router)
