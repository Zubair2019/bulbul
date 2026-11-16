# Bulbul

A multi-agent assistant for day-to-day tasks. A **supervisor** agent asks clarifying questions,
plans, and delegates to pluggable **specialist agents** (chef, shopping, nutrition, ...). Anything
with side effects (buying, sending) waits for your approval.

| Layer | Tech |
|---|---|
| Agents | Python, LangGraph (supervisor graph), LangChain chat models |
| Models | Per-agent fallback chains in `backend/config/models.yaml` (Gemini → OpenAI → Anthropic → Grok). The UI's model menu lists the `catalog` there: the picked model runs every step, and the chain stays behind it as fallback. |
| API | FastAPI, Server-Sent Events |
| UI | React + TypeScript (Vite, Tailwind) |
| Data | Postgres (LangGraph checkpoints + long-term memory + app tables); Redis later |
| Tracing | Langfuse (optional): one trace per chat turn, grouped by conversation (see [Tracing](#tracing)) |
| Integrations | MCP servers (phase 2) |

## Run it with Docker (everything)

Works the same on macOS, Linux and Windows. You need only [Docker](https://docs.docker.com/get-docker/)
(Docker Desktop on macOS/Windows, Docker Engine with the Compose plugin on Linux) and git.

```bash
git clone https://github.com/Zubair2019/bulbul.git
cd bulbul
cp .env.example .env          # Windows cmd: copy .env.example .env   (PowerShell: cp works)
```

Open `.env` and add **at least one model API key**: `GOOGLE_API_KEY` (a free Gemini key from
https://aistudio.google.com/apikey works), `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `XAI_API_KEY`.
Everything else is optional:

| Key | What it adds |
|---|---|
| `DEEPGRAM_API_KEY` | Good voice: Deepgram speech recognition and voice. Without it, voice uses the local Whisper model (downloaded on first use) and the browser's voice. |
| `LANGFUSE_*` | Tracing (see [Tracing](#tracing)). |
| `BULBUL_SECRET_KEY` | Keeps the Swiggy connection across restarts. Generate one with `docker compose run --rm backend python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |

Then start it:

```bash
docker compose up --build -d
```

The first build takes a few minutes. Open http://localhost:8080. Four containers run:

- **postgres**: the database (data kept in the `bulbul_pgdata` volume).
- **backend**: FastAPI and voice, not published to the host.
- **frontend**: nginx serves the built UI and proxies `/api` (including the SSE streams and the voice WebSocket) to the backend.
- **swiggy-stub**: a local stand-in for Swiggy Instamart (see [Swiggy](#swiggy-instamart-shopping)). No real orders.

Handy commands:

```bash
docker compose logs -f backend      # watch the backend
docker compose up --build -d        # rebuild after code changes
docker compose down                 # stop (add -v to also delete the database)
```

**Port already in use?** Set a different host port in `.env`, for example `BULBUL_PORT=8090`
(the app), `POSTGRES_PORT=5433` (the database, published on 127.0.0.1 only) or
`SWIGGY_STUB_PORT=8767` (the stub's sign-in page), then run `docker compose up -d` again.

**Voice** needs the browser's microphone permission. Browsers allow the microphone on
`http://localhost`, but not on plain `http://` from another machine, so open the app on the computer
that runs Docker (or put it behind HTTPS).

**Linux only:** the backend runs as user id 1000. If your user id is different (`id -u`) and you
use local Whisper, let it write its model: `chmod a+w voicecat/models`.

The backend reads API keys from `.env`. Its `DATABASE_URL` is overridden to point at the `postgres` service.

## Run it locally (for development)

Prerequisites: [uv](https://docs.astral.sh/uv/), Node 20+, Docker. This mode gives hot reload in the frontend.

```bash
cp .env.example .env          # then add at least one model API key
docker compose up -d postgres  # only the database
```

Backend (http://127.0.0.1:8000):

```bash
cd backend
uv sync
uv run python run.py
```

Frontend (http://localhost:5173):

```bash
cd frontend
npm install
npm run dev
```

No Postgres handy? Leave `DATABASE_URL` empty and the backend runs with in-memory storage.

## Voice (voicecat)

Hands-free voice, built for shopping while driving. Tap the **mic button** left of Send to talk to Bulbul in the open chat. Your words, Bulbul's replies and cards appear in the chat as you talk, and a card can be answered by voice or by tapping. To interrupt Bulbul, talk over it.

- **Where the code lives.** All voice code is in [voicecat/](voicecat/README.md), its own package. The backend mounts it at `/api/voice` (WebSocket `/api/voice/ws`), so voice runs on the same server and port as the app.
- **Speech providers are pluggable.**
  - Best feel: Deepgram for both (one key), or Cartesia / ElevenLabs for the voice.
  - Free: Whisper and Kokoro (local), or the browser's own speech.
- **Local models.** They go in `voicecat/models/`, which Docker mounts into the backend.
- **Turning it off.** Set `VOICE_ENABLED=false`.

## Conversation design

Bulbul should feel like one person you talk to, and work the same in a future voice mode:

- **Persona.** Bulbul's character and voice live in `backend/config/persona.md`. Edit it and restart the backend to apply. It shapes every reply and how questions are worded; app mechanics (cards, approvals, store offers) stay in code.
- **One voice.** Replies are 1-3 spoken-style sentences. Long material (a recipe) arrives as a collapsible card under the reply. Agents and plans stay behind the scenes ("Details" toggle).
- **Memory.** After each reply, a fast model call notes stable facts you stated (name, household size, cravings, diet, pantry, store). They're stored per user and used in later chats ("You mentioned Hyderabadi chicken biryani before. Still craving that?"). Remembered household size fills servings. See and delete them under **What Bulbul remembers** in the sidebar (`GET/DELETE /api/memory`).
- **One topic per turn.** Agents marked `only_on_request` (shopping) never run unless you ask. After a recipe, Bulbul asks: *"Do you have the ingredients at home, or should I check Swiggy Instamart?"*
- **Price check, then cart.** Accepting the offer ("yes", "I don't have anything") makes Bulbul check the store and summarize it in one sentence (items, rough total, what's missing). The picture cart appears only after your yes.
- **Change the cart your way.** Tap: tick/untick, quantity steppers. Or say it, before or while the card is open: "skip the saffron", "add 6 eggs and remove the milk", "yes". Nothing changes in your account until you approve.
- **Questions are spoken.** Questions appear as Bulbul talking, with tap-or-say chips; typing an answer works too.

## Swiggy (Instamart shopping)

The `shopping` agent shops on Swiggy Instamart through Swiggy's MCP server. It is a tool-using agent: it reads freely (addresses, product search, cart, orders), but **every cart change and every order waits for your approval**, with an approval card written from the real tool data. The order is always a separate approval after you have seen the full bill.

Swiggy allowlists OAuth redirect URIs during their review, so development runs against a **local stub** (`backend/stubs/swiggy_instamart.py`). The stub mirrors Swiggy's documented tools, OAuth 2.1 + PKCE, Dynamic Client Registration and result shapes, and places no real orders.

```bash
cd backend && uv run python -m stubs.swiggy_instamart   # http://127.0.0.1:8765
```

Set `SWIGGY_BASE_URL=http://127.0.0.1:8765` and `BULBUL_SECRET_KEY` in `.env`, restart the backend, and click **Connect** next to Swiggy in the sidebar. On the stub's sign-in page any phone number works and the OTP is `123456`.

Going live:
1. Apply at https://mcp.swiggy.com/builders/docs/operate/access/ with your redirect URI (`{PUBLIC_URL}/api/integrations/swiggy/callback`) and a short demo video.
2. Once allowlisted, set `SWIGGY_BASE_URL=https://mcp-staging.swiggy.com` (seeded data, no real orders).
3. Switch to `https://mcp.swiggy.com` after Swiggy approves production.

Tokens are stored encrypted (Fernet, `BULBUL_SECRET_KEY`), refreshed before expiry, and revoked on Disconnect. In Docker, compose runs its own stub (`swiggy-stub`, sign-in page on http://localhost:8766, state in the `swiggy_stub` volume). Connect once on http://localhost:8080; the dev app on 5173 and the Docker app each have their own stub, so each needs its own Connect.

## Tracing

Set `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` and `LANGFUSE_BASE_URL` (your project's region) in `.env` and restart the backend. With no keys, tracing is off and nothing changes.

- **One trace per chat turn**, named `chat-turn`: a new message, or the answer to a question / plan / approval card. Its input is what you typed; its output is what Bulbul replied or asked.
- **Sessions = conversations.** The thread id is the session id, so the Sessions view replays a whole chat. The user id, `environment` (`development`, or `docker` in compose), version, the picked model and tags (`message` / `resume`, `model:<id>`) are set on every observation.
- **Tree:** `handle-chat-turn` (agent) → `supervisor-graph` → graph nodes. Each subagent is its own `agent` node (`chef-agent`, ...) with its model calls inside. Generations are named by purpose (`decide-next-step`, `generate-chef-result`, `write-reply`) and carry the model, tokens, and attempt / fallback metadata. A failed model attempt shows as an ERROR generation before the fallback.
- **Privacy:** credential-like strings (API keys, bearer tokens) are masked before export. Output-parser spans are dropped as noise.
- **Linking:** `run_start` events and stored assistant messages carry the `trace_id`, for attaching user feedback scores later.

All of this lives in `backend/app/tracing.py`. The Langfuse agent skill used to set it up is in `.claude/skills/langfuse`.

## Debugging a chat

Every chat has one UUID, and everything hangs off it:

- **URL:** `/c/<chat-id>`. Reloads, links and back/forward keep the chat.
- **In the app:** the `# 2534de8d` chip top-right copies the full chat ID. **Details > IDs** shows the chat, the last request (run) and its Langfuse trace, plus **Open debug view**.
- **Logs:** every backend line is tagged `[chat=<first 8 chars> run=<first 8 chars>]`, e.g. `docker compose logs backend | grep chat=2534de8d`.
- **Debug view:** `GET /api/threads/<chat-id>/debug` returns messages, every run (status, error, Langfuse trace link), the live conversation state (pending card, offer, held cart, plan), a Langfuse session link and what Bulbul remembers.
- **Langfuse:** session = chat ID, one trace per request.

## Tests

```bash
cd backend && uv run pytest
```

The tests drive the full graph and API with a scripted fake model, so they need no API keys.

## How it works

```
START → load_context → supervisor ─clarify──────→ clarify ──────→ supervisor
                                  ─confirm_plan─→ confirm_plan ─→ supervisor
                                  ─delegate─────→ delegate ─┬───→ supervisor
                                                            ├───→ clarify   (agent needs input)
                                                            └───→ approve   (agent proposed side effects)
                                  ─respond──────→ respond → END
```

Rules enforced in code (not just prompts), in `backend/app/graph/builder.py`:

- **Clarify before acting**: an agent is never called until all its `required_inputs` are known
  (from the conversation, your answers, or memory). Otherwise the user is asked, at most 3 questions at once.
- **Plans for multi-step work** are shown for confirmation before running.
- **Side effects need approval**: agents only *propose* actions; the approval step decides.
- **Loop guards**: a step cap (`MAX_SUPERVISOR_STEPS`, default 12) and repeat detection.

### Adding an agent

1. Add `backend/app/agents/defs/<name>.yaml` (name, description, required inputs) and a prompt in `defs/prompts/`.
2. Add `<name>: { chain: default }` under `agents:` in `backend/config/models.yaml` (optional; unknown agents use `general`'s chain).

The supervisor routes on the `description`, so say what the agent does and does not do.

### Streaming contract

`POST /api/threads/{id}/runs` and `POST /api/threads/{id}/runs/resume` return `text/event-stream`
with `data: {"type": ...}` frames: `run_start`, `plan_update`, `agent_start`, `agent_end`, `token`,
`model_fallback`, `interrupt`, `error`, `run_end`. Types live in `backend/app/runtime.py` and
`frontend/src/lib/events.ts`.

## Roadmap

- Phase 2: MCP tools for agents (shopping, nutrition, calendar), shared food-state schema.
- Phase 3: real auth, memory settings page, Redis for background runs, self-hosted Langfuse.
