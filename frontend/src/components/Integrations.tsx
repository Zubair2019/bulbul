import { useState } from "react";
import { api } from "../lib/api";
import type { Integration } from "../lib/events";

const MODE_LABEL: Record<string, string> = { stub: "local stub", staging: "staging", production: "live" };

/** Sidebar section: connect / disconnect external services (OAuth sign-in in the same tab). */
export function Integrations({ items, onChange }: { items: Integration[]; onChange: () => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const visible = items.filter((i) => i.enabled);
  if (visible.length === 0) return null;

  const connect = async (id: string) => {
    setBusy(id);
    setError(null);
    try {
      const { url } = await api.connectIntegration(id);
      try {
        sessionStorage.setItem("bulbul.returnTo", window.location.pathname); // come back to this chat
      } catch {
        /* lands on the home screen instead */
      }
      window.location.href = url; // sign-in page; it redirects back to /?integration=...
    } catch (err) {
      setError(String(err).replace(/^Error: /, ""));
      setBusy(null);
    }
  };

  const disconnect = async (id: string) => {
    setBusy(id);
    try {
      await api.disconnectIntegration(id);
      onChange();
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
      <p className="mb-2 text-xs font-medium uppercase tracking-wide text-zinc-500">Connected apps</p>
      {visible.map((i) => (
        <div key={i.id} className="flex items-center justify-between gap-2 py-1 text-sm">
          <span className="flex min-w-0 items-center gap-1.5">
            <span
              className={`h-2 w-2 shrink-0 rounded-full ${i.connected ? "bg-emerald-500" : "bg-zinc-300 dark:bg-zinc-600"}`}
              aria-hidden
            />
            <span className="truncate">{i.name}</span>
            {i.mode !== "production" && (
              <span className="rounded bg-amber-100 px-1 text-[10px] text-amber-900 dark:bg-amber-900/40 dark:text-amber-200">
                {MODE_LABEL[i.mode] ?? i.mode}
              </span>
            )}
          </span>
          {i.connected ? (
            <button
              onClick={() => disconnect(i.id)}
              disabled={busy === i.id}
              className="text-xs text-zinc-500 hover:text-zinc-900 disabled:opacity-50 dark:hover:text-zinc-100"
            >
              Disconnect
            </button>
          ) : (
            <button
              onClick={() => connect(i.id)}
              disabled={busy === i.id}
              className="rounded-md bg-accent-600 px-2 py-0.5 text-xs font-medium text-white hover:bg-accent-700 disabled:opacity-50"
            >
              {busy === i.id ? "Opening…" : "Connect"}
            </button>
          )}
        </div>
      ))}
      {error && <p className="mt-1 text-xs text-red-600 dark:text-red-400">{error}</p>}
    </div>
  );
}
