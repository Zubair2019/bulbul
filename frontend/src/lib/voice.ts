// Live voice: one WebSocket to the backend's voicecat endpoint (/api/voice/ws) per session.
//
// Up:   the mic as PCM16 mono 16 kHz in 20 ms chunks (the server does voice detection, barge-in
//       and speech-to-text), plus {"type":"text"} for typed text or the browser's own recognition.
// Down: Bulbul's voice as PCM16 mono 24 kHz, plus JSON events (see voicecat/voicecat/api.py).
//       "run" + "bulbul" events let the chat show a voice request exactly like a typed one.

import type { ServerEvent } from "./events";

export type VoiceState = "connecting" | "listening" | "thinking" | "speaking";

export interface VoiceReady {
  chat_id: string;
  stt: string;
  tts: string;
  vad: string;
}

export interface VoiceHandlers {
  onReady: (info: VoiceReady) => void;
  onState: (state: VoiceState) => void;
  /** What the user is saying right now (interim) or just said (final). */
  onCaption: (text: string, final: boolean) => void;
  /** A Bulbul request starts: a new message, or the answer to the open card. */
  onRun: (text: string, isAnswer: boolean) => void;
  /** Each event of that request (tokens, cards, attachments, run_end…). */
  onBulbul: (event: ServerEvent) => void;
  onError: (message: string) => void;
  /** The session ended (stopped, or the connection dropped). */
  onClose: () => void;
}

// Mic -> 16 kHz PCM16, 320 samples (20 ms) per message. Averages each output sample's input
// window (a cheap low-pass) instead of dropping samples.
const CAPTURE_WORKLET = `
class VoiceCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.acc = 0; this.sum = 0; this.cnt = 0;
    this.buf = new Int16Array(320); this.n = 0;
  }
  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.sum += ch[i]; this.cnt++; this.acc += 1;
      if (this.acc >= this.ratio) {
        this.acc -= this.ratio;
        const v = Math.max(-1, Math.min(1, this.sum / this.cnt));
        this.sum = 0; this.cnt = 0;
        this.buf[this.n++] = v * 0x7fff;
        if (this.n === this.buf.length) {
          this.port.postMessage(this.buf.buffer, [this.buf.buffer]);
          this.buf = new Int16Array(320); this.n = 0;
        }
      }
    }
    return true;
  }
}
registerProcessor("voice-capture", VoiceCapture);
`;

// Plays queued Float32 chunks; "flush" drops everything at once (barge-in).
const PLAYER_WORKLET = `
class VoicePlayer extends AudioWorkletProcessor {
  constructor() {
    super();
    this.q = []; this.cur = null; this.i = 0;
    this.port.onmessage = (e) => {
      if (e.data === "flush") { this.q = []; this.cur = null; }
      else this.q.push(e.data);
    };
  }
  process(_inputs, outputs) {
    const out = outputs[0][0];
    let k = 0;
    while (k < out.length) {
      if (!this.cur) { this.cur = this.q.shift() || null; this.i = 0; if (!this.cur) break; }
      const n = Math.min(out.length - k, this.cur.length - this.i);
      out.set(this.cur.subarray(this.i, this.i + n), k);
      k += n; this.i += n;
      if (this.i >= this.cur.length) this.cur = null;
    }
    out.fill(0, k);
    for (let c = 1; c < outputs[0].length; c++) outputs[0][c].set(out);
    return true;
  }
}
registerProcessor("voice-player", VoicePlayer);
`;

function workletUrl(code: string): string {
  return URL.createObjectURL(new Blob([code], { type: "application/javascript" }));
}

/** PCM16 at `from` Hz -> Float32 at `to` Hz (linear interpolation). */
function pcm16ToFloat(buf: ArrayBuffer, from: number, to: number): Float32Array {
  const src = new Int16Array(buf);
  const n = Math.max(1, Math.round((src.length * to) / from));
  const out = new Float32Array(n);
  const step = from / to;
  for (let i = 0; i < n; i++) {
    const x = i * step;
    const j = Math.floor(x);
    const a = src[j] ?? 0;
    const b = src[j + 1] ?? a;
    out[i] = (a + (b - a) * (x - j)) / 0x8000;
  }
  return out;
}

