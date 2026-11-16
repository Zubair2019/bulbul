"""Conversation flow: one topic per turn, offers instead of side-quests, voice-friendly "yes".

Replays the drift seen in a real chat: asked for a recipe, Bulbul planned shopping too.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage

from app.graph.builder import _accepts
from app.schemas import KV, AgentResult, PlanItem, Question, SupervisorDecision
from stubs import swiggy_instamart as stub
from tests.test_graph_flow import post_sse, types
from tests.test_shopping import ToolFakeRouter, call, make

RECIPE_INPUTS = [KV(key="dish", value="chicken biryani"), KV(key="servings", value="4"),
                 KV(key="dietary_restrictions", value="None")]
CHEF_RESULT = AgentResult(status="done", summary="Chicken biryani for 4",
                          details="### Ingredients\n- Chicken: 750 g\n- Basmati rice: 2 cups\n- Curd: 1 cup\n\n### Steps\n1. Cook.")
OFFER_LABEL = "Check Swiggy Instamart"


@pytest.fixture(autouse=True)
def fresh_stub():
    stub._cart.update(addressId=None, items=[])
    stub._orders.clear()
    yield


def sup_system(router, i=-1):
    return [m for a, m in router.calls if a == "supervisor"][i][0].content


def responder_system(router):
    return [m for a, m in router.calls if a == "responder"][-1][0].content


@asynccontextmanager
async def recipe_turn(router):
    """First turn of the replayed chat: the user asks for a recipe (and nothing else)."""
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs",
                            {"message": "Give me an Indian chicken biryani recipe for 4, no dietary restrictions"})
        yield client, t, ev


async def test_recipe_request_does_not_shop_but_offers_it():
    # The model plans chef + shopping for a recipe request (the drift); shopping must be dropped
    # and offered after the recipe instead.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="confirm_plan", plan=[
                PlanItem(agent="chef", goal="Recipe", inputs=RECIPE_INPUTS),
                PlanItem(agent="shopping", goal="Buy the ingredients", inputs=[KV(key="items", value="chicken, rice")])]),
            SupervisorDecision(action="respond"),
        ],
        "chef": [CHEF_RESULT],
    })
    async with recipe_turn(router) as (client, t, ev):
        assert "interrupt" not in types(ev)  # no plan card for a single-topic request
        started = [e["agent"] for e in ev if e["type"] == "agent_start"]
        assert started == ["chef"]
        assert next(e for e in ev if e["type"] == "suggestions")["items"] == [OFFER_LABEL, "I have them"]
        assert ('End the reply with exactly this question: "Do you have the ingredients for chicken biryani at '
                'home, or should I check Swiggy Instamart?"') in responder_system(router)
        attachment = next(e for e in ev if e["type"] == "attachment")
        assert attachment["title"] == "Chicken biryani recipe" and "Basmati rice" in attachment["body"]
        assert "Do NOT repeat its contents" in responder_system(router)  # the spoken reply stays short
        msgs = (await client.get(f"/api/threads/{t['id']}/messages")).json()
        assert msgs[-1]["meta"]["suggestions"] == [OFFER_LABEL, "I have them"]  # chips survive a reload
        assert msgs[-1]["meta"]["attachments"][0]["title"] == "Chicken biryani recipe"


@pytest.mark.parametrize("reply", ["yes", "Sure, go ahead", OFFER_LABEL, "haan", "I don't have anything at home"])
async def test_saying_yes_checks_the_store_then_shows_the_cart_on_yes(reply):
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe", inputs=RECIPE_INPUTS),
            # (one-step request: Bulbul replies after the chef without asking the supervisor again)
            # turn 2: the model copies the recipe's list into the shopping step
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy the biryani ingredients",
                               inputs=[KV(key="items", value="Chicken 750 g, Basmati rice 2 cups, Curd 1 cup")]),
        ],
        "chef": [CHEF_RESULT],
        "shopping:chat": [
            AIMessage("", tool_calls=[call("get_addresses", {})]),
            AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "chicken"}, 1)]),
            AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
                {"spinId": "chicken-2", "skuId": "sku-chicken-2", "quantity": 1}]})]),
            AIMessage("I propose chicken."),
        ],
    })
    async with recipe_turn(router) as (client, t, _):
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": reply})
        system = sup_system(router)
        assert "The user just accepted Bulbul's offer" in system
        assert "Basmati rice: 2 cups" in system  # the recipe's own list, not a new one
        assert [e["agent"] for e in ev if e["type"] == "agent_start"] == ["shopping"]  # no second chef call
        # Price check first: no card yet, a one-line summary and a question instead.
        assert "interrupt" not in types(ev) and stub._cart["items"] == []
        assert next(e for e in ev if e["type"] == "suggestions")["items"] == ["Show me the cart", "Not now"]
        assert "You just checked Swiggy Instamart" in responder_system(router)
        assert '"Want me to put it in your Swiggy Instamart cart?"' in responder_system(router)

        # "yes" shows the picture cart, without searching the store again.
        n_chats = len([a for a, _ in router.calls if a == "shopping"])
        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "yes please"})
        card = next(e for e in ev2 if e["type"] == "interrupt")
        assert card["kind"] == "approve" and card["actions"][0]["items"][0]["name"].startswith("Chicken")
        assert len([a for a, _ in router.calls if a == "shopping"]) == n_chats


async def test_no_thanks_drops_the_offer():
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe", inputs=RECIPE_INPUTS),
            SupervisorDecision(action="respond"),
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy",  # model tries anyway
                               inputs=[KV(key="items", value="chicken")]),
        ],
        "chef": [CHEF_RESULT],
    })
    async with recipe_turn(router) as (client, t, _):
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "No thanks"})
        assert "The user just accepted Bulbul's offer" not in sup_system(router)
        assert "agent_start" not in types(ev)  # shopping blocked: the user declined
        assert not any(e["type"] == "suggestions" for e in ev)


async def test_shopping_is_blocked_when_nobody_asked():
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy snacks",
                                          inputs=[KV(key="items", value="snacks")])],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "feeling bored"})
        assert "agent_start" not in types(ev) and ev[-1]["status"] == "done"


async def test_asking_to_buy_directly_still_works():
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy milk",
                                          inputs=[KV(key="items", value="milk")])],
        "shopping:chat": [AIMessage("Couldn't decide.")],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "order milk on instamart"})
        assert [e["agent"] for e in ev if e["type"] == "agent_start"] == ["shopping"]


def test_accepts():
    offer = {"label": OFFER_LABEL, "store": "Swiggy Instamart"}
    for yes in ["yes", "Yeah please", "sure, go ahead", OFFER_LABEL, "ok", "haan", "use instamart"]:
        assert _accepts(yes, offer), yes
    for no in ["no thanks", "nah", "not now", "what about a dessert recipe instead?", ""]:
        assert not _accepts(no, offer), no


async def test_question_about_shopping_after_recipe_becomes_the_reply_with_offer():
    # Seen live: after the recipe, the model asked "want me to order the ingredients on Instamart?"
    # as its own question, so the recipe never showed. It must reply (recipe card + offer) instead.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="chef", goal="Recipe", inputs=RECIPE_INPUTS),
            SupervisorDecision(action="clarify", questions=[
                Question(id="order", text="Would you like me to order the ingredients on Swiggy Instamart now?",
                         options=["Yes, order ingredients", "No"])]),
        ],
        "chef": [CHEF_RESULT],
    })
    async with recipe_turn(router) as (client, t, ev):
        assert "interrupt" not in types(ev)
        assert any(e["type"] == "attachment" for e in ev)
        assert next(e for e in ev if e["type"] == "suggestions")["items"] == [OFFER_LABEL, "I have them"]


async def test_speech_to_text_quirks_and_follow_ups_still_reach_shopping():
    # Replays the live voice test: Whisper heard "in-stamart card", then "Actually add 6 eggs too."
    # Both were blocked by the keyword guard; neither should be.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Show the cart",
                               inputs=[KV(key="items", value="Instamart cart")]),
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Add eggs",
                               inputs=[KV(key="items", value="6 eggs")]),
        ],
        "shopping:chat": [AIMessage("", tool_calls=[call("get_cart", {})]), AIMessage("Your cart is empty."),
                          AIMessage("I'd add eggs.")],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "What's in my in-stamart card?"})
        assert [e["agent"] for e in ev if e["type"] == "agent_start"] == ["shopping"]
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "Actually add 6 eggs too."})
        assert [e["agent"] for e in ev if e["type"] == "agent_start"] == ["shopping"]  # follow-up


async def test_held_back_side_quest_becomes_an_honest_question():
    # The model wants to shop but nobody asked: Bulbul must not claim it's checking; it asks.
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Find snacks",
                                          inputs=[KV(key="items", value="snacks")]),
                       SupervisorDecision(action="delegate", delegate_to="shopping", goal="Find snacks",
                                          inputs=[KV(key="items", value="snacks")])],
        "shopping:chat": [AIMessage("Found chips.")],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "I could do with some snacks"})
        assert "agent_start" not in types(ev)
        system = responder_system(router)
        assert "Do NOT say you are checking" in system
        assert 'End the reply with exactly this question: "Want me to check Swiggy Instamart for that?"' in system
        assert next(e for e in ev if e["type"] == "suggestions")["items"][0] == "Yes, check Swiggy Instamart"
        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "yes"})
        assert [e["agent"] for e in ev2 if e["type"] == "agent_start"] == ["shopping"]
        assert "Goal: Find snacks" in sup_system(router)
