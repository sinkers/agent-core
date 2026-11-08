"""Telegram: one bot per agent, by long polling (no public endpoint needed), run by the front door's leader.

Pairing: the bot answers only people paired with it. An admin makes a one-time code (`POST
/v1/admin/agents/{agent}/pairing-code`, or `frontdoor.sh pair <agent>`); the person opens
https://t.me/<bot>?start=<code> (or sends `/start <code>`), and from then on their private chat with the bot is
bound to the agent as user `tg:<their Telegram id>`. Group chats are ignored.

Voice notes are transcribed by the front door (speech.py) and answered in text, and also spoken when the agent's
`telegram.voice_replies` is on.
"""

from __future__ import annotations

import asyncio
import time
import logging

import httpx

log = logging.getLogger(__name__)
PAIRING_MAX_TRIES = 5  # wrong codes from one sender in the window, then no more tries until it passes
PAIRING_WINDOW_S = 3600.0
API = "https://api.telegram.org"
MAX_TEXT = 4000  # Telegram's limit is 4096


def chunks(text: str, size: int = MAX_TEXT) -> list[str]:
    out, rest = [], text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        cut = cut if cut > size // 2 else size
        out.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return out + ([rest] if rest else [])


class TelegramBot:
    channel = "telegram"

    def __init__(self, agent: str, token: str, core, *, speech=None, voice_replies: bool = True,
                 http: httpx.AsyncClient | None = None):
        self.agent, self.core, self.speech = agent, core, speech
        self.voice_replies = voice_replies
        self.base = f"{API}/bot{token}"
        self.file_base = f"{API}/file/bot{token}"
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10))
        self.username: str | None = None
        self._pairing_tries: dict[object, list[float]] = {}  # sender -> times of recent wrong codes

    def _pairing_locked(self, sender) -> bool:
        recent = [t for t in self._pairing_tries.get(sender, []) if time.monotonic() - t < PAIRING_WINDOW_S]
        self._pairing_tries[sender] = recent
        return len(recent) >= PAIRING_MAX_TRIES

    def _pairing_failed(self, sender) -> None:
        self._pairing_tries.setdefault(sender, []).append(time.monotonic())

    async def call(self, method: str, **params) -> dict:
        files = params.pop("_files", None)
        r = await self.http.post(f"{self.base}/{method}", data=params if files else None,
                                 json=None if files else params, files=files)
        data = r.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: {data.get('description')}")
        return data["result"]

    async def run_forever(self) -> None:
        key = f"telegram_offset:{self.agent}"
        while True:
            try:
                if self.username is None:
                    self.username = (await self.call("getMe")).get("username")
                    log.info("telegram: @%s answers for %s", self.username, self.agent)
                offset = await self.core.db.kv_get(key) or 0
                updates = await self.call("getUpdates", offset=offset, timeout=25,
                                          allowed_updates=["message"])
                for u in updates:
                    await self.core.db.kv_set(key, u["update_id"] + 1)
                    try:
                        await self.handle_update(u)
                    except Exception:
                        log.exception("telegram update %s failed", u.get("update_id"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("telegram poll for %s failed: %s", self.agent, exc)
                await asyncio.sleep(5)

    async def handle_update(self, u: dict) -> None:
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        sender = msg.get("from") or {}
        if chat.get("type") != "private" or sender.get("is_bot"):
            return
        chat_id = str(chat["id"])
        name = " ".join(x for x in (sender.get("first_name"), sender.get("last_name")) if x) or sender.get("username")
        text = (msg.get("text") or "").strip()
        bound = await self.core.db.binding(self.agent, self.channel, chat_id)
        if text.startswith("/start"):
            code = text[len("/start"):].strip()
            if bound and not code:
                await self.send_text(chat_id, f"Hello {name or ''}, you're already paired. Just send a message.")
                return
            if code and self._pairing_locked(sender.get("id")):
                await self.core.db.event(self.agent, "pairing_refused", channel=self.channel,
                                         user=f"tg:{sender.get('id')}")
                await self.send_text(chat_id, "Too many tries. Please wait a while, then ask the owner for a new code.")
                return
            if code and await self.core.db.use_pairing_code(self.agent, code, f"tg:{sender.get('id')}"):
                await self.core.db.bind(self.agent, self.channel, chat_id, f"tg:{sender['id']}", name)
                await self.core.db.event(self.agent, "paired", channel=self.channel, user=f"tg:{sender['id']}")
                await self.send_text(chat_id, f"Paired. I'm {self.core.display_name(self.agent)}: send me a message "
                                              "or a voice note.")
                return
            if code:
                self._pairing_failed(sender.get("id"))
                await self.core.db.event(self.agent, "pairing_failed", channel=self.channel,
                                         user=f"tg:{sender.get('id')}")
                await self.send_text(chat_id, "That pairing code has expired or was already used. Ask the owner "
                                              "for a new one.")
                return
            await self.send_text(chat_id, "This bot is private. Ask its owner for a pairing code, then send "
                                          "/start CODE.")
            return
        if not bound:
            await self.send_text(chat_id, "This bot is private. Ask its owner for a pairing code, then send "
                                          "/start CODE.")
            return
        voice = msg.get("voice") or msg.get("audio")
        heard_voice = False
        if voice and not text:
            if not self.speech:
                await self.send_text(chat_id, "I can't listen to voice notes here yet; please type it.")
                return
            await self.call("sendChatAction", chat_id=chat_id, action="typing")
            try:
                audio = await self.download(voice["file_id"])
                text = await self.speech.transcribe(audio)
            except Exception:
                log.exception("telegram: voice note not transcribed")
                await self.send_text(chat_id, "Sorry, I couldn't make out that voice note. Could you type it?")
                return
            if not text:
                await self.send_text(chat_id, "I couldn't hear any words in that voice note.")
                return
            heard_voice = True
        if not text:
            await self.send_text(chat_id, "I can read text and voice notes here; files and pictures aren't "
                                          "supported on this channel yet.")
            return
        reply_to = {"channel": self.channel, "agent": self.agent, "chat_id": chat_id,
                    "message_id": msg.get("message_id"), "voice": heard_voice and self.voice_replies}
        await self.core.intake(
            self.agent, text=text, conversation_id=f"tg:{chat_id}", user_id=bound["user_id"],
            user_name=bound.get("user_name") or name, reply_to=reply_to, source="telegram",
            dedupe_key=f"tg:{self.agent}:{u['update_id']}", channel="voice" if heard_voice else "chat")

    async def download(self, file_id: str) -> bytes:
        f = await self.call("getFile", file_id=file_id)
        r = await self.http.get(f"{self.file_base}/{f['file_path']}")
        if r.is_error:  # not raise_for_status: its message carries this URL, and so the bot token, into the logs
            raise RuntimeError(f"telegram file download failed: HTTP {r.status_code}")
        return r.content

    # --- out ----------------------------------------------------------------------------------------------
    async def typing(self, reply_to: dict) -> None:
        try:
            await self.call("sendChatAction", chat_id=reply_to["chat_id"], action="typing")
        except Exception:
            log.debug("typing failed", exc_info=True)

    async def send_text(self, chat_id: str, text: str, reply_to_message: int | None = None) -> None:
        for i, part in enumerate(chunks(text)):
            params = {"chat_id": chat_id, "text": part}
            if reply_to_message and i == 0:
                params["reply_parameters"] = {"message_id": reply_to_message, "allow_sending_without_reply": True}
            await self.call("sendMessage", **params)

    async def send(self, reply_to: dict, text: str, *, holding: bool = False) -> None:
        await self.send_text(reply_to["chat_id"], text)
        if reply_to.get("voice") and self.speech and not holding:
            try:
                ogg = await self.speech.speak_ogg(text)
                if ogg:
                    await self.call("sendVoice", chat_id=reply_to["chat_id"], _files={"voice": ("reply.ogg", ogg)})
            except Exception:
                log.exception("telegram: spoken reply failed (the text was sent)")
