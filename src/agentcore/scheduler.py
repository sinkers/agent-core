"""Scheduled work: people ask an agent to do something later, once or on a repeat.

In-process `schedule` tools (same gateway rules as MCP tools):
  schedule_task(instruction, at?, repeat?, deliver_to?)   one-off at a time, or repeat on a cron (local time)
  list_scheduled_tasks()                                   what the asker has scheduled or receives
  cancel_scheduled_task(task_id)                           only the person who created it
  start_background_task(instruction, deliver_via)          longer work asked for on a call: run it now, off the
                                                           line, and send the result by Teams, email or a call back

The runner claims due tasks every 30 s (safe across pods: FOR UPDATE SKIP LOCKED plus a
10-minute lease), runs each through the normal agent loop as the requester (tools, confirms,
budgets and the kill switch all apply), delivers the result by Teams chat, and sets the next run.
While the kill switch is engaged, tasks wait rather than run. A background task is a one-off task due now;
the runner is woken straight away rather than waiting for its next tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from .org import org, tz, localise, place

log = logging.getLogger(__name__)
TZ = tz()
MIN_REPEAT = timedelta(minutes=15)
DELIVERY = {"teams": "a Teams message", "email": "an email", "call": "a Teams call back",
            "call_mobile": "a call to their mobile"}
MAX_ACTIVE = 50

TOOLS = [
    {"name": "schedule_task",
     "description": ("Schedule work for later. `instruction` is what to do at run time, written so it makes sense on "
                     "its own. Describe the work, never results you worked out now (write 'count the PRJ tickets in "
                     "progress', not '19 tickets'). "
                     "`at` is ISO 8601 local {place} time (e.g. 2026-10-02T09:00) for a one-off, or the first run of a "
                     "repeat. `repeat` is a cron expression in {place} time ('30 8 * * 1-5' = weekdays 08:30). "
                     "`deliver_to` is who gets the result (default: the person asking)."),
     "inputSchema": {"type": "object", "properties": {
         "instruction": {"type": "string"}, "at": {"type": "string"}, "repeat": {"type": "string"},
         "deliver_to": {"type": "string"}}, "required": ["instruction"]}},
    {"name": "list_scheduled_tasks", "description": "List the scheduled tasks the person asking created or receives.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "start_background_task",
     "description": ("Do longer work in the background, now, and send the person asking the result when it's done, "
                     "instead of keeping them waiting (on a call especially). `instruction` must stand on its own: "
                     "everything needed from this conversation (names, ticket keys, documents, links, what they want "
                     "back), as the work runs separately. `deliver_via` is how they asked to get the result: "
                     "'teams' (a Teams message), 'email' (files you make are attached), 'call' (a Teams call back "
                     "with a spoken summary; the full result also goes to their Teams chat) or 'call_mobile' (the "
                     "same, ringing their mobile). Ask them which before calling this."),
     "inputSchema": {"type": "object", "properties": {
         "instruction": {"type": "string"},
         "deliver_via": {"type": "string", "enum": list(DELIVERY)}},
         "required": ["instruction", "deliver_via"]}},
    {"name": "cancel_scheduled_task", "description": "Cancel a scheduled task by id (only its creator can).",
     "inputSchema": {"type": "object", "properties": {"task_id": {"type": "integer"}}, "required": ["task_id"]}},
]
TOOLS = localise(TOOLS)


def parse_local(at: str, now: datetime) -> datetime:
    dt = datetime.fromisoformat(at.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(timezone.utc)


def next_cron(cron: str, after: datetime) -> datetime:
    from croniter import croniter

    return croniter(cron, after.astimezone(TZ)).get_next(datetime).astimezone(timezone.utc)


def check_cron(cron: str, now: datetime) -> None:
    from croniter import croniter

    if not croniter.is_valid(cron):
        raise ValueError(f"not a valid cron expression: {cron!r}")
    first = next_cron(cron, now)
    if next_cron(cron, first) - first < MIN_REPEAT:
        raise ValueError("repeats must be at least 15 minutes apart")


def describe(t: dict) -> dict:
    local = t["run_at"].astimezone(TZ).strftime("%a %d %b %Y %H:%M")
    return {"id": t["id"], "instruction": t["instruction"], "next_run_local": local,
            "repeat": t.get("cron") or "once", "deliver_to": t.get("deliver_to_name"),
            "created_by": t.get("created_by_name"), "runs_so_far": t.get("run_count", 0)}


class ScheduleTools:
    in_process = True

    def __init__(self, store, outreach=None):
        self.store = store
        self.outreach = outreach  # resolves deliver_to names/emails via Graph
        self.on_new = None        # TaskRunner.wake, set by the app: background work starts straight away

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        user_id = getattr(ctx, "actor", None)
        if user_id == "unknown":
            user_id = None
        if name == "schedule_task":
            return json.dumps(await self.schedule(args, user_id))
        if name == "start_background_task":
            return json.dumps(await self.start_background(args, user_id, getattr(ctx, "conversation_id", None),
                                                          getattr(ctx, "actor_name", None)))
        if name == "list_scheduled_tasks":
            return json.dumps({"tasks": [describe(t) for t in await self.store.list_scheduled(user_id)]})
        if name == "cancel_scheduled_task":
            ok = await self.store.cancel_scheduled(int(args["task_id"]), user_id)
            return json.dumps({"cancelled": ok} if ok else {"cancelled": False, "reason": "not found, or not yours to cancel"})
        raise ValueError(f"unknown tool {name}")

    async def schedule(self, args: dict, user_id: str | None) -> dict:
        now = datetime.now(timezone.utc)
        if await self.store.count_scheduled() >= MAX_ACTIVE:
            return {"scheduled": False, "reason": f"the agent already has {MAX_ACTIVE} active tasks"}
        cron = (args.get("repeat") or "").strip() or None
        try:
            if cron:
                check_cron(cron, now)
            run_at = parse_local(args["at"], now) if args.get("at") else (next_cron(cron, now) if cron else None)
        except (ValueError, KeyError) as exc:
            return {"scheduled": False, "reason": str(exc)}
        if run_at is None:
            return {"scheduled": False, "reason": "give a time (`at`) or a repeat"}
        if run_at < now - timedelta(minutes=1):
            return {"scheduled": False, "reason": "that time is in the past"}
        requester_name = await self._name_for(user_id)
        deliver_id, deliver_name = user_id, requester_name
        if args.get("deliver_to") and self.outreach:
            people = await self.outreach.find(args["deliver_to"])
            if len(people) != 1:
                return {"scheduled": False, "reason": f"could not pick one person for {args['deliver_to']!r}",
                        "candidates": [p["name"] for p in people[:5]]}
            deliver_id, deliver_name = people[0]["id"], people[0]["name"]
        task_id = await self.store.create_scheduled({
            "instruction": args["instruction"].strip(), "created_by": user_id, "created_by_name": requester_name,
            "deliver_to": deliver_id, "deliver_to_name": deliver_name, "run_at": run_at, "cron": cron})
        return {"scheduled": True, **describe({"id": task_id, "instruction": args["instruction"], "run_at": run_at,
                                               "cron": cron, "deliver_to_name": deliver_name,
                                               "created_by_name": requester_name})}

    async def start_background(self, args: dict, user_id: str | None, origin: str | None,
                               name: str | None = None) -> dict:
        via = (args.get("deliver_via") or "").strip().lower().replace(" ", "_")
        via = {"chat": "teams", "message": "teams", "mail": "email", "phone": "call", "mobile": "call_mobile",
               "call_back": "call"}.get(via, via)
        if via not in DELIVERY:
            return {"started": False, "reason": f"deliver_via must be one of {', '.join(DELIVERY)}"}
        if not user_id:
            return {"started": False, "reason": f"background work is only for {org().name} staff the agent knows"}
        instruction = (args.get("instruction") or "").strip()
        if not instruction:
            return {"started": False, "reason": "say what to do"}
        if await self.store.count_scheduled() >= MAX_ACTIVE:
            return {"started": False, "reason": f"the agent already has {MAX_ACTIVE} active tasks"}
        name = name or await self._name_for(user_id)
        task_id = await self.store.create_scheduled({
            "instruction": instruction, "created_by": user_id, "created_by_name": name, "deliver_to": user_id,
            "deliver_to_name": name, "run_at": datetime.now(timezone.utc), "cron": None, "deliver_via": via,
            "background": True, "origin": origin})
        await self.store.set_pref(user_id, "background_delivery", DELIVERY[via])  # offered first next time
        if self.on_new:
            self.on_new()
        return {"started": True, "task_id": task_id, "deliver_via": via,
                "say": f"Started. They'll get the result as {DELIVERY[via]} when it's done, usually within a few minutes."}

    async def _name_for(self, user_id: str | None) -> str | None:
        if not user_id:
            return None
        ref = await self.store.conversation_ref_for_user(user_id)
        return ((ref or {}).get("user") or {}).get("name")


async def ensure_sweeps(store, sweeps: list, owner_id: str | None, owner_name: str | None,
                        now: datetime | None = None) -> list[str]:
    """Standing work from config (e.g. daily sweeps): each sweep is a repeating task owned by the agent's
    owner, created if it isn't already scheduled. Tasks are matched by their "[sweep:<name>]" tag, so changing
    a sweep's instruction or schedule in config replaces it. Returns the names created."""
    if not owner_id:
        return []
    now = now or datetime.now(timezone.utc)
    active = await store.list_scheduled(None)
    created = []
    for sw in sweeps:
        tag = f"[sweep:{sw.name}]"
        instruction = f"{tag} {sw.instruction.strip()}"
        mine = [t for t in active if (t.get("instruction") or "").startswith(tag)]
        if any(t["instruction"] == instruction and t.get("cron") == sw.cron for t in mine):
            continue
        for t in mine:  # the config changed: replace the old one
            await store.cancel_scheduled(t["id"], None)
        check_cron(sw.cron, now)
        await store.create_scheduled({"instruction": instruction, "created_by": owner_id, "created_by_name": owner_name,
                                      "deliver_to": owner_id, "deliver_to_name": owner_name,
                                      "run_at": next_cron(sw.cron, now), "cron": sw.cron, "tz": org().timezone})
        created.append(sw.name)
    return created


