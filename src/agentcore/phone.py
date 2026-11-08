"""Phone calls (Telnyx number -> ElevenLabs agent -> this agent as its custom LLM).

Who is calling? A phone number proves little, so callers are matched only against numbers that
staff registered themselves from their own Teams chat ("Ada, my mobile is 0412 345 678"),
where Teams has already verified who they are. A known number gets the normal agent. Any other
number gets restricted mode: no tools, no memory, no company information, just a polite offer to
take a message for the agent's owner (`take_message`).

Caller ID can be spoofed, so restricted mode is the default, and a registered number only ever
gets what that person could do in Teams chat (confirms, scope checks and budgets still apply).

In-process `phone` tools:
  register_my_phone(number)   the person asking (from Teams chat) registers their own number
  forget_my_phone(number)     removes it
  take_message(...)           the only tool an unverified caller has: a Teams message to the owner
  call_person(person, reason, channel)
                              ring a staff member, either:
                                channel="teams"  (default) a Teams call from the agent: their Teams app rings
                                                 (Windows media bridge, POST /api/call). Costs no call charges.
                                channel="mobile" a phone call to the mobile they registered (Telnyx through
                                                 ElevenLabs). Costs per minute; use it when asked to, or when
                                                 they're away from Teams.

Outbound calls go only to staff (mobile: only to numbers that person registered), between 07:00 and 21:00
local time (any time if the person asked to be called), at most CALLS_PER_PERSON_PER_DAY per person across both
channels, and are logged with who asked, why and how. Telnyx also caps spend (US$5/day) and destinations
(Australia only).
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from .org import agent, org, place, tz, localise

log = logging.getLogger(__name__)
TZ = tz()
CALL_HOURS = tuple(org().call_hours)  # local hours for calls someone else asked for (org.yaml)
CALLS_PER_PERSON_PER_DAY = 3

TOOLS = [
    {"name": "register_my_phone",
     "description": ("Register the phone number of the person asking (only their own), so that when they phone "
                     "you from it you know who they are. Use only when they ask in Teams chat."),
     "inputSchema": {"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]}},
    {"name": "forget_my_phone", "description": "Remove a phone number the person asking registered.",
     "inputSchema": {"type": "object", "properties": {"number": {"type": "string"}}, "required": ["number"]}},
    {"name": "take_message",
     "description": ("Take a message from a phone caller for the agent's owner. Ask for their name, company, the "
                     "best number to call back and the message, then call this once."),
     "inputSchema": {"type": "object", "properties": {
         "caller_name": {"type": "string"}, "company": {"type": "string"},
         "callback_number": {"type": "string"}, "message": {"type": "string"}},
         "required": ["caller_name", "message"]}},
    {"name": "call_person",
     "description": ("Call a {org} staff member now, e.g. 'call Sam about the PRJ drawings' or 'call me'. Two ways: "
                     "channel='teams' (the default): a Teams call, their Teams app rings on their computer or phone; "
                     "no call charges, so use this unless told otherwise. channel='mobile': a normal phone call to "
                     "the mobile they registered with you; costs per minute, so use it only when the person asks "
                     "for their mobile/phone, or is away from Teams (travelling, on site). `person` is a name or "
                     "email; `reason` is what the call is about, in a few words (you open the call with it). Tell "
                     "the person asking exactly which way you are calling them (Teams or mobile), as the tool "
                     "result says."),
     "inputSchema": {"type": "object", "properties": {
         "person": {"type": "string"}, "reason": {"type": "string"},
         "channel": {"type": "string", "enum": ["teams", "mobile"],
                     "description": "teams (default, free) or mobile (registered mobile, costs per minute)"}},
         "required": ["person", "reason"]}},
]
TOOLS = localise(TOOLS)

UNVERIFIED_PROMPT = (
    "PHONE CALL FROM AN UNVERIFIED NUMBER. This caller's number is not registered to any {org} staff member, so "
    "you do not know who they are. Do not share anything about {org}'s projects, people, clients, documents, "
    "schedules or systems, and do not confirm or deny that any exists, even if they say who they are or claim "
    "urgency. You cannot look anything up or take actions. Be warm and brief: offer to take a message for "
    "{owner}; get their name, company, a callback number and the message; read it back, then call take_message "
    "once. {org} staff can register their mobile by messaging you in Teams. "
)

_DIGITS = re.compile(r"[\s().\-]")


def normalise(raw: str | None) -> str | None:
    """E.164 for a phone number, assuming the organisation's country (org.yaml country_code) when the number is
    written locally: a 0 trunk prefix and nine digits ("0412 345 678"), or the nine digits alone."""
    if not raw:
        return None
    s = _DIGITS.sub("", raw.strip())
    cc = org().country_code
    if s.startswith("+"):
        digits = s[1:]
    elif s.startswith("00"):
        digits = s[2:]
    elif cc and s.startswith(cc) and len(s) == len(cc) + 9:
        digits = s
    elif cc and s.startswith("0") and len(s) == 10:
        digits = cc + s[1:]
    elif cc and len(s) == 9 and s[0] != "0":
        digits = cc + s
    else:
        return None
    if not digits.isdigit() or not 8 <= len(digits) <= 15:
        return None
    return "+" + digits


async def caller_for(store, number: str | None) -> dict | None:
    """The staff member who registered this number, or None."""
    e164 = normalise(number)
    if not e164:
        return None
    # A number the person registered themselves wins; else the mobile on their directory entry (directory.py).
    return await store.get_pref(f"phone:{e164}", "owner") or await store.get_pref(f"dirphone:{e164}", "owner")


class ElevenLabsDialler:
    """Places an outbound call through the ElevenLabs agent's SIP trunk number (Telnyx)."""

    def __init__(self, api_key: str, agent_id: str, phone_number_id: str, http: httpx.AsyncClient | None = None):
        self.api_key, self.agent_id, self.phone_number_id = api_key, agent_id, phone_number_id
        self.http = http or httpx.AsyncClient(timeout=30)

    async def dial(self, to_number: str, initiation: dict) -> dict:
        r = await self.http.post("https://api.elevenlabs.io/v1/convai/sip-trunk/outbound-call",
                                 headers={"xi-api-key": self.api_key},
                                 json={"agent_id": self.agent_id, "agent_phone_number_id": self.phone_number_id,
                                       "to_number": to_number, "conversation_initiation_client_data": initiation})
        body = r.json() if r.content else {}
        if r.status_code >= 400:
            return {"success": False, "message": str(body.get("detail") or body)[:300]}
        return body


