"""Config loading and provider choice (best available first, free last)."""

from __future__ import annotations

import importlib.util
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("VOICECAT_CONFIG", ROOT / "config" / "voice.yaml"))


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    # Relative model paths are relative to the voicecat folder.
    for section, keys in (("tts", ("kokoro",)),):
        for k in keys:
            sub = (cfg.get(section) or {}).get(k) or {}
            for f in ("model_path", "voices_path"):
                if sub.get(f) and not Path(sub[f]).is_absolute():
                    sub[f] = str(ROOT / sub[f])
    whisper = (cfg.get("stt") or {}).get("whisper") or {}
    if whisper.get("download_root") and not Path(whisper["download_root"]).is_absolute():
        whisper["download_root"] = str(ROOT / whisper["download_root"])
    vad = cfg.get("vad") or {}
    if vad.get("silero_model") and not Path(vad["silero_model"]).is_absolute():
        vad["silero_model"] = str(ROOT / vad["silero_model"])
    return cfg


def _has(pkg: str) -> bool:
    return importlib.util.find_spec(pkg) is not None


def availability(kind: str, name: str, cfg: dict) -> tuple[bool, str]:
    """Can this provider run here? Returns (ok, reason-if-not)."""
    sub = (cfg.get(kind) or {}).get(name) or {}
    if name == "browser":
        return True, ""
    if name in ("deepgram", "cartesia", "elevenlabs"):
        env = sub.get("api_key_env") or f"{name.upper()}_API_KEY"
        if not os.environ.get(env):
            return False, f"set {env}"
        if name == "cartesia" and not sub.get("voice"):
            return False, "set tts.cartesia.voice in voice.yaml"
        if name == "deepgram" and kind == "stt" and not _has("websockets"):
            return False, "pip install websockets"
        return True, ""
    if name == "whisper":
        return (True, "") if _has("faster_whisper") else (False, "install the 'local' extra (faster-whisper)")
    if name == "kokoro":
        if not _has("kokoro_onnx"):
            return False, "install the 'local' extra (kokoro-onnx)"
        missing = [f for f in ("model_path", "voices_path") if not Path(sub.get(f, "")).exists()]
        return (False, f"download the Kokoro {', '.join(missing)}") if missing else (True, "")
    return False, "unknown provider"


@dataclass
class Choice:
    stt: str
    tts: str
    report: dict  # {"stt": {name: "ok"|reason}, "tts": {...}}


def choose(cfg: dict, want_stt: str | None = None, want_tts: str | None = None) -> Choice:
    report: dict = {}
    picked: dict = {}
    for kind, want, env in (("stt", want_stt, "VOICECAT_STT"), ("tts", want_tts, "VOICECAT_TTS")):
        order = list((cfg.get(kind) or {}).get("order") or ["browser"])
        preferred = want or os.environ.get(env)
        if preferred:
            order = [preferred] + [o for o in order if o != preferred]
        report[kind] = {}
        for name in order:
            ok, why = availability(kind, name, cfg)
            report[kind][name] = "ok" if ok else why
            if ok and kind not in picked:
                picked[kind] = name
        picked.setdefault(kind, "browser")
    return Choice(stt=picked["stt"], tts=picked["tts"], report=report)
