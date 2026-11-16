"""Shopping agent on the local Swiggy Instamart stub (in-process MCP, no network, no API keys)."""

from __future__ import annotations

import json
from contextlib import AsyncExitStack

import httpx
import pytest
from langchain_core.messages import AIMessage
from mcp import Client

from app.main import app, build_services
from app.schemas import KV, SupervisorDecision
from app.settings import Settings
from stubs import swiggy_instamart as stub
from tests.test_graph_flow import FakeRouter, post_sse, types


def call(name, args, i=0):
    return {"name": name, "args": args, "id": f"call-{name}-{i}", "type": "tool_call"}


class ToolFakeRouter(FakeRouter):
    """Adds scripted tool-calling turns: script['<agent>:chat'] is a list of AIMessages."""

    async def chat(self, agent, messages, tools, on_fallback=None):
        self.calls.append((agent, messages))
        assert {t.name for t in tools} >= {"get_addresses", "search_products", "update_cart"}
        return self.script[f"{agent}:chat"].pop(0)


@pytest.fixture(autouse=True)
def fresh_stub():
    stub._cart.update(addressId=None, items=[])
    stub._orders.clear()
    yield


async def make(router, connected=True):
    stack = AsyncExitStack()
    settings = Settings(database_url="", swiggy_base_url="http://stub.local", bulbul_secret_key="")
    svc = await build_services(settings, stack, router=router,
                               swiggy_client_factory=lambda server, token: Client(stub.mcp))
    app.state.services = svc
    if connected:
        await svc["integrations"]["swiggy"]._store("dev-user", {"access_token": "t", "refresh_token": "r",
                                                                "expires_in": 3600})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), stack, svc


def _shopping_script(extra_calls=()):
    return [
        AIMessage("", tool_calls=[call("get_addresses", {})]),
        AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "palak"})]),
        AIMessage("", tool_calls=[
            call("update_cart", {"selectedAddressId": "addr-home",
                                 "items": [{"spinId": "spinach-2", "skuId": "sku-spinach-2", "quantity": 1}]}),
            *extra_calls]),
        AIMessage("I propose Fresh Spinach (Palak) 500 g for ₹45."),
    ]


async def test_cart_change_waits_for_approval_then_runs():
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak 500g")]),
            SupervisorDecision(action="respond"),
        ],
        "shopping:chat": _shopping_script(),
    })
    client, stack, svc = await make(router)
    async with stack, client:
        assert "shopping" in svc["registry"].agents
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak 500g on instamart"})
        intr = next(e for e in ev if e["type"] == "interrupt")
        assert intr["kind"] == "approve"
        (action,) = intr["actions"]
        assert action["tool"] == "update_cart"
        # Described by code from real tool data, not by the model:
        assert "Fresh Spinach (Palak) 500 g × 1 (₹45 each)" in action["description"]
        assert "Nothing is ordered yet" in action["description"]
        assert stub._cart["items"] == []  # nothing changed before approval

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True}})
        assert ev2[-1]["status"] == "done"
        assert stub._cart["items"] == [{"spinId": "spinach-2", "quantity": 1}]
        final_plan = [e for e in ev2 if e["type"] == "plan_update"][-1]["plan"]
        assert "Cart updated" in final_plan[0]["result"]["details"]
        assert "NOT placed" in final_plan[0]["result"]["details"]


async def test_declined_cart_change_changes_nothing():
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak")]),
            SupervisorDecision(action="respond"),
        ],
        "shopping:chat": _shopping_script(),
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak"})
        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": False}})
        assert ev2[-1]["status"] == "done" and stub._cart["items"] == []


async def test_order_is_never_proposed_together_with_a_cart_change():
    # The model tries to build the cart and check out in one go; checkout must be dropped so the
    # user sees the real bill before any order.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak")]),
            SupervisorDecision(action="respond"),
        ],
        "shopping:chat": _shopping_script(extra_calls=[
            call("checkout", {"addressId": "addr-home", "paymentMethod": "COD"}, 1)]),
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak and order it"})
        intr = next(e for e in ev if e["type"] == "interrupt")
        assert [a["tool"] for a in intr["actions"]] == ["update_cart"]
        await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True}})
        assert stub._orders == []


