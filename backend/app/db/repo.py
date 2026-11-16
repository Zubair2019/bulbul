"""App data access. Two interchangeable backends: Postgres and in-memory (dev/tests)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from psycopg.types.json import Jsonb

SCHEMA_SQL = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def _now() -> datetime:
    return datetime.now(UTC)


class Repo(Protocol):
    async def init(self, user_id: str) -> None: ...
    async def create_thread(self, user_id: str, title: str = "New chat") -> dict: ...
    async def list_threads(self, user_id: str) -> list[dict]: ...
    async def get_thread(self, user_id: str, thread_id: str) -> dict | None: ...
    async def rename_thread(self, thread_id: str, title: str) -> None: ...
    async def add_message(self, thread_id: str, role: str, content: str, kind: str = "text", meta: dict | None = None) -> dict: ...
    async def list_messages(self, thread_id: str) -> list[dict]: ...
    async def start_run(self, thread_id: str) -> str: ...
    async def end_run(self, run_id: str, status: str, error: str | None = None) -> None: ...
    async def set_run_trace(self, run_id: str, trace_id: str) -> None: ...
    async def list_runs(self, thread_id: str) -> list[dict]: ...
    async def record_response(self, thread_id: str, run_id: str, kind: str, response: dict) -> None: ...
    # Integrations (tokens arrive already encrypted; the repo never sees plaintext).
    async def get_oauth_client(self, provider: str, base_url: str, redirect_uri: str) -> str | None: ...
    async def save_oauth_client(self, provider: str, base_url: str, redirect_uri: str, client_id: str) -> None: ...
    async def get_token(self, user_id: str, provider: str, base_url: str) -> dict | None: ...
    async def save_token(self, user_id: str, provider: str, base_url: str, access_token: str,
                         refresh_token: str | None, expires_at: datetime) -> None: ...
    async def delete_token(self, user_id: str, provider: str, base_url: str) -> None: ...


class MemoryRepo:
    def __init__(self) -> None:
        self.threads: dict[str, dict] = {}
        self.messages: dict[str, list[dict]] = {}
        self.runs: dict[str, dict] = {}
        self.responses: list[dict] = []
        self.oauth_clients: dict[tuple, str] = {}
        self.tokens: dict[tuple, dict] = {}

    async def init(self, user_id: str) -> None:
        return None

    async def create_thread(self, user_id, title="New chat"):
        t = {"id": str(uuid.uuid4()), "user_id": user_id, "title": title, "created_at": _now(), "updated_at": _now()}
        self.threads[t["id"]] = t
        self.messages[t["id"]] = []
        return t

    async def list_threads(self, user_id):
        ts = [t for t in self.threads.values() if t["user_id"] == user_id]
        return sorted(ts, key=lambda t: t["updated_at"], reverse=True)

    async def get_thread(self, user_id, thread_id):
        t = self.threads.get(thread_id)
        return t if t and t["user_id"] == user_id else None

    async def rename_thread(self, thread_id, title):
        self.threads[thread_id]["title"] = title

    async def add_message(self, thread_id, role, content, kind="text", meta=None):
        m = {"id": str(uuid.uuid4()), "thread_id": thread_id, "role": role, "kind": kind,
             "content": content, "meta": meta or {}, "created_at": _now()}
        self.messages[thread_id].append(m)
        self.threads[thread_id]["updated_at"] = _now()
        return m

    async def list_messages(self, thread_id):
        return list(self.messages.get(thread_id, []))

    async def start_run(self, thread_id):
        rid = str(uuid.uuid4())
        self.runs[rid] = {"id": rid, "thread_id": thread_id, "status": "running", "error": None,
                          "trace_id": None, "started_at": _now(), "ended_at": None}
        return rid

    async def end_run(self, run_id, status, error=None):
        self.runs[run_id].update(status=status, error=error, ended_at=_now())

    async def set_run_trace(self, run_id, trace_id):
        self.runs[run_id]["trace_id"] = trace_id

    async def list_runs(self, thread_id):
        return sorted((dict(r) for r in self.runs.values() if r["thread_id"] == thread_id),
                      key=lambda r: r["started_at"])

    async def record_response(self, thread_id, run_id, kind, response):
        self.responses.append({"thread_id": thread_id, "run_id": run_id, "kind": kind, "response": response})

    async def get_oauth_client(self, provider, base_url, redirect_uri):
        return self.oauth_clients.get((provider, base_url, redirect_uri))

    async def save_oauth_client(self, provider, base_url, redirect_uri, client_id):
        self.oauth_clients[(provider, base_url, redirect_uri)] = client_id

    async def get_token(self, user_id, provider, base_url):
        t = self.tokens.get((user_id, provider, base_url))
        return dict(t) if t else None

    async def save_token(self, user_id, provider, base_url, access_token, refresh_token, expires_at):
        self.tokens[(user_id, provider, base_url)] = {
            "access_token": access_token, "refresh_token": refresh_token, "expires_at": expires_at}

    async def delete_token(self, user_id, provider, base_url):
        self.tokens.pop((user_id, provider, base_url), None)


class PostgresRepo:
    """Uses a psycopg AsyncConnectionPool configured with dict_row + autocommit."""

    def __init__(self, pool) -> None:
        self.pool = pool

    async def _fetch(self, sql: str, *args: Any) -> list[dict]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, args)
            return await cur.fetchall() if cur.description else []

    async def init(self, user_id):
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA_SQL, prepare=False)  # multi-statement: never prepared
            await conn.execute("INSERT INTO users (id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))

    async def create_thread(self, user_id, title="New chat"):
        rows = await self._fetch(
            "INSERT INTO threads (id, user_id, title) VALUES (%s, %s, %s) RETURNING *",
            uuid.uuid4(), user_id, title,
        )
        return _norm(rows[0])

    async def list_threads(self, user_id):
        rows = await self._fetch(
            "SELECT * FROM threads WHERE user_id = %s ORDER BY updated_at DESC LIMIT 200", user_id
        )
        return [_norm(r) for r in rows]

    async def get_thread(self, user_id, thread_id):
        try:
            tid = uuid.UUID(thread_id)
        except ValueError:
            return None
        rows = await self._fetch("SELECT * FROM threads WHERE id = %s AND user_id = %s", tid, user_id)
        return _norm(rows[0]) if rows else None

    async def rename_thread(self, thread_id, title):
        await self._fetch("UPDATE threads SET title = %s WHERE id = %s", title, uuid.UUID(thread_id))

    async def add_message(self, thread_id, role, content, kind="text", meta=None):
        tid = uuid.UUID(thread_id)
        rows = await self._fetch(
            "INSERT INTO messages (id, thread_id, role, kind, content, meta) VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
            uuid.uuid4(), tid, role, kind, content, Jsonb(meta or {}),
        )
        await self._fetch("UPDATE threads SET updated_at = now() WHERE id = %s", tid)
        return _norm(rows[0])

    async def list_messages(self, thread_id):
        rows = await self._fetch(
            "SELECT * FROM messages WHERE thread_id = %s ORDER BY created_at", uuid.UUID(thread_id)
        )
        return [_norm(r) for r in rows]

    async def start_run(self, thread_id):
        rid = uuid.uuid4()
        await self._fetch(
            "INSERT INTO runs (id, thread_id, status) VALUES (%s, %s, 'running')", rid, uuid.UUID(thread_id)
        )
        return str(rid)

    async def end_run(self, run_id, status, error=None):
        await self._fetch(
            "UPDATE runs SET status = %s, error = %s, ended_at = now() WHERE id = %s", status, error, uuid.UUID(run_id)
        )

    async def set_run_trace(self, run_id, trace_id):
        await self._fetch("UPDATE runs SET trace_id = %s WHERE id = %s", trace_id, uuid.UUID(run_id))

    async def list_runs(self, thread_id):
        rows = await self._fetch(
            "SELECT id, thread_id, status, error, trace_id, started_at, ended_at FROM runs "
            "WHERE thread_id = %s ORDER BY started_at", uuid.UUID(thread_id),
        )
        return [_norm(r) for r in rows]

    async def record_response(self, thread_id, run_id, kind, response):
        await self._fetch(
            "INSERT INTO approvals (id, thread_id, run_id, kind, response) VALUES (%s,%s,%s,%s,%s)",
            uuid.uuid4(), uuid.UUID(thread_id), uuid.UUID(run_id) if run_id else None, kind, Jsonb(response),
        )

    async def get_oauth_client(self, provider, base_url, redirect_uri):
        rows = await self._fetch(
            "SELECT client_id FROM oauth_clients WHERE provider=%s AND base_url=%s AND redirect_uri=%s",
            provider, base_url, redirect_uri,
        )
        return rows[0]["client_id"] if rows else None

    async def save_oauth_client(self, provider, base_url, redirect_uri, client_id):
        await self._fetch(
            "INSERT INTO oauth_clients (provider, base_url, redirect_uri, client_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (provider, base_url, redirect_uri) DO UPDATE SET client_id = EXCLUDED.client_id",
            provider, base_url, redirect_uri, client_id,
        )

    async def get_token(self, user_id, provider, base_url):
        rows = await self._fetch(
            "SELECT access_token, refresh_token, expires_at FROM integration_tokens "
            "WHERE user_id=%s AND provider=%s AND base_url=%s", user_id, provider, base_url,
        )
        return dict(rows[0]) if rows else None

    async def save_token(self, user_id, provider, base_url, access_token, refresh_token, expires_at):
        await self._fetch(
            "INSERT INTO integration_tokens (user_id, provider, base_url, access_token, refresh_token, expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (user_id, provider, base_url) DO UPDATE SET "
            "access_token=EXCLUDED.access_token, refresh_token=EXCLUDED.refresh_token, "
            "expires_at=EXCLUDED.expires_at, updated_at=now()",
            user_id, provider, base_url, access_token, refresh_token, expires_at,
        )

    async def delete_token(self, user_id, provider, base_url):
        await self._fetch(
            "DELETE FROM integration_tokens WHERE user_id=%s AND provider=%s AND base_url=%s",
            user_id, provider, base_url,
        )


def _norm(row: dict) -> dict:
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in row.items()}
