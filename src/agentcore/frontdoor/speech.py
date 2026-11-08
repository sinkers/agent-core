"""Speech for chat channels, done by the front door so workers never need a speech provider's key or network
access: voice notes in (speech to text) and spoken replies out (text to speech, as Ogg Opus for Telegram).

ElevenLabs: Scribe for speech to text, a premade voice for replies (ELEVENLABS_API_KEY).
"""

from __future__ import annotations

import asyncio
import logging
import re

import httpx

log = logging.getLogger(__name__)
EL_API = "https://api.elevenlabs.io/v1"
MAX_SPOKEN_CHARS = 900  # a voice note of about a minute; the full text is always sent as well


def speakable(text: str, rest_note: str = " The rest is in the message.") -> str:
    """Markdown and links out, so the voice reads sentences rather than symbols."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    t = re.sub(r"https?://\S+", "the link", t)
    t = re.sub(r"[*_`#>|]+", "", t)
    t = re.sub(r"\n{2,}", ". ", t).replace("\n", " ")
    t = re.sub(r"\s{2,}", " ", t).strip()
    if len(t) > MAX_SPOKEN_CHARS:
        cut = t[:MAX_SPOKEN_CHARS]
        t = cut[: cut.rfind(". ") + 1] if ". " in cut else cut
        t += rest_note
    return t


class ElevenLabsSpeech:
    def __init__(self, api_key: str, voice_id: str, *, http: httpx.AsyncClient | None = None):
        self.voice_id = voice_id
        self.http = http or httpx.AsyncClient(timeout=60)
        self.headers = {"xi-api-key": api_key}

    async def transcribe(self, audio: bytes, filename: str = "voice.ogg") -> str:
        r = await self.http.post(f"{EL_API}/speech-to-text", headers=self.headers,
                                 data={"model_id": "scribe_v1", "tag_audio_events": "false"},
                                 files={"file": (filename, audio)})
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    async def speak_ogg(self, text: str) -> bytes | None:
        """The text spoken, as Ogg Opus (what Telegram shows as a voice note); None if there is nothing to say."""
        say = speakable(text)
        if not say:
            return None
        r = await self.http.post(f"{EL_API}/text-to-speech/{self.voice_id}", headers=self.headers,
                                 params={"output_format": "mp3_44100_64"},
                                 json={"text": say, "model_id": "eleven_flash_v2_5"})
        r.raise_for_status()
        return await to_ogg_opus(r.content)


async def to_ogg_opus(mp3: bytes) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate(mp3)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {err.decode()[:200]}")
    return out