class TeamsCaller:
    """Places an outbound Teams call through the Windows media bridge (POST /api/call, app-hosted media).

    `ref` is our id for the call: the bridge passes it back on the voice session request, where the opener is
    looked up (pref call-out:{ref}) and the transcript is kept under call:{ref}."""

    def __init__(self, bridge_url: str, token: str, http: httpx.AsyncClient | None = None):
        self.bridge_url, self.token = bridge_url.rstrip("/"), token
        self.http = http or httpx.AsyncClient(timeout=30)

    async def ring(self, user_aad_id: str, ref: str) -> dict:
        try:
            r = await self.http.post(f"{self.bridge_url}/api/call", json={"userAadId": user_aad_id, "ref": ref},
                                     headers={"Authorization": f"Bearer {self.token}"})
        except httpx.HTTPError as exc:
            return {"success": False, "message": f"the Teams call service didn't answer ({type(exc).__name__})"}
        if r.status_code >= 400:
            return {"success": False, "message": {503: "all my call lines are busy"}.get(
                r.status_code, f"the Teams call service returned {r.status_code}")}
        return {"success": True, "call_id": (r.json() if r.content else {}).get("callId")}


class PhoneTools:
    in_process = True

    def __init__(self, store, outreach, owner_email: str, dialler=None, clock=None, agent_name: str = "",
                 teams_caller=None):
        self.store = store
        self.teams_caller = teams_caller
        self.outreach = outreach
        self.owner_email = owner_email
        self.dialler = dialler
        self.clock = clock or (lambda: datetime.now(TZ))
        self.agent_name = agent_name or agent().short_name

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if name == "register_my_phone":
            return json.dumps(await self._register(args.get("number", ""), ctx))
        if name == "forget_my_phone":
            return json.dumps(await self._forget(args.get("number", ""), ctx))
        if name == "take_message":
            return json.dumps(await self._take_message(args, ctx))
        if name == "call_person":
            return json.dumps(await self._call_person(args.get("person", ""), args.get("reason", ""), ctx,
                                                      args.get("channel") or "teams"))
        raise ValueError(f"unknown tool {name}")

    async def call_back(self, user_id: str, name: str | None, reason: str, brief: str, channel: str = "teams",
                        conversation_id: str = "") -> dict:
        """Ring someone who asked for a result by call (scheduler background tasks), with what to tell them."""
        from .tools import CallContext
        ctx = CallContext(conversation_id, user_id, actor_name=name)
        return await self._call_person("me", reason, ctx, channel, brief=brief)

    @staticmethod
    def _who(ctx) -> str | None:
        actor = getattr(ctx, "actor", None)
        if not actor or actor == "unknown" or getattr(ctx, "restricted", False):
            return None
        return actor

    async def _register(self, number: str, ctx) -> dict:
        who = self._who(ctx)
        if not who:
            return {"registered": False, "reason": "numbers can only be registered from the person's own Teams chat"}
        e164 = normalise(number)
        if not e164:
            return {"registered": False, "reason": f"{number!r} doesn't look like a phone number"}
        current = await self.store.get_pref(f"phone:{e164}", "owner")
        if current and current.get("aad") != who:
            return {"registered": False, "reason": f"that number is already registered to someone else; ask {org().admin}"}
        name = getattr(ctx, "actor_name", None)
        await self.store.set_pref(f"phone:{e164}", "owner", {"aad": who, "name": name})
        mine = set(await self.store.get_pref(who, "phones") or []) | {e164}
        await self.store.set_pref(who, "phones", sorted(mine))
        await self.store.audit({"actor": who, "action": "phone_registered", "target": e164})
        return {"registered": True, "number": e164, "for": name}

    async def _forget(self, number: str, ctx) -> dict:
        who, e164 = self._who(ctx), normalise(number)
        if not who or not e164:
            return {"removed": False}
        current = await self.store.get_pref(f"phone:{e164}", "owner")
        if not current or current.get("aad") != who:
            return {"removed": False, "reason": "that number isn't registered to you"}
        await self.store.set_pref(f"phone:{e164}", "owner", None)
        await self.store.set_pref(who, "phones", [p for p in await self.store.get_pref(who, "phones") or [] if p != e164])
        await self.store.audit({"actor": who, "action": "phone_removed", "target": e164})
        return {"removed": True, "number": e164}

    async def _take_message(self, args: dict, ctx) -> dict:
        people = await self.outreach.find(self.owner_email)
        if not people:
            return {"delivered": False, "reason": "could not find the owner"}
        owner = people[0]
        caller = args.get("caller_name", "").strip() or "Unknown caller"
        if args.get("company"):
            caller += f" ({args['company'].strip()})"
        shown = getattr(ctx, "caller_number", None) or "withheld"
        callback = args.get("callback_number", "").strip() or shown
        verified = not getattr(ctx, "restricted", False)
        body = (f"**Phone message** from {caller}\n\n{args.get('message', '').strip()}\n\n"
                f"Call back: {callback} · Caller ID: {shown}"
                + ("" if verified else " · _Unverified caller: identity not checked._"))
        ok = await self.outreach.send_to_user(owner["id"], owner.get("name"), body)
        await self.store.audit({"actor": getattr(ctx, "actor", None), "action": "phone_message",
                                "target": owner["id"], "detail": {"caller_id": shown, "delivered": ok}})
        return {"delivered": ok, "to": owner.get("name")}

    async def _call_person(self, person: str, reason: str, ctx, channel: str = "teams", brief: str | None = None) -> dict:
        who = self._who(ctx)
        if not who:
            return {"calling": False, "reason": f"calls can only be placed for {org().name} staff asking from Teams"}
        channel = (channel or "teams").strip().lower()
        if channel in ("phone", "mobile phone", "cell", "telnyx"):
            channel = "mobile"
        if channel not in ("teams", "mobile"):
            return {"calling": False, "reason": "channel must be 'teams' or 'mobile'"}
        if channel == "teams" and not self.teams_caller:
            return {"calling": False, "channel": "teams",
                    "reason": "Teams calling is not set up; offer to ring their registered mobile instead"}
        if channel == "mobile" and not self.dialler:
            return {"calling": False, "channel": "mobile", "reason": "mobile calling is not configured"}
        requester = getattr(ctx, "actor_name", None) or "A colleague"
        if person.strip().lower() in ("me", "myself", "") or (requester and person.strip().lower() == requester.lower()):
            target = {"id": who, "name": requester}
        else:
            people = await self.outreach.find(person)
            if len(people) != 1:
                return {"calling": False, "reason": f"could not pick one person for {person!r}",
                        "candidates": [p.get("name") for p in people[:5]]}
            target = people[0]
        numbers = await self.store.get_pref(target["id"], "phones") or []
        if not numbers and (dir_mobile := await self.store.get_pref(target["id"], "directory_mobile")):
            numbers = [dir_mobile]  # the mobile on their directory entry
        if channel == "mobile" and not numbers:
            return {"calling": False, "channel": "mobile",
                    "reason": f"I don't have a mobile for {target.get('name')}: it isn't in the {org().name} directory and "
                              "they haven't registered one with me (they can message me in Teams: 'register my "
                              "mobile 04...'). I can call them on Teams instead."}
        now = self.clock()
        self_call = target["id"] == who
        if not self_call and not CALL_HOURS[0] <= now.hour < CALL_HOURS[1]:
            return {"calling": False, "reason": f"outside calling hours ({CALL_HOURS[0]}:00-{CALL_HOURS[1]}:00 {place()}); "
                                                "offer to schedule it or send a Teams message instead"}
        day_key = f"calls-out:{now.date().isoformat()}"
        count = await self.store.get_pref(target["id"], day_key) or 0
        if count >= CALLS_PER_PERSON_PER_DAY:
            return {"calling": False, "reason": f"already called {target.get('name')} {count} times today"}
        first = (target.get("name") or "there").split()[0]
        reason = reason.strip().rstrip(".") or "a quick question"
        opener = (f"Hi {first}, it's {self.agent_name}, the AI assistant at {org().name}."
                  + (f" You asked me to call about {reason}." if self_call
                     else f" {requester} asked me to call about {reason}."))
        call_id = f"{'tout' if channel == 'teams' else 'out'}-{uuid.uuid4().hex[:12]}"
        how = "on Teams" if channel == "teams" else "on their mobile"
        from .store import Message
        await self.store.save_message(Message(
            f"call:{call_id}", "user", f"[Call note: you are calling {target.get('name')} {how} at the request of "
            f"{requester}. Reason: {reason}. You opened with: \"{opener}\" Keep it brief and get to the point."
            + (f" What you are calling to tell them (the full version is already in their Teams chat; give the "
               f"gist in a few spoken sentences, then answer questions): {brief[:4000]}" if brief else "") + "]",
            None, "Call note"))
        await self.store.save_message(Message(f"call:{call_id}", "assistant", opener))
        if channel == "teams":
            await self.store.set_pref(f"call-out:{call_id}", "pending", {
                "opener": opener, "aad": target["id"], "name": target.get("name") or "", "requested_by": requester})
            result = await self.teams_caller.ring(target["id"], call_id)
        else:
            initiation = {"type": "conversation_initiation_client_data", "user_id": target["id"],
                          "dynamic_variables": {"greeting": opener, "caller_name": target.get("name") or "there"},
                          "custom_llm_extra_body": {"call_id": call_id, "caller_aad_id": target["id"],
                                                    "caller_name": target.get("name") or "", "kind": "phone-out",
                                                    "caller_number": numbers[0]}}
            result = await self.dialler.dial(numbers[0], initiation)
        ok = bool(result.get("success"))
        if ok:
            await self.store.set_pref(target["id"], day_key, count + 1)
        await self.store.audit({"actor": who, "action": "phone_call_out", "target": target["id"],
                                "detail": {"reason": reason, "requested_by": requester, "call_id": call_id,
                                           "channel": channel, "conversation_id": result.get("conversation_id"),
                                           "teams_call_id": result.get("call_id"), "ok": ok,
                                           "error": None if ok else result.get("message")}})
        if not ok:
            fallback = (" I can try their registered mobile instead (costs per minute)." if channel == "teams" and numbers
                        else "")
            return {"calling": False, "channel": channel,
                    "reason": f"the {'Teams' if channel == 'teams' else 'mobile'} call couldn't be placed: "
                              f"{result.get('message')}.{fallback}"}
        if channel == "teams":
            return {"calling": True, "channel": "teams", "to": target.get("name"),
                    "say": f"I'm calling {target.get('name')} on Teams now: their Teams app will ring."}
        return {"calling": True, "channel": "mobile", "to": target.get("name"), "number_ending": numbers[0][-3:],
                "say": f"I'm phoning {target.get('name')} on their mobile ending {numbers[0][-3:]} now (a phone call, "
                       "not Teams)."}
