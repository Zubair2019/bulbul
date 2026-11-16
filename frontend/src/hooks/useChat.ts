import { useCallback, useEffect, useReducer, useRef } from "react";
import { api, resumeRun, startRun } from "../lib/api";
import type { Attachment, Interrupt, PlanStep, ResumeValue, ServerEvent, StoredMessage } from "../lib/events";

export interface ActivityItem {
  id: string;
  at: number;
  tone: "info" | "agent" | "warn" | "error";
  text: string;
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  kind: StoredMessage["kind"];
  content: string;
  attachments?: Attachment[]; // long content (a recipe) shown as cards under the reply
}

interface State {
  messages: ChatMessage[];
  streaming: string; // assistant text being streamed right now
  plan: PlanStep[];
  activity: ActivityItem[];
  pending: Interrupt | null;
  running: boolean;
  runId: string | null;
  error: string | null;
  loading: boolean;
  suggestions: string[]; // quick replies under the latest reply (tap or say one)
  attachments: Attachment[]; // arriving for the reply being streamed
  lastRunId: string | null; // the latest request in this chat (for debugging)
  lastTraceId: string | null;
}

type Action =
  | { type: "reset" }
  | { type: "loaded"; messages: ChatMessage[]; plan: PlanStep[]; pending: Interrupt | null; suggestions: string[] }
  | { type: "local_user"; content: string; kind?: ChatMessage["kind"] }
  | { type: "run_begin" }
  | { type: "event"; event: ServerEvent }
  | { type: "run_failed"; message: string };

const initial: State = {
  messages: [],
  streaming: "",
  plan: [],
  activity: [],
  pending: null,
  running: false,
  runId: null,
  error: null,
  loading: true,
  suggestions: [],
  attachments: [],
  lastRunId: null,
  lastTraceId: null,
};

let seq = 0;
const uid = () => `local-${Date.now()}-${seq++}`;
const act = (tone: ActivityItem["tone"], text: string): ActivityItem => ({ id: uid(), at: Date.now(), tone, text });

function reducer(s: State, a: Action): State {
  switch (a.type) {
    case "reset":
      return initial;
    case "loaded":
      return { ...s, loading: false, messages: a.messages, plan: a.plan, pending: a.pending, suggestions: a.suggestions };
    case "local_user":
      return { ...s, messages: [...s.messages, { id: uid(), role: "user", kind: a.kind ?? "text", content: a.content }] };
    case "run_begin":
      return { ...s, running: true, pending: null, error: null, streaming: "", activity: [], suggestions: [], attachments: [] };
    case "run_failed":
      return { ...s, running: false, error: a.message, activity: [...s.activity, act("error", a.message)] };
    case "event":
      return applyEvent(s, a.event);
  }
}

function applyEvent(s: State, e: ServerEvent): State {
  switch (e.type) {
    case "run_start":
      return {
        ...s,
        runId: e.run_id,
        lastRunId: e.run_id,
        lastTraceId: e.trace_id ?? null,
        activity: [...s.activity, act("info", "Thinking…")],
      };
    case "plan_update":
      return { ...s, plan: e.plan };
    case "agent_start":
      return { ...s, activity: [...s.activity, act("agent", `${e.agent} → ${e.goal}`)] };
    case "agent_end":
      return {
        ...s,
        activity: [...s.activity, act(e.status === "failed" ? "error" : "agent", `${e.agent} ${e.status}: ${e.summary}`)],
      };
    case "token":
      return { ...s, streaming: s.streaming + e.text };
    case "suggestions":
      return { ...s, suggestions: e.items };
    case "attachment":
      return { ...s, attachments: [...s.attachments, { kind: e.kind, title: e.title, body: e.body, cart: e.cart }] };
    case "model_fallback":
      return {
        ...s,
        streaming: e.reset ? "" : s.streaming,
        activity: [...s.activity, act("warn", `${e.agent}: ${e.from} failed (${e.reason}), trying ${e.to}`)],
      };
    case "interrupt": {
      const { type: _t, ...rest } = e;
      const pending = rest as Interrupt;
      const asked: ChatMessage = { id: uid(), role: "assistant", kind: pending.kind, content: interruptText(pending) };
      return { ...s, pending, messages: [...s.messages, asked] };
    }
    case "error":
      return { ...s, error: e.message, activity: [...s.activity, act("error", e.message)] };
    case "run_end": {
      const messages = s.streaming
        ? [...s.messages, { id: uid(), role: "assistant" as const, kind: "text" as const, content: s.streaming,
            attachments: s.attachments.length ? s.attachments : undefined }]
        : s.messages;
      const note = e.status === "cancelled" ? [act("warn", "Stopped")] : [];
      return { ...s, messages, streaming: "", running: false, runId: null, activity: [...s.activity, ...note] };
    }
  }
}