async def test_not_connected_tells_user_to_connect():
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak")]),
            SupervisorDecision(action="respond"),
        ],
    })
    client, stack, _ = await make(router, connected=False)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak"})
        end = next(e for e in ev if e["type"] == "agent_end")
        assert end["status"] == "failed" and "Connect" in end["summary"] and "Swiggy" in end["summary"]


async def test_integrations_api_and_disabled_by_default():
    client, stack, svc = await make(ToolFakeRouter({}), connected=True)
    seen = []

    def fake_swiggy(request):  # metadata + logout, no network
        seen.append(request.url.path)
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json={"revocation_endpoint": "http://stub.local/auth/logout"})
        return httpx.Response(200, json={"ok": True})

    svc["integrations"]["swiggy"]._http = httpx.AsyncClient(transport=httpx.MockTransport(fake_swiggy))
    async with stack, client:
        (sw,) = (await client.get("/api/integrations")).json()
        assert sw["connected"] is True and sw["mode"] == "stub"
        assert (await client.delete("/api/integrations/swiggy")).json()["connected"] is False
        assert seen[-1] == "/auth/logout"  # the session was revoked at Swiggy too

    stack2 = AsyncExitStack()
    svc = await build_services(Settings(database_url="", swiggy_base_url=""), stack2, router=FakeRouter({}))
    async with stack2:
        assert "shopping" not in svc["registry"].agents  # Swiggy off -> the supervisor never plans for it


def test_read_tool_data_handles_error_envelopes():
    from types import SimpleNamespace

    from app.integrations.swiggy import ToolCallError, read_tool_data

    err = SimpleNamespace(structured_content={"success": False, "error": {"message": "Item out of stock"}},
                          content=[], is_error=True)
    with pytest.raises(ToolCallError, match="out of stock"):
        read_tool_data(err)
    env = SimpleNamespace(structured_content=None, is_error=False,
                          content=[SimpleNamespace(type="text", text=json.dumps({"success": True, "data": {"a": 1}, "lat": 2}))])
    assert read_tool_data(env) == {"a": 1, "lat": 2}


def _unauthorized_group():
    """Same shape the MCP 2.x client raises on a 401: MCPError nested in anyio exception groups."""
    from mcp.shared.exceptions import MCPError

    try:
        try:
            raise MCPError(code=-32001, message="Unauthorized")
        except MCPError as inner:
            raise RuntimeError("negotiation failed") from inner
    except RuntimeError as err:
        return ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [err])])


class _Rejecting:
    async def __aenter__(self):
        raise _unauthorized_group()

    async def __aexit__(self, *a):
        return False


async def test_expired_session_asks_to_reconnect_instead_of_crashing():
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak")]),
            SupervisorDecision(action="respond"),
        ],
    })
    stack = AsyncExitStack()
    svc = await build_services(Settings(database_url="", swiggy_base_url="http://stub.local"), stack,
                               router=router, swiggy_client_factory=lambda server, token: _Rejecting())
    app.state.services = svc
    sw = svc["integrations"]["swiggy"]
    await sw._store("dev-user", {"access_token": "old", "refresh_token": "old", "expires_in": 3600})
    refreshes = []

    def swiggy_http(request):  # metadata ok, refresh rejected (session gone, e.g. stub restarted)
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json={"token_endpoint": "http://stub.local/auth/token"})
        refreshes.append(json.loads(request.content))
        return httpx.Response(400, json={"error": "invalid_grant"})

    sw._http = httpx.AsyncClient(transport=httpx.MockTransport(swiggy_http))
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak"})
        assert ev[-1]["status"] == "done", "must not crash the run"
        end = next(e for e in ev if e["type"] == "agent_end")
        assert end["status"] == "failed" and "connect swiggy again" in end["summary"].lower()
        assert refreshes and refreshes[0]["grant_type"] == "refresh_token"  # refreshed first, as documented
        assert (await sw.status("dev-user"))["connected"] is False  # dead token removed


def test_auth_error_detection_unwraps_groups():
    from app.integrations.swiggy import _is_auth_error

    assert _is_auth_error(_unauthorized_group())
    assert not _is_auth_error(ExceptionGroup("x", [ValueError("Address not serviceable")]))


