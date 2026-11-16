# voicecat: voice for Bulbul

Hands-free, real-time voice for Bulbul, designed for someone **shopping while driving**: talk naturally, interrupt Bulbul any time, and confirm carts and orders out loud.

voicecat is its own small framework in this folder (frames, processors, barge-in, pluggable speech providers). It has no server of its own: the Bulbul backend mounts its router at `/api/voice`. It still talks to Bulbul only through Bulbul's HTTP API (over loopback, so replies stream), like the web app does.

```
browser mic ─▶ WebSocket ─▶ VAD ─▶ speech-to-text ─▶ turns ─▶ brain (Bulbul API) ─▶ text-to-speech ─▶ WebSocket ─▶ speaker
                               └── user talks over Bulbul ─▶ Interruption ─▶ stop speaking now
```

## Use it

In the Bulbul app, tap the **mic button** just left of Send. Then:
- Talk in the open chat (a new chat is created if none is open).
- Your words and Bulbul's replies, cards and attachments show up in the chat as you go.
- Tap the button again to stop. Opening another chat also stops it.

**Other ways in:**
- Typing during a voice session joins the same conversation, and the answer is spoken.
- Tapping a card works too; voice picks up from there.

**Setup.** The backend depends on this folder (`voicecat[local]`, an editable path dependency), so `uv sync` in `backend/` installs it. API keys come from Bulbul's `.env`. In Docker, `voicecat/models/` is mounted into the backend container.

**Mounting it elsewhere:**

```python
from voicecat.api import voice_router, warm_up
app.include_router(voice_router(bulbul_url="http://127.0.0.1:8000"), prefix="/api/voice")
```

## Speech providers: best feel or free

Configured in [`config/voice.yaml`](config/voice.yaml). For each of speech-to-text and the voice, the **first available** provider in `order` is used. Override per session with `?stt=…&tts=…`, or globally with `VOICECAT_STT` / `VOICECAT_TTS`.

| | Speech-to-text | Voice | Needs |
|---|---|---|---|
| **Best feel** (default) | `deepgram` (streaming, ~300 ms) | `deepgram` (Aura voices) | `DEEPGRAM_API_KEY` in `.env`: one key covers both |
| **Best feel, alternatives** | | `cartesia` (~100 ms) or `elevenlabs` | `CARTESIA_API_KEY` + a voice id, or `ELEVENLABS_API_KEY` |
| **Free, local, private** (installed) | `whisper` (faster-whisper, `base.en`) | `kokoro` (int8) | `uv sync --extra local`, plus in `models/`: `kokoro-v1.0.int8.onnx` and `voices-v1.0.bin` (kokoro-onnx GitHub releases, `model-files-v1.0`) and `silero_vad.onnx` (silero-vad repo). Whisper downloads itself into `models/whisper` on first use. |
| **Free, zero setup** | `browser` (the browser's speech recognition) | `browser` (the browser's voices) | Chrome. Lower quality; the browser may hear Bulbul's own voice. |

**Voice detection:** `energy` is built in. For road noise, use `silero`: `uv sync --extra local` and put `silero_vad.onnx` (from the silero-vad project) in `models/`. It's picked up automatically.

The composer shows which providers the session uses (right of the voice status line), and `GET /api/voice/status` says why any provider is unavailable.

**Switching to the best feel:**
1. Add `DEEPGRAM_API_KEY` to Bulbul's `.env`. That one key is used for both speech-to-text and the voice.
2. Restart the backend.

**Choosing a different voice:**
- For another Deepgram voice, change `tts.deepgram.model` (for example `aura-2-apollo-en`).
- To use Cartesia or ElevenLabs instead, move it ahead of `deepgram` in `tts.order` and add its key. Cartesia also needs a voice id in `tts.cartesia.voice`.

Cloud providers come first in `order`, so they take over automatically. The local ones remain the fallback.

**Measured on this machine (CPU, local models):**
- Kokoro makes about 2 s of speech per second of work.
- Whisper transcribes a sentence in about 0.6–0.9 s.
- Models load once per backend process and are warmed up at start.

