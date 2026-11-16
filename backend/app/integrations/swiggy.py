"""Swiggy MCP integration: OAuth 2.1 + PKCE sign-in, token refresh, and MCP tool calls.

Follows https://mcp.swiggy.com/builders/docs/start/authenticate/ :
- the client registers itself via Dynamic Client Registration (no client id to apply for);
- the user signs in with phone + OTP in the browser (/auth/authorize), we get a code at our
  redirect URI and exchange it (with the PKCE verifier) at /auth/token;
- access tokens last 5 days; refresh with grant_type=refresh_token when <= 60s remain or on 401,
  and fall back to signing in again only if the refresh fails;
- tool results are read from `structuredContent` (see `read_tool_data`).

`SWIGGY_BASE_URL` picks the server: the local stub (http://127.0.0.1:8765), staging
(https://mcp-staging.swiggy.com, once Swiggy allowlists the redirect URI) or production.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.db.repo import Repo
from app.integrations.crypto import TokenCipher

log = logging.getLogger(__name__)

PROVIDER = "swiggy"
SERVERS = {"instamart": "im", "food": "food", "dineout": "dineout", "scenes": "scenes"}
SCOPE = "mcp:tools"
_PENDING_TTL = 600


class NotConnected(RuntimeError):
    """The user has not connected Swiggy (or the session expired and could not be refreshed)."""


class ToolCallError(RuntimeError):
    """A Swiggy tool returned an error; the message is safe to show the user."""


def read_tool_data(result: Any) -> Any:
    """Machine-readable payload of an MCP CallToolResult (Swiggy's documented helper, in Python)."""
    structured = getattr(result, "structuredContent", None) or getattr(result, "structured_content", None)
    text = next((b.text for b in (result.content or []) if getattr(b, "type", "") == "text"), None)
    envelope = None
    if text:
        try:
            envelope = json.loads(text)
        except json.JSONDecodeError:
            pass

    def failed(p: Any) -> bool:
        return isinstance(p, dict) and p.get("success") is False and p.get("error") is not None

    is_error = getattr(result, "isError", None) or getattr(result, "is_error", False)
    if is_error or failed(structured) or failed(envelope):
        error = (structured or {}).get("error") if isinstance(structured, dict) else None
        error = error or ((envelope or {}).get("error") if isinstance(envelope, dict) else None)
        message = error.get("message") if isinstance(error, dict) else error
        raise ToolCallError(message or text or "Tool call failed")
    if structured:
        return structured
    if isinstance(envelope, dict):
        siblings = {k: v for k, v in envelope.items() if k not in ("success", "message", "data")}
        if "data" not in envelope:
            return siblings
        data = envelope["data"]
        return {**siblings, **data} if isinstance(data, dict) else data
    raise ToolCallError("Tool result has no machine-readable payload")


def _pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def _all_errors(exc: BaseException, seen: set[int] | None = None):
    """The error and everything inside it: exception-group members, causes and contexts.
    The MCP client reports a 401 as an MCPError nested inside anyio exception groups."""
    seen = seen if seen is not None else set()
    if exc is None or id(exc) in seen:
        return
    seen.add(id(exc))
    yield exc
    for inner in getattr(exc, "exceptions", ()) or ():
        yield from _all_errors(inner, seen)
    for linked in (exc.__cause__, exc.__context__):
        if linked is not None:
            yield from _all_errors(linked, seen)


def _is_auth_error(exc: BaseException) -> bool:
    for e in _all_errors(exc):
        status = getattr(getattr(e, "response", None), "status_code", None)
        code = getattr(e, "code", None) or getattr(getattr(e, "error", None), "code", None)
        text = f"{type(e).__name__}: {e}"
        if status in (401, 419) or code == -32001 or "Unauthorized" in text or " 401" in text or "-32001" in text:
            return True
    return False


class SwiggyIntegration:
    def __init__(self, *, base_url: str, redirect_uri: str, repo: Repo, cipher: TokenCipher,
                 client_factory=None, http: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self.redirect_uri = redirect_uri
        self.repo = repo
        self.cipher = cipher
        self._client_factory = client_factory  # tests: (server, token) -> MCP Client
        self._http = http
        self._meta: dict | None = None
        self._pending: dict[str, dict] = {}   # state -> {user_id, verifier, ts}

    # ------------------------------------------------------------ info
    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    @property
    def mode(self) -> str:
        if not self.base_url:
            return "off"
        if any(h in self.base_url for h in ("127.0.0.1", "localhost", "stub")):
            return "stub"
        return "staging" if "staging" in self.base_url else "production"

    def server_url(self, server: str) -> str:
        return f"{self.base_url}/{SERVERS.get(server, server)}"

    # ------------------------------------------------------------ OAuth
    async def _post(self, url: str, body: dict | None = None, headers: dict | None = None) -> httpx.Response:
        if self._http:
            return await self._http.post(url, json=body, headers=headers)
        async with httpx.AsyncClient(timeout=20) as c:
            return await c.post(url, json=body, headers=headers)

    async def _get(self, url: str) -> httpx.Response:
        if self._http:
            return await self._http.get(url)
        async with httpx.AsyncClient(timeout=20) as c:
            return await c.get(url)

    async def metadata(self) -> dict:
        if self._meta is None:
            r = await self._get(f"{self.base_url}/.well-known/oauth-authorization-server")
            r.raise_for_status()
            self._meta = r.json()
        return self._meta

    async def client_id(self) -> str:
        """Our OAuth client id for this server + redirect URI, registering once via DCR."""
        cid = await self.repo.get_oauth_client(PROVIDER, self.base_url, self.redirect_uri)
        if cid:
            return cid
        meta = await self.metadata()
        r = await self._post(meta.get("registration_endpoint") or f"{self.base_url}/auth/register", {
            "client_name": "Bulbul", "redirect_uris": [self.redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": SCOPE,
        })
        if r.status_code >= 400:
            raise RuntimeError(f"Swiggy client registration failed ({r.status_code}): {r.text[:200]}")
        cid = r.json()["client_id"]
        await self.repo.save_oauth_client(PROVIDER, self.base_url, self.redirect_uri, cid)
        log.info("swiggy: registered OAuth client for %s", self.base_url)
        return cid

    async def start_auth(self, user_id: str) -> str:
        """URL to send the user's browser to (Swiggy's phone + OTP sign-in)."""
        now = time.time()
        abandoned = any(p["user_id"] == user_id for p in self._pending.values())
        self._pending = {s: p for s, p in self._pending.items() if now - p["ts"] < _PENDING_TTL}
        meta = await self.metadata()
        if abandoned:
            # The user's last sign-in never came back. A common cause is a client registration the
            # server no longer accepts (the authorize page then fails before redirecting to us),
            # so register a fresh client instead of reusing the cached one.
            log.info("swiggy: previous sign-in did not complete; registering a fresh OAuth client")
            await self.repo.save_oauth_client(PROVIDER, self.base_url, self.redirect_uri, "")
        cid = await self.client_id()
        verifier, challenge = _pkce()
        state = secrets.token_urlsafe(24)
        self._pending[state] = {"user_id": user_id, "verifier": verifier, "ts": now}
        query = urlencode({
            "response_type": "code", "client_id": cid, "redirect_uri": self.redirect_uri,
            "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "scope": SCOPE,
        })
        return f"{meta.get('authorization_endpoint') or self.base_url + '/auth/authorize'}?{query}"

    async def finish_auth(self, state: str, code: str) -> str:
        """Handle the redirect: verify state, exchange the code, store tokens. Returns the user id."""
        pending = self._pending.pop(state, None)
        if pending is None or time.time() - pending["ts"] > _PENDING_TTL:
            raise RuntimeError("This sign-in link expired or was already used. Please connect again.")
        meta = await self.metadata()
        r = await self._post(meta.get("token_endpoint") or f"{self.base_url}/auth/token", {
            "grant_type": "authorization_code", "code": code, "code_verifier": pending["verifier"],
            "redirect_uri": self.redirect_uri, "client_id": await self.client_id(),
        })
        if r.status_code >= 400:
            raise RuntimeError(f"Swiggy sign-in failed: {r.json().get('error_description', r.text[:200])}")
        await self._store(pending["user_id"], r.json())
        return pending["user_id"]

    async def _store(self, user_id: str, tok: dict) -> None:
        expires_at = datetime.now(UTC) + timedelta(seconds=int(tok.get("expires_in", 3600)))
        await self.repo.save_token(user_id, PROVIDER, self.base_url, self.cipher.encrypt(tok["access_token"]),
                                   self.cipher.encrypt(tok.get("refresh_token")), expires_at)

    async def _load(self, user_id: str) -> dict | None:
        row = await self.repo.get_token(user_id, PROVIDER, self.base_url)
        if not row:
            return None
        access = self.cipher.decrypt(row["access_token"])
        if not access:
            return None
        return {"access": access, "refresh": self.cipher.decrypt(row.get("refresh_token")), "expires_at": row["expires_at"]}

    async def refresh(self, user_id: str) -> str:
        tok = await self._load(user_id)
        if not tok or not tok["refresh"]:
            raise NotConnected("Swiggy isn't connected")
        meta = await self.metadata()
        r = await self._post(meta.get("token_endpoint") or f"{self.base_url}/auth/token",
                             {"grant_type": "refresh_token", "refresh_token": tok["refresh"]})
        if r.status_code >= 400:
            await self.repo.delete_token(user_id, PROVIDER, self.base_url)  # invalid_grant: must sign in again
            raise NotConnected("Your Swiggy session expired. Please connect Swiggy again.")
        new = r.json()
        await self._store(user_id, new)
        return new["access_token"]

    async def access_token(self, user_id: str) -> str:
        tok = await self._load(user_id)
        if not tok:
            raise NotConnected("Swiggy isn't connected")
        if tok["expires_at"] - datetime.now(UTC) <= timedelta(seconds=60):
            return await self.refresh(user_id)
        return tok["access"]

    async def status(self, user_id: str) -> dict:
        tok = await self._load(user_id) if self.enabled else None
        return {"id": PROVIDER, "name": "Swiggy", "enabled": self.enabled, "mode": self.mode,
                "connected": bool(tok), "expires_at": tok["expires_at"].isoformat() if tok else None}

    async def disconnect(self, user_id: str) -> None:
        tok = await self._load(user_id)
        if tok:
            try:
                meta = await self.metadata()
                url = meta.get("revocation_endpoint") or f"{self.base_url}/auth/logout"
                await self._post(url, headers={"Authorization": f"Bearer {tok['access']}"})
            except Exception as exc:  # noqa: BLE001 - best effort
                log.info("swiggy: logout call failed: %s", exc)
        await self.repo.delete_token(user_id, PROVIDER, self.base_url)

    # ------------------------------------------------------------ MCP
    def _make_client(self, server: str, token: str):
        if self._client_factory:
            return self._client_factory(server, token)
        import httpx2
        from mcp import Client
        from mcp.client.streamable_http import streamable_http_client

        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30)
        return Client(streamable_http_client(self.server_url(server), http_client=http))

    @asynccontextmanager
    async def session(self, user_id: str, server: str = "instamart") -> AsyncIterator[Any]:
        """An open MCP client for one agent run. Refreshes the token once on an auth failure."""
        token = await self.access_token(user_id)
        try:
            client = self._make_client(server, token)
            await client.__aenter__()
        except Exception as exc:  # noqa: BLE001
            if not _is_auth_error(exc):
                raise
            client = self._make_client(server, await self.refresh(user_id))
            await client.__aenter__()
        try:
            yield client
        finally:
            await client.__aexit__(None, None, None)

    async def call(self, user_id: str, server: str, tool: str, arguments: dict) -> Any:
        """One tool call in its own session (used to execute approved actions)."""
        async with self.session(user_id, server) as client:
            return read_tool_data(await client.call_tool(tool, arguments))