async def test_reconnect_after_abandoned_sign_in_registers_a_fresh_client():
    from app.db.repo import MemoryRepo
    from app.integrations.crypto import TokenCipher, new_key
    from app.integrations.swiggy import SwiggyIntegration

    registered = []

    def swiggy_http(request):
        if request.url.path.endswith("oauth-authorization-server"):
            return httpx.Response(200, json={"authorization_endpoint": "http://s/auth/authorize",
                                             "registration_endpoint": "http://s/auth/register"})
        registered.append(1)
        return httpx.Response(201, json={"client_id": f"client-{len(registered)}"})

    sw = SwiggyIntegration(base_url="http://s", redirect_uri="http://localhost:5173/cb", repo=MemoryRepo(),
                           cipher=TokenCipher(new_key()),
                           http=httpx.AsyncClient(transport=httpx.MockTransport(swiggy_http)))
    first = await sw.start_auth("dev-user")
    again = await sw.start_auth("dev-user")   # the first sign-in never came back
    assert "client_id=client-1" in first and "client_id=client-2" in again
    assert len(registered) == 2


async def test_after_cart_approval_bulbul_shows_the_bill_instead_of_ordering():
    # The supervisor would happily chain a checkout; after a cart update it must not even be asked.
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                               inputs=[KV(key="items", value="palak")]),
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Place the order",
                               inputs=[KV(key="items", value="palak")]),
        ],
        "shopping:chat": _shopping_script(),
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak"})
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True}})

        assert "interrupt" not in types(ev) and ev[-1]["status"] == "done"  # no order card
        assert len(router.script["supervisor"]) == 1  # the checkout decision was never requested
        responder_system = [m for a, m in router.calls if a == "responder"][-1][0].content
        assert "NO order has been placed" in responder_system and "The cart card below" in responder_system
        assert "to pay ₹" in responder_system  # the real bill from get_cart is in the results
        card = next(e for e in ev if e["type"] == "attachment" and e.get("kind") == "cart")
        assert [(i["name"], i["quantity"]) for i in card["cart"]["items"]] == [("Fresh Spinach (Palak)", 1)]
        assert card["cart"]["to_pay"] == card["cart"]["item_total"] + card["cart"]["delivery"] + card["cart"]["handling"]
        assert card["cart"]["items"][0]["image_url"].endswith("spinach.svg")
        assert stub._orders == []


async def test_cart_below_minimum_is_flagged_and_not_offered_for_ordering():
    small = _shopping_script()
    small[2] = AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
        {"spinId": "spinach-1", "skuId": "sku-spinach-1", "quantity": 1}]})])  # ₹25 < ₹99 minimum
    router = ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy spinach",
                                          inputs=[KV(key="items", value="palak")])],
        "shopping:chat": small,
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy palak"})
        await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True}})
        responder_system = [m for a, m in router.calls if a == "responder"][-1][0].content
        assert "blocks ordering: Minimum order value is ₹99" in responder_system
        assert "do NOT offer to place the order" in responder_system


def test_mode_labels():
    from app.db.repo import MemoryRepo
    from app.integrations.crypto import TokenCipher, new_key
    from app.integrations.swiggy import SwiggyIntegration

    def mode(url):
        return SwiggyIntegration(base_url=url, redirect_uri="x", repo=MemoryRepo(), cipher=TokenCipher(new_key())).mode

    assert mode("http://127.0.0.1:8765") == mode("http://swiggy-stub:8765") == "stub"
    assert mode("https://mcp-staging.swiggy.com") == "staging"
    assert mode("https://mcp.swiggy.com") == "production"
    assert mode("") == "off"


def _two_item_script():
    return [
        AIMessage("", tool_calls=[call("get_addresses", {})]),
        AIMessage("", tool_calls=[call("search_products", {"addressId": "addr-home", "query": "milk"}, 1),
                                  call("search_products", {"addressId": "addr-home", "query": "banana"}, 2)]),
        AIMessage("", tool_calls=[call("update_cart", {"selectedAddressId": "addr-home", "items": [
            {"spinId": "milk-2", "skuId": "sku-milk-2", "quantity": 1},
            {"spinId": "banana-1", "skuId": "sku-banana-1", "quantity": 1}]})]),
        AIMessage("I propose milk and bananas."),
    ]


async def _propose_two(router):
    client, stack, _ = await make(router)
    await stack.__aenter__()
    await client.__aenter__()
    t = (await client.post("/api/threads", json={})).json()
    ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "buy milk and bananas"})
    (action,) = next(e for e in ev if e["type"] == "interrupt")["actions"]
    return client, stack, t, action


