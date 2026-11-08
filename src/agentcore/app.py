"""HTTP service for one agent.

  POST /api/messages     Teams (Bot Framework), JWT-verified
  GET  /healthz          liveness
  GET  /readyz           readiness: database reachable and kill switch read recently
  GET  /metrics          Prometheus
  POST /internal/chat    same loop as Teams, for tests and other agents (bearer INTERNAL_API_TOKEN)
  GET  /internal/status  kill switch and budget state (bearer INTERNAL_API_TOKEN)
  PUT  /internal/budget  change the budget now, kept in the database (scripts/agents-budget.sh)
  GET  /internal/audit   recent audit log (bearer INTERNAL_API_TOKEN)
  POST /internal/directory/sync  refresh the staff directory now (directory.py; bearer INTERNAL_API_TOKEN)
  WS   /{agent}/debug/ws  live debug stream: turns, model calls, tools, logs (debug.py; Cognito sign-in)
  POST /internal/notify  agent-initiated Teams message and/or email (bearer INTERNAL_API_TOKEN)
  POST /internal/join    join a Teams meeting by link (bearer INTERNAL_API_TOKEN)
  POST /<agent>/api/calls        Graph calling notifications (voice agents only), JWT-verified
  GET  /<agent>/audio/<id>.wav   short-lived prompts Graph plays into the call

Environment: AGENT_DIR (folder holding agent.yaml and prompts/), SKILLS_DIR, PRICING_FILE,
DATABASE_URL, INTERNAL_API_TOKEN, and the Teams app variables named in agent.yaml.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os

import httpx
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from . import metrics as m
from . import telemetry
from .agent import Agent, Turn
from .openrouter import model_client
from .budget import BudgetTracker
from .org import org, set_agent, slug
from .config import PRICING_DEFAULT, load_agent_config, load_pricing
from .killswitch import KillSwitch
from .mail import GraphMailer
from .skills import SkillLoader
from .store import MemoryStore, PostgresStore
from .teams import AuthError, BotAuthenticator, BotConnector, conversation_reference, is_addressed, message_text
from .tools import ToolGateway, build_clients

log = logging.getLogger(__name__)


class ChatIn(BaseModel):
    conversation_id: str
    text: str
    user_id: str | None = None
    user_name: str | None = None
    tier: str | None = None
    channel: str = "chat"  # "voice" runs the spoken-style path (tests)


class NotifyIn(BaseModel):
    """An agent-initiated message. Only the organisation's staff may be addressed (external mail needs confirm)."""

    text: str
    teams_user_aad_id: str | None = None
    email_to: list[str] = []
    subject: str | None = None
    requested_by: str


class BudgetChange(BaseModel):
    """PUT /internal/budget body (module level: FastAPI resolves the annotation from the module's globals)."""

    daily_usd: float | None = None
    monthly_usd: float | None = None
    by: str = "internal"
    reset: bool = False


def _require_internal(authorization: str | None) -> None:
    expected = os.environ.get("INTERNAL_API_TOKEN", "")
    given = (authorization or "").removeprefix("Bearer ").strip()
    if not expected or not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="unauthorised")


def build_outreach(cfg, connector, store, teams_ids: dict):
    """TeamsOutreach for internal use (people lookup, messages to the owner), or None without Teams."""
    if not connector or not all(teams_ids.values()):
        return None
    from .outreach import TeamsOutreach
    return TeamsOutreach(connector=connector, store=store, footer=cfg.footer,
                         teams_app_external_id=os.environ.get("TEAMS_APP_EXTERNAL_ID"), **teams_ids)


