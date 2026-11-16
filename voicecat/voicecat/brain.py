"""The brain: hands each user turn to Bulbul and turns Bulbul's events into speech.

- A new turn becomes a Bulbul message, or the answer to whatever Bulbul is waiting on (a
  question, a cart or order card), so a driver can say "yes", "skip the ghee" or "biryani".
- Bulbul's reply streams into TTS token by token; when an agent starts working, a short filler
  ("Let me check Instamart.") fills the silence.
- Cards are read out in a driving-friendly way (counts and totals, not every line); the full card
  goes to the screen.
- Barge-in / talking while Bulbul thinks: the running request is NOT cancelled. It finishes
  quietly (its answer stays in the chat) and the new words are handled right after.
"""

from __future__ import annotations

import asyncio
import logging
import re

from voicecat.frames import BotText, BotTurnEnd, ClientEvent, UserTurn
from voicecat.bulbul_client import BulbulClient
from voicecat.pipeline import Direction, Processor
from voicecat.tts import speakable

log = logging.getLogger(__name__)

_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "go", "ahead", "please", "haan", "correct", "right",
        "confirm", "proceed", "do", "it"}
_NO = {"no", "nope", "nah", "not", "don't", "dont", "stop", "cancel", "nahi"}


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z']+", text.lower()))


def is_yes(text: str) -> bool:
    w = _words(text)
    return bool(w & _YES) and not (w & _NO)


def _rupees(value: float) -> str:
    return f"{round(value):,} rupees".replace(",", "")


def _list_names(names: list[str], limit: int = 3) -> str:
    short = [re.sub(r"\s*\(.*?\)", "", n).strip() for n in names[:limit]]
    if not short:
        return ""
    if len(names) > limit:
        return ", ".join(short) + " and more"
    return short[0] if len(short) == 1 else ", ".join(short[:-1]) + " and " + short[-1]


def speak_card(card: dict) -> str:
    """What to SAY for a card Bulbul is waiting on (the screen shows the full card)."""
    kind = card.get("kind")
    if kind == "clarify":
        parts = []
        for q in card.get("questions", []):
            line = q.get("text", "").strip()
            opts = [o for o in q.get("options") or [] if o]
            if opts:
                line += " You can say " + (opts[0] if len(opts) == 1 else ", ".join(opts[:-1]) + " or " + opts[-1]) + "."
            parts.append(line)
        return " ".join(parts)
    if kind == "confirm_plan":
        steps = "; then ".join(s.get("goal", "") for s in card.get("plan", []))
        return f"Here's my plan: {steps}. Shall I go ahead?"
    if kind == "approve":
        lead = (card.get("notice") + " ") if card.get("notice") else ""
        for a in card.get("actions", []):
            if a.get("tool") == "update_cart" and a.get("items"):
                items = a["items"]
                total = sum((i.get("price") or 0) * (i.get("quantity") or 1) for i in items)
                names = _list_names([i.get("name", "") for i in items])
                return (f"{lead}I've picked {len(items)} item{'s' if len(items) != 1 else ''}, including {names}, "
                        f"for about {_rupees(total)}. Shall I add them to your cart? You can also tell me what to change.")
            if a.get("tool") == "checkout":
                d = a.get("description", "")
                m = re.search(r"(\d+) items?, ₹([\d,.]+) to pay, paid by (.+?), delivered to (\w+)", d)
                if m:
                    return (f"{lead}Ready to place your order: {m.group(1)} items, {m.group(2).replace(',', '')} rupees, "
                            f"{m.group(3)}, delivered to {m.group(4)}. Shall I place it?")
                return f"{lead}{speakable(d)} Shall I place it?"
        descs = " ".join(speakable(a.get("description", "")) for a in card.get("actions", []))
        return f"{lead}{descs} Shall I go ahead?"
    return ""


