import { useCallback, useEffect, useRef, useState } from "react";
import type { ServerEvent } from "../lib/events";
import { VoiceSession, type VoiceReady, type VoiceState, voiceSupported } from "../lib/voice";

export type VoiceUiState = "off" | VoiceState;

interface Options {
  /** The chat to talk in; created first when none is open. */
  chatId: string | null;
  ensureChat: () => Promise<string>;
  onRun: (text: string, isAnswer: boolean) => void;
  onBulbul: (event: ServerEvent) => void;
}

/** A live voice conversation in the open chat (the mic button in the composer). */
export function useVoice({ chatId, ensureChat, onRun, onBulbul }: Options) {
  const [state, setState] = useState<VoiceUiState>("off");
  const [caption, setCaption] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [info, setInfo] = useState<VoiceReady | null>(null);
  const session = useRef<VoiceSession | null>(null);
  const sessionChat = useRef<string | null>(null);
  const cb = useRef({ onRun, onBulbul });
  cb.current = { onRun, onBulbul };

  const stop = useCallback(() => {
    session.current?.stop();
    session.current = null;
  }, []);

  const start = useCallback(async () => {
    if (session.current) return;
    setError(null);
    setCaption("");
    const s = new VoiceSession({
      onReady: (i) => setInfo(i),
      onState: (st) => setState(st),
      onCaption: (text) => setCaption(text),
      onRun: (text, isAnswer) => {
        setCaption("");
        cb.current.onRun(text, isAnswer);
      },
      onBulbul: (ev) => cb.current.onBulbul(ev),
      onError: (m) => setError(m),
      onClose: () => {
        if (session.current === s) session.current = null;
        sessionChat.current = null;
        setState("off");
        setCaption("");
        setInfo(null);
      },
    });
    session.current = s;
    setState("connecting");
    try {
      const id = chatId ?? (await ensureChat());
      sessionChat.current = id;
      await s.start(id);
    } catch (err) {
      setError(String(err));
      s.stop();
    }
  }, [chatId, ensureChat]);

  // Opening another chat ends the voice session (it belongs to the chat it started in).
  useEffect(() => {
    if (session.current && sessionChat.current && chatId !== sessionChat.current) stop();
  }, [chatId, stop]);

  useEffect(() => stop, [stop]); // leaving the page

  const sendText = useCallback((text: string) => session.current?.sendText(text), []);

  return {
    supported: voiceSupported(),
    state,
    active: state !== "off",
    caption,
    error,
    info,
    start,
    stop,
    toggle: () => (session.current ? stop() : void start()),
    sendText,
    clearError: () => setError(null),
  };
}

export type Voice = ReturnType<typeof useVoice>;
