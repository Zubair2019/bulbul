// Mirrors the backend SSE contract in backend/app/runtime.py and backend/app/schemas.py.
// Keep these in sync (phase 2: generate from the backend's JSON schema).

export type StepStatus = "pending" | "running" | "done" | "failed" | "skipped";

export interface KV {
  key: string;
  value: string;
}

export interface Question {
  id: string;
  text: string;
  options: string[];
}

/** One line of a proposed cart (picture card). */
export interface ActionItem {
  spin_id: string;
  sku_id?: string | null;
  name: string;
  size?: string | null;
  price?: number | null;
  quantity: number;
  image_url?: string | null;
}

export interface ProposedAction {
  kind: string;
  description: string;
  /** Set for tool agents: the exact tool call that runs if approved. */
  server?: string | null;
  tool?: string | null;
  arguments?: string | null;
  /** For a cart change: the items, so the user can (de)select them. */
  items?: ActionItem[];
}

/** A connected app (GET /api/integrations). */
export interface Integration {
  id: string;
  name: string;
  enabled: boolean;
  mode: "off" | "stub" | "staging" | "production";
  connected: boolean;
  expires_at: string | null;
}

export interface AgentResult {
  status: "done" | "needs_input" | "failed";
  summary: string;
  details: string;
  questions: Question[];
  proposed_actions: ProposedAction[];
}

export interface PlanStep {
  id: string;
  agent: string;
  goal: string;
  inputs: KV[];
  status: StepStatus;
  result: AgentResult | null;
}

export type Interrupt =
  | { kind: "clarify"; questions: Question[]; assumptions: string[] }
  | { kind: "confirm_plan"; plan: { agent: string; goal: string }[]; assumptions: string[] }
  | { kind: "approve"; step_id: string; agent: string; actions: ProposedAction[]; notice?: string };

export type RunStatus = "done" | "interrupted" | "cancelled" | "failed";

export type ServerEvent =
  | { type: "run_start"; run_id: string; trace_id?: string }
  | { type: "plan_update"; plan: PlanStep[] }
  | { type: "agent_start"; step_id: string; agent: string; goal: string }
  | { type: "agent_end"; step_id: string; agent: string; status: AgentResult["status"]; summary: string }
  | { type: "token"; text: string }
  | { type: "suggestions"; items: string[] }
  | { type: "attachment"; kind?: "text" | "cart"; title: string; body: string; cart?: CartViewData }
  | { type: "model_fallback"; agent: string; from: string; to: string; reason: string; reset?: boolean }
  | ({ type: "interrupt" } & Interrupt)
  | { type: "error"; message: string }
  | { type: "run_end"; status: RunStatus };

// Values sent to POST /runs/resume
export type ResumeValue =
  | { answers: Record<string, string> }
  | { approved: boolean; feedback?: string; items?: { spin_id: string; quantity: number }[] }
  | { edit: string }; // typed/spoken while a card is open: "yes", "no", or a change ("skip the ghee")

/** Something Bulbul remembers about the user (GET /api/memory). */
export interface MemoryFact {
  key: string;
  text: string;
  category: string;
  updated_at: string | null;
}

export interface CartViewData {
  store?: string | null;
  items: { name: string; size?: string | null; quantity: number; price?: number | null; line_total?: number | null; image_url?: string | null }[];
  item_total?: number | null;
  delivery?: number | null;
  handling?: number | null;
  to_pay?: number | null;
  warning?: string | null;
}

/** Long content shown under a reply: a recipe (markdown) or a store cart (picture card). */
export interface Attachment {
  kind?: "text" | "cart";
  title: string;
  body: string;
  cart?: CartViewData;
}

export interface Thread {
  id: string;
  title: string;
  updated_at: string;
}

export interface StoredMessage {
  id: string;
  role: "user" | "assistant";
  kind: "text" | "clarify" | "confirm_plan" | "approve" | "answer" | "error";
  content: string;
  meta: Record<string, unknown>;
  created_at: string;
}

/** A model the user can pick (GET /api/models). */
export interface ModelOption {
  id: string;
  name: string;
  description: string;
  provider: string;
  available: boolean;
}
