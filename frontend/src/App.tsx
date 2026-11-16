import { useCallback, useEffect, useState } from "react";
import { ActivityPanel } from "./components/ActivityPanel";
import { ChatIdChip } from "./components/ChatIds";
import { ChatView } from "./components/ChatView";
import { ModelPicker } from "./components/ModelPicker";
import { Sidebar } from "./components/Sidebar";
import { useChat } from "./hooks/useChat";
import { useVoice } from "./hooks/useVoice";
import { api } from "./lib/api";
import type { Integration, MemoryFact, ModelOption, Thread } from "./lib/events";

const MODEL_KEY = "bulbul.model";

/** Each chat has its own URL: /c/<chat-id>. Reloads, links and back/forward keep the chat. */
function chatIdFromPath(): string | null {
  const m = window.location.pathname.match(/^\/c\/([0-9a-fA-F-]{36})\/?$/);
  return m ? m[1] : null;
}

function savedModel(): string | null {
  try {
    return localStorage.getItem(MODEL_KEY);
  } catch {
    return null;
  }
}

export default function App() {
  const [threads, setThreads] = useState<Thread[]>([]);
  const [activeId, setActiveId] = useState<string | null>(chatIdFromPath);

  const openChat = useCallback((id: string | null) => {
    setActiveId(id);
    const path = id ? `/c/${id}` : "/";
    if (window.location.pathname !== path) window.history.pushState(null, "", path);
  }, []);

  useEffect(() => {
    const onPop = () => setActiveId(chatIdFromPath());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const [backendError, setBackendError] = useState<string | null>(null);
  const [noProviders, setNoProviders] = useState(false);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [integrations, setIntegrations] = useState<Integration[]>([]);
  const [memory, setMemory] = useState<MemoryFact[]>([]);
  // Agent activity is a behind-the-scenes view; Bulbul should feel like one person, so it's off by default.
  const [showActivity, setShowActivity] = useState<boolean>(() => {
    try {
      return localStorage.getItem("bulbul.activity") === "1";
    } catch {
      return false;
    }
  });
  const toggleActivity = () =>
    setShowActivity((v) => {
      try {
        localStorage.setItem("bulbul.activity", v ? "0" : "1");
      } catch {
        /* not remembered */
      }
      return !v;
    });
  const refreshMemory = useCallback(() => {
    api.memory().then(setMemory).catch(() => undefined);
  }, []);
  const [notice, setNotice] = useState<{ tone: "warn" | "error" | "ok"; text: string } | null>(null);

  const refreshIntegrations = useCallback(() => {
    api.integrations().then(setIntegrations).catch(() => undefined);
  }, []);

  // Coming back from an app's sign-in page: /?integration=swiggy&status=connected|error&message=...
  useEffect(() => {
    const q = new URLSearchParams(window.location.search);
    const name = q.get("integration");
    if (!name) return;
    const label = name.charAt(0).toUpperCase() + name.slice(1);
    setNotice(
      q.get("status") === "connected"
        ? { tone: "ok", text: `${label} connected.` }
        : { tone: "error", text: `${label} sign-in failed: ${q.get("message") ?? "unknown error"}` },
    );
    let back = "/";
    try {
      back = sessionStorage.getItem("bulbul.returnTo") || "/";
      sessionStorage.removeItem("bulbul.returnTo");
    } catch {
      /* go home */
    }
    window.history.replaceState(null, "", back);
    setActiveId(chatIdFromPath());
  }, []);
  const [model, setModelState] = useState<string | null>(savedModel);

  const setModel = useCallback((id: string | null) => {
    setModelState(id);
    try {
      if (id) localStorage.setItem(MODEL_KEY, id);
      else localStorage.removeItem(MODEL_KEY);
    } catch {
      /* storage blocked: the choice just isn't remembered */
    }
  }, []);

  const refreshThreads = useCallback(async () => {
    try {
      setThreads(await api.listThreads());
      setBackendError(null);
    } catch (err) {
      setBackendError(String(err));
    }
  }, []);

  useEffect(() => {
    refreshThreads();
    refreshIntegrations();
    refreshMemory();
    api
      .health()
      .then((h) => setNoProviders(!Object.values(h.providers).some(Boolean)))
      .catch(() => undefined);
    api
      .models()
      .then((ms) => {
        setModels(ms);
        // A remembered model that is gone or lost its API key falls back to Auto.
        const saved = savedModel();
        if (saved && !ms.some((m) => m.id === saved && m.available)) setModel(null);
      })
      .catch(() => undefined);
  }, [refreshThreads, refreshIntegrations, refreshMemory, setModel]);

  // After every run: thread titles may change, and a run can find an app disconnected.
  const onRunFinished = useCallback(() => {
    refreshThreads();
    refreshIntegrations();
    refreshMemory(); // Bulbul may have learned something
  }, [refreshThreads, refreshIntegrations, refreshMemory]);
  const chat = useChat(activeId, onRunFinished, model);

  const newChat = useCallback(async () => {
    const t = await api.createThread();
    setThreads((ts) => [t, ...ts]);
    openChat(t.id);
    return t.id;
  }, [openChat]);

  // Typing into an empty screen creates a thread first, then sends.
  const [queued, setQueued] = useState<string | null>(null);
  useEffect(() => {
    if (queued && activeId && !chat.loading) {
      chat.send(queued);
      setQueued(null);
    }
  }, [queued, activeId, chat.loading, chat]);

  const voice = useVoice({ chatId: activeId, ensureChat: newChat, onRun: chat.voiceRun, onBulbul: chat.voiceEvent });

  const send = async (text: string) => {
    // During a voice session, typed text and quick replies go to it: answered (and spoken) in turn.
    if (voice.active && voice.info) {
      voice.sendText(text);
      return;
    }
    if (!activeId) {
      await newChat();
      setQueued(text);
    } else {
      await chat.send(text);
    }
  };

  return (
    <div className="flex h-full">
      <Sidebar
        threads={threads}
        activeId={activeId}
        onSelect={openChat}
        onNew={newChat}
        integrations={integrations}
        onIntegrationsChange={refreshIntegrations}
        memory={memory}
        onMemoryChange={refreshMemory}
      />
      <main className="relative flex min-w-0 flex-1 flex-col">
        <div className="absolute right-3 top-2 z-10 hidden items-center gap-1 lg:flex">
          {activeId && <ChatIdChip chatId={activeId} />}
          <button
            onClick={toggleActivity}
            className="rounded-md px-2 py-1 text-xs text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
            title="Show what Bulbul is doing behind the scenes"
          >
            {showActivity ? "Hide details" : "Details"}
          </button>
        </div>
        {backendError && (
          <Banner tone="error">Can't reach the backend. Is it running on port 8000? ({backendError})</Banner>
        )}
        {notice && (
          <Banner tone={notice.tone} onClose={() => setNotice(null)}>
            {notice.text}
          </Banner>
        )}
        {noProviders && !backendError && (
          <Banner tone="warn">No model API keys are configured. Add at least one key to .env and restart the backend.</Banner>
        )}
        <ChatView
          chat={chat}
          onSend={send}
          hasThread={activeId !== null}
          voice={voice}
          modelPicker={<ModelPicker models={models} value={model} onChange={setModel} />}
        />
      </main>
      {showActivity && (
        <ActivityPanel
          plan={chat.plan}
          activity={chat.activity}
          running={chat.running}
          ids={activeId ? { chatId: activeId, runId: chat.lastRunId, traceId: chat.lastTraceId } : null}
        />
      )}
    </div>
  );
}

function Banner({
  tone,
  children,
  onClose,
}: {
  tone: "warn" | "error" | "ok";
  children: React.ReactNode;
  onClose?: () => void;
}) {
  const cls = {
    error: "bg-red-50 text-red-800 border-red-200 dark:bg-red-950/40 dark:text-red-200 dark:border-red-900",
    warn: "bg-amber-50 text-amber-900 border-amber-200 dark:bg-amber-950/40 dark:text-amber-200 dark:border-amber-900",
    ok: "bg-emerald-50 text-emerald-900 border-emerald-200 dark:bg-emerald-950/40 dark:text-emerald-200 dark:border-emerald-900",
  }[tone];
  return (
    <div className={`flex items-center justify-between gap-3 border-b px-4 py-2 text-sm ${cls}`}>
      <span>{children}</span>
      {onClose && (
        <button onClick={onClose} className="text-xs opacity-70 hover:opacity-100" aria-label="Dismiss">
          &#x2715;
        </button>
      )}
    </div>
  );
}
