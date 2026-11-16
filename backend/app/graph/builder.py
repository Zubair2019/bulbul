"""The Bulbul supervisor graph.

    START -> load_context -> supervisor --clarify------> clarify ------> supervisor
                                        --confirm_plan-> confirm_plan -> supervisor
                                        --delegate-----> delegate --+--> supervisor
                                                                    +--> clarify  (agent needs input)
                                                                    +--> approve  (agent proposed side effects)
                                                                    +--> respond  (price check: cart held for a "yes",
                                                                                   or a one-step request is done)
                                        --approve------> approve      (user said yes to a held cart)
                                        --revise_cart--> revise_cart -> approve   (cart changed by chat)
                                        --respond------> respond -> remember -> END

Rules that must never depend on the model behaving are enforced here in code:
required agent inputs, the step cap, repeat detection, and approval of side effects.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage
from langgraph.config import get_store, get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import interrupt

from app import memory
from app.agents.registry import Registry
from app.graph.prompts import RESPONDER_SYSTEM, SUPERVISOR_SYSTEM
from app.llm.router import AllModelsFailed, Router
from app.schemas import KV, AgentResult, PlanStep, Question, SupervisorDecision

log = logging.getLogger(__name__)

HISTORY_WINDOW = 40
MAX_QUESTIONS = 2  # voice-friendly: prefer one question at a time


class BulbulState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    plan: list[dict]           # PlanStep.model_dump() items
    plan_confirmed: bool
    step_count: int
    decision: dict | None      # last SupervisorDecision.model_dump()
    assumptions: list[str]
    user_ctx: dict
    note: str | None           # system note for the responder (e.g. step cap reached)
    current_step: str | None   # id of the step delegate just ran
    answers: dict              # this request's clarify answers, by question id (= input name)
    pause_for_user: bool       # set by approve: reply now instead of planning the next step
    request_text: str          # what the user asked in this request (new message)
    offer: dict | None         # the next step Bulbul offered at the end of its last reply (kept across requests)
    accepted_offer: dict | None  # that offer, if this request accepted it ("yes", or tapping the chip)
    suggestions: list[str]     # quick replies shown under this reply
    attachments: list[dict]    # long results shown as cards under this reply ({title, body})
    held_cart: dict | None     # price check done: a proposed cart waiting for the user's "yes"
    cart_edit: str | None      # a change to the proposed cart asked for in words ("skip the ghee")
    remembered_for: str | None  # request already read by the memory step (avoid re-reading on resume)
    cart_notice: str | None    # shown on the cart card when a change asked for in words couldn't be applied
    voice: bool                # this request came by voice (voicecat): the reply will be spoken
    recent_agents: list[str]   # agents that ran in the previous request (follow-ups continue with them)
    blocked: dict | None       # an only-on-request agent the model wanted but the user didn't ask for
    declined_agent: str | None  # the user just said no to an offer of this agent
    single_step: bool          # the supervisor's last decision was a one-step request (reply right after it)


def _user_id(config) -> str:
    return config["configurable"]["user_id"]


def _emit(event: dict[str, Any]) -> None:
    get_stream_writer()(event)


def _fallback_hook(agent: str):
    return lambda info: _emit({"type": "model_fallback", "agent": agent, **info})


def _lookup(ctx: dict, dotted: str | None):
    cur: Any = ctx
    for part in (dotted or "").split("."):
        if not part or not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _plan_text(plan: list[dict]) -> str:
    if not plan:
        return "(no steps yet)"
    out = []
    for s in plan:
        step = PlanStep.model_validate(s)
        line = f"- [{step.status}] {step.agent}: {step.goal}"
        if step.result:
            line += f"\n  result ({step.result.status}): {step.result.summary}"
            if step.result.details:
                line += f"\n  details: {step.result.details[:4000]}"
        out.append(line)
    return "\n".join(out)


_NUMBER_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "seven": "7", "eight": "8", "nine": "9", "ten": "10", "twelve": "12", "dozen": "12",
}
_NONE_WORDS = {"none", "no", "nothing", "nope", "n/a", "na"}


def _tokens(text: str) -> set[str]:
    toks = set(re.findall(r"[a-z0-9]+", text.lower()))
    return toks | {_NUMBER_WORDS[t] for t in toks if t in _NUMBER_WORDS}


def _user_text(state: BulbulState) -> set[str]:
    """Tokens of the recent conversation: what the user said, and what Bulbul already showed them
    (e.g. a recipe's ingredient list that the user now wants to buy)."""
    parts = [m.content for m in state.get("messages", [])[-HISTORY_WINDOW:]
             if isinstance(m, (HumanMessage, AIMessage)) and isinstance(m.content, str)]
    return _tokens(" ".join(parts))


def _model_outage(task: str, exc: Exception) -> str:
    """Short, user-facing summary of an all-models-failed error (full text goes to the log)."""
    text = str(exc)
    reasons = []
    if "429" in text or "RESOURCE_EXHAUSTED" in text or "RateLimit" in text:
        reasons.append("rate limit / quota")
    if "503" in text or "UNAVAILABLE" in text or "overloaded" in text.lower():
        reasons.append("model overloaded")
    if "imeout" in text:
        reasons.append("timeout")
    why = ", ".join(reasons) or "provider error"
    return f"No model was available for {task} ({why}). Add another provider key or try again shortly."


_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please", "haan", "ha", "han", "alright",
        "go", "do", "definitely", "absolutely", "of course", "approve", "confirm", "place", "proceed",
        "good", "perfect", "great", "fine", "correct", "right", "show"}
_NO = {"no", "nope", "nah", "not", "later", "skip", "nahi", "mat"}


_DONT_HAVE = re.compile(r"\b(don'?t|do not|dont|haven'?t)\s+have\b|\bnothing (at|in)\b|\bno ingredients\b|"
                        r"\bout of\b|\bneed (to buy|everything|them|it all)\b|\bcheck\b")
