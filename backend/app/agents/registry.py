"""Agent registry: one YAML file per agent in app/agents/defs/.

Adding an agent = adding a YAML file (+ optional prompt file). The supervisor's
prompt is built from each agent's `description` and `required_inputs`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_core.runnables import RunnableLambda
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.llm.router import Router
from app.tracing import agent_run_name
from app.schemas import KV, AgentResult


class RequiredInput(BaseModel):
    name: str
    ask: str
    options: list[str] = Field(default_factory=list)
    from_memory: str | None = None  # dotted key in user context, e.g. "profile.diet"
    # Words that can't be "invented" facts (e.g. what to look up: "my cart", "orders"): a value made
    # only of these is accepted even if the user's words differ ("card" misheard for "cart").
    lookup_words: list[str] = Field(default_factory=list)


class AgentDef(BaseModel):
    name: str
    description: str
    system_prompt: str = ""
    required_inputs: list[RequiredInput] = Field(default_factory=list)
    kind: Literal["llm", "tools"] = "llm"  # tools = MCP tool-using agent (see tool_agent.py)
    # Conversation behaviour (see graph/builder.py):
    only_on_request: bool = False          # never planned unless the user asked / accepted an offer
    request_keywords: list[str] = Field(default_factory=list)  # words that count as asking for it
    store: str | None = None               # shown to the user, e.g. "Swiggy Instamart"
    offer_after: list[str] = Field(default_factory=list)       # offer this agent after these agents ran
    tools: list[str] = Field(default_factory=list)
    mcp_servers: list[str] = Field(default_factory=list)  # e.g. ["swiggy:instamart"]


class Agent:
    """Phase 1 agent: a single structured LLM call. Tool-using agents subclass this later."""

    def __init__(self, definition: AgentDef, router: Router):
        self.d = definition
        self.router = router

    async def run(self, goal: str, inputs: list[KV], on_fallback=None) -> AgentResult:
        facts = "\n".join(f"- {kv.key}: {kv.value}" for kv in inputs) or "- (none)"
        messages = [
            SystemMessage(
                self.d.system_prompt
                + "\n\nYou are one specialist inside a larger assistant. You receive a task brief, "
                "not the full conversation. Return status 'needs_input' with questions if the brief is "
                "missing something essential; never invent facts about the user. Put any side effect "
                "(buying, sending, booking) in proposed_actions instead of claiming you did it."
            ),
            HumanMessage(f"Task: {goal}\n\nFacts:\n{facts}"),
        ]

        async def _run(_brief: dict) -> AgentResult:
            return await self.router.structured(self.d.name, messages, AgentResult, on_fallback=on_fallback)

        # A named runnable so tracing shows this subagent as its own `agent` node (input = the task
        # brief, output = its result), with its model calls nested inside.
        runnable = RunnableLambda(_run).with_config(run_name=agent_run_name(self.d.name))
        return await runnable.ainvoke({"goal": goal, "inputs": {kv.key: kv.value for kv in inputs}})


class Registry:
    def __init__(self, agents: dict[str, Agent]):
        self.agents = agents

    def get(self, name: str) -> Agent | None:
        return self.agents.get(name)

    def capabilities(self) -> str:
        """Plain-language list of what Bulbul can do, for the responder."""
        return "\n".join(f"- {a.d.description.strip().splitlines()[0]}" for a in self.agents.values())

    def describe(self) -> str:
        lines = []
        for a in self.agents.values():
            req = ", ".join(r.name for r in a.d.required_inputs) or "none"
            only = " [only when the user asks]" if a.d.only_on_request else ""
            lines.append(f"- {a.d.name}{only}: {a.d.description.strip()} (required inputs: {req})")
        return "\n".join(lines)


def load_registry(defs_dir: Path, router: Router, integrations: dict[str, Any] | None = None) -> Registry:
    """`integrations` maps provider -> integration (e.g. {"swiggy": SwiggyIntegration}). A tool agent
    whose integration is missing or disabled is skipped, so the supervisor never plans for it."""
    agents: dict[str, Agent] = {}
    for path in sorted(defs_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        prompt_file = raw.pop("prompt_file", None)
        if prompt_file:
            raw["system_prompt"] = (path.parent / prompt_file).read_text(encoding="utf-8")
        d = AgentDef.model_validate(raw)
        if d.kind == "tools":
            from app.agents.tool_agent import ToolAgent

            provider, _, server = (d.mcp_servers[0] if d.mcp_servers else ":").partition(":")
            integ = (integrations or {}).get(provider)
            if integ is None or not getattr(integ, "enabled", False):
                continue
            agents[d.name] = ToolAgent(d, router, integ, server)  # type: ignore[assignment]
            continue
        agents[d.name] = Agent(d, router)
    if not agents:
        raise RuntimeError(f"No agent definitions found in {defs_dir}")
    return Registry(agents)
