"""Long-term memory: small, stable facts about the user, kept across conversations.

After each reply, a fast model call reads the user's latest message(s) and updates a short list
of facts ("Name is Asha.", "Usually cooks for 4.", "Craves Hyderabadi chicken biryani.").
Facts are stored per user in the LangGraph store and fed back to the supervisor and responder,
so Bulbul can say "you mentioned..." and pick sensible defaults instead of asking.

Only what the USER said counts. The user can see and delete every fact (GET/DELETE /api/memory).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

FACTS_NS = "facts"
PROFILE_NS = "profile"   # older structured slots (e.g. dietary_restrictions from clarify answers)
MAX_FACTS = 60

Category = Literal["identity", "household", "food", "diet", "pantry", "preference", "other"]


class Fact(BaseModel):
    key: str = Field(description="short snake_case id, e.g. 'name', 'household_size', 'craving_biryani'")
    text: str = Field(description="one short third-person sentence, e.g. 'Usually cooks for 4 people.'")
    category: Category = "other"


class MemoryUpdate(BaseModel):
    remember: list[Fact] = Field(default_factory=list, description="new or changed facts (reuse a key to update)")
    forget: list[str] = Field(default_factory=list, description="keys the user contradicted or asked to forget")


MEMORY_SYSTEM = """You keep a short memory of stable facts about the user of a personal assistant.

Read the user's latest message(s) and update the memory:
- Only facts the USER stated or clearly confirmed. Never facts from the assistant's own words.
- Keep things useful later: their name, who they cook/shop for (household size), foods and
  dishes they like, crave or dislike, diet and allergies, what is usually (not) in their
  kitchen, preferred stores, city. Skip passing moods ("bored tonight") and one-off details.
- One short third-person sentence per fact. Reuse an existing key to update a fact; put the
  key of any fact the user contradicted or asked you to forget in `forget`.
- Most messages contain nothing worth remembering: then return empty lists.

## Current memory
{facts}"""

_TRIVIAL = {"yes", "yeah", "yep", "no", "nope", "ok", "okay", "sure", "thanks", "thank", "you", "hi", "hello",
            "hey", "please", "done", "next", "cool", "nice", "great", "haan", "nahi", "go", "ahead", "it"}


def worth_reading(text: str) -> bool:
    """Skip the memory call for messages that can't contain a fact ("yes", "no thanks", "hi")."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    return bool(words) and not set(words) <= _TRIVIAL


async def load(store, user_id: str) -> list[dict]:
    """All remembered facts (plus legacy profile slots), oldest first."""
    if store is None:
        return []
    out = []
    for item in await store.asearch((user_id, FACTS_NS), limit=MAX_FACTS):
        out.append({"key": item.key, "text": item.value.get("text", ""), "category": item.value.get("category", "other"),
                    "updated_at": item.updated_at.isoformat() if item.updated_at else None, "ns": FACTS_NS})
    for item in await store.asearch((user_id, PROFILE_NS)):
        val = item.value.get("value")
        if val:
            out.append({"key": item.key, "text": f"{item.key.replace('_', ' ').capitalize()}: {val}.", "category": "diet",
                        "updated_at": item.updated_at.isoformat() if item.updated_at else None, "ns": PROFILE_NS})
    return sorted(out, key=lambda f: f["updated_at"] or "")


async def forget(store, user_id: str, key: str) -> bool:
    for ns in (FACTS_NS, PROFILE_NS):
        if await store.aget((user_id, ns), key):
            await store.adelete((user_id, ns), key)
            return True
    return False


async def update_from_turn(router, store, user_id: str, user_text: str) -> MemoryUpdate | None:
    """Ask a fast model what to remember from the user's words, and apply it. Never raises."""
    if store is None or not worth_reading(user_text):
        return None
    facts = await load(store, user_id)
    listed = "\n".join(f"- {f['key']}: {f['text']}" for f in facts if f["ns"] == FACTS_NS) or "(empty)"
    try:
        upd = await router.structured("memory", [SystemMessage(MEMORY_SYSTEM.format(facts=listed)),
                                                 HumanMessage(f"User said:\n{user_text}")], MemoryUpdate)
    except Exception as exc:  # noqa: BLE001 - memory is best-effort
        log.info("memory: skipped (%s)", type(exc).__name__)
        return None
    now = datetime.now(UTC).isoformat()
    for key in upd.forget:
        await forget(store, user_id, key)
    for f in upd.remember[:5]:
        key = re.sub(r"[^a-z0-9_]+", "_", f.key.lower()).strip("_")[:60] or "fact"
        await store.aput((user_id, FACTS_NS), key, {"text": f.text.strip()[:200], "category": f.category, "at": now})
    if upd.remember or upd.forget:
        log.info("memory: +%s -%s", [f.key for f in upd.remember], upd.forget)
    return upd
