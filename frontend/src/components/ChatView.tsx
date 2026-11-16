import { type ReactNode, useEffect, useRef, useState } from "react";
import type { useChat } from "../hooks/useChat";
import type { Voice } from "../hooks/useVoice";
import { ApprovalCard, ClarifyCard, PlanCard } from "./InterruptCards";
import { MessageBubble } from "./MessageBubble";

type Chat = ReturnType<typeof useChat>;

const SUGGESTIONS = ["I want to make palak paneer tonight", "Help me plan meals for this week", "Explain how compound interest works"];

export function ChatView({
  chat,
  onSend,
  hasThread,
  voice,
  modelPicker,
}: {
  chat: Chat;
  onSend: (t: string) => void;
  hasThread: boolean;
  voice?: Voice;
  modelPicker?: ReactNode;
}) {
  const bottomRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [chat.messages.length, chat.streaming, chat.pending]);

  const empty = !hasThread || (!chat.loading && chat.messages.length === 0 && !chat.running);

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-6">
          {empty ? (
            <div className="mt-24 text-center">
              <h1 className="text-2xl font-semibold tracking-tight">What can I help with today?</h1>
              <p className="mt-2 text-sm text-zinc-500">I'll ask before acting, and nothing is bought or sent without your OK.</p>
              <div className="mt-6 flex flex-wrap justify-center gap-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s}
                    onClick={() => onSend(s)}
                    className="rounded-full border border-zinc-300 px-3 py-1.5 text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>
          ) : (
            <>
              {chat.messages.map((m, i) =>
                // While a question card is open, it already shows the question; skip its summary bubble.
                chat.pending && i === chat.messages.length - 1 && m.kind === chat.pending.kind ? null : (
                  <MessageBubble key={m.id} message={m} />
                ),
              )}
              {chat.streaming && (
                <MessageBubble message={{ id: "streaming", role: "assistant", kind: "text", content: chat.streaming }} streaming />
              )}
              {chat.running && !chat.streaming && <TypingDots />}
              {!chat.running && !chat.pending && chat.suggestions.length > 0 && (
                <QuickReplies items={chat.suggestions} onPick={onSend} />
              )}
              {chat.pending?.kind === "clarify" && <ClarifyCard pending={chat.pending} onAnswer={chat.answer} disabled={chat.running} />}
              {chat.pending?.kind === "confirm_plan" && <PlanCard pending={chat.pending} onAnswer={chat.answer} disabled={chat.running} />}
              {chat.pending?.kind === "approve" && <ApprovalCard pending={chat.pending} onAnswer={chat.answer} disabled={chat.running} />}
              {chat.error && !chat.running && (
                <p className="rounded-md bg-red-50 px-3 py-2 text-sm text-red-800 dark:bg-red-950/40 dark:text-red-200">{chat.error}</p>
              )}
            </>
          )}
          <div ref={bottomRef} />
        </div>
      </div>
      <Composer
        onSend={onSend}
        onStop={chat.stop}
        running={chat.running}
        voice={voice}
        placeholder={
          voice?.active
            ? "Talk, or type here…"
            : chat.pending?.kind === "clarify"
            ? "Answer in your own words…"
            : chat.pending?.kind === "approve"
              ? "Say yes, or tell me what to change…"
              : "Message Bulbul…"
        }
        footer={modelPicker}
      />
    </div>
  );
}

/** Tap-or-say quick replies under Bulbul's latest reply (e.g. "Buy the ingredients on Swiggy Instamart"). */
function QuickReplies({ items, onPick }: { items: string[]; onPick: (t: string) => void }) {
  return (
    <div className="-mt-1 flex flex-wrap gap-2" role="group" aria-label="Suggested replies">
      {items.map((t, i) => (
        <button
          key={t}
          onClick={() => onPick(t)}
          className={
            i === 0
              ? "rounded-full bg-accent-600 px-3.5 py-1.5 text-sm font-medium text-white shadow-sm hover:bg-accent-700"
              : "rounded-full border border-zinc-300 px-3.5 py-1.5 text-sm hover:bg-zinc-100 dark:border-zinc-700 dark:hover:bg-zinc-800"
          }
        >
          {t}
        </button>
      ))}
    </div>
  );
}

function TypingDots() {
  return (
    <div className="flex gap-1 px-1 py-2" aria-label="Working">
      {[0, 150, 300].map((d) => (
        <span key={d} className="h-2 w-2 animate-bounce rounded-full bg-zinc-400" style={{ animationDelay: `${d}ms` }} />
      ))}
    </div>
  );
}

