import { useState } from "react";
import type { Interrupt, ResumeValue } from "../lib/events";
import { CartCard } from "./CartCard";

type Pending<K extends Interrupt["kind"]> = Extract<Interrupt, { kind: K }>;
interface CardProps<K extends Interrupt["kind"]> {
  pending: Pending<K>;
  onAnswer: (v: ResumeValue) => void;
  disabled: boolean;
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-accent-100 bg-accent-50/60 p-4 dark:border-accent-700/40 dark:bg-accent-700/10">
      <div className="mb-3 text-sm font-semibold text-accent-700 dark:text-accent-100">{title}</div>
      {children}
    </div>
  );
}

function Assumptions({ items }: { items: string[] }) {
  if (!items.length) return null;
  return <p className="mt-3 text-xs text-zinc-500">Assuming: {items.join("; ")}</p>;
}

const primaryBtn =
  "rounded-lg bg-accent-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-700 disabled:opacity-40";
const secondaryBtn =
  "rounded-lg border border-zinc-300 px-3 py-1.5 text-sm hover:bg-white disabled:opacity-40 dark:border-zinc-700 dark:hover:bg-zinc-800";

/** Bulbul asking, in its own voice: the question as text, plus tap-or-say chips.
 * One question: a chip answers immediately. Typing in the composer also answers (free text). */
export function ClarifyCard({ pending, onAnswer, disabled }: CardProps<"clarify">) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const single = pending.questions.length === 1;
  const pick = (id: string, v: string) => {
    const next = { ...answers, [id]: v };
    setAnswers(next);
    if (single || pending.questions.every((q) => next[q.id])) onAnswer({ answers: next });
  };
  return (
    <div className="prose-chat">
      {pending.questions.map((q) => (
        <div key={q.id} className="mb-2">
          <p className="!mb-2">{q.text}</p>
          {q.options.length > 0 && (
            <div className="flex flex-wrap gap-2" role="group" aria-label={q.text}>
              {q.options.map((o) => (
                <button
                  key={o}
                  onClick={() => pick(q.id, o)}
                  disabled={disabled}
                  className={`rounded-full border px-3.5 py-1.5 text-sm not-prose ${
                    answers[q.id] === o
                      ? "border-accent-600 bg-accent-600 text-white"
                      : "border-zinc-300 bg-white hover:border-accent-500 dark:border-zinc-700 dark:bg-zinc-900"
                  }`}
                >
                  {o}
                </button>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

export function PlanCard({ pending, onAnswer, disabled }: CardProps<"confirm_plan">) {
  const [feedback, setFeedback] = useState("");
  return (
    <Card title="Here's my plan — OK to go ahead?">
      <ol className="list-decimal space-y-1 pl-5 text-sm">
        {pending.plan.map((s, i) => (
          <li key={i}>
            <span className="font-medium capitalize">{s.agent}</span> — {s.goal}
          </li>
        ))}
      </ol>
      <Assumptions items={pending.assumptions} />
      <input
        value={feedback}
        onChange={(e) => setFeedback(e.target.value)}
        placeholder="Optional: changes or notes"
        disabled={disabled}
        className="mt-3 w-full rounded-md border border-zinc-300 bg-white px-2.5 py-1.5 text-sm outline-none focus:border-accent-500 dark:border-zinc-700 dark:bg-zinc-900"
      />
      <div className="mt-3 flex gap-2">
        <button onClick={() => onAnswer({ approved: true, feedback })} disabled={disabled} className={primaryBtn}>
          Go ahead
        </button>
        <button onClick={() => onAnswer({ approved: false, feedback })} disabled={disabled} className={secondaryBtn}>
          Change plan
        </button>
      </div>
    </Card>
  );
}

const ACTION_LABEL: Record<string, string> = {
  update_cart: "Update cart",
  clear_cart: "Empty cart",
  checkout: "Place order",
  create_address: "Add address",
  delete_address: "Delete address",
};

export function ApprovalCard({ pending, onAnswer, disabled }: CardProps<"approve">) {
  const placesOrder = pending.actions.some((a) => a.tool === "checkout");
  const cart = pending.actions.find((a) => a.tool === "update_cart" && a.items?.length);
  if (cart && pending.actions.length === 1) {
    return <CartCard action={cart} agent={pending.agent} notice={pending.notice} onAnswer={onAnswer} disabled={disabled} />;
  }
  return (
    <Card title="Before I go ahead">
      <ul className="space-y-1.5 text-sm">
        {pending.actions.map((a, i) => (
          <li key={i} className="flex gap-2">
            <span className="rounded bg-amber-100 px-1.5 py-0.5 font-mono text-xs text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
              {ACTION_LABEL[a.kind] ?? a.kind}
            </span>
            {a.description}
          </li>
        ))}
      </ul>
      {placesOrder && (
        <p className="mt-3 text-sm font-medium text-amber-800 dark:text-amber-300">Approving places this order.</p>
      )}
      <div className="mt-4 flex gap-2">
        <button onClick={() => onAnswer({ approved: true })} disabled={disabled} className={primaryBtn}>
          {placesOrder ? "Place order" : "Approve"}
        </button>
        <button onClick={() => onAnswer({ approved: false })} disabled={disabled} className={secondaryBtn}>
          Decline
        </button>
      </div>
    </Card>
  );
}