def create_app() -> FastAPI:
    agent_dir = Path(os.environ["AGENT_DIR"])
    cfg = load_agent_config(agent_dir / "agent.yaml")
    set_agent(cfg.agent.display_name, cfg.agent.owner)  # who this process is, for messages the engine writes
    telemetry.setup(cfg.agent.name)
    from . import debug
    debug.install(cfg.agent.name)
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Bedrock, Polly and JWT checks run in worker threads; the default pool is tiny in a small
        # container, and a busy call could starve the model call (once a 40 s stall).
        from concurrent.futures import ThreadPoolExecutor
        asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=32, thread_name_prefix="agent"))
        dsn = os.environ.get("DATABASE_URL")
        if dsn:
            store = PostgresStore(dsn)
            await store.open()
        elif os.environ.get("ALLOW_MEMORY_STORE") == "1":
            store = MemoryStore()
        else:
            raise RuntimeError("DATABASE_URL is required (no agent state on the pod filesystem)")
        skills = SkillLoader(os.environ["SKILLS_DIR"], cfg.agent.name, cfg.skills.always, cfg.skills.on_demand)
        clients = build_clients(cfg)
        tools = ToolGateway(cfg, skills, clients)
        state["tools_clients"] = clients  # the teams outreach client is added once the connector exists

        killswitch = KillSwitch(cfg.agent.name)
        await killswitch.poll_once()
        killswitch.start()
        budget = BudgetTracker(cfg.budget, store, load_pricing(os.environ.get("PRICING_FILE") or PRICING_DEFAULT))
        from .memory import memory_from_config
        memory = memory_from_config(cfg)
        agent = Agent(cfg, model=model_client(cfg.models), store=store,
                      budget=budget, tools=tools, skills=skills, killswitch=killswitch,
                      persona=Agent.load_persona(agent_dir), memory=memory)
        log.info("memory: %s", "hindsight" if memory else "off")
        teams_ids = {k: os.environ.get(getattr(cfg.teams, k + "_env"), "") for k in ("app_id", "app_password", "tenant_id")}
        connector = BotConnector(**teams_ids) if all(teams_ids.values()) else None
        if connector:
            from .transcripts import record_outbound
            _send = connector.send

            async def send_and_record(ref: dict, text: str, reply_to_id: str | None = None):
                """Messages the agent starts itself (summaries, scheduled results, outreach, notices) go into that
                chat's history too, so the agent knows what it said there. Replies are stored by the agent loop."""
                out = await _send(ref, text, reply_to_id)
                if reply_to_id is None:
                    await record_outbound(store, (ref.get("conversation") or {}).get("id"),
                                          text.split(f"\n\n_{cfg.footer}")[0])
                return out
            connector.send = send_and_record
        auth = BotAuthenticator(teams_ids["app_id"]) if teams_ids["app_id"] else None
        mailer = GraphMailer(sender=cfg.agent.upn, **teams_ids) if all(teams_ids.values()) else None
        # People lookup and Teams messages to staff. Built for every agent with a Teams identity: the mailbox
        # poller and scheduler need it even when the agent doesn't get the outreach *tools* (agents
        # without those tools once never started their email checks because this was tied to the tool).
        outreach = build_outreach(cfg, connector, store, teams_ids)
        if outreach and "teams" in cfg.mcp_servers:
            clients["teams"] = outreach
        directory_task = None
        if cfg.directory and all(teams_ids.values()):  # daily copy of the GAL (directory.py)
            from .directory import Directory
            directory = Directory(store, app_id=teams_ids["app_id"], app_password=teams_ids["app_password"],
                                  tenant_id=teams_ids["tenant_id"], memory=memory,
                                  agent_mailboxes=set(cfg.mailbox.ignore_senders) | {cfg.agent.upn})
            await directory.load()
            agent.directory = directory
            if outreach:
                outreach.directory = directory
            state["directory"] = directory
            directory_task = asyncio.create_task(directory.run_forever())
        if "schedule" in cfg.mcp_servers:
            from .scheduler import ScheduleTools
            clients["schedule"] = ScheduleTools(store, outreach)
        if "attachments" in cfg.mcp_servers:
            from .attachments import AttachmentTools
            clients["attachments"] = AttachmentTools(store, bot_token=connector.token if connector else None,
                                                     sharepoint=clients.get("openclaw"), jira=clients.get("jira"))
        if "files" in cfg.mcp_servers:  # files the agent sends: chat, Jira, email, SharePoint (files.py)
            from .files import FileTools
            clients["files"] = FileTools(store, attachments=clients.get("attachments"), sharepoint=clients.get("openclaw"),
                                         jira=clients.get("jira"), connector=connector, mailer=mailer, footer=cfg.footer,
                                         internal_domains=set(cfg.mailbox.internal_domains),
                                         author=cfg.agent.display_name)
            clients["files"].gateway = tools
        if "facts" in cfg.mcp_servers:  # the sales fact register (facts.py)
            from .config import SalesConfig
            from .facts import FactTools
            sales = cfg.sales or SalesConfig()
            clients["facts"] = FactTools(store, [a.model_dump() for a in sales.fact_approvers], owner=sales.owner)
        if "voice" in cfg.mcp_servers:  # speaking speed, per person and per call (speakers.py)
            from .speakers import VoiceTools
            clients["voice"] = VoiceTools(store)
        if "method" in cfg.mcp_servers:  # the method, published versions (method.py; scripts/method-sync.py)
            from .method import MethodTools
            clients["method"] = MethodTools(files=clients.get("files"))
        if "brand" in cfg.mcp_servers:  # branded decks and one-pagers (brand.py; the brand pack)
            from .brand import BrandTools
            clients["brand"] = BrandTools(files=clients.get("files"), facts=clients.get("facts"))
        if "plan" in cfg.mcp_servers:  # project plans: schedule, level, MS Project XML, Gantt views (plan/)
            from .plan.tools import PlanTools
            clients["plan"] = PlanTools(store, files=clients.get("files"), apps=clients.get("openclaw"),
                                        apps_folder=cfg.agent.name)
            clients["plan"].gateway = tools
        if "calendar" in cfg.mcp_servers and all(teams_ids.values()):
            from .calendar_tools import CalendarTools
            clients["calendar"] = CalendarTools(store, mailbox=cfg.agent.upn, app_id=teams_ids["app_id"],
                                                app_password=teams_ids["app_password"], tenant_id=teams_ids["tenant_id"],
                                                internal_domains=set(cfg.mailbox.internal_domains))
        if "review" in cfg.mcp_servers:
            from .review import ReviewModelTools
            clients["review"] = ReviewModelTools(cfg.models, store)
        gh_reviews = None
        import base64
        gh_key = os.environ.get("GITHUB_APP_PRIVATE_KEY") or (
            base64.b64decode(os.environ["GITHUB_APP_PRIVATE_KEY_B64"]).decode()
            if os.environ.get("GITHUB_APP_PRIVATE_KEY_B64") else "")
        if cfg.github.enabled and os.environ.get("GITHUB_APP_ID") and gh_key:
            # automated PR reviews from GitHub webhooks (github_hooks.py); the agent is attached once it exists
            from .github_app import GitHubApp
            from .github_hooks import GitHubReviews
            gh_reviews = GitHubReviews(agent=None, store=store, cfg=cfg.github, review_tools=clients.get("review"),
                                       gh=GitHubApp(os.environ["GITHUB_APP_ID"], gh_key))
            clients["github"] = gh_reviews  # the read-only get_file tool for the review turn
        state["github"] = gh_reviews
        if "history" in cfg.mcp_servers:
            from .transcripts import TranscriptTools
            clients["history"] = TranscriptTools(store)
        if "phone" in cfg.mcp_servers and outreach:
            from .phone import ElevenLabsDialler, PhoneTools
            el = {k: os.environ.get(k) for k in ("ELEVENLABS_API_KEY", "ELEVENLABS_AGENT_ID", "ELEVENLABS_PHONE_NUMBER_ID")}
            dialler = ElevenLabsDialler(*el.values()) if all(el.values()) else None
            bridge = os.environ.get("VOICE_BRIDGE_URL", "")
            teams_caller = None
            if bridge and os.environ.get("VOICE_BRIDGE_TOKEN"):
                from .phone import TeamsCaller
                teams_caller = TeamsCaller(bridge, os.environ["VOICE_BRIDGE_TOKEN"])
            clients["phone"] = PhoneTools(store, outreach, owner_email=cfg.agent.owner_email, dialler=dialler,
                                          agent_name=cfg.agent.display_name.split()[0], teams_caller=teams_caller)
        if "web" in cfg.mcp_servers:  # web search and public pages (web.py)
            from .web import WebTools
            clients["web"] = WebTools(store, cfg.web)
        if "escalation" in cfg.mcp_servers:  # chases, warnings and the overdue list (escalation.py)
            from .escalation import EscalationTools
            clients["escalation"] = EscalationTools(
                store, cfg.escalation, teams=outreach, mail=clients.get("mail"), jira=clients.get("jira"),
                phone=clients.get("phone"), connector=connector, owner=cfg.agent.owner,
                internal_domains=set(cfg.mailbox.internal_domains), killswitch=killswitch)
        await tools.discover()
        mailbox_task = None
        if cfg.mailbox.enabled and all(teams_ids.values()) and outreach:  # incoming email (mailbox.py)
            from .mailbox import MailboxPoller
            poller = MailboxPoller(
                agent=agent, store=store, mailbox=cfg.agent.upn, app_id=teams_ids["app_id"],
                app_password=teams_ids["app_password"], tenant_id=teams_ids["tenant_id"], people=outreach,
                owner_email=cfg.agent.owner_email, internal_domains=set(cfg.mailbox.internal_domains),
                poll_seconds=cfg.mailbox.poll_seconds, ignore_senders=set(cfg.mailbox.ignore_senders),
                # Calendar invites: agents with voice join the meeting at its start time (bridge or Graph path).
                joiner=(lambda url, requester: join_meeting(state, url, requester))
                if (cfg.voice.enabled or os.environ.get("VOICE_BRIDGE_URL")) else None)
            if clients.get("attachments"):  # email attachments are read through the agent's own mailbox access
                clients["attachments"].mail_fetch = poller.fetch_attachment
            mailbox_task = asyncio.create_task(poller.run_forever())
        jira_watch_task = None
        if cfg.jira_watch.enabled and clients.get("jira"):  # Jira comments that @mention the agent (jira_watch.py)
            from .jira_watch import JiraWatcher
            jira_watch_task = asyncio.create_task(JiraWatcher(
                agent=agent, store=store, jira=clients["jira"], poll_seconds=cfg.jira_watch.poll_seconds,
                backfill_hours=cfg.jira_watch.backfill_hours).run_forever())
        github_task = None
        if gh_reviews:
            gh_reviews.agent = agent
            github_task = asyncio.create_task(gh_reviews.run_forever())
        primer_task = None
        if cfg.primer and clients.get("openclaw"):  # the owner's SharePoint primer files (primer.py)
            from .primer import PrimerSync
            primer = PrimerSync(cfg.primer, skills, clients["openclaw"], store, cfg.agent.name)
            await primer.refresh()
            primer_task = asyncio.create_task(primer.run_forever())
        voice = None
        if cfg.voice.enabled and all(teams_ids.values()) and os.environ.get(cfg.voice.public_base_url_env):
            from .voice import AudioStore, AwsSpeech, GraphCalls, VoiceController

            async def post_summary(aad_id: str, text: str) -> None:
                ref = await store.conversation_ref_for_user(aad_id)
                if ref and connector and killswitch.outbound_allowed:
                    await connector.send(ref, f"{agent._strip_footer(text)}\n\n_{cfg.footer}_")

            speech = AwsSpeech(cfg.models.region, cfg.voice.polly_voice, cfg.voice.language, cfg.voice.polly_engine)
            if cfg.voice.elevenlabs_voice_id and os.environ.get("ELEVENLABS_API_KEY"):
                from .voice import ElevenLabsTTS
                speech = ElevenLabsTTS(os.environ["ELEVENLABS_API_KEY"], cfg.voice.elevenlabs_voice_id,
                                       cfg.voice.elevenlabs_model, fallback=speech)
            log.info("voice: tts=%s", type(speech).__name__)
            voice = VoiceController(
                agent=agent, transport=GraphCalls(**teams_ids),
                speech=speech,
                audio=AudioStore(), public_base_url=os.environ[cfg.voice.public_base_url_env], name=cfg.agent.name,
                notify_summary=post_summary, max_minutes=cfg.voice.max_call_minutes,
                wake_words=cfg.voice.wake_words)
        state.update(agent=agent, store=store, killswitch=killswitch, budget=budget, connector=connector,
                     auth=auth, mailer=mailer, skills=skills, tools=tools, voice=voice, outreach=outreach)
        alerts = asyncio.create_task(_budget_alert_loop(state))
        runner_task = None
        async def deliver(user_id: str, text: str) -> bool:
            body = f"{agent._strip_footer(text)}\n\n_{cfg.footer}_"  # agent replies already carry one footer
            ref = await store.conversation_ref_for_user(user_id)
            if ref and connector:
                await connector.send(ref, body)
                return True
            return bool(outreach) and await outreach.send_to_user(user_id, None, body)

        if "schedule" in cfg.mcp_servers:
            from .scheduler import TaskRunner
            if cfg.sales and cfg.sales.sweeps:  # the pre-sales agent's standing sweeps (scheduler.ensure_sweeps)
                from .scheduler import ensure_sweeps
                try:
                    made = await ensure_sweeps(store, cfg.sales.sweeps, cfg.sales.owner_aad_id, cfg.sales.owner)
                    if made:
                        log.info("scheduled sweeps: %s", ", ".join(made))
                except Exception:
                    log.exception("couldn't schedule the sweeps")
            esc = cfg.escalation
            sweep_owner = next((a for a in esc.approvers if a.aad_id), None)
            if esc.enabled and esc.sweep_cron and sweep_owner and "escalation" in cfg.mcp_servers:
                from .config import Sweep
                from .scheduler import ensure_sweeps
                try:
                    await ensure_sweeps(store, [Sweep(name="escalation", cron=esc.sweep_cron, instruction=(
                        "Run the escalation sweep (escalation skill, 'Daily sweep'): chase_status; close what has "
                        "been answered or done; send the warnings that are due; then escalate what is due and give "
                        "me the draft to approve. Report in a few lines; nothing due: say so in one line."))],
                        sweep_owner.aad_id, sweep_owner.name)
                except Exception:
                    log.exception("couldn't schedule the escalation sweep")
            from .scheduler import ResultDelivery
            runner = TaskRunner(agent=agent, store=store, deliver=ResultDelivery(
                teams=deliver, mailer=mailer, outreach=outreach, files=clients.get("files"), phone=clients.get("phone"),
                footer=cfg.footer, strip_footer=agent._strip_footer))
            clients["schedule"].on_new = runner.wake  # background work starts now, not at the next tick
            runner_task = asyncio.create_task(runner.run_forever())
        sweeper_task = None
        if "history" in cfg.mcp_servers:  # agents that take calls: summarise calls however they ended
            from .transcripts import CallSweeper
            sweeper_task = asyncio.create_task(CallSweeper(agent=agent, store=store, deliver=deliver).run_forever())
        rediscover = asyncio.create_task(_rediscover_loop(tools))

        async def resume_later():  # requests a restart cut off (resume.py)
            from .resume import RESUME_DELAY_SECONDS, resume_interrupted
            await asyncio.sleep(RESUME_DELAY_SECONDS)
            if not connector:
                return

            async def post(conv: str, text: str) -> None:
                ref = await store.get_conversation_ref(conv)
                if ref:
                    await connector.send(ref, text)
            for outcome in await resume_interrupted(agent, store, post):
                log.info("interrupted turn: %s", outcome)
        resume_task = asyncio.create_task(resume_later())
        log.info("agent %s ready; tools=%s skills=%s teams=%s", cfg.agent.name, sorted(tools.specs),
                 [s["name"] for s in skills.list()], bool(connector))
        yield
        alerts.cancel()
        if runner_task:
            runner_task.cancel()
        if sweeper_task:
            sweeper_task.cancel()
        from .resume import DRAIN_SECONDS
        left = await agent.drain(DRAIN_SECONDS)  # let turns in progress finish before the pod goes (resume.py)
        if left:
            log.warning("stopping with %d turn(s) still running; the next start will pick them up", left)
        resume_task.cancel()
        if directory_task:
            directory_task.cancel()
        if primer_task:
            primer_task.cancel()
        if mailbox_task:
            mailbox_task.cancel()
        if jira_watch_task:
            jira_watch_task.cancel()
        if github_task:
            github_task.cancel()
        rediscover.cancel()
        await killswitch.stop()
        if isinstance(store, PostgresStore):
            await store.close()

    app = FastAPI(title=f"agent-{cfg.agent.name}", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "agent": cfg.agent.name}

    @app.get("/readyz")
    async def readyz(response: Response):
        ks: KillSwitch = state["killswitch"]
        loop_time = asyncio.get_running_loop().time()
        fresh = ks.override is not None or (ks.last_ok is not None and loop_time - ks.last_ok < 30)
        db_ok = await state["store"].ping()
        m.KILLSWITCH.labels(cfg.agent.name).set(0 if ks.outbound_allowed else 1)
        ok = db_ok and fresh
        response.status_code = 200 if ok else 503
        return {"ok": ok, "db": db_ok, "killswitch_fresh": fresh, "outbound_allowed": ks.outbound_allowed}

    @app.get("/metrics")
    async def metrics():
        m.KILLSWITCH.labels(cfg.agent.name).set(0 if state["killswitch"].outbound_allowed else 1)
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post("/api/messages")
    @app.post(f"/{cfg.agent.name}/api/messages")  # path used behind the shared ingress
    async def messages(request: Request, background: BackgroundTasks, authorization: str | None = Header(None)):
        activity = await request.json()
        auth: BotAuthenticator | None = state["auth"]
        if auth is None:
            raise HTTPException(status_code=503, detail="Teams app not configured")
        try:
            await auth.verify(authorization, activity.get("serviceUrl"))
        except AuthError as exc:
            log.warning("rejected Bot Framework request: %s", exc)
            raise HTTPException(status_code=401, detail="invalid token") from None
        store = state["store"]
        ref = conversation_reference(activity)
        user = activity.get("from") or {}
        conv_id = (activity.get("conversation") or {}).get("id", "")
        # Keep a reference on first contact and on install events.
        await store.upsert_conversation_ref(conv_id, ref, user.get("aadObjectId"))
        if activity.get("type") == "invoke" and activity.get("name") == "fileConsent/invoke":
            files = state["tools_clients"].get("files")
            if files:  # the person clicked Allow/Decline on a file the agent offered (files.py)
                background.add_task(files.on_consent, activity)
            return Response(status_code=200)
        if activity.get("type") != "message":
            await store.audit({"actor": user.get("aadObjectId"), "action": f"activity:{activity.get('type')}",
                               "target": conv_id, "detail": {"action": activity.get("action")}})
            return Response(status_code=200)
        bot_id = (activity.get("recipient") or {}).get("id")
        first_name = cfg.agent.display_name.split()[0]
        text = message_text(activity, bot_id)
        if "attachments" in cfg.mcp_servers:  # files sent to the bot: keep their links server-side (attachments.py)
            from .attachments import file_attachments, remember_attachments
            await remember_attachments(store, conv_id, file_attachments(activity))
        conv_type = (activity.get("conversation") or {}).get("conversationType", "personal")
        turn = Turn(conv_id, text, user.get("aadObjectId"), user.get("name"),
                    scope="private" if conv_type == "personal" else "shared",
                    source={"personal": "teams_1to1", "groupChat": "teams_group", "channel": "teams_channel"}.get(
                        conv_type, "teams_" + conv_type))
        if not is_addressed(activity, bot_id, first_name):
            from .store import Message
            await store.save_message(Message(conv_id, "user", text, turn.user_id, turn.user_name))  # read, don't reply
            return Response(status_code=200)
        voice = state.get("voice")
        if voice:
            from .voice import MEETING_LINK_RE
            link = MEETING_LINK_RE.search(activity.get("text") or "")
            if link:  # asked in chat with a meeting link
                background.add_task(_join_from_chat, state, link.group(0), turn, ref, activity.get("id"))
                return Response(status_code=200)
        background.add_task(_answer_in_teams, state, turn, ref, activity.get("id"))
        return Response(status_code=200)

    @app.post("/internal/chat")
    async def internal_chat(body: ChatIn, authorization: str | None = Header(None)):
        _require_internal(authorization)
        result = await state["agent"].handle(
            Turn(body.conversation_id, body.text, body.user_id, body.user_name, body.tier,  # type: ignore[arg-type]
                 channel=body.channel))
        return result.__dict__

    @app.post(f"/{cfg.agent.name}/github/webhook")
    async def github_webhook(request: Request):
        """GitHub App webhook (github_hooks.py): signature checked over the raw body before anything else."""
        hooks = state.get("github")
        if not hooks:
            return Response(status_code=503)
        body = await request.body()
        code, note = await hooks.receive(request.headers.get("x-github-event", ""),
                                         request.headers.get("x-github-delivery", ""), body,
                                         request.headers.get("x-hub-signature-256"),
                                         os.environ.get("GITHUB_WEBHOOK_SECRET", ""))
        return JSONResponse({"result": note}, status_code=code)

    @app.get("/internal/status")
    async def internal_status(authorization: str | None = Header(None)):
        _require_internal(authorization)
        st = await state["budget"].state(datetime.now(timezone.utc))
        return {"agent": cfg.agent.name, "enabled": cfg.agent.enabled,
                "outbound_allowed": state["killswitch"].outbound_allowed, "budget": st.__dict__,
                "tools": sorted(state["tools"].specs), "skills": state["skills"].list(),
                "tiers": {k: v.model_id for k, v in cfg.models.tiers.items()},
                # every turn running now, whatever the channel (chat, email, Jira, calls): deploys wait for none
                "active_turns": sorted(state["agent"].active)}

    @app.put("/internal/budget")
    async def internal_budget(change: BudgetChange, authorization: str | None = Header(None)):
        """Change this agent's budget now, without a build or restart (scripts/agents-budget.sh). Kept in the
        database until reset; reset goes back to agent.yaml."""
        _require_internal(authorization)
        now = datetime.now(timezone.utc)
        try:
            override = await state["budget"].set_override(
                None if change.reset else change.daily_usd, None if change.reset else change.monthly_usd,
                change.by, now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        await state["store"].audit({"actor": change.by, "action": "budget_changed",
                                    "detail": {"override": override, "reset": change.reset}})
        st = await state["budget"].state(now)
        return {"agent": cfg.agent.name, "override": override, "budget": st.__dict__,
                "defaults": {"daily_usd": cfg.budget.daily_usd, "monthly_usd": cfg.budget.monthly_usd}}

    # ---- live debug stream for the debug console (debug.py) ----------------
    debug_verifier = None
    if os.environ.get("DEBUG_COGNITO_CLIENT_ID"):
        debug_verifier = debug.CognitoVerifier(
            region=os.environ.get("DEBUG_COGNITO_REGION", "ap-southeast-2"),
            pool_id=os.environ.get("DEBUG_COGNITO_POOL_ID", ""), client_id=os.environ["DEBUG_COGNITO_CLIENT_ID"],
            viewers=[v for v in os.environ.get("DEBUG_VIEWERS", cfg.agent.owner_email).split(",") if v.strip()])

    @app.websocket(f"/{cfg.agent.name}/debug/ws")
    async def debug_ws(ws: WebSocket):
        await debug.serve(ws, verifier=debug_verifier,
                          origins=os.environ.get("DEBUG_ORIGINS", org().apps_url).split(","),
                          internal_token=os.environ.get("INTERNAL_API_TOKEN", ""),
                          audit=state["store"].audit if "store" in state else None)

    @app.post("/internal/directory/sync")
    async def internal_directory_sync(authorization: str | None = Header(None)):
        _require_internal(authorization)
        if "directory" not in state:
            raise HTTPException(status_code=404, detail="directory sync not enabled for this agent")
        return await state["directory"].sync()

    @app.get("/internal/audit")
    async def internal_audit(limit: int = 100, authorization: str | None = Header(None)):
        _require_internal(authorization)
        return await state["store"].recent_audit(min(limit, 1000))

    @app.post("/internal/notify")
    async def internal_notify(body: NotifyIn, authorization: str | None = Header(None)):
        _require_internal(authorization)
        if not state["killswitch"].outbound_allowed:
            raise HTTPException(status_code=423, detail="kill switch engaged")
        external = [a for a in body.email_to if a.lower().rsplit("@", 1)[-1] not in {d.lower() for d in org().internal_domains}]
        if external:
            raise HTTPException(status_code=403, detail=f"external recipients need a confirm: {external}")
        sent: dict[str, str] = {}
        store = state["store"]
        if body.teams_user_aad_id:
            ref = await store.conversation_ref_for_user(body.teams_user_aad_id)
            if not ref or not state["connector"]:
                sent["teams"] = "no conversation reference: the user has not installed the app yet"
            else:
                await state["connector"].send(ref, f"{body.text}\n\n_{cfg.footer}_")
                sent["teams"] = "sent"
        if body.email_to:
            if not state["mailer"]:
                sent["email"] = "mail not configured"
            else:
                await state["mailer"].send(body.email_to, body.subject or f"From {cfg.agent.display_name}",
                                           body.text, cfg.footer)
                sent["email"] = "sent"
        await store.audit({"actor": body.requested_by, "action": "notify", "target": ",".join(
            filter(None, [body.teams_user_aad_id, *body.email_to])), "detail": sent})
        return sent

    @app.post("/internal/join")
    async def internal_join(body: dict, authorization: str | None = Header(None)):
        _require_internal(authorization)
        if not state.get("voice") and not os.environ.get("VOICE_BRIDGE_URL"):
            raise HTTPException(status_code=404, detail="voice not enabled for this agent")
        if not state["killswitch"].outbound_allowed:
            raise HTTPException(status_code=423, detail="kill switch engaged")
        if os.environ.get("VOICE_BRIDGE_URL"):
            return {"call_id": await join_meeting(state, body["link"], body.get("requested_by"))}
        return {"call_id": await state["voice"].join_meeting(body["link"], body.get("requested_by"),
                                                              bool(body.get("greet_now")))}

    @app.post(f"/{cfg.agent.name}/api/calls")
    async def calls(request: Request, background: BackgroundTasks, authorization: str | None = Header(None)):
        voice, auth = state.get("voice"), state.get("auth")
        if not voice or not auth:
            raise HTTPException(status_code=404)
        try:
            await auth.verify(authorization, None)
        except AuthError as exc:
            log.warning("rejected calling notification: %s", exc)
            raise HTTPException(status_code=401, detail="invalid token") from None
        background.add_task(voice.handle, await request.json())
        return Response(status_code=202)

    # ---- live voice via an ElevenLabs agent (voice_llm.py) --------------------------------
    @app.get(f"/{cfg.agent.name}/voice/session")
    async def voice_session(call_id: str, caller_aad_id: str = "", caller_name: str = "", kind: str = "p2p",
                            ref: str = "", reconnect: str = "", authorization: str | None = Header(None)):
        from .voice_llm import bearer_ok, initiation_data, outbound_teams_call, signed_url
        if not bearer_ok(authorization, os.environ.get("VOICE_BRIDGE_TOKEN")):
            raise HTTPException(status_code=401, detail="unauthorised")
        if not state["killswitch"].outbound_allowed:
            raise HTTPException(status_code=423, detail="kill switch engaged")
        agent_id, key = os.environ.get("ELEVENLABS_AGENT_ID"), os.environ.get("ELEVENLABS_API_KEY")
        if not agent_id or not key:
            raise HTTPException(status_code=503, detail="ElevenLabs agent not configured")
        from . import speakers
        bridge_call_id = call_id
        if kind == "teams-out":  # a Teams call we placed (phone.call_person): open with why we rang
            call_id, caller_name, greeting = await outbound_teams_call(state["store"], ref, call_id, caller_name)
        elif kind == "meeting":
            greeting = f"Hi all, {cfg.agent.display_name.split()[0]} here, the AI note-taker."
        else:
            from .voice import first_contact_greeting
            greeting = await first_contact_greeting(state["store"], caller_aad_id or None, caller_name or None,
                                                    cfg.agent.display_name.split()[0], cfg.agent.owner)
        speakers.link_call(bridge_call_id, call_id)
        await state["store"].set_pref(f"call:{call_id}", "voice_kind", kind)
        speed = await speakers.speed_for_call(state["store"], call_id, caller_aad_id or None, kind)
        if reconnect:  # the bridge reconnected the voice after a speed change: confirm it, don't greet again
            greeting = ("Okay, I'll talk a bit slower." if speed < 0.97 else
                        "Okay, I'll pick up the pace." if speed > 1.03 else "Okay, back to my normal speed.")
        await state["store"].audit({"actor": caller_aad_id or None, "action": "voice_session", "target": call_id,
                                    "detail": {"kind": kind, "speed": speed, "reconnect": bool(reconnect)}})
        return {"signed_url": await signed_url(key, agent_id),
                "conversation_initiation_client_data": initiation_data(
                    call_id=call_id, caller_aad_id=caller_aad_id, caller_name=caller_name, kind=kind, greeting=greeting,
                    speed=speed)}

    @app.post(f"/{cfg.agent.name}/voice/session")
    async def voice_session_ended(request: Request, authorization: str | None = Header(None)):
        """The bridge reports how a call's voice conversation ended (voice_llm.report_call_end), or who is speaking
        in a meeting (type "speakers", every ~500 ms: speakers.record)."""
        from .voice_llm import bearer_ok, report_call_end
        if not bearer_ok(authorization, os.environ.get("VOICE_BRIDGE_TOKEN")):
            raise HTTPException(status_code=401, detail="unauthorised")
        body = await request.json()
        if body.get("type") == "speakers":
            from . import speakers
            speakers.record(body)
            return {"ok": True}

        async def notify_owner(text: str) -> None:
            outreach = state.get("outreach")
            owners = await outreach.find(cfg.agent.owner_email) if outreach else []
            if owners:
                await outreach.send_to_user(owners[0]["id"], owners[0].get("name"), text)
        return await report_call_end(state["store"], body, notify_owner)

    @app.post(f"/{cfg.agent.name}/v1/chat/completions")
    @app.post(f"/{cfg.agent.name}/v1/v1/chat/completions")  # if ElevenLabs appends /v1 to a /v1 base URL
    async def voice_llm(request: Request, authorization: str | None = Header(None)):
        from fastapi.responses import StreamingResponse
        from .voice_llm import bearer_ok, stream_turn
        if not bearer_ok(authorization, os.environ.get("CUSTOM_LLM_TOKEN")):
            raise HTTPException(status_code=401, detail="unauthorised")
        body = await request.json()
        return StreamingResponse(stream_turn(state["agent"], body), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @app.post(f"/{cfg.agent.name}/voice/phone-init")
    async def phone_init(request: Request, authorization: str | None = Header(None)):
        """ElevenLabs conversation-initiation webhook for phone calls (phone.py): who is calling?"""
        from .phone import caller_for, normalise
        from .voice_llm import bearer_ok
        if not bearer_ok(authorization, os.environ.get("PHONE_INIT_TOKEN")):
            raise HTTPException(status_code=401, detail="unauthorised")
        body = await request.json()
        number = normalise(body.get("caller_id"))
        call_id = str(body.get("conversation_id") or body.get("call_sid") or "unknown")
        known = await caller_for(state["store"], number) if number else None
        first = cfg.agent.display_name.split()[0]
        speed = 1.0
        if known:
            from . import speakers
            speed = await speakers.speed_for_call(state["store"], call_id, known["aad"], "phone")
            from .voice import first_contact_greeting
            greeting = await first_contact_greeting(state["store"], known["aad"], known.get("name"), first,
                                                    cfg.agent.owner)
        else:
            greeting = (f"Hi, you've reached {first}, the AI assistant at {org().name}. I can take a message for "
                        f"{cfg.agent.owner}. Who's calling?")
        await state["store"].audit({"actor": (known or {}).get("aad"), "action": "phone_call_in", "target": call_id,
                                    "detail": {"caller_id": number or "withheld", "verified": bool(known)}})
        return {"type": "conversation_initiation_client_data",
                **({"conversation_config_override": {"tts": {"speed": speed}}} if abs(speed - 1.0) > 0.005 else {}),
                "user_id": (known or {}).get("aad"),
                "dynamic_variables": {"greeting": greeting, "caller_name": (known or {}).get("name") or "there"},
                "custom_llm_extra_body": {"call_id": call_id, "caller_aad_id": (known or {}).get("aad") or "",
                                          "caller_name": (known or {}).get("name") or "", "kind": "phone",
                                          "caller_number": number or ""}}

    @app.get(f"/{cfg.agent.name}/audio/{{key}}.wav")
    async def audio(key: str):
        voice = state.get("voice")
        wav = voice.audio.get(key) if voice else None
        if wav is None:
            raise HTTPException(status_code=404)
        return Response(wav, media_type="audio/wav")

    return app


async def join_meeting(state: dict, link: str, requester_aad_id: str | None, http: httpx.AsyncClient | None = None) -> str:
    """Join a Teams meeting by link: through the Windows media bridge (live audio) when VOICE_BRIDGE_URL is set,
    else the Graph record-and-reply path. Returns the call id."""
    bridge = os.environ.get("VOICE_BRIDGE_URL", "").rstrip("/")
    if bridge:
        client = http or httpx.AsyncClient(timeout=30)
        r = await client.post(f"{bridge}/api/join", json={"joinUrl": link, "requestedBy": requester_aad_id or ""},
                              headers={"Authorization": f"Bearer {os.environ.get('VOICE_BRIDGE_TOKEN', '')}"})
        if r.status_code >= 400:
            raise RuntimeError(f"bridge join failed ({r.status_code}): {r.text[:200]}")
        return (r.json() or {}).get("callId", "")
    if not state.get("voice"):
        raise RuntimeError("voice is not enabled for this agent")
    return await state["voice"].join_meeting(link, requester_aad_id)


async def _join_from_chat(state: dict, link: str, turn: Turn, ref: dict, reply_to: str | None) -> None:
    connector, footer = state["connector"], state["agent"].cfg.footer
    try:
        if not state["killswitch"].outbound_allowed:
            return
        await join_meeting(state, link, turn.user_id)
        text = "Joining the meeting now. I'll announce myself, take notes, and post a summary here afterwards."
    except Exception as exc:
        log.exception("meeting join failed")
        text = f"I couldn't join that meeting: {str(exc)[:300]}"
    if connector:
        await connector.send(ref, f"{text}\n\n_{footer}_", reply_to)


async def _answer_in_teams(state: dict, turn: Turn, ref: dict, reply_to: str | None) -> None:
    try:
        connector = state["connector"]

        async def ack(text: str) -> None:  # first responder: "just a moment, let me pull that up"
            if connector:
                await connector.send(ref, text, reply_to)

        result = await state["agent"].handle(turn, on_ack=ack)
        # Re-check the switch right before sending: the turn may have taken longer than the poll interval.
        if result.reply and state["connector"] and state["killswitch"].outbound_allowed:
            await state["connector"].send(ref, result.reply, reply_to)
    except Exception:
        log.exception("failed to answer in Teams")


async def _rediscover_loop(tools: ToolGateway) -> None:
    """Retry tool discovery while an allow-listed server is missing (e.g. it started after the agent)."""
    while True:
        await asyncio.sleep(60)
        servers = {r.server for r in tools.rules.values()}
        offered = {name.split("__", 1)[0] for name in tools.specs}
        if (servers - offered) & set(tools.clients):
            try:
                await tools.discover()
            except Exception:
                log.exception("tool rediscovery failed")


async def _budget_alert_loop(state: dict) -> None:
    agent: Agent = state["agent"]
    owner_id = os.environ.get("OWNER_AAD_ID")
    while True:
        try:
            for alert in await state["budget"].alerts_due(datetime.now(timezone.utc)):
                text = f"Budget alert for {agent.cfg.agent.display_name}: {alert}.\n\n_{agent.cfg.footer}_"
                await state["store"].audit({"action": "budget_alert", "detail": {"alert": alert}})
                log.warning(text)
                ref = await state["store"].conversation_ref_for_user(owner_id) if owner_id else None
                if ref and state["connector"] and state["killswitch"].outbound_allowed:
                    await state["connector"].send(ref, text)
        except Exception:
            log.exception("budget alert loop")
        await asyncio.sleep(60)


app = create_app() if os.environ.get("AGENT_DIR") else None
