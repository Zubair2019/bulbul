"""Tool-using agents backed by an MCP server (first one: Swiggy Instamart shopping).

The model works in a loop: it calls read-only tools (addresses, search, cart view, order
status) directly, as often as it needs. Tools that change the user's account (cart edits,
placing an order, address changes) are NEVER executed inside the loop. They become
`ProposedAction`s with a description written by code from the real tool data, so the
approval card shows exactly what will run. The approve node executes them only after the
user says yes (`ToolAgent.execute`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from app.integrations.swiggy import NotConnected, SwiggyIntegration, ToolCallError, read_tool_data
from app.schemas import KV, ActionItem, AgentResult, CartLine, CartView, ProposedAction

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 10
MAX_RESULT_CHARS = 6000

# Changes the user's account: needs approval.
WRITE_TOOLS = {"update_cart", "clear_cart", "checkout", "create_address", "delete_address"}
# Not offered to the model yet (UPI payment flow, support reports).
HIDDEN_TOOLS = {"confirm_order", "check_payment_status", "report_error"}

_NOT_EXECUTED = (
    "NOT EXECUTED. This tool changes the user's Swiggy account, so it has been queued for the user's "
    "approval. Do not call it again. Finish now with a short summary of what you propose."
)


def _rupees(v: Any) -> str:
    try:
        return f"₹{float(v):g}"
    except (TypeError, ValueError):
        return "₹?"


class _RunMemory:
    """What the agent has seen this run, used to describe proposals in plain words."""

    def __init__(self) -> None:
        self.variations: dict[str, dict] = {}   # spinId -> {name, size, price}
        self.addresses: dict[str, str] = {}     # id -> "Home (12 Lake View Road…)"
        self.cart: dict | None = None

    def learn(self, tool: str, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if tool == "get_addresses":
            for a in data.get("addresses", []):
                self.addresses[a.get("id")] = f"{a.get('addressCategory') or 'Address'} ({(a.get('addressLine') or '')[:40]})"
        if tool in ("search_products", "your_go_to_items"):
            for p in [*data.get("products", []), *data.get("similarProducts", [])]:
                for v in p.get("variations", []):
                    self.variations[v.get("spinId")] = {
                        "name": p.get("name"), "size": v.get("quantityDescription"), "price": v.get("price"),
                        "sku": v.get("skuId"), "image": _image(v) or _image(p)}
        if tool in ("get_cart", "update_cart"):
            self.cart = data

    def cart_items(self, args: dict) -> list[ActionItem]:
        """The proposed cart as picture cards (from what search actually returned)."""
        out = []
        for it in args.get("items") or []:
            v = self.variations.get(it.get("spinId"), {})
            out.append(ActionItem(
                spin_id=str(it.get("spinId")), sku_id=it.get("skuId") or v.get("sku"),
                name=v.get("name") or str(it.get("spinId")), size=v.get("size"),
                price=v.get("price") if isinstance(v.get("price"), (int, float)) else None,
                quantity=max(1, int(it.get("quantity") or 1)), image_url=v.get("image")))
        return out

    def describe(self, tool: str, args: dict) -> str:
        if tool == "update_cart":
            lines, total = [], 0.0
            for it in args.get("items") or []:
                v = self.variations.get(it.get("spinId"), {})
                qty = int(it.get("quantity") or 1)
                price = v.get("price")
                if isinstance(price, (int, float)):
                    total += price * qty
                lines.append(f"{v.get('name') or it.get('spinId')} {v.get('size') or ''} × {qty} ({_rupees(price)} each)".replace("  ", " "))
            where = self.addresses.get(args.get("selectedAddressId"), args.get("selectedAddressId"))
            return (f"Replace your Instamart cart (deliver to {where}) with: " + "; ".join(lines)
                    + f". Items total about {_rupees(total)}, before delivery and handling fees. Nothing is ordered yet.")
        if tool == "checkout":
            where = self.addresses.get(args.get("addressId"), args.get("addressId"))
            pay = {"COD": "cash on delivery"}.get(str(args.get("paymentMethod")), args.get("paymentMethod"))
            to_pay = ((self.cart or {}).get("billBreakdown") or {}).get("toPay", {}).get("value")
            n = len((self.cart or {}).get("items") or [])
            amount = f"{_rupees(to_pay)} to pay" if to_pay is not None else "amount shown in your cart"
            return f"Place the Instamart order: {n} items, {amount}, paid by {pay}, delivered to {where}."
        if tool == "clear_cart":
            return "Empty your Instamart cart."
        return f"{tool} with {json.dumps(args, ensure_ascii=False)}"


class ToolAgent:
    """Same interface as `Agent` (`.d`, `.run`), plus `.execute` for approved actions."""

    def __init__(self, definition, router, integration: SwiggyIntegration, server: str):
        self.d = definition
        self.router = router
        self.integration = integration
        self.server = server

    @staticmethod
    def _user_id() -> str:
        from langgraph.config import get_config

        return get_config()["configurable"]["user_id"]

    async def run(self, goal: str, inputs: list[KV], on_fallback=None) -> AgentResult:
        user_id = self._user_id()
        try:
            async with self.integration.session(user_id, self.server) as client:
                return await self._loop(client, goal, inputs, on_fallback)
        except NotConnected as exc:
            return AgentResult(status="failed", summary=(
                f"{str(exc).rstrip('.')}. Ask the user to click 'Connect' next to Swiggy in the sidebar, then try again."))
        except Exception as exc:  # noqa: BLE001 - a Swiggy outage must not crash the whole run
            if type(exc).__name__ == "AllModelsFailed":
                raise  # handled by the delegate node
            log.warning("swiggy: %s failed: %r", self.d.name, exc)
            return AgentResult(status="failed", summary=(
                "Couldn't reach Swiggy right now (connection error). Nothing was changed. "
                "Suggest trying again in a moment."))

    async def revise(self, action: dict, instruction: str, on_fallback=None) -> dict | None:
        """Change a proposed (not yet approved) cart as the user asked in words, e.g. "skip the ghee,
        add 6 eggs". Returns the new update_cart action, or None if it couldn't be done."""
        current = action.get("items") or []
        args = json.loads(action.get("arguments") or "{}")
        lines = "\n".join(f"- spinId {i['spin_id']} (skuId {i.get('sku_id')}): {i['name']} {i.get('size') or ''}, "
                          f"quantity {i['quantity']}, ₹{i.get('price')} each" for i in current)
        goal = (f"The user is reviewing this proposed Instamart cart (delivery address id "
                f"{args.get('selectedAddressId')}):\n{lines}\n\nThey said: \"{instruction}\"\n\n"
                "Apply exactly that change. Keep every other item and quantity as it is. Search only for items "
                "being added or swapped. Then call update_cart ONCE with the complete new list.")
        seed = _RunMemory()
        for i in current:
            seed.variations[i["spin_id"]] = {"name": i["name"], "size": i.get("size"), "price": i.get("price"),
                                             "sku": i.get("sku_id"), "image": i.get("image_url")}
        try:
            async with self.integration.session(self._user_id(), self.server) as client:
                result = await self._loop(client, goal, [], on_fallback, memory=seed)
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "AllModelsFailed":
                return None
            log.warning("swiggy: revising the cart failed: %r", exc)
            return None
        new = next((a for a in result.proposed_actions if a.tool == "update_cart" and a.items), None)
        return new.model_dump() if new else None

    async def _loop(self, client, goal: str, inputs: list[KV], on_fallback, memory: "_RunMemory | None" = None) -> AgentResult:
        listed = (await client.list_tools()).tools
        memory = memory or _RunMemory()
        proposals: list[ProposedAction] = []

        async def call(name: str, **args: Any) -> str:
            data = read_tool_data(await client.call_tool(name, args))
            memory.learn(name, data)
            text = json.dumps(data, ensure_ascii=False, default=str)
            return text if len(text) <= MAX_RESULT_CHARS else text[:MAX_RESULT_CHARS] + " …(truncated)"

        tools = {
            t.name: StructuredTool(
                name=t.name, description=(t.description or t.name)[:1000],
                args_schema=t.input_schema or {"type": "object", "properties": {}},
                coroutine=lambda _n=t.name, **kw: call(_n, **kw),
            )
            for t in listed if t.name not in HIDDEN_TOOLS
        }
        facts = "\n".join(f"- {kv.key}: {kv.value}" for kv in inputs) or "- (none)"
        messages: list = [
            SystemMessage(self.d.system_prompt + "\n\nTools that change the account (" + ", ".join(sorted(WRITE_TOOLS))
                          + ") are queued for the user's approval instead of running; call each at most once."),
            HumanMessage(f"Task: {goal}\n\nFacts:\n{facts}"),
        ]

        final = ""
        for _ in range(MAX_TOOL_ROUNDS):
            ai: AIMessage = await self.router.chat(self.d.name, messages, list(tools.values()), on_fallback=on_fallback)
            messages.append(ai)
            if not ai.tool_calls:
                final = ai.content if isinstance(ai.content, str) else _text(ai.content)
                break
            for tc in ai.tool_calls:
                name, args = tc["name"], tc.get("args") or {}
                if name not in tools:
                    messages.append(ToolMessage(f"Unknown tool {name}", tool_call_id=tc["id"], status="error"))
                elif name in WRITE_TOOLS:
                    if not any(p.tool == name for p in proposals):
                        proposals.append(ProposedAction(
                            kind=name, description=memory.describe(name, args),
                            server=self.server, tool=name, arguments=json.dumps(args, ensure_ascii=False),
                            items=memory.cart_items(args) if name == "update_cart" else []))
                    messages.append(ToolMessage(_NOT_EXECUTED, tool_call_id=tc["id"]))
                else:
                    try:
                        # Invoking with the ToolCall makes LangChain emit a `tool` run (traced) and a ToolMessage.
                        messages.append(await tools[name].ainvoke({**tc, "type": "tool_call"}))
                    except ToolCallError as exc:
                        messages.append(ToolMessage(f"Error: {exc}", tool_call_id=tc["id"], status="error"))
        else:
            final = final or "I ran out of steps before finishing."

        # Never place an order in the same approval as building the cart: the user must see the
        # real bill (with fees) first. The order is proposed in a later step.
        if any(p.tool == "update_cart" for p in proposals):
            proposals = [p for p in proposals if p.tool != "checkout"]

        summary = (final.strip().splitlines() or ["Done."])[0][:300]
        # The agent looked at the cart (and isn't proposing a new one): show it as a card.
        view = cart_view(memory.cart, self.d.store, memory.variations) if memory.cart and not proposals else None
        return AgentResult(status="done", summary=summary, details=final.strip(), proposed_actions=proposals, cart=view)

    async def execute(self, action: dict, user_id: str, views: list | None = None) -> str:
        """Run one approved action. Returns a plain-text outcome for the supervisor/responder; a cart
        it leads to is appended to `views` (shown as a card)."""
        tool, args = action.get("tool"), json.loads(action.get("arguments") or "{}")
        try:
            data = await self.integration.call(user_id, action.get("server") or self.server, tool, args)
        except NotConnected as exc:
            return f"{tool}: not done ({exc})"
        except ToolCallError as exc:
            return f"{tool}: failed ({exc})"
        except Exception as exc:  # noqa: BLE001
            log.warning("swiggy: executing %s failed: %r", tool, exc)
            return f"{tool}: not done (couldn't reach Swiggy; nothing was changed by this step)"
        if tool == "update_cart":
            data = await self.integration.call(user_id, self.server, "get_cart", {})
            if views is not None:
                seen = {i["spin_id"]: {"image": i.get("image_url")} for i in action.get("items") or []}
                views.append(cart_view(data, self.d.store, seen))
            bill = data.get("billBreakdown") or {}
            parts = [f"{i['name']} {i.get('quantityDescription', '')} × {i['quantity']} = {_rupees(i.get('lineTotal'))}"
                     for i in data.get("items", [])]
            warn = f" Warning: {data['cartWarning']}." if data.get("cartWarning") else ""
            return ("Cart updated: " + "; ".join(parts) + f". Item total {_rupees(bill.get('itemTotal', {}).get('value'))}, "
                    f"delivery {_rupees(bill.get('deliveryFee', {}).get('value'))}, handling {_rupees(bill.get('handlingFee', {}).get('value'))}, "
                    f"to pay {_rupees(bill.get('toPay', {}).get('value'))}.{warn} The order is NOT placed yet; ask the user "
                    "whether to place it.")
        if tool == "checkout":
            return (f"Order placed: id {data.get('orderId')}, status {data.get('status')}, to pay {_rupees(data.get('toPay'))} "
                    f"by {data.get('paymentMethod')}." + (" (Local stub: no real order.)" if data.get("stub") else ""))
        return f"{tool}: done."