type Recognition = {
  continuous: boolean;
  interimResults: boolean;
  lang: string;
  onresult: ((e: { resultIndex: number; results: ArrayLike<{ isFinal: boolean; 0: { transcript: string } }> }) => void) | null;
  onend: (() => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  start: () => void;
  stop: () => void;
};

export function voiceSupported(): boolean {
  return typeof window !== "undefined" && !!navigator.mediaDevices?.getUserMedia && "AudioWorkletNode" in window;
}

export class VoiceSession {
  private ws: WebSocket | null = null;
  private ctx: AudioContext | null = null;
  private mic: MediaStream | null = null;
  private player: AudioWorkletNode | null = null;
  private recognition: Recognition | null = null;
  private outRate = 24000;
  private closed = false;
  private speakingInBrowser = 0;

  constructor(private h: VoiceHandlers) {}

  /** Call from a click (browsers only allow audio after a user gesture). */
  async start(chatId: string): Promise<void> {
    this.h.onState("connecting");
    this.ctx = new AudioContext();
    await this.ctx.resume();
    try {
      this.mic = await navigator.mediaDevices.getUserMedia({
        audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch {
      this.fail("Microphone is blocked. Allow it for this site to talk to Bulbul.");
      return;
    }
    if (this.closed) return this.cleanup();

    await this.ctx.audioWorklet.addModule(workletUrl(CAPTURE_WORKLET));
    await this.ctx.audioWorklet.addModule(workletUrl(PLAYER_WORKLET));
    this.player = new AudioWorkletNode(this.ctx, "voice-player", { outputChannelCount: [1] });
    this.player.connect(this.ctx.destination);
    const capture = new AudioWorkletNode(this.ctx, "voice-capture");
    const silent = this.ctx.createGain();
    silent.gain.value = 0;
    this.ctx.createMediaStreamSource(this.mic).connect(capture);
    capture.connect(silent).connect(this.ctx.destination); // keeps the capture node running

    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/api/voice/ws?chat=${encodeURIComponent(chatId)}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    capture.port.onmessage = (e) => {
      if (ws.readyState === WebSocket.OPEN) ws.send(e.data as ArrayBuffer);
    };
    ws.onmessage = (e) => {
      if (typeof e.data === "string") this.onEvent(JSON.parse(e.data));
      else this.play(e.data as ArrayBuffer);
    };
    ws.onerror = () => this.fail("Lost the voice connection.");
    ws.onclose = () => this.stop();
  }

  /** Typed text during a voice session: answered (and spoken) in the same conversation. */
  sendText(text: string): void {
    this.send({ type: "text", text });
  }

  stop(): void {
    if (this.closed) return;
    this.closed = true;
    this.cleanup();
    this.h.onClose();
  }

  private cleanup(): void {
    try {
      this.recognition?.stop();
    } catch {
      /* already stopped */
    }
    this.recognition = null;
    window.speechSynthesis?.cancel();
    this.mic?.getTracks().forEach((t) => t.stop());
    this.mic = null;
    if (this.ws && this.ws.readyState <= WebSocket.OPEN) this.ws.close();
    this.ws = null;
    void this.ctx?.close();
    this.ctx = null;
  }

  private fail(message: string): void {
    this.h.onError(message);
    this.stop();
  }

  private send(data: object): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(data));
  }

  private play(buf: ArrayBuffer): void {
    if (!this.ctx || !this.player) return;
    this.player.port.postMessage(pcm16ToFloat(buf, this.outRate, this.ctx.sampleRate));
  }

  private onEvent(e: Record<string, unknown>): void {
    switch (e.type) {
      case "ready":
        this.outRate = Number(e.out_rate) || 24000;
        if (e.stt === "browser") this.startRecognition();
        this.h.onReady(e as unknown as VoiceReady);
        break;
      case "state":
        this.h.onState(e.state as VoiceState);
        break;
      case "transcript":
        this.h.onCaption(String(e.text ?? ""), Boolean(e.final));
        break;
      case "run":
        this.h.onRun(String(e.text ?? ""), Boolean(e.answer));
        break;
      case "bulbul":
        this.h.onBulbul(e.event as ServerEvent);
        break;
      case "speak":
        this.speakInBrowser(String(e.text ?? ""));
        break;
      case "interrupt": // the user talked over Bulbul: stop now
        this.player?.port.postMessage("flush");
        if (this.speakingInBrowser) {
          this.speakingInBrowser = 0;
          window.speechSynthesis?.cancel();
          this.send({ type: "speaking", value: false });
        }
        break;
      case "error":
        this.h.onError(String(e.message ?? "Voice error"));
        break;
    }
  }

  // ---- free fallbacks: the browser's own speech recognition and voices
  private startRecognition(): void {
    const W = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
    const Ctor = W.SpeechRecognition ?? W.webkitSpeechRecognition;
    if (!Ctor) {
      this.h.onError("This browser can't recognise speech. Type instead, or set up a speech provider.");
      return;
    }
    const r = new Ctor();
    r.continuous = true;
    r.interimResults = true;
    r.lang = navigator.language || "en-IN";
    r.onresult = (ev) => {
      for (let i = ev.resultIndex; i < ev.results.length; i++) {
        const res = ev.results[i];
        const text = res[0].transcript.trim();
        if (!text) continue;
        if (res.isFinal) this.send({ type: "text", text });
        this.h.onCaption(text, res.isFinal);
      }
    };
    r.onend = () => {
      if (!this.closed && this.recognition === r) {
        try {
          r.start(); // it stops by itself after a pause; keep listening
        } catch {
          /* restarting too fast */
        }
      }
    };
    r.onerror = (ev) => {
      if (ev.error === "not-allowed") this.fail("Speech recognition is blocked in this browser.");
    };
    this.recognition = r;
    r.start();
  }

  private speakInBrowser(text: string): void {
    const synth = window.speechSynthesis;
    if (!synth || !text) return;
    const u = new SpeechSynthesisUtterance(text);
    u.rate = 1.05;
    u.onstart = () => {
      if (this.speakingInBrowser++ === 0) this.send({ type: "speaking", value: true });
    };
    const done = () => {
      if (this.speakingInBrowser > 0 && --this.speakingInBrowser === 0) this.send({ type: "speaking", value: false });
    };
    u.onend = done;
    u.onerror = done;
    synth.speak(u);
  }
}