class TaskRunner:
    def __init__(self, *, agent, store, deliver, interval: float = 30):
        self.agent = agent
        self.store = store
        self.deliver = deliver  # async (aad_id, text, via="teams", task=None) -> bool
        self.interval = interval
        self._wake = asyncio.Event()

    def wake(self) -> None:
        """Run due work now (a background task was just started) instead of at the next tick."""
        self._wake.set()

    async def run_forever(self) -> None:
        running: set[asyncio.Task] = set()
        while True:
            # Ticks run alongside each other (claims are leased), so a long job never delays the next one.
            task = asyncio.create_task(self._safe_tick())
            running.add(task)
            task.add_done_callback(running.discard)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def _safe_tick(self) -> None:
        try:
            await self.tick()
        except Exception:
            log.exception("scheduler tick failed")

    async def tick(self, now: datetime | None = None) -> int:
        if not self.agent.killswitch.outbound_allowed:
            return 0  # tasks wait while the agent is stopped
        now = now or datetime.now(timezone.utc)
        due = await self.store.claim_due(now)
        # Each in its own task: one long background job mustn't hold up another person's.
        await asyncio.gather(*(self._run(t, now) for t in due))
        return len(due)

    async def _run(self, t: dict, now: datetime) -> None:
        from .agent import Turn

        when = t["run_at"].astimezone(TZ).strftime("%a %d %b %H:%M")
        via = t.get("deliver_via") or "teams"
        who = t.get("deliver_to_name") or "the requester"
        if t.get("background"):
            how = {"teams": "as a Teams message", "email": "by email (any file you make with files__create_file is "
                   "attached automatically, so don't send files separately)",
                   "call": "in their Teams chat, and you'll then call them to give the gist",
                   "call_mobile": "in their Teams chat, and you'll then ring their mobile to give the gist"}[via]
            text = (f"[Background task #{t['id']}, asked for by {t.get('created_by_name') or 'a colleague'} at {when} "
                    f"{place()}, to do now while they get on with other things] {t['instruction']}\n"
                    f"Your reply is delivered to {who} automatically {how}: write that message itself, complete and "
                    "standalone (they aren't waiting in a chat), and do not send it separately.")
        else:
            text = (f"[Scheduled task #{t['id']}, requested by {t.get('created_by_name') or 'a colleague'}; "
                    f"due {when} {place()}] {t['instruction']}\n"
                    f"Do it now with fresh data. Your reply is delivered to {who} "
                    "automatically as a Teams message, so write the message itself and do not send it separately.")
        result_text = ""
        try:
            result = await self.agent.handle(Turn(f"task:{t['id']}", text, t.get("created_by"), t.get("created_by_name"),
                                                  trigger="schedule", source="background" if t.get("background") else None))
            result_text = result.reply or ""
            if result_text and t.get("deliver_to"):
                label = "Background task done" if t.get("background") else "Scheduled task"
                header = f"**{label}:** {t['instruction'][:120]}\n\n"
                sent = await self.deliver(t["deliver_to"], header + result_text, via=via, task=t)
                if not sent:
                    log.warning("scheduled task %s: could not deliver to %s", t["id"], t.get("deliver_to_name"))
        except Exception as exc:
            log.exception("scheduled task %s failed", t["id"])
            result_text = f"failed: {exc}"
        next_run = next_cron(t["cron"], now) if t.get("cron") else None
        await self.store.finish_scheduled(t["id"], next_run, result_text, now)
        await self.store.audit({"actor": t.get("created_by"), "action": "scheduled_task_run", "target": str(t["id"]),
                                "detail": {"next_run": next_run.isoformat() if next_run else None}})


