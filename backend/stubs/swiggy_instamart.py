"""Local stand-in for Swiggy's Instamart MCP server + OAuth server (development only).

Swiggy's docs say to build against a local stub until they allowlist your OAuth redirect
(then staging at mcp-staging.swiggy.com, then production). This stub mirrors the documented
shapes so Bulbul's real code path - Dynamic Client Registration, OAuth 2.1 + PKCE, Bearer
auth, streamable HTTP MCP, `structuredContent` results - runs end to end on localhost:

    GET  /.well-known/oauth-authorization-server   RFC 8414 metadata
    GET  /.well-known/oauth-protected-resource     RFC 9728 metadata
    POST /auth/register                            Dynamic Client Registration (RFC 7591)
    GET  /auth/authorize                           fake "phone + OTP" page (any phone, OTP 123456)
    POST /auth/token                               authorization_code / refresh_token
    POST /auth/logout                              revoke
    POST /im                                       MCP (Instamart tools), Bearer required

Nothing here talks to Swiggy and no order is real. Single-user: one cart per stub process.

Run:  uv run python -m stubs.swiggy_instamart        (http://127.0.0.1:8765)
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from mcp.server.mcpserver import MCPServer
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

BASE_URL = os.environ.get("SWIGGY_STUB_BASE_URL", "http://127.0.0.1:8765").rstrip("/")
# Where the user's BROWSER reaches the sign-in page. Differs from BASE_URL in Docker, where the
# backend calls http://swiggy-stub:8765 but the browser opens http://localhost:8766.
PUBLIC_URL = os.environ.get("SWIGGY_STUB_PUBLIC_URL", BASE_URL).rstrip("/")


def _origin(request: Request) -> str:
    """Base URL as the caller addressed us (server-to-server endpoints are advertised this way)."""
    return f"{request.url.scheme}://{request.headers.get('host') or request.url.netloc}"
OTP = "123456"
ACCESS_TTL = 432000  # 5 days, as documented

# ---------------------------------------------------------------- seeded data
ADDRESSES = [
    {"id": "addr-home", "addressCategory": "Home", "addressLine": "12 Lake View Road, Indiranagar, Bengaluru 560038"},
    {"id": "addr-work", "addressCategory": "Work", "addressLine": "4th Floor, Tech Park, Koramangala, Bengaluru 560095"},
]


# Picture tiles served by the stub itself (/images/<productId>.svg). Real Swiggy returns CDN URLs.
_LOOK = {
    "spinach": ("🥬", "#dcfce7"), "paneer": ("🧀", "#fef9c3"), "onion": ("🧅", "#fae8ff"),
    "tomato": ("🍅", "#fee2e2"), "ginger": ("🫚", "#fef3c7"), "garlic": ("🧄", "#f5f5f4"),
    "green-chilli": ("🌶️", "#dcfce7"), "cream": ("🥛", "#eff6ff"), "ghee": ("🧈", "#fef9c3"),
    "butter": ("🧈", "#fef3c7"), "cumin": ("🌰", "#ffedd5"), "turmeric": ("🟡", "#fef9c3"),
    "garam-masala": ("🫙", "#ffedd5"), "chilli-powder": ("🌶️", "#fee2e2"), "kasuri-methi": ("🌿", "#dcfce7"),
    "salt": ("🧂", "#f1f5f9"), "oil": ("🫒", "#fef9c3"), "chickpeas": ("🫘", "#ffedd5"),
    "toor-dal": ("🫘", "#fef3c7"), "rice": ("🍚", "#f8fafc"), "lemon": ("🍋", "#fef9c3"),
    "coriander": ("🌿", "#dcfce7"), "milk": ("🥛", "#eff6ff"), "banana": ("🍌", "#fef9c3"),
    "chicken": ("🍗", "#ffedd5"), "chicken-boneless": ("🍗", "#ffedd5"), "mutton": ("🥩", "#fee2e2"),
    "eggs": ("🥚", "#fef9c3"), "curd": ("🥣", "#f8fafc"), "mint": ("🌿", "#dcfce7"), "saffron": ("🌸", "#ffedd5"),
    "bay-leaf": ("🍃", "#dcfce7"), "cardamom": ("🫛", "#dcfce7"), "cloves": ("🟤", "#ffedd5"),
    "cinnamon": ("🪵", "#ffedd5"), "star-anise": ("⭐", "#ffedd5"), "biryani-masala": ("🫙", "#fee2e2"),
    "coriander-powder": ("🫙", "#fef3c7"), "ginger-garlic-paste": ("🫙", "#fef3c7"), "cashews": ("🥜", "#fef3c7"),
    "potato": ("🥔", "#fef3c7"), "capsicum": ("🫑", "#dcfce7"), "bread": ("🍞", "#fef3c7"), "atta": ("🌾", "#fef3c7"),
    "sugar": ("🍬", "#f8fafc"), "tea": ("🍵", "#dcfce7"), "pasta": ("🍝", "#fef9c3"), "cheese": ("🧀", "#fef9c3"),
}


def _p(pid, name, brand, variations):
    return {
        "productId": pid, "name": name, "brand": brand, "imageUrl": f"{PUBLIC_URL}/images/{pid}.svg",
        "variations": [
            {"spinId": f"{pid}-{i}", "skuId": f"sku-{pid}-{i}", "quantityDescription": q, "price": price,
             "mrp": mrp, "isInStockAndAvailable": stock}
            for i, (q, price, mrp, stock) in enumerate(variations, 1)
        ],
    }


CATALOGUE = [
    _p("spinach", "Fresh Spinach (Palak)", "Fresho", [("250 g", 25, 30, True), ("500 g", 45, 55, True)]),
    _p("paneer", "Malai Paneer", "Amul", [("200 g", 95, 100, True), ("500 g", 225, 240, True)]),
    _p("onion", "Onion", "Fresho", [("500 g", 22, 28, True), ("1 kg", 40, 50, True)]),
    _p("tomato", "Tomato - Hybrid", "Fresho", [("500 g", 24, 30, True), ("1 kg", 45, 55, True)]),
    _p("ginger", "Ginger", "Fresho", [("100 g", 15, 18, True)]),
    _p("garlic", "Garlic", "Fresho", [("100 g", 20, 25, True), ("250 g", 45, 55, False)]),
    _p("green-chilli", "Green Chilli", "Fresho", [("100 g", 10, 12, True)]),
    _p("cream", "Fresh Cream", "Amul", [("250 ml", 65, 70, True)]),
    _p("ghee", "Pure Cow Ghee", "Amul", [("200 ml", 140, 150, True), ("500 ml", 320, 340, True)]),
    _p("butter", "Salted Butter", "Amul", [("100 g", 58, 60, True)]),
    _p("cumin", "Cumin Seeds (Jeera)", "Catch", [("100 g", 55, 60, True)]),
    _p("turmeric", "Turmeric Powder", "Everest", [("100 g", 35, 38, True)]),
    _p("garam-masala", "Garam Masala", "MDH", [("100 g", 78, 82, True)]),
    _p("chilli-powder", "Kashmiri Red Chilli Powder", "Everest", [("100 g", 62, 66, True)]),
    _p("kasuri-methi", "Kasuri Methi", "MDH", [("25 g", 30, 32, True)]),
    _p("salt", "Iodised Salt", "Tata", [("1 kg", 28, 30, True)]),
    _p("oil", "Sunflower Oil", "Fortune", [("1 L", 165, 180, True)]),
    _p("chickpeas", "Kabuli Chana (Chickpeas)", "Tata Sampann", [("500 g", 89, 99, True)]),
    _p("toor-dal", "Toor Dal", "Tata Sampann", [("1 kg", 175, 190, True)]),
    _p("rice", "Basmati Rice", "India Gate", [("1 kg", 145, 160, True), ("5 kg", 650, 720, True)]),
    _p("lemon", "Lemon", "Fresho", [("4 pcs", 20, 24, True)]),
    _p("coriander", "Coriander Leaves", "Fresho", [("100 g", 12, 15, True)]),
    _p("milk", "Toned Milk", "Nandini", [("500 ml", 26, 26, True), ("1 L", 50, 50, True)]),
    _p("banana", "Banana - Robusta", "Fresho", [("6 pcs", 42, 50, True)]),
    _p("chicken", "Chicken Curry Cut (with bone)", "Licious", [("500 g", 189, 210, True), ("1 kg", 359, 399, True)]),
    _p("chicken-boneless", "Chicken Breast Boneless", "Licious", [("450 g", 249, 279, True)]),
    _p("mutton", "Mutton Curry Cut", "Licious", [("500 g", 499, 549, True)]),
    _p("eggs", "Farm Eggs", "Eggoz", [("6 pcs", 54, 60, True), ("12 pcs", 99, 110, True)]),
    _p("curd", "Fresh Curd (Dahi)", "Nandini", [("400 g", 35, 38, True), ("1 kg", 80, 85, True)]),
    _p("mint", "Mint Leaves (Pudina)", "Fresho", [("100 g", 15, 18, True)]),
    _p("saffron", "Kashmiri Saffron (Kesar)", "Lion", [("0.5 g", 199, 230, True)]),
    _p("bay-leaf", "Bay Leaf (Tej Patta)", "Catch", [("25 g", 25, 28, True)]),
    _p("cardamom", "Green Cardamom (Elaichi)", "Catch", [("25 g", 99, 110, True)]),
    _p("cloves", "Cloves (Laung)", "Catch", [("25 g", 55, 60, True)]),
    _p("cinnamon", "Cinnamon Sticks (Dalchini)", "Catch", [("50 g", 45, 50, True)]),
    _p("star-anise", "Star Anise (Chakri Phool)", "Catch", [("25 g", 40, 45, True)]),
    _p("biryani-masala", "Biryani Masala", "Shan", [("50 g", 75, 80, True)]),
    _p("coriander-powder", "Coriander Powder (Dhania)", "Everest", [("100 g", 32, 35, True)]),
    _p("ginger-garlic-paste", "Ginger Garlic Paste", "Smith & Jones", [("200 g", 55, 60, True)]),
    _p("cashews", "Cashews (Kaju)", "Happilo", [("100 g", 120, 140, True)]),
    _p("potato", "Potato", "Fresho", [("1 kg", 35, 42, True)]),
    _p("capsicum", "Capsicum - Green", "Fresho", [("250 g", 25, 30, True)]),
    _p("bread", "Whole Wheat Bread", "Modern", [("400 g", 50, 55, True)]),
    _p("atta", "Whole Wheat Atta", "Aashirvaad", [("5 kg", 265, 290, True)]),
    _p("sugar", "Sugar", "Madhur", [("1 kg", 52, 58, True)]),
    _p("tea", "Tea", "Tata Tea Gold", [("250 g", 140, 155, True)]),
    _p("pasta", "Penne Pasta", "Del Monte", [("500 g", 115, 130, True)]),
    _p("cheese", "Mozzarella Cheese", "Amul", [("200 g", 120, 130, True)]),
]
_SYNONYMS = {"palak": "spinach", "jeera": "cumin", "haldi": "turmeric", "methi": "kasuri", "dhania": "coriander",
             "chana": "chickpeas", "chili": "chilli", "pyaz": "onion", "tamatar": "tomato", "adrak": "ginger",
             "lahsun": "garlic", "dal": "toor", "yogurt": "curd", "yoghurt": "curd", "dahi": "curd",
             "pudina": "mint", "kesar": "saffron", "elaichi": "cardamom", "laung": "cloves", "clove": "cloves",
             "dalchini": "cinnamon", "tej": "bay", "egg": "eggs", "cilantro": "coriander", "chillies": "chilli",
             "chilies": "chilli", "chiles": "chilli", "thighs": "chicken", "drumsticks": "chicken"}


def _variation(spin_id: str) -> tuple[dict, dict] | None:
    for prod in CATALOGUE:
        for v in prod["variations"]:
            if v["spinId"] == spin_id:
                return prod, v
    return None


# ---------------------------------------------------------------- state (single user)
_clients: dict[str, dict] = {}      # client_id -> {redirect_uris}
_codes: dict[str, dict] = {}        # code -> {client_id, redirect_uri, challenge, user, exp}
_tokens: dict[str, dict] = {}       # access/refresh token -> {user, exp}
_cart: dict[str, Any] = {"addressId": None, "items": []}
_orders: list[dict] = []

# Kept across restarts (dev convenience: no re-registering / re-signing in after a restart).
STATE_FILE = Path(os.environ.get("SWIGGY_STUB_STATE", Path(__file__).with_name(".swiggy_stub_state.json")))


def _load_state() -> None:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    _clients.update(data.get("clients", {}))
    _tokens.update({k: v for k, v in data.get("tokens", {}).items() if v.get("exp", 0) > time.time()})
    _cart.update(data.get("cart", {}))
    _orders[:] = data.get("orders", [])


def _save_state() -> None:
    try:
        STATE_FILE.write_text(json.dumps({"clients": _clients, "tokens": _tokens, "cart": _cart, "orders": _orders}),
                              encoding="utf-8")
    except OSError:
        pass


def _valid_token(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    tok = auth[7:] if auth.lower().startswith("bearer ") else ""
    info = _tokens.get(tok)
    return info["user"] if info and info["exp"] > time.time() else None


def _issue(user: str) -> dict:
    tok = "stub-" + secrets.token_urlsafe(24)
    _tokens[tok] = {"user": user, "exp": time.time() + ACCESS_TTL}
    # Documented: the rotated refresh token currently equals the new access token.
    return {"access_token": tok, "refresh_token": tok, "token_type": "Bearer", "expires_in": ACCESS_TTL,
            "scope": "mcp:tools mcp:resources mcp:prompts"}


# ---------------------------------------------------------------- MCP tools
mcp = MCPServer(name="swiggy-instamart-stub", instructions="LOCAL STUB of Swiggy Instamart. No real orders.")


@mcp.tool(structured_output=True)
def get_addresses(page: int = 1) -> dict[str, Any]:
    """Get the user's saved delivery addresses (10 per page). Use resolution.defaultAddressId as the default."""
    return {"addresses": ADDRESSES, "pagination": {"page": 1, "pageSize": 10, "total": len(ADDRESSES), "hasMore": False},
            "resolution": {"resolutionSource": "ranked_saved_address", "defaultAddressId": "addr-home"}}