def cart_view(data: dict, store: str | None, variations: dict | None = None) -> CartView:
    """Swiggy's get_cart payload -> what the cart card shows (pictures from the cart or from search)."""
    def val(x):
        return (x or {}).get("value") if isinstance(x, dict) else x

    bill = data.get("billBreakdown") or {}
    lines = []
    for i in data.get("items") or []:
        seen = (variations or {}).get(i.get("spinId"), {})
        lines.append(CartLine(
            name=i.get("name") or seen.get("name") or str(i.get("spinId")),
            size=i.get("quantityDescription") or seen.get("size"), quantity=int(i.get("quantity") or 1),
            price=i.get("price"), line_total=i.get("lineTotal"), image_url=_image(i) or seen.get("image")))
    return CartView(store=store, items=lines, item_total=val(bill.get("itemTotal")), delivery=val(bill.get("deliveryFee")),
                    handling=val(bill.get("handlingFee")), to_pay=val(bill.get("toPay")),
                    warning=data.get("cartWarning"))


def _image(obj: dict) -> str | None:
    """Product picture URL, whatever the field is called (Swiggy's exact field may differ)."""
    for key in ("imageUrl", "image_url", "image", "imageURL", "thumbnail"):
        val = obj.get(key)
        if isinstance(val, str) and val.startswith(("http://", "https://")):
            return val
    imgs = obj.get("images")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, str):
            return first
        if isinstance(first, dict):
            return _image(first)
    return None


def apply_selection(action: dict, selected: list[dict]) -> dict:
    """Rewrite an approved update_cart to the user's selection. Only items that were proposed can
    be kept; quantities are clamped. Returns the action to execute."""
    proposed = {i["spin_id"]: i for i in action.get("items") or []}
    keep = []
    for sel in selected:
        item = proposed.get(str(sel.get("spin_id")))
        if item is None:
            continue  # never execute something that wasn't on the card
        qty = max(1, min(20, int(sel.get("quantity") or item.get("quantity") or 1)))
        keep.append({**item, "quantity": qty})
    args = json.loads(action.get("arguments") or "{}")
    args["items"] = [{"spinId": i["spin_id"], "skuId": i.get("sku_id"), "quantity": i["quantity"]} for i in keep]
    return {**action, "items": keep, "arguments": json.dumps(args, ensure_ascii=False)}


def _text(content: Any) -> str:
    return "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content or [])
