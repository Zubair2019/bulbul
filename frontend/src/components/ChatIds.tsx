import { useState } from "react";

/** Copyable IDs for debugging: grep the backend logs for `chat=<short id>`, or open the debug JSON. */

const short = (id: string) => id.slice(0, 8);

function useCopy() {
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      // Clipboard blocked: fall back to a prompt the user can copy from.
      window.prompt("Copy this ID", text);
    }
    setCopied(text);
    setTimeout(() => setCopied(null), 1500);
  };
  return { copied, copy };
}

/** Small chip next to "Details": the chat's short ID; click copies the full UUID. */
export function ChatIdChip({ chatId }: { chatId: string }) {
  const { copied, copy } = useCopy();
  return (
    <button
      onClick={() => copy(chatId)}
      title={`Chat ID ${chatId}\nClick to copy. Logs: grep chat=${short(chatId)}`}
      className="rounded-md px-2 py-1 font-mono text-xs text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
    >
      {copied === chatId ? "Copied" : `# ${short(chatId)}`}
    </button>
  );
}

/** "IDs" section for the Details panel: chat, last request, its trace, and the debug view. */
export function DebugIds({ chatId, runId, traceId }: { chatId: string; runId: string | null; traceId: string | null }) {
  const { copied, copy } = useCopy();
  const row = (label: string, value: string | null) =>
    value && (
      <div className="flex items-center justify-between gap-2">
        <span className="text-zinc-500">{label}</span>
        <button onClick={() => copy(value)} title={value} className="truncate font-mono hover:text-accent-600">
          {copied === value ? "Copied" : short(value)}
        </button>
      </div>
    );
  return (
    <section className="border-b border-zinc-200 px-4 py-4 text-xs dark:border-zinc-800">
      <h2 className="mb-2 font-semibold uppercase tracking-wide text-zinc-500">IDs</h2>
      <div className="space-y-1">
        {row("Chat", chatId)}
        {row("Last request", runId)}
        {row("Trace", traceId)}
      </div>
      <a
        href={`/api/threads/${chatId}/debug`}
        target="_blank"
        rel="noreferrer"
        className="mt-2 inline-block text-accent-600 hover:underline"
      >
        Open debug view
      </a>
    </section>
  );
}