@mcp.tool(structured_output=True)
def search_products(addressId: str, query: str) -> dict[str, Any]:
    """Search Instamart products available at an address. Returns products with variations; add a
    variation's spinId + skuId to the cart, not the parent product."""
    if addressId not in {a["id"] for a in ADDRESSES}:
        raise ValueError("Address not serviceable")
    words = [w for w in query.lower().replace(",", " ").split() if len(w) > 2]
    words = [_SYNONYMS.get(w, w) for w in words]
    generic = {"leaves", "leaf", "green", "powder", "fresh", "plain", "stick", "sticks", "seeds", "whole", "pods",
               "large", "small", "medium", "red", "white"}

    def score(p: dict) -> int:
        hay = (p["name"] + " " + p["productId"]).lower()
        return sum(1 if w in generic else 3 for w in words if w in hay)

    ranked = sorted((p for p in CATALOGUE if score(p) > 0), key=score, reverse=True)
    return {"products": ranked[:5], "similarProducts": [], "query": query}


@mcp.tool(structured_output=True)
def your_go_to_items(addressId: str) -> dict[str, Any]:
    """The user's frequently or recently ordered items at this address."""
    return {"products": [p for p in CATALOGUE if p["productId"] in {"milk", "banana", "onion", "tomato"}]}


