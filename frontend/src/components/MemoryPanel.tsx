import { useState } from "react";
import { api } from "../lib/api";
import type { MemoryFact } from "../lib/events";

/** Sidebar section: what Bulbul remembers about you, each fact deletable. */
export function MemoryPanel({ facts, onChange }: { facts: MemoryFact[]; onChange: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const forget = async (key: string) => {
    setBusy(key);
    try {
      await api.forget(key);
      onChange();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center justify-between text-xs font-medium uppercase tracking-wide text-zinc-500 hover:text-zinc-800 dark:hover:text-zinc-200"
      >
        <span>What Bulbul remembers{facts.length ? ` (${facts.length})` : ""}</span>
        <span aria-hidden>{open ? "−" : "+"}</span>
      </button>
      {open && (
        <ul className="mt-2 max-h-48 space-y-1 overflow-y-auto">
          {facts.length === 0 && <li className="text-xs text-zinc-500">Nothing yet. Tell me about yourself as we chat.</li>}
          {facts.map((f) => (
            <li key={f.key} className="group flex items-start justify-between gap-2 text-[13px] text-zinc-700 dark:text-zinc-300">
              <span>{f.text}</span>
              <button
                onClick={() => forget(f.key)}
                disabled={busy === f.key}
                title="Forget this"
                aria-label={`Forget: ${f.text}`}
                className="shrink-0 text-zinc-400 opacity-60 hover:text-red-600 group-hover:opacity-100"
              >
                ✕
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