// Same text the backend stores for an interrupt (runtime._interrupt_text).
function interruptText(p: Interrupt): string {
  if (p.kind === "clarify") return p.questions.map((q) => q.text).join("\n");
  if (p.kind === "confirm_plan") return p.plan.map((s, i) => `${i + 1}. ${s.agent}: ${s.goal}`).join("\n");
  return p.actions.map((a) => a.description).join("\n");
}

function describeAnswer(value: ResumeValue): string {
  if ("edit" in value) return value.edit;
  if ("answers" in value) {
    return Object.entries(value.answers)
      .filter(([, v]) => v.trim())
      .map(([k, v]) => (k === "_free_text" ? v : `${k}: ${v}`))
      .join("\n");
  }
  if (value.approved && value.items) {
    const n = value.items.length;
    return `Approved ${n} item${n === 1 ? "" : "s"} for the cart`;
  }
  return (value.approved ? "Approved" : "Declined") + (value.feedback ? ` — ${value.feedback}` : "");
}

/** `model`: catalog id sent with every run and resume (null = Auto). */
export function useChat(threadId: string | null, onRunFinished?: () => void, model: string | null = null) {
  const [state, dispatch] = useReducer(reducer, initial);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    dispatch({ type: "reset" });
    abortRef.current?.abort();
    if (!threadId) return;
    let cancelled = false;
    Promise.all([api.messages(threadId), api.state(threadId)])
      .then(([msgs, st]) => {
        if (cancelled) return;
        const messages = msgs.map((m) => ({
          id: m.id, role: m.role, kind: m.kind, content: m.content,
          attachments: Array.isArray(m.meta?.attachments) ? (m.meta.attachments as Attachment[]) : undefined,
        }));
        const last = msgs[msgs.length - 1];
        const suggestions =
          last?.role === "assistant" && Array.isArray(last.meta?.suggestions) ? (last.meta.suggestions as string[]) : [];
        dispatch({ type: "loaded", messages, plan: st.plan, pending: st.pending, suggestions });
      })
      .catch((err) => !cancelled && dispatch({ type: "run_failed", message: String(err) }));
    return () => {
      cancelled = true;
    };
  }, [threadId]);

  const consume = useCallback(
    async (stream: AsyncGenerator<ServerEvent>) => {
      dispatch({ type: "run_begin" });
      try {
        for await (const event of stream) dispatch({ type: "event", event });
      } catch (err) {
        if ((err as Error).name !== "AbortError") dispatch({ type: "run_failed", message: String(err) });
        else dispatch({ type: "event", event: { type: "run_end", status: "cancelled" } });
      } finally {
        onRunFinished?.();
      }
    },
    [onRunFinished],
  );

  const answer = useCallback(
    async (value: ResumeValue) => {
      if (!threadId) return;
      dispatch({ type: "local_user", content: describeAnswer(value), kind: "answer" });
      abortRef.current = new AbortController();
      await consume(resumeRun(threadId, value, model, abortRef.current.signal));
    },
    [threadId, model, consume],
  );

  const send = useCallback(
    async (text: string) => {
      if (!threadId || !text.trim()) return;
      // While a question is open, typed (or spoken) text is the answer to it.
      if (state.pending?.kind === "clarify") return answer({ answers: { _free_text: text } });
      // While an approval card is open: "yes", "no", or a change ("skip the ghee, add eggs").
      if (state.pending?.kind === "approve") return answer({ edit: text });
      dispatch({ type: "local_user", content: text });
      abortRef.current = new AbortController();
      await consume(startRun(threadId, text, model, abortRef.current.signal));
    },
    [threadId, model, state.pending, answer, consume],
  );

  const stop = useCallback(async () => {
    if (state.runId) await api.cancel(state.runId).catch(() => undefined);
    abortRef.current?.abort();
  }, [state.runId]);

  // A live voice session makes the requests itself; it hands them here so the chat shows them
  // exactly like typed ones (the user's words, the streamed reply, cards, attachments).
  const voiceRun = useCallback((text: string, isAnswer: boolean) => {
    dispatch({ type: "local_user", content: text, kind: isAnswer ? "answer" : "text" });
    dispatch({ type: "run_begin" });
  }, []);

  const voiceEvent = useCallback(
    (event: ServerEvent) => {
      dispatch({ type: "event", event });
      if (event.type === "run_end") onRunFinished?.();
    },
    [onRunFinished],
  );

  return { ...state, send, answer, stop, voiceRun, voiceEvent };
}