**Whisper vocabulary hint:** `stt.whisper.vocabulary` lists words to expect (Swiggy Instamart, cart, paneer, ghee…), which reduces mishearings.

## How the conversation behaves

- **Barge-in.** Talk over Bulbul for about a third of a second and it stops speaking immediately: the server drops queued speech and the client flushes its audio. A cough or a bump in the road doesn't count.
- **Interrupting never cancels Bulbul.** If Bulbul is still thinking (or still streaming) when you speak, that request finishes quietly: its answer stays in the chat, unspoken. What you just said is handled right after.
- **No silence.** When an agent starts working, Bulbul says a short filler ("Let me check Instamart.", see `fillers` in the config).
- **Driving-friendly replies.** Requests are sent with `voice: true`, so Bulbul answers in 1-2 spoken sentences, says prices in words, and never says "look at the screen". Cards are read out briefly: "I've picked 19 items, including chicken, basmati rice and curd, for about 1500 rupees. Shall I add them to your cart?" The full card is on screen.
- **Everything works by voice.** "Yes", "no", "skip the ghee and add eggs" (cart), "biryani" (a question), "place it" (order). Orders are read back first and need a clear yes.
- **One chat.** Voice talks in the open Bulbul chat: history, cards and debug IDs are all there.

## Protocol (`ws://host/api/voice/ws?chat=&stt=&tts=`)

- **Client → server:**
  - binary: PCM16 mono 16 kHz mic audio (20 ms chunks)
  - `{"type":"text","text":…}`: typed input, or the browser's own speech recognition
  - `{"type":"speaking","value":bool}`: browser speech started / stopped
- **Server → client:**
  - binary: PCM16 mono 24 kHz (Bulbul's voice)
  - JSON:
    - `ready`, `state` (listening / thinking / speaking), `transcript`, `user_turn`
    - `run` (a Bulbul request starts: `{text, answer}`)
    - `bulbul` (each of its Bulbul events, so the chat shows it like a typed request)
    - `bot_text`, `speak` (browser voice), `interrupt` (stop playback now)
    - `card`, `queued`, `error`, `bot_turn_end`

## Code map

| File | What |
|---|---|
| `voicecat/frames.py` | Frame types; system frames skip queues (instant interruption) |
| `voicecat/pipeline.py` | Processor (queue + task, interruption handling) and Pipeline |
| `voicecat/vad.py` | Energy / Silero voice detection, start/stop of speech, barge-in |
| `voicecat/stt.py` | Deepgram (streaming), Whisper (per utterance), browser |
| `voicecat/turns.py` | When the user has finished a thought |
| `voicecat/brain.py` | Bulbul requests, fillers, spoken cards, finish-quietly on interrupt |
| `voicecat/tts.py` | Sentence-by-sentence speech; Cartesia, ElevenLabs, Kokoro, browser |
| `voicecat/transport.py`, `api.py` | WebSocket transport; the mountable router (sessions, `/ws`, `/status`) and model warm-up |
| `frontend/src/lib/voice.ts`, `hooks/useVoice.ts` | (in Bulbul) the browser side: mic capture, playback, browser-speech fallback, the composer button |

## Tests

```bash
cd voicecat && uv run pytest
```

The tests use a fake Bulbul (streamed events with delays) and a fake voice, so they need no keys. They cover:
- a spoken turn, fillers, and barge-in (speech stops, the Bulbul request isn't cancelled)
- talking while Bulbul thinks
- cart, order and question cards answered by voice
- voice detection on synthetic audio, sentence splitting, provider choice, and the router mounted in an app

## Known limits / next

- **HTTPS for phones.** Browsers only allow the mic on `https://` (or localhost), so using it from a phone needs HTTPS.
- **WebSocket for now.** Audio rides on a WebSocket, which works through any network. A WebRTC transport (better on bad mobile links, built-in echo cancellation path) can be added behind the same processors.
- **Chat history isn't trimmed on barge-in.** When you interrupt, Bulbul's chat still holds the full reply text, not just what was heard.
