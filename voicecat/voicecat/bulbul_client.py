"""Talks to Bulbul's HTTP API (the same one the web app uses): runs, resumes, chat state.

Voice is just another client of Bulbul; nothing here imports Bulbul code.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx


class BulbulClient:
    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None, headers: dict[str, str] | None = None):
        self.base_url = base_url.rstrip("/")
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=180.0), headers=headers)

    async def close(self) -> None:
        await self._http.aclose()

    async def create_chat(self) -> str:
        r = await self._http.post(f"{self.base_url}/api/threads", json={})
        r.raise_for_status()
        return r.json()["id"]

    async def chat_exists(self, chat_id: str) -> bool:
        r = await self._http.get(f"{self.base_url}/api/threads/{chat_id}/messages")
        return r.status_code == 200

    async def pending(self, chat_id: str) -> dict | None:
        """The question or card this chat is waiting on, if any."""
        r = await self._http.get(f"{self.base_url}/api/threads/{chat_id}/state")
        return r.json().get("pending") if r.status_code == 200 else None

    def say(self, chat_id: str, text: str) -> AsyncIterator[dict]:
        return self._events(f"/api/threads/{chat_id}/runs", {"message": text, "voice": True})

    def answer(self, chat_id: str, value: dict) -> AsyncIterator[dict]:
        return self._events(f"/api/threads/{chat_id}/runs/resume", {"value": value})

    async def _events(self, path: str, body: dict) -> AsyncIterator[dict]:
        async with self._http.stream("POST", f"{self.base_url}{path}", json=body,
                                     headers={"Accept": "text/event-stream"}) as r:
            if r.status_code >= 400:
                detail = (await r.aread()).decode(errors="replace")[:200]
                raise RuntimeError(f"Bulbul returned {r.status_code}: {detail}")
            async for line in r.aiter_lines():
                if line.startswith("data: "):
                    yield json.loads(line[6:])