def _router():
    return ToolFakeRouter({
        "supervisor": [SupervisorDecision(action="delegate", delegate_to="shopping", goal="Buy milk and bananas",
                                          inputs=[KV(key="items", value="milk, bananas")]),
                       SupervisorDecision(action="respond")],  # used after a decline
        "shopping:chat": _two_item_script(),
    })


async def test_cart_card_carries_pictures_and_details():
    client, stack, _, action = await _propose_two(_router())
    try:
        names = [(i["name"], i["size"], i["price"], i["quantity"]) for i in action["items"]]
        assert names == [("Toned Milk", "1 L", 50, 1), ("Banana - Robusta", "6 pcs", 42, 1)]
        assert all(i["image_url"].endswith(".svg") for i in action["items"])
    finally:
        await client.__aexit__(None, None, None)
        await stack.__aexit__(None, None, None)


async def test_user_edits_cart_on_the_card_before_approving():
    router = _router()
    client, stack, t, action = await _propose_two(router)
    try:
        # Keep only the milk, 2 of them; also try to sneak in an item that was never proposed.
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True, "items": [
            {"spin_id": "milk-2", "quantity": 2}, {"spin_id": "ghee-2", "quantity": 5}]}})
        assert ev[-1]["status"] == "done"
        assert stub._cart["items"] == [{"spinId": "milk-2", "quantity": 2}]  # banana dropped, ghee ignored
        responder = [m for a, m in router.calls if a == "responder"][-1]
        said = " ".join(m.content for m in responder if isinstance(m.content, str))
        assert "Toned Milk 1 L × 2" in said and "removed Banana - Robusta" in said
    finally:
        await client.__aexit__(None, None, None)
        await stack.__aexit__(None, None, None)


async def test_unticking_everything_changes_nothing():
    client, stack, t, _ = await _propose_two(_router())
    try:
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs/resume", {"value": {"approved": True, "items": []}})
        assert ev[-1]["status"] == "done" and stub._cart["items"] == []
    finally:
        await client.__aexit__(None, None, None)
        await stack.__aexit__(None, None, None)


async def test_show_my_cart_and_my_address_replay():
    # Replays chat 6c361493: "can you show me my cart again please" listed nothing, and
    # "what is my address?" was answered with "I don't have your address" without checking.
    stub._cart.update(addressId="addr-home", items=[{"spinId": "chicken-1", "quantity": 1},
                                                     {"spinId": "rice-1", "quantity": 1}])
    router = ToolFakeRouter({
        "supervisor": [
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Show the cart",
                               inputs=[KV(key="items", value="current cart")]),
            SupervisorDecision(action="delegate", delegate_to="shopping", goal="Find the delivery address",
                               inputs=[KV(key="items", value="address")]),
        ],
        "shopping:chat": [
            AIMessage("", tool_calls=[call("get_cart", {})]),
            AIMessage("Here are the current items in your cart: chicken and rice."),
            AIMessage("", tool_calls=[call("get_addresses", {}, 1)]),
            AIMessage("Your default address is Home, 12 Lake View Road, Indiranagar."),
        ],
    })
    client, stack, _ = await make(router)
    async with stack, client:
        t = (await client.post("/api/threads", json={})).json()
        ev = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "can you show me my cart again please"})
        card = next(e for e in ev if e["type"] == "attachment" and e.get("kind") == "cart")
        assert card["title"] == "Your Swiggy Instamart cart"
        assert [i["name"] for i in card["cart"]["items"]] == ["Chicken Curry Cut (with bone)", "Basmati Rice"]
        assert all(i["image_url"] for i in card["cart"]["items"])
        assert card["cart"]["to_pay"] == 189 + 145 + 0 + 9  # items + free delivery (over ₹199) + handling
        msgs = (await client.get(f"/api/threads/{t['id']}/messages")).json()
        assert msgs[-1]["meta"]["attachments"][0]["kind"] == "cart"  # still there after a reload

        ev2 = await post_sse(client, f"/api/threads/{t['id']}/runs", {"message": "what is my address?"})
        assert [e["agent"] for e in ev2 if e["type"] == "agent_start"] == ["shopping"]  # not blocked any more
        system = [m for a, m in router.calls if a == "responder"][-1][0].content
        assert "12 Lake View Road" in system  # the real address reaches the reply
