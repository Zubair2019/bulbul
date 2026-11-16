import type { ActivityItem } from "../hooks/useChat";
import { DebugIds } from "./ChatIds";
import type { PlanStep, StepStatus } from "../lib/events";

const STATUS_STYLE: Record<StepStatus, string> = {
  pending: "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400",
  running: "bg-accent-100 text-accent-700 animate-pulse dark:bg-accent-700/30 dark:text-accent-100",
  done: "bg-emerald-100 text-emerald-800 dark:bg-emerald-900/40 dark:text-emerald-200",
  failed: "bg-red-100 text-red-800 dark:bg-red-900/40 dark:text-red-200",
  skipped: "bg-zinc-100 text-zinc-500 dark:bg-zinc-800 dark:text-zinc-500",
};

const TONE_DOT: Record<ActivityItem["tone"], string> = {
  info: "bg-zinc-400",
  agent: "bg-accent-500",
  warn: "bg-amber-500",
  error: "bg-red-500",
};

export function ActivityPanel({
  plan,
  activity,
  running,
  ids,
}: {
  plan: PlanStep[];
  activity: ActivityItem[];
  running: boolean;
  ids?: { chatId: string; runId: string | null; traceId: string | null } | null;
}) {
  return (
    <aside className="hidden w-80 shrink-0 flex-col border-l border-zinc-200 lg:flex dark:border-zinc-800">
      {ids && <DebugIds chatId={ids.chatId} runId={ids.runId} traceId={ids.traceId} />}
      <section className="border-b border-zinc-200 px-4 py-4 dark:border-zinc-800">
        <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">Plan</h2>
        {plan.length === 0 ? (
          <p className="text-sm text-zinc-500">No steps yet.</p>
        ) : (
          <ol className="space-y-2">
            {plan.map((s) => (
              <li key={s.id} className="text-sm">
                <div className="flex items-center gap-2">
                  <span className={`rounded px-1.5 py-0.5 text-[11px] font-medium ${STATUS_STYLE[s.status]}`}>{s.status}</span>
                  <span className="font-medium capitalize">{s.agent}</span>
                </div>
                <p className="mt-0.5 text-zinc-600 dark:text-zinc-400">{s.goal}</p>
                {s.result?.summary && <p className="mt-0.5 text-xs text-zinc-500">{s.result.summary}</p>}
              </li>
            ))}
          </ol>
        )}
      </section>
      <section className="flex-1 overflow-y-auto px-4 py-4">
        <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
          Activity {running && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent-500" />}
        </h2>
        {activity.length === 0 ? (
          <p className="text-sm text-zinc-500">Agent activity for the current request shows here.</p>
        ) : (
          <ul className="space-y-2">
            {activity.map((a) => (
              <li key={a.id} className="flex gap-2 text-sm">
                <span className={`mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full ${TONE_DOT[a.tone]}`} />
                <span className="text-zinc-700 dark:text-zinc-300">{a.text}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}