_EDIT_WORDS = {"add", "remove", "skip", "without", "except", "more", "less", "extra", "instead", "replace",
               "drop", "minus", "plus", "double", "swap", "fewer", "also", "only", "change", "make"}
_DECLINE_ONLY = _NO | {"thanks", "thank", "you", "now", "not", "right", "maybe", "i", "it", "for", "today", "fine"}


def _is_decline(text: str) -> bool:
    words = set(re.findall(r"[a-z]+", text.lower()))
    return bool(words) and bool(words & _NO) and words <= _DECLINE_ONLY | {"have", "them", "i've", "ive", "got"}


def _cart_edit_intent(text: str, offer: dict) -> bool:
    """Is this a change to the held/shown cart ("skip the ghee", "add eggs", "2 kg rice instead")?"""
    words = set(re.findall(r"[a-z]+", text.lower()))
    names = {w for item in offer.get("item_names", []) for w in re.findall(r"[a-z]{4,}", item.lower())}
    return bool(words & _EDIT_WORDS or words & names)


def _accepts(text: str, offer: dict) -> bool:
    """Did the user take the offered next step? ("yes", "sure, go ahead", or the chip's own words)."""
    t = text.strip().lower()
    if not t:
        return False
    if t in {offer.get("label", "").lower(), *[s.lower() for s in offer.get("accept", [])]}:
        return True
    if offer.get("kind") == "agent" and _DONT_HAVE.search(t):
        return True  # "I don't have anything at home" -> go check the store
    words = set(re.findall(r"[a-z]+", t))
    if words & _NO and not words & {"buy", "order"}:
        return False
    store_words = set(re.findall(r"[a-z]+", (offer.get("store") or "").lower()))
    short = len(words) <= 8
    return bool(short and (words & _YES or words & store_words or words & {"buy", "order", "get"}))


def _asked_for(agent_def, state: BulbulState) -> bool:
    """True if this request asked for an only-on-request agent (or accepted Bulbul's offer of it).

    Robust to speech-to-text: keywords also match with spacing/punctuation removed
    ("in-stamart" -> "instamart"), and a follow-up right after the agent ran ("add six eggs
    too") counts as asking for it."""
    if (state.get("accepted_offer") or {}).get("agent") == agent_def.name:
        return True
    if agent_def.name in (state.get("recent_agents") or []):
        return True  # continuing what the agent was just doing
    text = " ".join([state.get("request_text") or "", *map(str, (state.get("answers") or {}).values())]).lower()
    squashed = re.sub(r"[^a-z0-9]", "", text)
    return any(k.lower() in text or re.sub(r"[^a-z0-9]", "", k.lower()) in squashed
               for k in agent_def.request_keywords)


_CART_WORDS = {"buy", "order", "add", "cart", "put", "purchase", "basket"}


def _explicit_cart_request(state: BulbulState) -> bool:
    """Did the user ask for a cart change outright ("buy milk on Instamart")? Otherwise (accepted a
    "should I check the store?" offer, "I don't have anything") Bulbul checks prices first."""
    words = set(re.findall(r"[a-z]+", (state.get("request_text") or "").lower()))
    return bool(words & _CART_WORDS)


def _accepted_text(offer: dict | None) -> str:
    if not offer or offer.get("kind") != "agent":
        return ""
    lines = [f"\n## The user just accepted Bulbul's offer\nOffer: {offer.get('question')}",
             f"Now delegate to `{offer['agent']}` ({offer.get('store')})."]
    if offer.get("goal"):  # an offer made after Bulbul held back from acting on its own
        lines.append(f"Goal: {offer['goal']}. Inputs: {offer.get('items') or '(as discussed)'}")
    elif offer.get("items"):
        lines.append("Use this ingredient list from the recipe already shown (copy it into `items`; "
                     "do not ask the chef again):\n" + offer["items"])
    return "\n".join(lines) + "\n"


_MEMORY_STOP = {"user", "user's", "users", "the", "has", "have", "been", "with", "their", "they", "likes", "like",
                "usually", "people", "lately", "these", "days", "that", "this", "from", "into", "about", "home"}


def _relevant_memory(state: BulbulState) -> list[str]:
    """What the reply may mention: who the user is (name), plus remembered facts that relate to
    what's being talked about right now. "How are you?" shouldn't bring up a biryani craving;
    "I feel like cooking" or a biryani recipe should."""
    ctx = state.get("user_ctx") or {}
    talk = _tokens(" ".join([state.get("request_text") or "", *map(str, (state.get("answers") or {}).values())])) \
        | _results_text(state)
    out = list(ctx.get("identity") or [])
    for fact in ctx.get("remembered") or []:
        words = {t for t in _tokens(fact) if len(t) >= 4 and t not in _MEMORY_STOP}
        if fact not in out and words & talk:
            out.append(fact)
    return out


def _results_text(state: BulbulState) -> set[str]:
    """Tokens of what earlier steps of this request produced (e.g. the chef's ingredient list)."""
    parts = []
    for s in state.get("plan", []):
        r = s.get("result") or {}
        parts += [r.get("summary") or "", r.get("details") or ""]
    return _tokens(" ".join(parts))


def _grounded(value: str, user_tokens: set[str], squashed: str = "") -> bool:
    """True if a proposed input value is traceable to what the user actually said.
    `squashed` is the user's text without spaces/punctuation, so speech-to-text splits like
    "in-stamart" still match "Instamart"."""
    vt = _tokens(value)
    if vt & _NONE_WORDS or value.strip().lower() in _NONE_WORDS:
        return bool(user_tokens & _NONE_WORDS)
    meaningful = {t for t in vt if t.isdigit() or len(t) >= 3}
    return bool(meaningful) and any(t in user_tokens or (len(t) >= 4 and t in squashed) for t in meaningful)