def _cart_view() -> dict[str, Any]:
    items, total = [], 0
    for it in _cart["items"]:
        found = _variation(it["spinId"])
        if not found:
            continue
        prod, v = found
        line = v["price"] * it["quantity"]
        total += line
        items.append({"spinId": v["spinId"], "name": prod["name"], "brand": prod["brand"],
                      "quantityDescription": v["quantityDescription"], "quantity": it["quantity"],
                      "price": v["price"], "lineTotal": line, "imageUrl": prod.get("imageUrl")})
    delivery = 0 if total >= 199 or total == 0 else 30
    handling = 9 if total else 0
    warn = "Minimum order value is ₹99" if 0 < total < 99 else None
    return {"addressId": _cart["addressId"], "items": items,
            "billBreakdown": {"itemTotal": {"value": total}, "deliveryFee": {"value": delivery},
                              "handlingFee": {"value": handling}, "toPay": {"value": total + delivery + handling}},
            "availablePaymentMethods": ["COD", "UPI"], "cartWarning": warn, "addressWarning": None,
            "unserviceableItems": []}


@mcp.tool(structured_output=True)
def update_cart(selectedAddressId: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """Replace the ENTIRE Instamart cart with these items: [{spinId, skuId, quantity}]. Send every item."""
    clean = []
    for it in items:
        found = _variation(str(it.get("spinId")))
        if not found:
            raise ValueError(f"Unknown item {it.get('spinId')}")
        if not found[1]["isInStockAndAvailable"]:
            raise ValueError(f"Item out of stock at this address: {found[0]['name']} {found[1]['quantityDescription']}")
        clean.append({"spinId": found[1]["spinId"], "quantity": max(1, int(it.get("quantity", 1)))})
    _cart.update(addressId=selectedAddressId, items=clean)
    return _cart_view()


@mcp.tool(structured_output=True)
def get_cart() -> dict[str, Any]:
    """Current Instamart cart with items and bill breakdown (billBreakdown.toPay.value is the total)."""
    return _cart_view()


@mcp.tool(structured_output=True)
def clear_cart() -> dict[str, Any]:
    """Remove all items from the Instamart cart."""
    _cart.update(addressId=None, items=[])
    return {"cleared": True}


@mcp.tool(structured_output=True)
def get_payment_options() -> dict[str, Any]:
    """Live payment methods available for the cart."""
    return {"paymentMethods": [{"method": "COD", "label": "Cash on delivery"}, {"method": "UPI", "label": "UPI"}]}


@mcp.tool(structured_output=True)
def checkout(addressId: str, paymentMethod: str = "COD") -> dict[str, Any]:
    """Place and confirm the Instamart order for the current cart (STUB: no real order is placed)."""
    view = _cart_view()
    if not view["items"]:
        raise ValueError("Cart is empty")
    if view["cartWarning"]:
        raise ValueError("Minimum order not met (cart under ₹99)")
    order = {"orderId": "STUB-" + uuid.uuid4().hex[:8].upper(), "status": "CONFIRMED", "paymentMethod": paymentMethod,
             "addressId": addressId, "toPay": view["billBreakdown"]["toPay"]["value"], "items": view["items"],
             "placedAt": int(time.time()), "stub": True}
    _orders.insert(0, order)
    _cart.update(addressId=None, items=[])
    return order


@mcp.tool(structured_output=True)
def get_delivery_status(orderId: str, addressId: str) -> dict[str, Any]:
    """Latest delivery ETA and status for an Instamart order."""
    order = next((o for o in _orders if o["orderId"] == orderId), None)
    if order is None:
        raise ValueError("Order not found")
    mins = max(0, 15 - int((time.time() - order["placedAt"]) / 60))
    return {"orderId": orderId, "etaText": f"Arriving in {mins} min" if mins else "Delivered", "delivered": mins == 0,
            "cancelled": False, "pollIntervalSec": 60, "disabled": False}


@mcp.tool(structured_output=True)
def get_orders() -> dict[str, Any]:
    """Instamart order history (most recent first)."""
    return {"orders": [{k: o[k] for k in ("orderId", "status", "toPay", "placedAt")} for o in _orders]}


# ---------------------------------------------------------------- OAuth endpoints
@mcp.custom_route("/images/{pid}.svg", methods=["GET"])
async def product_image(request: Request) -> Response:
    emoji, bg = _LOOK.get(request.path_params["pid"], ("🛒", "#f4f4f5"))
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120"><rect width="120" height="120" rx="18" '
           f'fill="{bg}"/><text x="60" y="64" font-size="64" text-anchor="middle" dominant-baseline="middle">{emoji}</text></svg>')
    return Response(svg, media_type="image/svg+xml", headers={"Cache-Control": "public, max-age=86400"})


