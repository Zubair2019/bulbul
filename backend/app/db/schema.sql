-- App tables. LangGraph creates its own checkpoint/store tables via .setup().
-- Phase 1 applies this idempotently at startup; switch to Alembic once the schema settles.

CREATE TABLE IF NOT EXISTS users (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS threads (
    id          UUID PRIMARY KEY,
    user_id     TEXT NOT NULL REFERENCES users(id),
    title       TEXT NOT NULL DEFAULT 'New chat',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS threads_user_updated ON threads (user_id, updated_at DESC);

-- Display copy of the conversation (fast history loading; checkpoints hold the real state).
CREATE TABLE IF NOT EXISTS messages (
    id          UUID PRIMARY KEY,
    thread_id   UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    role        TEXT NOT NULL,                -- user | assistant
    kind        TEXT NOT NULL DEFAULT 'text', -- text | clarify | confirm_plan | approve | error
    content     TEXT NOT NULL DEFAULT '',
    meta        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS messages_thread_created ON messages (thread_id, created_at);

CREATE TABLE IF NOT EXISTS runs (
    id          UUID PRIMARY KEY,
    thread_id   UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,                -- running | done | interrupted | cancelled | failed
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    ended_at    TIMESTAMPTZ
);

-- Audit trail of every clarify / plan / approval answer the user gave.
CREATE TABLE IF NOT EXISTS approvals (
    id          UUID PRIMARY KEY,
    thread_id   UUID NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
    run_id      UUID,
    kind        TEXT NOT NULL,
    response    JSONB NOT NULL,
    decided_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- OAuth clients registered via Dynamic Client Registration, one per (provider, server, redirect).
CREATE TABLE IF NOT EXISTS oauth_clients (
    provider      TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    redirect_uri  TEXT NOT NULL,
    client_id     TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, base_url, redirect_uri)
);

-- Per-user integration tokens. Token columns are encrypted (Fernet, key = BULBUL_SECRET_KEY).
CREATE TABLE IF NOT EXISTS integration_tokens (
    user_id        TEXT NOT NULL REFERENCES users(id),
    provider       TEXT NOT NULL,
    base_url       TEXT NOT NULL,
    access_token   TEXT NOT NULL,
    refresh_token  TEXT,
    expires_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, provider, base_url)
);

-- Langfuse trace of each run (one request), for debugging from the chat ID.
ALTER TABLE runs ADD COLUMN IF NOT EXISTS trace_id TEXT;
