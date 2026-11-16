import { useEffect, useRef, useState } from "react";
import type { ModelOption } from "../lib/events";

const PROVIDER_KEYS: Record<string, string> = {
  google: "GOOGLE_API_KEY",
  openai: "OPENAI_API_KEY",
  anthropic: "ANTHROPIC_API_KEY",
  xai: "XAI_API_KEY",
};

const AUTO ={ id: null, name: "Auto", description: "Each agent uses its configured models" };

/** ChatGPT-style model menu. `value` null means Auto (per-agent chains from models.yaml). */
export function ModelPicker({
  models,
  value,
  onChange,
}: {
  models: ModelOption[];
  value: string | null;
  onChange: (id: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const close = (e: MouseEvent | KeyboardEvent) => {
      if (e instanceof KeyboardEvent ? e.key === "Escape" : !ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", close);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", close);
    };
  }, [open]);

  const current = models.find((m) => m.id === value) ?? AUTO;
  const pick = (id: string | null) => {
    onChange(id);
    setOpen(false);
  };

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Choose model"
        className="flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100"
      >
        {current.name}
        <svg viewBox="0 0 20 20" className={`h-3.5 w-3.5 transition-transform ${open ? "rotate-180" : ""}`} fill="currentColor" aria-hidden>
          <path d="M5.23 7.21a.75.75 0 0 1 1.06.02L10 11.06l3.71-3.83a.75.75 0 1 1 1.08 1.04l-4.25 4.39a.75.75 0 0 1-1.08 0L5.21 8.27a.75.75 0 0 1 .02-1.06Z" />
        </svg>
      </button>

      {open && (
        <ul
          role="listbox"
          className="absolute bottom-full left-0 z-20 mb-1 w-80 max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-zinc-200 bg-white p-1 shadow-lg dark:border-zinc-700 dark:bg-zinc-900"
        >
          {[AUTO, ...models].map((m) => {
            const available = !("available" in m) || m.available;
            const selected = m.id === value;
            return (
              <li key={m.id ?? "auto"}>
                <button
                  role="option"
                  aria-selected={selected}
                  disabled={!available}
                  onClick={() => pick(m.id)}
                  className="flex w-full items-start gap-3 rounded-lg px-3 py-2 text-left hover:bg-zinc-100 disabled:cursor-not-allowed disabled:opacity-45 disabled:hover:bg-transparent dark:hover:bg-zinc-800"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block text-sm font-medium">{m.name}</span>
                    <span className="block text-xs text-zinc-500">
                      {available ? m.description : `Needs ${PROVIDER_KEYS["provider" in m ? m.provider : ""] ?? "an API key"} in .env`}
                    </span>
                  </span>
                  {selected && (
                    <svg viewBox="0 0 20 20" className="mt-0.5 h-4 w-4 text-accent-600" fill="currentColor" aria-hidden>
                      <path d="M16.7 5.3a1 1 0 0 1 0 1.4l-7.5 7.5a1 1 0 0 1-1.4 0L3.3 9.7a1 1 0 1 1 1.4-1.4l3.8 3.8 6.8-6.8a1 1 0 0 1 1.4 0Z" />
                    </svg>
                  )}
                </button>
              </li>
            );
          })}
          <li className="border-t border-zinc-100 px-3 pb-1 pt-2 text-[11px] leading-snug text-zinc-500 dark:border-zinc-800">
            The chosen model handles every step. If it fails, Bulbul falls back to the next configured model.
          </li>
        </ul>
      )}
    </div>
  );
}
