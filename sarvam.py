"""Thin client for Sarvam AI speech-to-text, chat completions and text-to-speech."""
from __future__ import annotations

import json
import logging
import os

import requests

log = logging.getLogger("sarvam")

BASE = os.getenv("SARVAM_BASE_URL", "https://api.sarvam.ai").rstrip("/")
KEY = os.getenv("SARVAM_API_KEY", "")
STT_MODEL = os.getenv("SARVAM_STT_MODEL", "saaras:v4")
LLM_MODEL = os.getenv("SARVAM_LLM_MODEL", "sarvam-105b-conversations")
LLM_REASONING = os.getenv("SARVAM_REASONING", "low")      # low | high | max | none
TTS_MODEL = os.getenv("SARVAM_TTS_MODEL", "bulbul:v3")
TTS_SPEAKER = os.getenv("SARVAM_TTS_SPEAKER", "priya")
TTS_PACE = float(os.getenv("SARVAM_TTS_PACE", "0.95"))
TTS_CODEC = os.getenv("SARVAM_TTS_CODEC", "mp3")           # mp3 keeps mobile data small

TTS_LANGS = {"bn-IN", "en-IN", "gu-IN", "hi-IN", "kn-IN", "ml-IN", "mr-IN", "od-IN", "pa-IN", "ta-IN", "te-IN"}

# Words that help the recogniser in a railway context (saaras:v4 keyterms).
KEYTERMS = ["Sleeper", "AC", "3AC", "2AC", "Tatkal", "Chair Car", "Junction", "Express",
            "Rajdhani", "Vande Bharat", "Shatabdi", "Lower berth", "Upper berth", "Senior citizen"]


class SarvamError(RuntimeError):
    pass


def _headers(json_body: bool = True) -> dict:
    if not KEY:
        raise SarvamError("SARVAM_API_KEY is not set")
    h = {"api-subscription-key": KEY}
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _check(r: requests.Response, what: str) -> dict:
    if r.status_code >= 400:
        log.error("%s failed %s: %s", what, r.status_code, r.text[:500])
        raise SarvamError(f"{what} failed ({r.status_code})")
    return r.json()


def transcribe(audio: bytes, filename: str, content_type: str, lang: str) -> str:
    data = {"model": STT_MODEL, "language_code": lang or "unknown"}
    if STT_MODEL.startswith("saaras:v4"):
        data["keyterms"] = json.dumps(KEYTERMS)
    else:
        data["mode"] = "transcribe"
    r = requests.post(f"{BASE}/speech-to-text", headers=_headers(False), data=data,
                      files={"file": (filename, audio, content_type or "application/octet-stream")}, timeout=45)
    return (_check(r, "speech-to-text").get("transcript") or "").strip()


def chat(messages: list[dict]) -> str:
    body = {
        "model": LLM_MODEL, "messages": messages, "temperature": 0.2,
        "max_tokens": 1200, "response_format": {"type": "json_object"},
    }
    if LLM_REASONING.lower() in ("low", "high", "max"):
        body["reasoning_effort"] = LLM_REASONING.lower()
    else:
        body["reasoning_effort"] = None
    h = _headers()
    h["Authorization"] = f"Bearer {KEY}"
    r = requests.post(f"{BASE}/v1/chat/completions", headers=h, json=body, timeout=60)
    if r.status_code == 400 and "response_format" in r.text:   # fall back if JSON mode is rejected
        body.pop("response_format")
        r = requests.post(f"{BASE}/v1/chat/completions", headers=h, json=body, timeout=60)
    msg = _check(r, "chat")["choices"][0]["message"]
    return msg.get("content") or ""


def speak(text: str, lang: str) -> tuple[str, str]:
    """Return (base64_audio, mime_type)."""
    body = {
        "text": text[:2400], "language_code": lang if lang in TTS_LANGS else "hi-IN",
        "model": TTS_MODEL, "speaker": TTS_SPEAKER, "pace": TTS_PACE,
        "speech_sample_rate": 22050, "output_audio_codec": TTS_CODEC,
    }
    r = requests.post(f"{BASE}/text-to-speech", headers=_headers(), json=body, timeout=45)
    audios = _check(r, "text-to-speech").get("audios") or []
    mime = {"mp3": "audio/mpeg", "wav": "audio/wav", "opus": "audio/ogg", "aac": "audio/aac",
            "flac": "audio/flac"}.get(TTS_CODEC, "audio/wav")
    return (audios[0] if audios else ""), mime
