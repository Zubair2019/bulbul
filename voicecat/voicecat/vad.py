"""Voice activity detection: when the user starts and stops speaking, and barge-in.

Two analyzers:
- `EnergyVAD` (default, no downloads): loudness above an adaptive noise floor.
- `SileroVAD` (recommended in a car): a small neural model run with onnxruntime. Needs the
  `silero_vad.onnx` file (see README) and `onnxruntime`.

`VADProcessor` turns per-window speech probabilities into UserStartedSpeaking /
UserStoppedSpeaking, and pushes an Interruption when the user talks over the bot for long enough.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from voicecat.audio import pcm_to_float, rms_db
from voicecat.frames import AudioIn, ClientEvent, Interruption, UserStartedSpeaking, UserStoppedSpeaking
from voicecat.pipeline import Direction, Processor

log = logging.getLogger(__name__)

WINDOW = 512  # samples at 16 kHz = 32 ms


class EnergyVAD:
    name = "energy"

    def __init__(self, margin_db: float = 12.0, floor_db: float = -60.0):
        self.margin_db = margin_db
        self.noise_db = floor_db

    def __call__(self, window: np.ndarray) -> float:
        db = rms_db(window)
        # Track the noise floor: follow quiet windows quickly, loud ones very slowly.
        rate = 0.05 if db < self.noise_db + self.margin_db else 0.002
        self.noise_db += (db - self.noise_db) * rate
        excess = db - (self.noise_db + self.margin_db)
        return float(np.clip(0.5 + excess / 12.0, 0.0, 1.0))


class SileroVAD:
    name = "silero"

    def __init__(self, model_path: str):
        import onnxruntime as ort  # optional dependency

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = opts.intra_op_num_threads = 1
        self._sess = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, 64), dtype=np.float32)
        self._sr = np.array(16000, dtype=np.int64)

    def __call__(self, window: np.ndarray) -> float:
        x = np.concatenate([self._context, window.reshape(1, -1).astype(np.float32)], axis=1)
        out, self._state = self._sess.run(None, {"input": x, "state": self._state, "sr": self._sr})
        self._context = x[:, -64:]
        return float(out[0][0])

    @staticmethod
    def available(model_path: str | None) -> bool:
        if not model_path or not Path(model_path).exists():
            return False
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True


class VADProcessor(Processor):
    def __init__(self, analyzer, threshold: float = 0.6, start_secs: float = 0.2, stop_secs: float = 0.8,
                 interrupt_secs: float = 0.45):
        super().__init__()
        self.analyzer = analyzer
        self.threshold = threshold
        self.start_secs, self.stop_secs, self.interrupt_secs = start_secs, stop_secs, interrupt_secs
        self._buf = np.zeros(0, dtype=np.float32)
        self._speaking = False
        self._speech_run = 0.0      # consecutive speech (seconds)
        self._silence_run = 0.0     # consecutive silence while speaking
        self._turn_speech = 0.0     # speech in the current user turn
        self._interrupted = False   # already interrupted the bot in this turn

    async def process(self, frame, direction: Direction) -> None:
        if not isinstance(frame, AudioIn):
            await self.push(frame, direction)
            return
        self._buf = np.concatenate([self._buf, pcm_to_float(frame.pcm)])
        step = WINDOW / 16000
        while len(self._buf) >= WINDOW:
            window, self._buf = self._buf[:WINDOW], self._buf[WINDOW:]
            speech = self.analyzer(window) >= self.threshold
            await self._update(speech, step)
        await self.push(frame, direction)  # STT needs the audio too

    async def _update(self, speech: bool, step: float) -> None:
        if speech:
            self._speech_run += step
            self._silence_run = 0.0
        else:
            self._speech_run = 0.0
            self._silence_run += step

        if not self._speaking and self._speech_run >= self.start_secs:
            self._speaking, self._turn_speech, self._interrupted = True, self._speech_run, False
            await self.push(UserStartedSpeaking())
            await self.push(ClientEvent({"type": "user_speaking", "value": True}))
        elif self._speaking:
            if speech:
                self._turn_speech += step
            if (not self._interrupted and self.session and self.session.bot_speaking
                    and self._turn_speech >= self.interrupt_secs):
                self._interrupted = True
                log.info("barge-in: user spoke over the bot (%.2fs)", self._turn_speech)
                await self.push(Interruption())
            if self._silence_run >= self.stop_secs:
                self._speaking = False
                await self.push(UserStoppedSpeaking())
                await self.push(ClientEvent({"type": "user_speaking", "value": False}))
