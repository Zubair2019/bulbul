"""Shared data contracts: supervisor decisions, agent tasks/results, plan steps.

Fields avoid free-form dicts on purpose: some providers (notably Gemini) handle
open-ended object schemas poorly in structured output, so key/value data is a list.
"""

from typing import Literal

from pydantic import BaseModel, Field


class KV(BaseModel):
    key: str
    value: str


class Question(BaseModel):
    id: str = Field(description="Short snake_case id, e.g. 'servings'")
    text: str = Field(description="The question to show the user")
    options: list[str] = Field(default_factory=list, description="Suggested quick-reply answers (0-5)")


class PlanItem(BaseModel):
    agent: str
    goal: str
    inputs: list[KV] = Field(default_factory=list, description="The agent's required inputs for this step, by exact input name")


class SupervisorDecision(BaseModel):
    action: Literal["clarify", "delegate", "confirm_plan", "respond"] = Field(
        description=(
            "clarify: ask the user questions before acting. "
            "delegate: send one task to one agent. "
            "confirm_plan: show a multi-step plan and wait for the user's go-ahead. "
            "respond: write the final answer to the user."
        )
    )
    questions: list[Question] = Field(default_factory=list, description="For clarify: at most 3 questions")
    assumptions: list[str] = Field(default_factory=list, description="Defaults you are assuming; tell the user")
    delegate_to: str | None = Field(default=None, description="For delegate: agent name")
    goal: str | None = Field(default=None, description="For delegate: one clear instruction for the agent")
    inputs: list[KV] = Field(default_factory=list, description="For delegate: the facts the agent needs")
    plan: list[PlanItem] = Field(default_factory=list, description="For confirm_plan: the steps you intend to run")


class ActionItem(BaseModel):
    """One line of a proposed cart, shown as a picture card the user can (de)select."""

    spin_id: str
    sku_id: str | None = None
    name: str
    size: str | None = None
    price: float | None = None
    quantity: int = 1
    image_url: str | None = None


class ProposedAction(BaseModel):
    kind: str = Field(description="e.g. 'place_order', 'send_email'")
    description: str
    # Set by tool agents only (leave empty): the exact tool call that runs if the user approves.
    server: str | None = Field(default=None, description="Leave empty")
    tool: str | None = Field(default=None, description="Leave empty")
    arguments: str | None = Field(default=None, description="Leave empty")  # JSON object as a string
    items: list[ActionItem] = Field(default_factory=list, description="Leave empty")  # cart lines, for the UI


class CartLine(BaseModel):
    name: str
    size: str | None = None
    quantity: int = 1
    price: float | None = None
    line_total: float | None = None
    image_url: str | None = None


class CartView(BaseModel):
    """A store cart as the user should see it (a picture card under the reply)."""

    store: str | None = None
    items: list[CartLine] = Field(default_factory=list)
    item_total: float | None = None
    delivery: float | None = None
    handling: float | None = None
    to_pay: float | None = None
    warning: str | None = None


class AgentResult(BaseModel):
    status: Literal["done", "needs_input", "failed"]
    summary: str = Field(description="1-3 sentences for the supervisor")
    details: str = Field(default="", description="Full output in markdown (lists, recipes, etc.)")
    questions: list[Question] = Field(default_factory=list, description="For needs_input: what is missing")
    proposed_actions: list[ProposedAction] = Field(
        default_factory=list, description="Side effects the agent wants; never executed without approval"
    )
    cart: CartView | None = Field(default=None, description="Leave empty")  # set by tool agents from real data


StepStatus = Literal["pending", "running", "done", "failed", "skipped"]


class PlanStep(BaseModel):
    id: str
    agent: str
    goal: str
    inputs: list[KV] = Field(default_factory=list)
    status: StepStatus = "pending"
    result: AgentResult | None = None