@mcp.custom_route("/.well-known/oauth-authorization-server", methods=["GET"])
async def as_metadata(request: Request) -> Response:
    return JSONResponse({
        "issuer": _origin(request), "authorization_endpoint": f"{PUBLIC_URL}/auth/authorize",
        "token_endpoint": f"{_origin(request)}/auth/token", "registration_endpoint": f"{_origin(request)}/auth/register",
        "revocation_endpoint": f"{_origin(request)}/auth/logout", "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"], "scopes_supported": ["mcp:tools", "mcp:resources", "mcp:prompts"],
    })


@mcp.custom_route("/.well-known/oauth-protected-resource", methods=["GET"])
async def pr_metadata(request: Request) -> Response:
    return JSONResponse({"resource": _origin(request), "authorization_servers": [_origin(request)],
                         "scopes_supported": ["mcp:tools", "mcp:resources", "mcp:prompts"]})


@mcp.custom_route("/auth/register", methods=["POST"])
async def register(request: Request) -> Response:
    body = await request.json()
    uris = body.get("redirect_uris") or []
    if not uris or not all(u.startswith("https://") or u.startswith("http://localhost") or u.startswith("http://127.0.0.1")
                           for u in uris):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    cid = "stub-client-" + secrets.token_hex(6)
    _clients[cid] = {"redirect_uris": uris, "client_name": body.get("client_name")}
    return JSONResponse({"client_id": cid, "redirect_uris": uris, "token_endpoint_auth_method": "none"}, status_code=201)