def answer_for(card: dict, text: str) -> dict:
    """The resume value Bulbul expects for this card, from what the user said."""
    kind = card.get("kind")
    if kind == "clarify":
        return {"answers": {"_free_text": text}}
    if kind == "approve":
        return {"edit": text}  # Bulbul reads "yes" / "no" / "skip the ghee"
    if kind == "confirm_plan":
        return {"approved": True} if is_yes(text) else {"approved": False, "feedback": text}
    return {"edit": text}


class BulbulBrain(Processor):
    def __init__(self, client: BulbulClient, fillers: dict | None = None):
        super().__init__()
        self.client = client
        self.fillers = fillers or {}
        self.pending: dict | None = None   # what Bulbul is waiting on (question / card), last seen
        self._said_while_busy: list[str] = []  # what the user said while Bulbul was busy
        self._run_task: asyncio.Task | None = None
        self._quiet = False                # finish the current request without speaking it

    async def on_stop(self) -> None:
        if self._run_task and not self._run_task.done():
            self._run_task.cancel()

    async def on_interrupt(self) -> None:
        if self.session and self.session.brain_busy:
            self._quiet = True  # let it finish quietly; the answer stays in the chat

    async def process(self, frame, direction: Direction) -> None:
        if isinstance(frame, UserTurn):
            await self.push(ClientEvent({"type": "user_turn", "text": frame.text}))
            if self.session.brain_busy:
                self._said_while_busy.append(frame.text)
                self._quiet = True
                await self.push(ClientEvent({"type": "queued", "text": frame.text}))
            else:
                self._start(frame.text)
            return
        await self.push(frame, direction)

    def _start(self, text: str) -> None:
        self.session.brain_busy = True
        self._run_task = asyncio.create_task(self._run(text))

    def _filler(self, agent: str) -> str:
        return self.fillers.get(agent) or self.fillers.get("default") or ""

    async def _run(self, text: str) -> None:
        if not self.session.bot_speaking:
            await self.session.set_state("thinking")
        # Ask Bulbul what it's waiting on now: the user may have tapped a card in the app meanwhile.
        try:
            card = await self.client.pending(self.session.chat_id)
        except Exception:  # noqa: BLE001
            card = self.pending
        self.pending = None
        events = self.client.answer(self.session.chat_id, answer_for(card, text)) if card \
            else self.client.say(self.session.chat_id, text)
        # The chat UI shows this request (and, below, every Bulbul event) like one it made itself.
        await self.push(ClientEvent({"type": "run", "text": text, "answer": bool(card)}))
        filler_said = ended = False
        try:
            async for ev in events:
                t = ev.get("type")
                ended = ended or t == "run_end"
                await self.push(ClientEvent({"type": "bulbul", "event": ev}))
                if t == "token":
                    if not self._quiet:
                        await self.push(BotText(ev["text"]))
                elif t == "agent_start":
                    filler = self._filler(ev.get("agent", ""))
                    if filler and not filler_said and not self._quiet:
                        filler_said = True
                        await self.push(BotText(filler + " "))
                        await self.push(BotTurnEnd())  # say it now, don't wait for more text
                elif t == "interrupt":
                    self.pending = {k: v for k, v in ev.items() if k != "type"}
                    await self.push(ClientEvent({"type": "card", "card": self.pending}))
                    if not self._quiet:
                        await self.push(BotText(speak_card(self.pending) + " "))
                elif t == "error":
                    await self.push(ClientEvent(ev))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("Bulbul request failed: %s", exc)
            if not self._quiet:
                await self.push(BotText("Sorry, I couldn't reach Bulbul just now. "))
            await self.push(ClientEvent({"type": "error", "message": str(exc)}))
        finally:
            if not ended:  # the chat UI must not wait forever for this request
                await self.push(ClientEvent({"type": "bulbul", "event": {"type": "run_end", "status": "failed"}}))
            await self.push(BotTurnEnd())
            self.session.brain_busy = False
            self._quiet = False
            if self._said_while_busy:
                nxt, self._said_while_busy = " ".join(self._said_while_busy), []
                self._start(nxt)
            # The light goes back to "listening" at the output, once this reply's speech has
            # actually been sent and played (see TransportOut), not when the text stops streaming.
