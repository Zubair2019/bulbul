import type { Integration, MemoryFact, Thread } from "../lib/events";
import { Integrations } from "./Integrations";
import { MemoryPanel } from "./MemoryPanel";

interface Props {
  threads: Thread[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
  integrations: Integration[];
  onIntegrationsChange: () => void;
  memory: MemoryFact[];
  onMemoryChange: () => void;
}

export function Sidebar({ threads, activeId, onSelect, onNew, integrations, onIntegrationsChange, memory, onMemoryChange }: Props) {
  return (
    <aside className="hidden w-64 shrink-0 flex-col border-r border-zinc-200 bg-zinc-50 md:flex dark:border-zinc-800 dark:bg-zinc-900/60">
      <div className="flex items-center justify-between px-4 py-4">
        <span className="text-lg font-semibold tracking-tight">Bulbul</span>
        <button
          onClick={onNew}
          className="rounded-md border border-zinc-300 px-2.5 py-1 text-sm hover:bg-white dark:border-zinc-700 dark:hover:bg-zinc-800"
        >
          + New
        </button>
      </div>
      <nav className="flex-1 overflow-y-auto px-2 pb-4">
        {threads.length === 0 && <p className="px-2 text-sm text-zinc-500">No conversations yet.</p>}
        {threads.map((t) => (
          <button
            key={t.id}
            onClick={() => onSelect(t.id)}
            className={`mb-0.5 block w-full truncate rounded-md px-3 py-2 text-left text-sm ${
              t.id === activeId
                ? "bg-accent-100 text-accent-700 dark:bg-accent-700/30 dark:text-accent-100"
                : "text-zinc-700 hover:bg-zinc-200/60 dark:text-zinc-300 dark:hover:bg-zinc-800"
            }`}
            title={t.title}
          >
            {t.title}
          </button>
        ))}
      </nav>
      <MemoryPanel facts={memory} onChange={onMemoryChange} />
      <Integrations items={integrations} onChange={onIntegrationsChange} />
      <div className="border-t border-zinc-200 px-4 py-3 text-xs text-zinc-500 dark:border-zinc-800">Signed in as dev user</div>
    </aside>
  );
}