def _squashed_user_text(state: BulbulState) -> str:
    parts = [m.content for m in state.get("messages", [])[-HISTORY_WINDOW:]
             if isinstance(m, HumanMessage) and isinstance(m.content, str)]
    parts.append(state.get("request_text") or "")
    return re.sub(r"[^a-z0-9]", "", " ".join(parts).lower())


def _earlier_inputs(state: BulbulState, agent: str) -> dict[str, str]:
    """Inputs used by earlier steps of this run for the same agent (already validated)."""
    out: dict[str, str] = {}
    for s in state.get("plan", []):
        if s["agent"] == agent:
            out.update({kv["key"]: kv["value"] for kv in s.get("inputs", [])})
    return out


def _step_index(state: BulbulState) -> int:
    ids = [s["id"] for s in state.get("plan", [])]
    return ids.index(state["current_step"]) if state.get("current_step") in ids else len(ids) - 1


def load_persona(path) -> str:
    """Bulbul's character and voice from config/persona.md (HTML comments are notes for editors)."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        log.warning("persona file %s not found; using a minimal persona", path)
        return "You are Bulbul, the user's warm, attentive personal assistant. Talk briefly and naturally."
    return re.sub(r"<!--.*?-->", "", text, flags=re.S).strip()


def build_graph(router: Router, registry: Registry, max_steps: int, checkpointer=None, store=None,
                persona: str | None = None):
    persona = persona or load_persona(Path(__file__).resolve().parents[2] / "config" / "persona.md")
    # ---------------------------------------------------------------- nodes
    async def load_context(state: BulbulState, config) -> dict:
        ctx: dict = {}
        store_ = get_store()
        if store_ is not None:
            for item in await store_.asearch((_user_id(config), "profile")):
                ctx.setdefault("profile", {})[item.key] = item.value.get("value")
            facts = await memory.load(store_, _user_id(config))
            if facts:
                ctx["remembered"] = [f["text"] for f in facts]
                ctx["identity"] = [f["text"] for f in facts if f["category"] == "identity"]
        # The offer from Bulbul's last reply is answered by this request, or dropped if the user moved on.
        offer, text = state.get("offer"), state.get("request_text") or ""
        accepted, edit = None, None
        if offer and not _is_decline(text):
            if offer.get("kind") == "cart" and _cart_edit_intent(text, offer) and not _accepts(text, offer):
                accepted, edit = offer, text      # "skip the ghee and add eggs" before seeing the card
            elif _accepts(text, offer):
                accepted = offer
        declined = offer.get("agent") if offer and not accepted and _is_decline(text) else None
        return {"user_ctx": ctx, "offer": None, "accepted_offer": accepted, "cart_edit": edit, "held_cart": None,
                "declined_agent": declined, "blocked": None}

    async def supervisor(state: BulbulState, config) -> dict:
        held = state.get("accepted_offer") or {}
        if held.get("kind") == "cart" and held.get("step") and not state.get("plan"):
            # The user said yes (or asked for changes) to a cart found by a price check: show it.
            step = held["step"]
            route = "revise_cart" if state.get("cart_edit") else "approve"
            return {"plan": [step], "current_step": step["id"], "decision": {"action": route},
                    "step_count": state.get("step_count", 0) + 1}
        steps = state.get("step_count", 0)
        if steps >= max_steps:
            return {
                "decision": SupervisorDecision(action="respond").model_dump(),
                "note": f"Step limit ({max_steps}) reached. Summarize progress and ask the user how to continue.",
            }

        plan = state.get("plan", [])
        system = SUPERVISOR_SYSTEM.format(
            agents=registry.describe(),
            user_ctx=json.dumps(state.get("user_ctx") or {}, ensure_ascii=False) or "{}",
            plan_confirmed=state.get("plan_confirmed", False),
            plan=_plan_text(plan),
            accepted=_accepted_text(state.get("accepted_offer")),
            persona=persona,
        )
        history = state["messages"][-HISTORY_WINDOW:]
        try:
            decision = await router.structured(
                "supervisor", [SystemMessage(system), *history], SupervisorDecision, _fallback_hook("supervisor")
            )
        except AllModelsFailed as exc:
            log.warning("All models failed for the supervisor: %s", exc)
            _emit({"type": "error", "message": _model_outage("planning", exc)})
            return {
                "decision": SupervisorDecision(action="respond").model_dump(),
                "note": "The assistant could not reach any language model. Apologize briefly.",
                "step_count": steps + 1,
            }

        log.info("supervisor decision (raw): %s", decision.model_dump(exclude_defaults=True))
        raw_decision = chosen = decision
        enforced = _enforce(decision, state)
        retry_note = _missing_plan_inputs_note(decision, enforced)
        if retry_note:
            # A single-step "plan" is turned into a delegation, but the model often leaves the
            # inputs out of plan items. Ask once more for a proper delegation before bothering
            # the user with questions they may already have answered.
            log.info("supervisor: retrying, %s", retry_note)
            try:
                retry = await router.structured(
                    "supervisor", [SystemMessage(system), *history, SystemMessage(retry_note)],
                    SupervisorDecision, _fallback_hook("supervisor"),
                )
                log.info("supervisor decision (retry): %s", retry.model_dump(exclude_defaults=True))
                enforced, chosen = _enforce(retry, state), retry
            except AllModelsFailed:
                pass  # keep the clarify we already have
        decision = enforced
        assumptions = list(dict.fromkeys([*state.get("assumptions", []), *decision.assumptions]))
        # The model chose a single step for the whole request (multi-step requests come as a
        # plan of 2+ items), so once it's done, delegate can reply without asking again.
        single = chosen.action == "delegate" or (chosen.action == "confirm_plan" and len(chosen.plan) == 1)
        update = {"decision": decision.model_dump(), "assumptions": assumptions, "step_count": steps + 1,
                  "single_step": single and decision.action == "delegate"}
        held_back = registry.get(raw_decision.delegate_to or "") if raw_decision.action == "delegate" else None
        if held_back and held_back.d.only_on_request and decision.action == "respond":
            # The model wanted this agent but the user didn't ask: offer it instead of acting.
            items = next((kv.value for kv in raw_decision.inputs if kv.key == "items"), "")
            update["blocked"] = {"agent": held_back.d.name, "store": held_back.d.store or held_back.d.name,
                                 "goal": raw_decision.goal or "", "items": items}
        return update

    def _missing_plan_inputs_note(raw: SupervisorDecision, enforced: SupervisorDecision) -> str | None:
        """Correction for the model when a 1-step plan became a delegation without required inputs."""
        if raw.action != "confirm_plan" or len(raw.plan) != 1 or enforced.action != "clarify":
            return None
        step = raw.plan[0]
        agent = registry.get(step.agent)
        if agent is None:
            return None
        given = {kv.key for kv in step.inputs if kv.value and kv.value.strip()}
        names = [r.name for r in agent.d.required_inputs if r.name not in given]
        if not names:
            return None
        return (
            f"Correction: that was a single step, so choose `delegate` to `{agent.d.name}` directly "
            f"(no plan) and pass its required inputs {names} in `inputs`, using facts already in the "
            "conversation (including the user's answers). Choose `clarify` only for facts nobody has given."
        )

    def _unrequested(agent_name: str | None, state: BulbulState) -> bool:
        a = registry.get(agent_name or "")
        return bool(a and a.d.only_on_request and not _asked_for(a.d, state))

    def _enforce(d: SupervisorDecision, state: BulbulState) -> SupervisorDecision:
        """Code-level guardrails applied to every supervisor decision."""
        if d.action == "clarify" and not d.questions:
            return SupervisorDecision(action="respond", assumptions=d.assumptions)

        # Do only what was asked: drop steps for agents the user didn't ask for (e.g. shopping
        # added to a recipe request). Bulbul offers them after replying instead.
        if d.action == "confirm_plan" and any(_unrequested(p.agent, state) for p in d.plan):
            kept = [p for p in d.plan if not _unrequested(p.agent, state)]
            log.info("supervisor: dropped unrequested steps %s", [p.agent for p in d.plan if p not in kept])
            d = d.model_copy(update={"plan": kept})
        if d.action == "delegate" and _unrequested(d.delegate_to, state):
            log.info("supervisor: blocked unrequested delegation to %s", d.delegate_to)
            return SupervisorDecision(action="respond", assumptions=d.assumptions)
        # The same side-quest disguised as a question ("want me to order the ingredients?") after a
        # finished step: reply instead; Bulbul's own offer asks that question after the answer.
        if d.action == "clarify" and any(s.get("status") == "done" for s in state.get("plan", [])):
            asked = " ".join(q.text.lower() + " " + " ".join(q.options).lower() for q in d.questions)
            for a in registry.agents.values():
                if a.d.only_on_request and not _asked_for(a.d, state) and \
                        any(k.lower() in asked for k in a.d.request_keywords):
                    log.info("supervisor: turned an unrequested %s question into a reply", a.d.name)
                    return SupervisorDecision(action="respond", assumptions=d.assumptions)

        if d.action == "confirm_plan":
            if state.get("plan_confirmed") or len(d.plan) < 2:
                # Already confirmed, or not really multi-step: just carry on.
                first = d.plan[0] if d.plan else None
                if first is None:
                    return SupervisorDecision(action="respond", assumptions=d.assumptions)
                d = d.model_copy(update={"action": "delegate", "delegate_to": first.agent, "goal": first.goal,
                                         "inputs": first.inputs or d.inputs})
            else:
                unknown = [p.agent for p in d.plan if registry.get(p.agent) is None]
                if unknown:
                    return SupervisorDecision(action="respond", assumptions=d.assumptions)
                if not any(registry.get(p.agent).d.kind == "tools" or registry.get(p.agent).d.only_on_request
                           for p in d.plan):
                    # Nothing here acts in the world: just do it (no plan card), one step at a time.
                    done = {(s["agent"], s["goal"]) for s in state.get("plan", []) if s.get("status") == "done"}
                    nxt = next((p for p in d.plan if (p.agent, p.goal) not in done), d.plan[0])
                    d = d.model_copy(update={"action": "delegate", "delegate_to": nxt.agent, "goal": nxt.goal,
                                             "inputs": nxt.inputs or d.inputs})
                else:
                    return d

        if d.action != "delegate":
            return d

        agent = registry.get(d.delegate_to or "")
        if agent is None or not d.goal:
            return SupervisorDecision(action="respond", assumptions=d.assumptions)

        # Required inputs. A value counts only if it is grounded: the user said it, memory holds it,
        # or an earlier step of this run already used it. Anything else (including values the model
        # made up) becomes a clarifying question.
        proposed = {kv.key: kv.value for kv in d.inputs if kv.value and kv.value.strip()}
        # Grounded = said by the user, or produced by an earlier step (passing results forward).
        user_text = _user_text(state) | _results_text(state)
        if state.get("accepted_offer"):
            user_text |= _tokens(state["accepted_offer"].get("items") or "")  # the list the user said yes to
        # Facts the user told Bulbul in earlier chats are known too ("we're 4 at home" -> servings 4).
        user_text |= _tokens(" ".join((state.get("user_ctx") or {}).get("remembered") or []))
        earlier = _earlier_inputs(state, agent.d.name)
        answers = state.get("answers") or {}
        inputs = dict(proposed)
        missing: list[Question] = []
        squashed = _squashed_user_text(state)
        for req in agent.d.required_inputs:
            value = proposed.get(req.name)
            lookup = set().union(*(_tokens(w) for w in req.lookup_words)) if req.lookup_words else set()
            if value and lookup and _tokens(value) <= lookup:
                continue  # e.g. "my cart": not a fact anyone could invent
            if value and _grounded(value, user_text, squashed):
                continue
            remembered = _lookup(state.get("user_ctx") or {}, req.from_memory)
            if remembered:
                inputs[req.name] = str(remembered)
            elif req.name in earlier:
                inputs[req.name] = earlier[req.name]
            elif str(answers.get(req.name) or "").strip():
                inputs[req.name] = str(answers[req.name])  # the user answered this very question
            else:
                inputs.pop(req.name, None)
                missing.append(Question(id=req.name, text=req.ask, options=req.options))
        if missing:
            log.info("supervisor: forcing clarify for %s (proposed=%s)", [q.id for q in missing], proposed)
            return SupervisorDecision(action="clarify", questions=missing[:MAX_QUESTIONS], assumptions=d.assumptions)

        # Repeat detection: never re-run an identical finished step.
        for s in state.get("plan", []):
            if s["agent"] == agent.d.name and s["goal"] == d.goal and s["status"] in ("done", "failed"):
                return SupervisorDecision(action="respond", assumptions=d.assumptions)

        return d.model_copy(update={"inputs": [KV(key=k, value=v) for k, v in inputs.items()]})

    async def clarify(state: BulbulState, config) -> dict:
        d = SupervisorDecision.model_validate(state["decision"])
        questions = [q.model_dump() for q in d.questions[:MAX_QUESTIONS]]
        # Nothing with side effects before interrupt(): this node re-runs from the top on resume.
        reply = interrupt({"kind": "clarify", "questions": questions, "assumptions": d.assumptions})

        answers: dict = (reply or {}).get("answers", {}) if isinstance(reply, dict) else {"_free_text": str(reply)}
        asked = "\n".join(f"- {q['text']}" for q in questions)
        lines = [f"- {k}: {v}" for k, v in answers.items() if k != "_free_text" and str(v).strip()]
        if answers.get("_free_text"):
            lines.append(f"- (free text) {answers['_free_text']}")

        # Remember answers that map to memory-backed inputs (e.g. dietary restrictions),
        # both long-term (store) and for the rest of this run (user_ctx).
        store_ = get_store()
        ctx = {**(state.get("user_ctx") or {})}
        profile = dict(ctx.get("profile", {}))
        for a in registry.agents.values():
            for req in a.d.required_inputs:
                if req.from_memory and req.from_memory.startswith("profile.") and answers.get(req.name):
                    key = req.from_memory.split(".", 1)[1]
                    profile[key] = answers[req.name]
                    if store_ is not None:
                        await store_.aput((_user_id(config), "profile"), key, {"value": answers[req.name]})
        if profile:
            ctx["profile"] = profile

        given = {k: str(v) for k, v in answers.items() if k != "_free_text" and str(v).strip()}
        return {
            "user_ctx": ctx,
            "answers": {**(state.get("answers") or {}), **given},
            "messages": [
                AIMessage("\n".join(q["text"] for q in questions)),
                HumanMessage("My answers:\n" + ("\n".join(lines) or "- (no answer)")),
            ]
        }

    async def confirm_plan(state: BulbulState, config) -> dict:
        d = SupervisorDecision.model_validate(state["decision"])
        proposed = [p.model_dump() for p in d.plan]
        reply = interrupt({"kind": "confirm_plan", "plan": proposed, "assumptions": d.assumptions})
        approved = bool(reply.get("approved")) if isinstance(reply, dict) else False
        feedback = (reply.get("feedback") or "").strip() if isinstance(reply, dict) else ""

        plan_text = "\n".join(f"{i + 1}. {p['agent']}: {p['goal']}" for i, p in enumerate(proposed))
        msgs: list[AnyMessage] = [AIMessage(f"Here's my plan:\n{plan_text}")]
        if approved:
            steps = [
                PlanStep(id=uuid.uuid4().hex[:8], agent=p["agent"], goal=p["goal"]).model_dump() for p in proposed
            ]
            msgs.append(HumanMessage("Plan approved." + (f" Note: {feedback}" if feedback else "")))
            _emit({"type": "plan_update", "plan": steps})
            return {"messages": msgs, "plan": steps, "plan_confirmed": True}
        msgs.append(HumanMessage("I don't want that plan." + (f" {feedback}" if feedback else "")))
        return {"messages": msgs, "plan_confirmed": False}

    async def delegate(state: BulbulState, config) -> dict:
        d = SupervisorDecision.model_validate(state["decision"])
        agent = registry.get(d.delegate_to or "")
        plan = [PlanStep.model_validate(s) for s in state.get("plan", [])]

        step = next((s for s in plan if s.status == "pending" and s.agent == agent.d.name), None)
        if step is None:
            step = PlanStep(id=uuid.uuid4().hex[:8], agent=agent.d.name, goal=d.goal or "")
            plan.append(step)
        step.goal, step.inputs, step.status = d.goal or step.goal, d.inputs, "running"

        _emit({"type": "plan_update", "plan": [s.model_dump() for s in plan]})
        _emit({"type": "agent_start", "step_id": step.id, "agent": step.agent, "goal": step.goal})
        try:
            result = await agent.run(step.goal, step.inputs, on_fallback=_fallback_hook(agent.d.name))
        except AllModelsFailed as exc:
            result = AgentResult(status="failed", summary=f"All models failed: {exc}"[:500])
        step.result = result
        step.status = "failed" if result.status == "failed" else "done"

        _emit({"type": "agent_end", "step_id": step.id, "agent": step.agent, "status": result.status,
               "summary": result.summary})
        _emit({"type": "plan_update", "plan": [s.model_dump() for s in plan]})

        update: dict = {"plan": [s.model_dump() for s in plan], "current_step": step.id}
        cart = next((a for a in result.proposed_actions if a.tool == "update_cart" and a.items), None)
        if cart and not _explicit_cart_request(state):
            # Price check first: tell the user what the store has and the rough total; the cart card
            # comes only after they say yes (see respond / supervisor).
            update["held_cart"] = {"step": step.model_dump(), "store": agent.d.store or agent.d.name,
                                   "count": len(cart.items),
                                   "total": sum((i.price or 0) * i.quantity for i in cart.items),
                                   "item_names": [i.name for i in cart.items]}
        if result.status == "needs_input" and result.questions:
            update["decision"] = SupervisorDecision(action="clarify", questions=result.questions[:MAX_QUESTIONS]).model_dump()
        return update

    async def approve(state: BulbulState, config) -> dict:
        idx = _step_index(state)
        last = PlanStep.model_validate(state["plan"][idx])
        actions = [a.model_dump() for a in (last.result.proposed_actions if last.result else [])]
        payload = {"kind": "approve", "step_id": last.id, "agent": last.agent, "actions": actions}
        if state.get("cart_notice"):
            payload["notice"] = state["cart_notice"]
        reply = interrupt(payload)
        said = (reply.get("edit") or "").strip() if isinstance(reply, dict) else ""
        if said:
            # Typed or spoken while the card is open: "yes" approves, "no" declines, anything else
            # changes the cart ("remove the ghee, add eggs").
            is_cart = any(a.get("tool") == "update_cart" for a in actions)
            if _is_decline(said):
                reply = {"approved": False}
            elif is_cart and _cart_edit_intent(said, {"item_names": [i["name"] for a in actions for i in a.get("items") or []]}) \
                    and not _accepts(said, {"kind": "cart"}):
                return {"cart_edit": said, "messages": [HumanMessage(f"Change the cart: {said}")]}
            else:
                reply = {"approved": _accepts(said, {"kind": "approve"})}
        approved = bool(reply.get("approved")) if isinstance(reply, dict) else False
        listed = "\n".join(f"- {a['kind']}: {a['description']}" for a in actions)
        plan = [dict(s) for s in state["plan"]]
        result = dict(plan[idx]["result"])
        selected = reply.get("items") if isinstance(reply, dict) else None
        changes: list[str] = []
        if approved and isinstance(selected, list):
            # The user edited the cart on the card (unticked items / changed quantities).
            from app.agents.tool_agent import apply_selection

            edited = []
            for a in actions:
                if a.get("tool") == "update_cart" and a.get("items"):
                    new = apply_selection(a, selected)
                    kept = {i["spin_id"] for i in new["items"]}
                    dropped = [i["name"] for i in a["items"] if i["spin_id"] not in kept]
                    if dropped:
                        changes.append("removed " + ", ".join(dropped))
                    if not new["items"]:
                        continue  # nothing left to add: skip this action
                    a = new
                edited.append(a)
            actions = edited
            approved = bool(actions)
        if approved:
            # Execute the approved tool calls now, in order. This code runs after interrupt(), so
            # exactly once per approval (the node re-runs from the top only before the answer).
            agent = registry.get(last.agent)
            outcomes, views = [], []
            for a in actions:
                if a.get("tool") and hasattr(agent, "execute"):
                    outcomes.append(await agent.execute(a, _user_id(config), views))
                else:
                    outcomes.append(f"{a['kind']}: not executed (no tool is connected for it).")
            done = "\n".join(f"- {o}" for o in outcomes)
            chosen = [f"{i['name']} {i.get('size') or ''} × {i['quantity']}".replace("  ", " ")
                      for a in actions for i in (a.get("items") or [])]
            text = f"I approve these actions:\n{listed}"
            if chosen:
                text = "I approve the cart with only these items: " + "; ".join(chosen)
                text += f" ({'; '.join(changes)})." if changes else "."
            result["details"] = (result.get("details") or "") + f"\n\n### Done after the user approved\n{done}"
            result["summary"] = (result.get("summary") or "") + " | After approval: " + " ".join(outcomes)[:400]
            _emit({"type": "agent_end", "step_id": last.id, "agent": last.agent, "status": "done",
                   "summary": "Approved and done: " + " ".join(outcomes)[:300]})
        else:
            text = f"I do NOT approve these actions:\n{listed}"
            result["summary"] = (result.get("summary") or "") + " | The user declined; nothing was changed."
        result["proposed_actions"] = []
        if approved and views:
            result["cart"] = views[-1].model_dump()  # the updated cart, shown as a card with the bill
        plan[idx] = {**plan[idx], "result": result}
        _emit({"type": "plan_update", "plan": plan})
        update: dict = {"messages": [HumanMessage(text)], "plan": plan, "pause_for_user": False}

        ran = {a.get("tool") for a in actions} if approved else set()
        if "checkout" in ran:
            update.update(pause_for_user=True, note=(
                "An order was just placed after the user approved it. Confirm it: order id, what was "
                "ordered, amount to pay and payment method, address, and how to track it."))
        elif "update_cart" in ran:
            # Never chain checkout onto a cart change: show the bill, let the user decide.
            note = ("The cart was just updated after the user approved it, but NO order has been placed. "
                    "The cart card below your reply shows every item and the bill: don't list them. Say the total "
                    "to pay in one sentence and mention anything that couldn't be added. ")
            warnings = [w.strip().rstrip(".") for o in outcomes for w in re.findall(r"Warning: ([^.]+\.?)", o)]
            if warnings:
                note += ("IMPORTANT: the cart has a problem that blocks ordering: " + "; ".join(warnings)
                         + ". Say so plainly and ask what to add or change; do NOT offer to place the order yet.")
            else:
                note += "End by asking whether to place the order (cash on delivery to the same address)."
            update.update(pause_for_user=True, note=note)
        return update

    def _attachments(state: BulbulState) -> list[dict]:
        """Long results (a recipe) go into a collapsible card under the reply, not the spoken text."""
        out = []
        for s in state.get("plan", []):
            agent = registry.get(s["agent"])
            r = s.get("result") or {}
            if s.get("status") == "done" and r.get("cart") and (r["cart"].get("items") or []):
                store = r["cart"].get("store") or "store"
                out.append({"kind": "cart", "title": f"Your {store} cart", "cart": r["cart"],
                            "body": "\n".join(f"- {i['name']} {i.get('size') or ''} x {i['quantity']}"
                                              for i in r["cart"]["items"])})
                continue
            details = r.get("details") or ""
            if s.get("status") != "done" or agent is None or agent.d.kind == "tools" or \
                    (len(details) < 300 and details.count("\n") < 4):
                continue
            dish = next((kv["value"] for kv in s.get("inputs", []) if kv["key"] == "dish"), None)
            title = f"{dish[:1].upper()}{dish[1:]} recipe" if dish and agent.d.name == "chef" else (r.get("summary") or "Details")[:80]
            out.append({"title": title, "body": r["details"]})
        return out

    def _held_cart_offer(held: dict) -> dict:
        store = held["store"]
        return {"kind": "cart", "store": store, "step": held["step"], "item_names": held["item_names"],
                "label": "Show me the cart", "accept": ["show me the cart", "show me", "show the cart"],
                "question": f"Want me to put it in your {store} cart?"}

    def _next_offer(state: BulbulState) -> dict | None:
        """Offer an only-on-request agent after the agents it follows (e.g. shopping after chef)."""
        plan = state.get("plan", [])
        done = {s["agent"]: s for s in plan if s.get("status") == "done"}
        ran = {s["agent"] for s in plan}
        for a in registry.agents.values():
            src = next((done[x] for x in a.d.offer_after if x in done), None)
            if not a.d.only_on_request or src is None or a.d.name in ran:
                continue
            store = a.d.store or a.d.name
            dish = next((kv["value"] for kv in src.get("inputs", []) if kv["key"] == "dish"), None)
            what = f"the ingredients for {dish}" if dish else "the ingredients"
            items = ((src.get("result") or {}).get("details") or "")[:4000]
            return {"kind": "agent", "agent": a.d.name, "store": store, "label": f"Check {store}",
                    "accept": [f"check {store}".lower(), "check it", "please check"],
                    "decline_label": "I have them",
                    "question": f"Do you have {what} at home, or should I check {store}?", "items": items, "dish": dish}
        return None

    async def respond(state: BulbulState, config) -> dict:
        results = _plan_text(state.get("plan", []))
        held = state.get("held_cart")
        offer = None if state.get("pause_for_user") else (_held_cart_offer(held) if held else _next_offer(state))
        blocked = state.get("blocked")
        if not offer and blocked and blocked["agent"] != state.get("declined_agent"):
            offer = {"kind": "agent", "agent": blocked["agent"], "store": blocked["store"],
                     "label": f"Yes, check {blocked['store']}", "goal": blocked["goal"], "items": blocked["items"],
                     "question": f"Want me to check {blocked['store']} for that?"}
        attachments = _attachments(state)
        note = state.get("note") or ""
        if held:
            note += (f"\nYou just checked {held['store']} (nothing is in the cart yet): {held['count']} items "
                     f"available, about ₹{held['total']:g} before delivery and handling fees. Say that in one or two "
                     "sentences, naming a few key items and anything that wasn't available.")
        if attachments:
            note += ("\nAttached below your reply as a card: " + "; ".join(a["title"] for a in attachments)
                     + ". Do NOT repeat its contents; introduce it in one sentence (for a cart: the item count "
                     "and the total to pay).")
        if offer:
            note += f'\nEnd the reply with exactly this question: "{offer["question"]}"'
        if blocked:
            note += ("\nNothing was checked or done in the store for this request. Do NOT say you are checking or "
                     "doing it; just ask the closing question.")
        if state.get("voice"):
            note += ("\nVOICE MODE: this reply will be spoken aloud to someone who may be driving. Use 1-2 short "
                     "sentences, plain words, no markdown, lists, tables or emoji. Say prices and quantities as words "
                     "(\"about fifteen hundred rupees\"). Never ask them to look at the screen or a card; give the "
                     "one or two facts that matter (e.g. item count and total). Before anything is ordered, read back "
                     "what will happen and ask for a clear yes.")
        remembered = _relevant_memory(state)
        system = RESPONDER_SYSTEM.format(
            persona=persona,
            capabilities=registry.capabilities(),
            memory="\n".join(f"- {f}" for f in remembered) or "(nothing yet)",
            results=results,
            assumptions="\n".join(f"- {a}" for a in state.get("assumptions", [])) or "(none)",
            note=f"\n## Note\n{note.strip()}" if note.strip() else "",
        )
        for a in attachments:
            _emit({"type": "attachment", **a})
        _start_memory_update(state, config)  # runs while the reply streams; `remember` awaits it
        history = state["messages"][-HISTORY_WINDOW:]
        chunks: list[str] = []

        def on_fallback(info: dict) -> None:
            if info.get("reset"):
                chunks.clear()
            _emit({"type": "model_fallback", "agent": "responder", **info})

        try:
            # Spoken replies use the fast chain: a driver can't wait for a slow model.
            agent_name = "voice_responder" if state.get("voice") else "responder"
            async for text in router.stream_text(agent_name, [SystemMessage(system), *history], on_fallback):
                chunks.append(text)
                _emit({"type": "token", "text": text})
        except AllModelsFailed as exc:
            msg = "Sorry, I couldn't reach any language model right now. Please check the API keys and try again."
            log.warning("All models failed for the responder: %s", exc)
            _emit({"type": "error", "message": _model_outage("writing the reply", exc)})
            chunks = [msg]
            _emit({"type": "token", "text": msg})
        suggestions = [offer["label"], offer.get("decline_label") or "Not now"] if offer else []
        if suggestions:
            _emit({"type": "suggestions", "items": suggestions})
        return {"messages": [AIMessage("".join(chunks))], "note": None, "offer": offer, "suggestions": suggestions,
                "attachments": attachments,
                "recent_agents": sorted({s["agent"] for s in state.get("plan", []) if s.get("status") == "done"})}

    # Memory updates in flight, by thread. The update only needs the user's words, so it starts
    # when the reply starts and runs alongside it instead of holding the turn open afterwards.
    memory_tasks: dict[str, tuple[str, asyncio.Task]] = {}

    def _said(state: BulbulState) -> str:
        return " ".join([state.get("request_text") or "",
                         *[str(v) for k, v in (state.get("answers") or {}).items()]]).strip()

    def _start_memory_update(state: BulbulState, config) -> None:
        said = _said(state)
        if not said or said == state.get("remembered_for"):
            return
        thread = str((config.get("configurable") or {}).get("thread_id", ""))
        task = asyncio.ensure_future(memory.update_from_turn(router, get_store(), _user_id(config), said))
        memory_tasks[thread] = (said, task)

    async def remember(state: BulbulState, config) -> dict:
        """Learn stable facts from what the user said in this request (best effort, fast model)."""
        said = _said(state)
        if not said or said == state.get("remembered_for"):
            return {}
        thread = str((config.get("configurable") or {}).get("thread_id", ""))
        started, task = memory_tasks.pop(thread, (None, None))
        if task is not None and started == said:
            await task
        else:
            await memory.update_from_turn(router, get_store(), _user_id(config), said)
        return {"remembered_for": said}

    async def revise_cart(state: BulbulState, config) -> dict:
        """Apply a change asked for in words to the proposed cart, then show the card again."""
        idx = _step_index(state)
        plan = [dict(s) for s in state["plan"]]
        step = plan[idx]
        agent = registry.get(step["agent"])
        result = dict(step.get("result") or {})
        actions = result.get("proposed_actions") or []
        cart = next((a for a in actions if a.get("tool") == "update_cart"), None)
        edit = state.get("cart_edit") or ""
        notice = None
        if cart is not None and hasattr(agent, "revise"):
            _emit({"type": "agent_start", "step_id": step["id"], "agent": step["agent"], "goal": f"Change the cart: {edit}"})
            new = await agent.revise(cart, edit, on_fallback=_fallback_hook(agent.d.name))
            if new is not None:
                result["proposed_actions"] = [new if a is cart else a for a in actions]
            else:
                # Never come back with the same card silently: say the change didn't happen.
                notice = f"I couldn't make that change just now (\"{edit}\"). Tap to adjust, or try again in a moment."
            _emit({"type": "agent_end", "step_id": step["id"], "agent": step["agent"], "status": "done",
                   "summary": "Cart updated as asked" if new else "Couldn't apply that change"})
        plan[idx] = {**step, "result": result}
        return {"plan": plan, "cart_edit": None, "cart_notice": notice}

    # ---------------------------------------------------------------- routing
    def route_supervisor(state: BulbulState) -> str:
        return state["decision"]["action"]

    def route_delegate(state: BulbulState) -> str:
        if state["decision"]["action"] == "clarify":
            return "clarify"
        if state.get("held_cart"):
            return "respond"  # price check: summarize, offer the cart
        last = state["plan"][_step_index(state)]
        if last.get("result") and last["result"].get("proposed_actions"):
            return "approve"
        if state.get("single_step") and last.get("status") == "done" and \
                not any(s.get("status") == "pending" for s in state["plan"]):
            # The model asked for exactly one step and it's done: reply now. Asking the
            # supervisor again would cost a whole model call (~1.3 s) just to choose `respond`.
            # Multi-step requests come as a plan of 2+ items and keep the loop.
            return "respond"
        return "supervisor"

    def route_approve(state: BulbulState) -> str:
        # After a cart update or a placed order, hand back to the user (bill / confirmation)
        # instead of letting the supervisor chain the next side effect on its own.
        if state.get("cart_edit"):
            return "revise_cart"
        return "respond" if state.get("pause_for_user") else "supervisor"

    g = StateGraph(BulbulState)
    g.add_node("load_context", load_context)
    g.add_node("supervisor", supervisor)
    g.add_node("clarify", clarify)
    g.add_node("confirm_plan", confirm_plan)
    g.add_node("delegate", delegate)
    g.add_node("approve", approve)
    g.add_node("respond", respond)
    g.add_node("remember", remember)
    g.add_node("revise_cart", revise_cart)

    g.add_edge(START, "load_context")
    g.add_edge("load_context", "supervisor")
    g.add_conditional_edges(
        "supervisor",
        route_supervisor,
        {"clarify": "clarify", "confirm_plan": "confirm_plan", "delegate": "delegate", "respond": "respond",
         "approve": "approve", "revise_cart": "revise_cart"},
    )
    g.add_edge("clarify", "supervisor")
    g.add_edge("confirm_plan", "supervisor")
    g.add_conditional_edges(
        "delegate", route_delegate,
        {"clarify": "clarify", "approve": "approve", "supervisor": "supervisor", "respond": "respond"},
    )
    g.add_conditional_edges("approve", route_approve,
                            {"respond": "respond", "supervisor": "supervisor", "revise_cart": "revise_cart"})
    g.add_edge("revise_cart", "approve")
    g.add_edge("respond", "remember")
    g.add_edge("remember", END)

    return g.compile(checkpointer=checkpointer, store=store)
