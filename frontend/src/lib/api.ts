import type { Integration, Interrupt, MemoryFact, ModelOption, PlanStep, ResumeValue, ServerEvent, StoredMessage, Thread } from "./events";

const BASE = "/api";

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);
  return res.json() as Promise<T>;
}

export const api = {
  health: () => fetch(`${BASE}/health`).then((r) => json<{ providers: Record<string, boolean>; storage: string }>(r)),
  models: () => fetch(`${BASE}/models`).then((r) => json<ModelOption[]>(r)),
  integrations: () => fetch(`${BASE}/integrations`).then((r) => json<Integration[]>(r)),
  connectIntegration: (id: string) =>
    fetch(`${BASE}/integrations/${id}/connect`, { method: "POST" }).then((r) => json<{ url: string }>(r)),
  disconnectIntegration: (id: string) =>
    fetch(`${BASE}/integrations/${id}`, { method: "DELETE" }).then((r) => json<Integration>(r)),
  memory: () => fetch(`${BASE}/memory`).then((r) => json<MemoryFact[]>(r)),
  forget: (key: string) => fetch(`${BASE}/memory/${encodeURIComponent(key)}`, { method: "DELETE" }),
  listThreads: () => fetch(`${BASE}/threads`).then((r) => json<Thread[]>(r)),
  createThread: () =>
    fetch(`${BASE}/threads`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    }).then((r) => json<Thread>(r)),
  messages: (id: string) => fetch(`${BASE}/threads/${id}/messages`).then((r) => json<StoredMessage[]>(r)),
  state: (id: string) =>
    fetch(`${BASE}/threads/${id}/state`).then((r) => json<{ pending: Interrupt | null; plan: PlanStep[] }>(r)),
  cancel: (runId: string) => fetch(`${BASE}/runs/${runId}/cancel`, { method: "POST" }),
};

/** POST a JSON body and yield parsed SSE events (`data: {...}` frames). */
export async function* streamEvents(path: string, body: unknown, signal?: AbortSignal): AsyncGenerator<ServerEvent> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok || !res.body) throw new Error(`${res.status} ${res.statusText}: ${await res.text()}`);

  const reader = res.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value;
    let sep: number;
    while ((sep = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      for (const line of frame.split("\n")) {
        if (line.startsWith("data: ")) yield JSON.parse(line.slice(6)) as ServerEvent;
      }
    }
  }
}

// `model`: catalog id from api.models(), or null for Auto.
export const startRun = (threadId: string, message: string, model: string | null, signal?: AbortSignal) =>
  streamEvents(`/threads/${threadId}/runs`, { message, model }, signal);

export const resumeRun = (threadId: string, value: ResumeValue, model: string | null, signal?: AbortSignal) =>
  streamEvents(`/threads/${threadId}/runs/resume`, { value, model }, signal);