function Composer({
  onSend,
  onStop,
  running,
  voice,
  placeholder,
  footer,
}: {
  onSend: (t: string) => void;
  onStop: () => void;
  running: boolean;
  voice?: Voice;
  placeholder: string;
  footer?: ReactNode;
}) {
  const [text, setText] = useState("");
  const submit = () => {
    // In a voice session typed text joins the conversation (queued if Bulbul is busy).
    if (!text.trim() || (running && !voice?.active)) return;
    onSend(text.trim());
    setText("");
  };
  return (
    <div className="border-t border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-950">
      {voice && (voice.active || voice.error) && <VoiceStatus voice={voice} />}
      <div className="mx-auto flex max-w-3xl items-end gap-2 rounded-xl border border-zinc-300 bg-white px-3 py-2 focus-within:border-accent-500 dark:border-zinc-700 dark:bg-zinc-900">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          rows={1}
          placeholder={placeholder}
          className="max-h-40 min-h-[28px] flex-1 resize-none bg-transparent py-1 text-[15px] outline-none placeholder:text-zinc-400"
        />
        {voice?.supported && <VoiceButton voice={voice} />}
        {running && !(voice?.active && text.trim()) ? (
          <button onClick={onStop} className="rounded-lg bg-zinc-200 px-3 py-1.5 text-sm font-medium hover:bg-zinc-300 dark:bg-zinc-800 dark:hover:bg-zinc-700">
            Stop
          </button>
        ) : (
          <button
            onClick={submit}
            disabled={!text.trim()}
            className="rounded-lg bg-accent-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-accent-700 disabled:opacity-40"
          >
            Send
          </button>
        )}
      </div>
      {footer && <div className="mx-auto mt-1.5 flex max-w-3xl items-center">{footer}</div>}
    </div>
  );
}

const VOICE_LABEL: Record<Voice["state"], string> = {
  off: "",
  connecting: "Starting voice…",
  listening: "Listening",
  thinking: "Thinking… keep talking to add more",
  speaking: "Speaking · talk to interrupt",
};

/** Live voice on/off, just left of Send. The colour and motion show who is talking. */
function VoiceButton({ voice }: { voice: Voice }) {
  const { state, active } = voice;
  const tone = !active
    ? "text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
    : state === "connecting"
      ? "bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300"
      : "bg-accent-600 text-white hover:bg-accent-700";
  return (
    <button
      type="button"
      onClick={voice.toggle}
      aria-pressed={active}
      aria-label={active ? "End live voice" : "Start live voice"}
      title={active ? "End live voice" : "Talk to Bulbul (live voice)"}
      className={`relative grid h-8 w-8 shrink-0 place-items-center rounded-full transition-colors ${tone}`}
    >
      {active && state === "listening" && (
        <span className="absolute inset-0 animate-ping rounded-full bg-accent-500/40" aria-hidden />
      )}
      {active && state === "speaking" ? <WaveIcon /> : <MicIcon />}
    </button>
  );
}

function VoiceStatus({ voice }: { voice: Voice }) {
  if (voice.error) {
    return (
      <div className="mx-auto mb-1.5 flex max-w-3xl items-center justify-between gap-2 text-xs text-red-700 dark:text-red-300" role="alert">
        <span>{voice.error}</span>
        <button onClick={voice.clearError} className="opacity-70 hover:opacity-100" aria-label="Dismiss">
          &#x2715;
        </button>
      </div>
    );
  }
  const live = voice.state === "listening" && voice.caption;
  return (
    <div className="mx-auto mb-1.5 flex max-w-3xl items-center gap-2 text-xs text-zinc-500" aria-live="polite">
      <span
        className={`h-1.5 w-1.5 shrink-0 rounded-full ${voice.state === "connecting" ? "bg-zinc-400" : "animate-pulse bg-accent-500"}`}
        aria-hidden
      />
      <span className="truncate">
        {live ? <span className="text-zinc-800 dark:text-zinc-200">“{voice.caption}”</span> : VOICE_LABEL[voice.state]}
      </span>
      {voice.info && (
        <span className="ml-auto shrink-0 text-[11px] text-zinc-400" title="Speech-to-text · voice">
          {voice.info.stt} · {voice.info.tts}
        </span>
      )}
    </div>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" className="relative h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden>
      <rect x="9" y="3" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
    </svg>
  );
}

function WaveIcon() {
  return (
    <svg viewBox="0 0 24 24" className="relative h-[18px] w-[18px]" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" aria-hidden>
      <path d="M4 10v4M8 7v10M12 4v16M16 7v10M20 10v4" />
    </svg>
  );
}