class ResultDelivery:
    """Gets a task's result to the person the way they asked (TaskRunner `deliver`).

    teams        their 1:1 Teams chat (`teams(user_id, text)`, the app's usual delivery)
    email        from the agent's mailbox, with the files the task made attached; Teams if email can't be sent
    call         the full result to Teams first (nothing is lost if they miss the call), then a Teams call
    call_mobile  the same, ringing their mobile
    """

    def __init__(self, *, teams, mailer=None, outreach=None, files=None, phone=None, footer: str = "",
                 strip_footer=None):
        self.teams, self.mailer, self.outreach = teams, mailer, outreach
        self.files, self.phone, self.footer = files, phone, footer
        self.strip_footer = strip_footer or (lambda t: t)  # agent replies carry the chat footer; email adds its own

    async def __call__(self, user_id: str, text: str, via: str = "teams", task: dict | None = None) -> bool:
        task = task or {}
        conv = f"task:{task['id']}" if task.get("id") else ""
        if via == "email":
            address = await self.outreach.email_for(user_id) if self.outreach and self.mailer else None
            if address:
                files = await self.files.outbox_files(conv) if self.files and conv else []
                subject = f"Done: {(task.get('instruction') or 'the work you asked for')[:90]}"
                try:
                    await self.mailer.send([address], subject, self.strip_footer(text), self.footer,
                                           attachments=files or None)
                    return True
                except Exception:
                    log.exception("background task %s: email failed; sending by Teams", task.get("id"))
            return await self.teams(user_id, text + "\n\n_(I couldn't email this, so it's here instead.)_")
        if via in ("call", "call_mobile"):
            sent = await self.teams(user_id, text)
            if not self.phone:
                return sent
            reason = f"the {(task.get('instruction') or 'work')[:80].rstrip('.')} you asked me to look into"
            outcome = await self.phone.call_back(user_id, task.get("deliver_to_name"), reason, text,
                                                 "mobile" if via == "call_mobile" else "teams", conv)
            if not outcome.get("calling"):
                await self.teams(user_id, f"I tried to call you with this but couldn't: {outcome.get('reason')}")
            return sent
        return await self.teams(user_id, text)