_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Swiggy sign-in (local stub)</title><style>
body{{font-family:system-ui,sans-serif;background:#f6f6f6;margin:0;display:grid;place-items:center;min-height:100vh}}
form{{background:#fff;padding:28px;border-radius:14px;box-shadow:0 2px 12px #0001;width:min(340px,90vw)}}
h1{{font-size:18px;margin:0 0 4px}} p{{color:#666;font-size:13px;margin:0 0 16px}}
label{{font-size:13px;display:block;margin:10px 0 4px}} input{{width:100%;box-sizing:border-box;padding:9px;border:1px solid #ccc;border-radius:8px}}
button{{margin-top:16px;width:100%;padding:10px;border:0;border-radius:8px;background:#fc8019;color:#fff;font-weight:600}}
.stub{{background:#fff4e5;color:#8a4b00;padding:8px;border-radius:8px;font-size:12px;margin-bottom:12px}} .err{{color:#b00020;font-size:13px}}
</style></head><body><form method="post" action="/auth/authorize">
<div class="stub">Local development stub, not Swiggy. Any phone number works; the OTP is <b>123456</b>.</div>
<h1>Sign in to Swiggy</h1><p>Bulbul wants access to Instamart on your behalf.</p>{error}
<label>Phone number</label><input name="phone" required value="9876543210">
<label>OTP</label><input name="otp" required inputmode="numeric" autocomplete="one-time-code">
{hidden}<button type="submit">Verify and continue</button></form></body></html>"""


def _client_ok(cid: str, redirect_uri: str) -> bool:
    return cid in _clients and redirect_uri in _clients[cid]["redirect_uris"]  # exact match, as documented


@mcp.custom_route("/auth/authorize", methods=["GET", "POST"])
async def authorize(request: Request) -> Response:
    params = dict(request.query_params) if request.method == "GET" else dict(await request.form())
    cid, redirect_uri = params.get("client_id", ""), params.get("redirect_uri", "")
    if not _client_ok(cid, redirect_uri):
        return HTMLResponse("<h1>Bad client_id or redirect_uri (must exactly match registration)</h1>", status_code=400)
    if params.get("code_challenge_method") != "S256" or not params.get("code_challenge"):
        return HTMLResponse("<h1>PKCE S256 required</h1>", status_code=400)
    keep = ["client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope", "response_type"]
    hidden = "".join(f'<input type="hidden" name="{k}" value="{html.escape(params.get(k, ""))}">' for k in keep)
    if request.method == "GET":
        return HTMLResponse(_PAGE.format(error="", hidden=hidden))
    if params.get("otp") != OTP:
        return HTMLResponse(_PAGE.format(error='<p class="err">Wrong OTP. Use 123456.</p>', hidden=hidden), status_code=401)
    code = secrets.token_urlsafe(16)
    _codes[code] = {"client_id": cid, "redirect_uri": redirect_uri, "challenge": params["code_challenge"],
                    "user": "stub-user-" + params.get("phone", "")[-4:], "exp": time.time() + 120}
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': params.get('state', '')})}", status_code=302)


@mcp.custom_route("/auth/token", methods=["POST"])
async def token(request: Request) -> Response:
    ctype = request.headers.get("content-type", "")
    body = await request.json() if "json" in ctype else dict(await request.form())
    grant = body.get("grant_type")
    if grant == "authorization_code":
        info = _codes.pop(body.get("code", ""), None)
        if not info or info["exp"] < time.time() or info["redirect_uri"] != body.get("redirect_uri"):
            return JSONResponse({"error": "invalid_grant", "error_description": "bad or expired code"}, status_code=400)
        digest = base64.urlsafe_b64encode(hashlib.sha256(body.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if digest != info["challenge"]:
            return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
        return JSONResponse(_issue(info["user"]))
    if grant == "refresh_token":
        info = _tokens.pop(body.get("refresh_token", ""), None)
        if not info:
            return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)
        out = _issue(info["user"])
        out["expires_in"] = 2592000
        return JSONResponse(out)
    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def logout(request: Request) -> Response:
    auth = request.headers.get("authorization", "")
    _tokens.pop(auth[7:], None)
    return JSONResponse({"ok": True})


def build_app():
    """Starlette app: MCP at /im behind Bearer auth, plus the OAuth routes."""
    _load_state()
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    # Keep the SDK's DNS-rebinding protection, but accept the names this stub is really reached by
    # (e.g. "swiggy-stub:8765" from the backend container, "localhost:8766" from the browser).
    hosts = {"127.0.0.1:*", "localhost:*"} | {urlparse(u).netloc for u in (BASE_URL, PUBLIC_URL)}
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=sorted(hosts),
                                         allowed_origins=sorted({f"http://{h}" for h in hosts}))
    app = mcp.streamable_http_app(streamable_http_path="/im", stateless_http=True, json_response=True,
                                  transport_security=security)
    app.add_middleware(_BearerGuard)
    app.add_middleware(_SaveAfterRequest)
    return app


class _SaveAfterRequest:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        await self.app(scope, receive, send)
        if scope["type"] == "http" and scope.get("method") == "POST":
            _save_state()


class _BearerGuard:
    """401 + JSON-RPC -32001 on /im without a valid Bearer token (the documented auth failure)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/im" and _valid_token(Request(scope)) is None:
            resp = JSONResponse({"jsonrpc": "2.0", "error": {"code": -32001, "message": "Unauthorized"}, "id": None},
                                status_code=401,
                                headers={"WWW-Authenticate": f'Bearer resource_metadata="{_origin(Request(scope))}/.well-known/oauth-protected-resource"'})
            return await resp(scope, receive, send)
        return await self.app(scope, receive, send)


if __name__ == "__main__":
    import uvicorn

    host_port = BASE_URL.rsplit(":", 1)
    port = int(os.environ.get("PORT") or (host_port[1] if len(host_port) == 2 and host_port[1].isdigit() else 8765))
    uvicorn.run(build_app(), host=os.environ.get("HOST", "127.0.0.1"), port=port, log_level="info")
