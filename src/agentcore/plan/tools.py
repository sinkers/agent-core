"""The agent's `plan` tools (in-process): build, schedule, level, check and export project plans.

  make_plan(plan, plan_id?, site?, folder_path?)
                                  schedule and level a plan spec (model.py); returns the dates, critical path,
                                  over-allocations and health; plan_id updates an existing plan. Every call also
                                  writes the MS Project XML (the master file): into the
                                  outbox always, and into the plan's SharePoint folder once it has one (site and
                                  folder_path here or on publish_plan), replacing the agent's own copy on each revision
                                  unless someone has edited it in SharePoint since
  export_plan(plan_id, formats)   msproject_xml (master file), html (viewer), mermaid (text), mermaid_html (page),
                                  as files in the outbox: send_file, attach_to_jira, email_file, save_file_to_sharepoint
  import_project_xml(file)        an existing MS Project XML (sent in chat or email, or a SharePoint link) as a plan
                                  the agent can review, re-level and re-export
  publish_plan(plan_id, site?, folder_path?)
                                  the viewer (with the MS Project XML to download) at <apps site>/<agent>/plan-<name>/,
                                  behind sign-in, and optionally a link file in a SharePoint folder (through the
                                  gateway's sharepoint_put rule); make_plan on a published plan republishes it
  plan_to_jira(plan_id, apply?)    the plan's dates, epic parents, Blocks links and order onto the Jira tickets its tasks
                                  name (jira-timeline skill); a preview unless apply, which asks for a yes
  plan_to_confluence(plan_id, space_id?, parent_id?)
                                  the plan as a native Confluence page in Atlassian's project-plan layout
                                  (confluence.py); creates it once, then replaces it (update_page: asks first)
  edit_plan(plan_id, set?, upsert_tasks?, remove_tasks?, upsert_resources?, remove_resources?, non_working?)
                                  change part of a saved plan without writing it all out again
  get_plan(plan_id, under?)       the saved spec (or one branch of it), to read before editing
  list_plans(query?)              plans saved, with their sub-plans and the masters that include them
  plan_deliverables(plan_id, stage?, refresh?)
                                  deliverables by phase (deliverables.py): each method deliverable for the plan's
                                  stages, the tasks that produce it, and its documents and status from the register
  plan_map_deliverables(plan_id)  proposed task -> method deliverable tags, for a person to confirm
  plan_gate_pack(plan_id, gate)   a gate's evidence list as a branded Excel workbook
  plan_overview(plan_id, level?, formats?)
                                  the one-page view (overview.py): workstreams with durations and key milestones,
                                  as an editable PowerPoint slide (brand master), PNG and SVG

Large plans are programmes (programme.py): each part is its own plan (a sub-plan) that can be built and revised by
itself, and a master plan includes them with {"id": "civil", "subplan": "<plan_id>"}. Scheduling the master pulls
every sub-plan in, so the master's viewer, MS Project XML, Jira and Confluence views show the whole programme.
Changing a sub-plan refreshes the published viewers of the masters that include it.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import re
import time
import uuid
from datetime import datetime

from ..org import org, localise, agent
from .confluence import confluence_page
from .health import check
from .model import PlanError, from_spec
from .mspdi import export_xml, import_xml
from .deliverables import REGISTER_CACHE_S, build as build_deliverables, propose_tags, read_register
from .overview import overview_data, overview_png, overview_pptx, overview_svg
from .programme import expand, subplan_ids
from .render import html_viewer, mermaid, mermaid_page
from .schedule import schedule

APPS_URL = org().apps_url.rstrip("/")  # where plans publish (org.yaml); each agent under its own folder
EDIT_GRACE_S = 120  # SharePoint's last-modified within this of the agent's own save is its save, not an edit
FORMATS = {"msproject_xml": ".xml", "html": ".html", "mermaid": ".mmd", "mermaid_html": "-mermaid.html"}
OVERVIEW_FORMATS = {"pptx": " - overview.pptx", "png": " - overview.png", "svg": " - overview.svg"}
PROGRAMME_NOTE = (
    " Large plans: don't write one huge spec. Build each part (a stage, workstream or package) as its own plan, then "
    "a master plan that includes them with tasks {id, subplan: <plan_id>, depends_on?}; link across parts in the "
    "master with \"<that task's id>/<task id in the sub-plan>\" (e.g. \"civil/m3\"). Use the same resource ids "
    "across parts so levelling sees each person's whole load. Change one part later with edit_plan.")
TOOLS = [
    {"name": "make_plan",
     "description": (
         "Build a project plan (Gantt) from a spec, schedule it on the {place} working calendar ({holidays_label}), "
         "find the critical path and float, level resources so no one is over-booked, and check its health. Spec: "
         "{name, start, status_date?, days_per_point?, non_working?: [{date, name}], resources: [{id, name, email?, role?, "
         "max_units? (1 = full time), leave?: [{from, to}]}], tasks: [{id, name, parent? (makes the parent a summary), "
         "duration (working days) | story_points | milestone: true, depends_on?: [id | {id, type: FS|SS|FF|SF, lag}], "
         "resources?: [id | {id, units}], start_no_earlier?, deadline?, priority?, percent_complete?, baseline_start?, "
         "baseline_finish?, jira? (issue key), notes?, document?, key? (true: show on the overview), url?, "
         "subplan? (a plan_id to include, see below), deliverable? ({org} method deliverable ID(s) the task produces, "
         "e.g. \"D2.02\": plan_deliverables), gate? (\"G3\": the milestone that CLOSES a stage, {org} method; "
         "a milestone named \"G3 ...\" is one automatically), gateway? (true: a mid-stage gateway such as design freeze "
         "(IFC) in Stage 4 or IST passed in Stage 6)}]}. Each stage's gate is its last milestone: work that continues "
         "after it belongs in the next stage or in a stream that runs across stages (e.g. long-lead procurement, "
         "released at G2), and the health check flags it. A good plan: stages as summary tasks, deliverables "
         "as tasks, milestones for stage gates, approvals, external dates and handover, real dependencies (no open "
         "ends), named owners with leave, explicit contingency tasks, Jira keys on tasks. Returns plan_id; pass it "
         "back with a changed spec to revise the plan. Every plan always has its MS Project XML (the master file): "
         "each call returns it as a file, and once the plan has a SharePoint home (give site and folder_path, e.g. "
         "the project's folder in 3.0 Projects, the first time) it saves the XML there and keeps that copy up to "
         "date on every revision. Give the project's folder for every real plan." + PROGRAMME_NOTE),
     "inputSchema": {"type": "object", "properties": {
         "plan": {"type": "object"}, "plan_id": {"type": "string"},
         "replace_edited": {"type": "boolean", "description": (
             "only after the person said yes: replace a SharePoint copy of the XML that someone else has edited")},
         "site": {"type": "string", "description": "SharePoint site for the MS Project XML, e.g. 3.0 Projects"},
         "folder_path": {"type": "string", "description": "the project's folder in that site"}},
         "required": ["plan"]}},
    {"name": "export_plan",
     "description": (
         "Produce a plan's files: msproject_xml (MS Project XML, the master file: opens in MS Project and ProjectLibre), "
         "html (a self-contained viewer: Gantt with critical path and dependencies, milestones, resource load heat map, "
         "levelled/unlevelled toggle, health), mermaid (Mermaid gantt text for Confluence or Markdown), mermaid_html "
         "(a page that draws it). Default: msproject_xml and html, levelled. SharePoint downloads .html files rather "
         "than showing them, so the html viewer is for email, chat or download. Returns file references for send_file, "
         "attach_to_jira, email_file or save_file_to_sharepoint."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"},
         "formats": {"type": "array", "items": {"type": "string", "enum": list(FORMATS)}},
         "levelled": {"type": "boolean", "description": "default true"}}, "required": ["plan_id"]}},
    {"name": "import_project_xml",
     "description": ("Read an existing MS Project XML plan (a file sent in this chat or email, or a SharePoint link) "
                     "into a plan you can check, re-level and export. Returns plan_id, the spec and the health check."),
     "inputSchema": {"type": "object", "properties": {"file": {"type": "string"}}, "required": ["file"]}},
    {"name": "publish_plan",
     "description": (
         "Publish a plan's web viewer to {apps_host} ({org} sign-in only), with the MS Project XML downloadable "
         "from the page. The address stays the same for the plan, and revising it with make_plan republishes it. "
         "Give site and folder_path (e.g. the project's folder in 3.0 Projects) to also put a link to the viewer "
         "there, so people find it from SharePoint; replacing an existing link asks first. Returns the URL."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"},
         "site": {"type": "string", "description": "SharePoint site for the link, e.g. 3.0 Projects"},
         "folder_path": {"type": "string", "description": "folder in that site's documents"}},
         "required": ["plan_id"]}},
    {"name": "plan_to_jira",
     "description": (
         "Make the Jira timeline match the plan, for tasks that carry a jira key: Start date and Due date from the "
         "levelled schedule (milestones get one day), each ticket's parent set to its stage's epic (a summary task with a "
         "jira key), a Blocks link for every finish-to-start dependency between keyed tasks (Jira only draws Blocks; "
         "SS/FF/SF are listed, not linked), and rank in plan order so the timeline reads top to bottom. Without apply "
         "it only previews the changes against Jira's current values; apply=true makes them (asks for a yes) and then "
         "audits the result. The plan is the master for dates and order; Jira stays the master for status and "
         "assignee. Load the jira-timeline skill first."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"}, "apply": {"type": "boolean", "description": "default false: preview only"}},
         "required": ["plan_id"]}},
    {"name": "plan_to_confluence",
     "description": (
         "Write the plan as a Confluence page laid out like Atlassian's project plan template: status panel with "
         "delivery date and links to the web viewer, overview, goals and success measures, scope in/out, a timeline "
         "(Gantt as a shaded table), milestones, work breakdown with owners and Jira links, team and resources, "
         "decisions (DACI), risks and dependencies with schedule health, communications. Add the narrative to the plan "
         "spec first (make_plan): summary, budget, approach, goals, success_measures, scope_in, scope_out, "
         "assumptions, stakeholders [{name, role: Driver|Approver|Contributor|Informed, notes}], risks [{risk, "
         "likelihood, impact, owner, mitigation}], communications [{what, audience, when, how}]. First time: give "
         "space_id (list_spaces, or get_page on a page in the space) and optionally parent_id (that page, to nest under it); it creates the page. After that it replaces the same page, "
         "which needs a yes. Publish the viewer first (publish_plan) so the page links to it."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"}, "space_id": {"type": "string"},
         "parent_id": {"type": "string", "description": "page to create it under"}}, "required": ["plan_id"]}},
    {"name": "edit_plan",
     "description": (
         "Change part of a saved plan (or sub-plan) without writing the whole spec again: then it is scheduled, "
         "saved and re-exported exactly like make_plan (MS Project XML kept current, published viewers refreshed, "
         "including any master plan that includes this one). set: top-level fields {name, start, status_date, ...}. "
         "upsert_tasks: tasks by id; an existing task gets only the fields given (null removes a field), a new one "
         "is added (after: <task id> places it after that task, else at the end). remove_tasks: ids (their subtasks "
         "go too, and links to them are dropped and listed). upsert_resources / remove_resources: the same for "
         "people. non_working: replaces the list. site and folder_path move where the plan's MS Project XML is kept "
         "(don't move the file yourself: the next revision would save it back to the old folder). Read the current "
         "spec with get_plan first."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"}, "set": {"type": "object"},
         "upsert_tasks": {"type": "array", "items": {"type": "object"}},
         "remove_tasks": {"type": "array", "items": {"type": "string"}},
         "upsert_resources": {"type": "array", "items": {"type": "object"}},
         "remove_resources": {"type": "array", "items": {"type": "string"}},
         "non_working": {"type": "array"},
         "site": {"type": "string", "description": "move the plan's MS Project XML home: SharePoint site"},
         "folder_path": {"type": "string", "description": "and folder (saved there from now on)"},
         "replace_edited": {"type": "boolean"}}, "required": ["plan_id"]}},
    {"name": "get_plan",
     "description": ("A saved plan's spec, to read before edit_plan. under: a task id to return only that branch "
                     "(the task and everything beneath it). Also lists its sub-plans and the master plans that "
                     "include it."),
     "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}, "under": {"type": "string"}},
                     "required": ["plan_id"]}},
    {"name": "list_plans",
     "description": ("Plans saved so far: plan_id, name, dates, size, viewer link, sub-plans and the masters that "
                     "include each one. query filters by name."),
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
    {"name": "plan_deliverables",
     "description": (
         "Deliverables by phase for a plan: every {org} method deliverable for the plan's stages (those with a gate in "
         "the plan), the tasks that produce it (tasks tagged deliverable: \"D2.02\"), and the documents that satisfy "
         "it with their revision, status and folder link, read live from the project register (the plan's "
         "spec.register, e.g. PRJ-GEN-REG-0005). Status: the register's (Draft, In review, Approved...), or Not "
         "started / In progress (no number) / Not planned; late and at-risk against the task's finish and the gate. "
         "Also gate evidence counts and findings (gate evidence missing, deliverables with no task, work started "
         "without a number). The published viewer shows the same as its Deliverables tab. refresh re-reads the "
         "register now (otherwise up to 15 minutes old). stage: 0-7 to limit it."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"}, "stage": {"type": "integer"}, "refresh": {"type": "boolean"}},
         "required": ["plan_id"]}},
    {"name": "plan_map_deliverables",
     "description": (
         "Propose which plan task produces each {org} method deliverable not yet tagged: candidates by name within the "
         "stage that ends at that deliverable's gate. Show the proposals to the person, then tag the confirmed ones "
         "with edit_plan (upsert_tasks [{id, deliverable: [\"D2.02\"]}]); for a task inside a sub-plan, edit the "
         "sub-plan with the task's own id (the part after \"<stage>/\")."),
     "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}}, "required": ["plan_id"]}},
    {"name": "plan_gate_pack",
     "description": (
         "A gate's evidence list for the Board or leadership team paper: every deliverable the stage must produce, "
         "with producer and approver roles, its documents (number, revision, status, link) and the tasks behind it, "
         "as a {org} Excel workbook in the outbox. gate: G0-G6."),
     "inputSchema": {"type": "object", "properties": {"plan_id": {"type": "string"}, "gate": {"type": "string"}},
                     "required": ["plan_id", "gate"]}},
    {"name": "plan_overview",
     "description": (
         "The high-level view of a plan for slide packs, emails and status reports: its workstreams as bars with "
         "their durations (critical path in ochre) and the key milestones as diamonds, on a month or quarter "
         "timeline. formats: pptx (a {org} deck: the overview drawn as editable shapes on the master template, then a "
         "milestone table), png (for Word, email, Teams), svg. Default pptx and png. level: which outline level is a "
         "workstream (1 = the top-level stages or sub-plans; 2 = one level down). Mark the milestones and any "
         "deeper workstreams that matter with key: true in the spec (edit_plan); otherwise the milestones at the "
         "workstream level are used. Returns the files and the data (workstreams with start, finish and duration; "
         "milestones with dates) for writing it up."),
     "inputSchema": {"type": "object", "properties": {
         "plan_id": {"type": "string"}, "level": {"type": "integer"},
         "formats": {"type": "array", "items": {"type": "string", "enum": list(OVERVIEW_FORMATS)}}},
         "required": ["plan_id"]}},
]
TOOLS = localise(TOOLS)


def _slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-")[:60] or "plan"


def app_name(plan_name: str) -> str:
    """Apps site slug: lowercase letters, digits and hyphens, at most 64."""
    return "plan-" + (re.sub(r"[^a-z0-9]+", "-", plan_name.lower()).strip("-")[:55].strip("-") or "project")


def link_file(url: str) -> bytes:
    """A Windows internet shortcut: SharePoint lists it and opens the URL when clicked."""
    return f"[InternetShortcut]\r\nURL={url}\r\n".encode()


def summarise(s) -> dict:
    plan = s.plan
    wbs = plan.wbs()
    findings = check(s)
    crit = [f"{wbs[t]} {plan.tasks[t].name}" for t in s.critical]
    milestones = [{"name": t.name, "date": s.start_date(t).isoformat(), "float": t.float_days,
                   **({"deadline": t.deadline.isoformat()} if t.deadline else {})}
                  for t in (plan.tasks[i] for i in plan.order) if t.milestone]
    return {"start": s.calendar.start.isoformat(), "finish": s.finish_date_of(s.lev_finish).isoformat(),
            "finish_unlevelled": s.finish_date_of(s.finish).isoformat(),
            "levelling_adds_working_days": s.lev_finish - s.finish, "tasks": len(plan.leaves()),
            "critical_path": crit[:30], "milestones": milestones, "over_allocated_before_levelling": s.unlevelled_over,
            "still_over_allocated": s.over_allocated,
            "health": {"errors": [f["message"] for f in findings if f["severity"] == "error"],
                       "warnings": [f["message"] for f in findings if f["severity"] == "warning"][:15],
                       "info": [f["message"] for f in findings if f["severity"] == "info"][:10]}}


class PlanTools:
    in_process = True

    def __init__(self, store, files=None, apps=None, apps_folder: str = "plans"):
        self.store = store
        self.apps_folder = apps_folder  # this agent's folder on the apps site (its name)
        self.files = files  # FileTools: outbox for exports, resolve() for imports, SharePoint saves
        self.apps = apps    # MCP client for the openclaw server (org.apps_publish_tool)
        self.gateway = None  # ToolGateway, set by the app: Confluence writes go through its rules

    async def list_tools(self) -> list[dict]:
        return TOOLS

    async def _spec(self, plan_id: str) -> dict | None:
        saved = await self.store.get_pref(f"plan:{plan_id}", "spec")
        return saved.get("spec") if saved else None

    async def _publish(self, plan_id: str, s) -> str:
        """Publish (or refresh) the viewer; returns its URL. The app name is fixed at first publish, so renaming
        the plan keeps the address."""
        saved = await self.store.get_pref(f"plan:{plan_id}", "published") or {}
        app = saved.get("app") or app_name(s.plan.name)
        try:
            png = overview_png(overview_data(s))
        except Exception:  # the overview is a bonus: never block publishing the plan
            png = None
        links = [("Overview for slides (PNG)", "overview.png")] if png else []
        try:
            deliv = await self._deliverables(plan_id, await self._spec(plan_id) or {}, s)
        except Exception:  # deliverables are a bonus on the viewer: never block publishing
            deliv = None
        files = [{"path": "index.html", "mime_type": "text/html",
                  "content_base64": base64.b64encode(html_viewer(s, download="plan.xml", links=links,
                                                                 deliverables=deliv)).decode()},
                 {"path": "plan.xml", "mime_type": "application/xml",
                  "content_base64": base64.b64encode(export_xml(s, True)).decode()}]
        if png:
            files.append({"path": "overview.png", "mime_type": "image/png",
                          "content_base64": base64.b64encode(png).decode()})
        # One file per call: the openclaw server's request limit was 100 kB (index.html first: an app needs one)
        for f in files:
            await self.apps.call_tool(org().apps_publish_tool, {"agent": self.apps_folder, "app_name": app, "files": [f]})
        url = f"{APPS_URL}/{self.apps_folder}/{app}/"
        await self.store.set_pref(f"plan:{plan_id}", "published", {"app": app, "url": url})
        return url

    async def _xml(self, plan_id: str, s, ctx, conv: str, site: str = "", folder_path: str = "",
                   replace_edited: bool = False) -> dict:
        """The plan's MS Project XML, always: a fresh outbox file, and its SharePoint copy kept current.

        The first save into a folder is an ordinary new file (an existing file of that name asks first). After that
        the agent replaces only the copy it saved itself, and only if nobody has changed it in SharePoint since; an
        edited copy is left alone and it is told to import it first, so MS Project edits are never overwritten."""
        data = export_xml(s, True)
        filename = _slug(s.plan.name) + ".xml"
        out = {"file": await self.files._put_outbox(conv, filename, data), "name": filename}
        gateway = self.gateway or getattr(self.files, "gateway", None)
        home = await self.store.get_pref(f"plan:{plan_id}", "xml") or {}
        if site and (site, folder_path) != (home.get("site"), home.get("folder_path")):
            home = {"site": site, "folder_path": folder_path, "filename": filename}  # a new home: a new file there
        if not home.get("site"):
            out["sharepoint"] = "not saved: give site and folder_path (the project's folder) to keep it in SharePoint"
            return out
        if not gateway:
            out["sharepoint"] = "SharePoint isn't available here"
            return out
        path = "/".join(x.strip("/") for x in (home["folder_path"], home["filename"]) if x.strip("/"))
        put = {"site": home["site"], "folder_path": home["folder_path"], "filename": home["filename"],
               "content_base64": base64.b64encode(data).decode()}
        if home.get("saved_at"):  # the agent's copy: replace it, unless it was edited after its last save
            seen = await gateway.call("openclaw__sharepoint_get", {"site": home["site"], "path": path}, ctx, True)
            m = re.search(r"\*\*last_modified:\*\*\s*(\S+)", seen.content or "")
            if m:
                try:
                    changed = datetime.fromisoformat(m.group(1).replace("Z", "+00:00")).timestamp()
                except ValueError:
                    changed = 0
                if changed > home["saved_at"] + EDIT_GRACE_S and not replace_edited:
                    out["sharepoint"] = {"saved": False, "path": f"{home['site']}/{path}", "reason": (
                        "someone has edited this file in SharePoint since I last saved it, so I left it alone. Ask "
                        "the person: import_project_xml it to take their changes into the plan, or replace it with "
                        "this version (make_plan again with replace_edited: true, which asks them for a yes)")}
                    return out
            outcome = await gateway.call("openclaw__sharepoint_put", {**put, "overwrite": True},
                                         dataclasses.replace(ctx, confirmed=True), True)  # his own file: no yes
        else:
            outcome = await gateway.call("openclaw__sharepoint_put", put, ctx, True)
            if outcome.status == "needs_confirm":  # a file of that name is already there: a "yes" replaces it
                await self.store.put_pending(conv, "openclaw__sharepoint_put", put)
        if outcome.status == "ok":
            home["saved_at"] = time.time()
            await self.store.set_pref(f"plan:{plan_id}", "xml", home)
        out["sharepoint"] = {"saved": outcome.status == "ok", "status": outcome.status,
                             "path": f"{home['site']}/{path}", "result": outcome.content[:400]}
        return out

    async def _confluence(self, args: dict, ctx, conv: str) -> dict:
        plan_id = args.get("plan_id", "")
        spec = await self._spec(plan_id)
        if not spec:
            return {"error": "no such plan: build it with make_plan first"}
        if not self.gateway:
            return {"error": "Confluence isn't available here"}
        s, _ = await self._build(spec)
        published = await self.store.get_pref(f"plan:{plan_id}", "published") or {}
        doc = confluence_page(s, spec, published.get("url"))
        page = await self.store.get_pref(f"plan:{plan_id}", "confluence") or {}
        if page.get("page_id"):
            tool, call = "confluence__update_page", {"page_id": page["page_id"], "body": doc, "mode": "replace",
                                                     "version_message": f"Plan updated by {agent().display_name}"}
        else:
            if not args.get("space_id"):
                return {"error": "space_id is needed the first time: list_spaces, or get_page on a page in the space"}
            tool, call = "confluence__create_page", {"space_id": args["space_id"], "title": f"{s.plan.name} - project plan",
                                                     "body": doc, **({"parent_id": args["parent_id"]}
                                                                     if args.get("parent_id") else {})}
        outcome = await self.gateway.call(tool, call, ctx, True)
        if outcome.status == "needs_confirm":  # a "yes" replays it (agent.py pending)
            await self.store.put_pending(conv, tool, call)
            return {"status": "needs_confirm", "page": page.get("url"), "reason": outcome.content[:300]}
        if outcome.status != "ok":
            return {"status": outcome.status, "error": outcome.content[:800]}
        try:
            result = json.loads(outcome.content)
        except ValueError:
            result = {}
        if result.get("id") and not page.get("page_id"):
            page = {"page_id": result["id"], "url": result.get("url")}
            await self.store.set_pref(f"plan:{plan_id}", "confluence", page)
        return {"status": "ok", "page": result.get("url") or page.get("url"), "created": tool.endswith("create_page"),
                **({} if published else {"tip": "publish_plan first so the page links to the interactive Gantt"})}

    async def _gw(self, tool: str, args: dict, ctx) -> dict:
        outcome = await self.gateway.call(tool, args, ctx, True)
        if outcome.status != "ok":
            raise PlanError(f"{tool}: {outcome.content[:300]}")
        try:
            return json.loads(outcome.content)
        except ValueError:
            return {"text": outcome.content[:300]}

    async def _jira(self, args: dict, ctx) -> dict:
        spec = await self._spec(args.get("plan_id", ""))
        if not spec:
            return {"error": "no such plan: build it with make_plan first"}
        if not self.gateway:
            return {"error": "Jira isn't available here"}
        s, _ = await self._build(spec)
        plan = s.plan
        keyed = [plan.tasks[i] for i in plan.order if plan.tasks[i].jira]
        if not keyed:
            return {"error": "no task in the plan has a jira key: add jira: \"KEY-123\" to the tasks (and stages) first"}
        keys = [t.jira for t in keyed]
        if len(set(keys)) != len(keys):
            return {"error": "two tasks name the same Jira ticket; each ticket can be one task only"}
        current = {i["key"]: i for i in (await self._gw("jira__timeline_audit", {"keys": keys}, ctx)).get("items", [])}
        missing = [k for k in keys if k not in current]
        task_of = {t.jira: t for t in keyed}

        def epic_of(t):
            p = t.parent
            while p and not plan.tasks[p].jira:
                p = plan.tasks[p].parent
            return plan.tasks[p].jira if p else None
        changes: list[dict] = []
        for t in keyed:
            if t.jira in missing:
                continue
            cur = current[t.jira]
            start, due = s.start_date(t).isoformat(), s.finish_date(t).isoformat()
            edit = {}
            if cur.get("start") != start:
                edit["start_date"] = start
            if cur.get("due") != due:
                edit["due_date"] = due
            parent = epic_of(t)
            if parent and cur.get("parent") != parent:
                edit["parent"] = parent
            if edit:
                changes.append({"tool": "jira__update_issue", "args": {"key": t.jira, **edit},
                                "why": f"{t.name}: " + ", ".join(f"{k.replace('_date', '')} {cur.get(k.replace('_date', ''))} -> {v}"
                                                                 for k, v in edit.items())})
        not_drawn = []
        for t in keyed:
            for link in t.links:
                pred = plan.tasks[link.pred]
                if not pred.jira or pred.jira in missing or t.jira in missing:
                    continue
                if link.type != "FS":
                    not_drawn.append(f"{pred.jira} -> {t.jira} ({link.type}): Jira's timeline only draws finish-to-start")
                    continue
                if t.jira not in (current[pred.jira].get("blocks") or []):
                    changes.append({"tool": "jira__link_issues", "args": {"blocker": pred.jira, "blocked": t.jira},
                                    "why": f"{pred.name} blocks {t.name}"})
        ranked = [k for k in current if k in task_of]  # Jira's order (audit returns rank order)
        wanted = [k for k in keys if k not in missing]
        if ranked != wanted and len(wanted) > 1:
            changes.append({"tool": "jira__rank_issues", "args": {"keys": wanted}, "why": "put the tickets in plan order"})
        out = {"plan": plan.name, "tickets": len(keys), "missing_in_jira": missing, "changes": len(changes),
               "preview": [c["why"] for c in changes][:80], "not_drawn": not_drawn[:20]}
        if not args.get("apply"):
            out["next"] = "plan_to_jira with apply=true makes these changes (asks for a yes)" if changes else "Jira already matches"
            return out
        done, failed = 0, []
        for c in changes:
            try:
                await self._gw(c["tool"], c["args"], ctx)
                done += 1
            except PlanError as exc:
                failed.append({"change": c["why"], "error": str(exc)[:300]})
        after = await self._gw("jira__timeline_audit", {"keys": wanted}, ctx) if wanted else {}
        out.update({"applied": done, "failed": failed, "audit": after.get("summary"),
                    "findings": [f["message"] for f in after.get("findings", []) if f["severity"] != "info"][:20]})
        return out

    async def _deliverables(self, plan_id: str, spec: dict, s, refresh: bool = False) -> dict | None:
        """Deliverables by phase for a plan, when it names a register or tags any task; None otherwise. The
        register is cached for REGISTER_CACHE_S."""
        from ..method import method_deliverables
        cfg = spec.get("register") or {}
        if not cfg and not any(t.deliverable for t in s.plan.tasks.values()):
            return None
        register = None
        if cfg and self.apps:
            cached = await self.store.get_pref(f"plan:{plan_id}", "register_cache") or {}
            if not refresh and cached.get("cfg") == cfg and time.time() - (cached.get("data") or {}).get("read_at", 0) < REGISTER_CACHE_S:
                register = cached["data"]
            else:
                try:
                    register = await read_register(self.apps, cfg)
                except Exception as exc:
                    register = {"error": f"couldn't read the register: {str(exc)[:200]}"}
                if not register.get("error"):
                    await self.store.set_pref(f"plan:{plan_id}", "register_cache", {"cfg": cfg, "data": register})
                elif cached.get("data"):
                    register = {**cached["data"], "error": register["error"] + " (showing the last good copy)"}
        return build_deliverables(s, method_deliverables(), register)

    async def _build(self, spec: dict, plan_id: str = ""):
        """Schedule a plan, with its sub-plans pulled in (programme.py). Returns (schedule, sub-plans included).
        `plan_id` (the plan being saved) can't appear anywhere among its own sub-plans."""
        expanded, included = await expand(spec, self._spec, (plan_id,) if plan_id else ())
        for t in expanded.get("tasks") or []:
            if t.get("subplan_of") and not t.get("url"):
                pub = await self.store.get_pref(f"plan:{t['subplan_of']}", "published") or {}
                if pub.get("url"):
                    t["url"] = pub["url"]  # the master's viewer links to each sub-plan's own viewer
        return schedule(from_spec(expanded)), included

    async def _link_subplans(self, plan_id: str, spec: dict) -> None:
        """Record on each sub-plan which masters include it, so a change to it refreshes them."""
        for sub in set(subplan_ids(spec)):
            used = await self.store.get_pref(f"plan:{sub}", "used_by") or []
            if plan_id not in used:
                await self.store.set_pref(f"plan:{sub}", "used_by", used + [plan_id])

    async def _refresh_masters(self, plan_id: str, seen: set | None = None) -> list[dict]:
        """Republish every published master that includes this plan (and theirs in turn)."""
        seen = seen or {plan_id}
        out = []
        for master in await self.store.get_pref(f"plan:{plan_id}", "used_by") or []:
            if master in seen:
                continue
            seen.add(master)
            spec = await self._spec(master)
            if not spec or plan_id not in subplan_ids(spec):
                continue  # it no longer includes this plan
            entry = {"plan_id": master, "name": spec.get("name")}
            if self.apps and await self.store.get_pref(f"plan:{master}", "published"):
                try:
                    ms, _ = await self._build(spec)
                    entry["viewer"] = await self._publish(master, ms)
                    entry["finish"] = ms.finish_date_of(ms.lev_finish).isoformat()
                except Exception as exc:
                    entry["error"] = f"couldn't refresh it: {str(exc)[:300]}"
            else:
                entry["note"] = "not published: its files update the next time it is exported"
            out.append(entry)
            out += await self._refresh_masters(master, seen)
        return out

    @staticmethod
    def _edit(spec: dict, args: dict) -> tuple[dict, list[str]]:
        """Apply edit_plan's changes to a copy of the spec; returns it and notes on what else changed."""
        import copy
        spec = copy.deepcopy(spec)
        notes: list[str] = []
        for k, v in (args.get("set") or {}).items():
            if k in ("tasks", "resources"):
                raise PlanError(f"set can't replace {k}: use upsert_{k} / remove_{k}")
            if v is None:
                spec.pop(k, None)
            else:
                spec[k] = v
        tasks = list(spec.get("tasks") or [])
        gone = set(str(x) for x in args.get("remove_tasks") or [])
        if gone:
            unknown = gone - {str(t.get("id")) for t in tasks}
            if unknown:
                raise PlanError(f"remove_tasks: no task {', '.join(sorted(unknown))}")
            grew = True
            while grew:  # subtasks of a removed task go too
                more = {str(t["id"]) for t in tasks if t.get("parent") and str(t["parent"]) in gone} - gone
                gone |= more
                grew = bool(more)
            tasks = [t for t in tasks if str(t.get("id")) not in gone]
            for t in tasks:
                deps = t.get("depends_on") or []
                kept = [d for d in deps if str(d if isinstance(d, str) else d.get("id")).split("/")[0] not in gone]
                if len(kept) != len(deps):
                    t["depends_on"] = kept
                    notes.append(f"{t['id']}: dropped its link(s) to removed tasks")
            notes.insert(0, f"removed {len(gone)} task(s)")
        index = {str(t.get("id")): i for i, t in enumerate(tasks)}
        for up in args.get("upsert_tasks") or []:
            tid = str(up.get("id") or "").strip()
            if not tid:
                raise PlanError("every task in upsert_tasks needs an id")
            fields = {k: v for k, v in up.items() if k != "after"}
            if tid in index:
                t = tasks[index[tid]]
                for k, v in fields.items():
                    if v is None:
                        t.pop(k, None)
                    else:
                        t[k] = v
            else:
                after = str(up.get("after") or "")
                if after and after not in index:
                    raise PlanError(f"{tid}: after {after!r} isn't a task in the plan")
                pos = index[after] + 1 if after else len(tasks)
                tasks.insert(pos, fields)
                notes.append(f"added {tid}")
            index = {str(t.get("id")): i for i, t in enumerate(tasks)}
        spec["tasks"] = tasks
        resources = list(spec.get("resources") or [])
        drop = set(str(x) for x in args.get("remove_resources") or [])
        if drop:
            resources = [r for r in resources if str(r.get("id")) not in drop]
            for t in tasks:
                rs = t.get("resources") or []
                kept = [r for r in rs if str(r if isinstance(r, str) else r.get("id")) not in drop]
                if len(kept) != len(rs):
                    t["resources"] = kept
                    notes.append(f"{t['id']}: unassigned {', '.join(sorted(drop))}")
        rindex = {str(r.get("id")): i for i, r in enumerate(resources)}
        for up in args.get("upsert_resources") or []:
            rid = str(up.get("id") or "").strip()
            if not rid:
                raise PlanError("every resource in upsert_resources needs an id")
            if rid in rindex:
                resources[rindex[rid]].update({k: v for k, v in up.items() if v is not None})
            else:
                resources.append(up)
                rindex[rid] = len(resources) - 1
        spec["resources"] = resources
        if args.get("non_working") is not None:
            spec["non_working"] = args["non_working"]
        return spec, notes

    def _branch(self, spec: dict, under: str) -> list[dict]:
        tasks = spec.get("tasks") or []
        keep = {under}
        grew = True
        while grew:
            more = {str(t["id"]) for t in tasks if t.get("parent") and str(t["parent"]) in keep} - keep
            keep |= more
            grew = bool(more)
        return [t for t in tasks if str(t.get("id")) in keep]

    async def _list(self, query: str = "") -> dict:
        rows = []
        for who, value in await self.store.prefs_with_key("spec"):
            if not who.startswith("plan:") or not isinstance(value, dict) or not value.get("spec"):
                continue
            pid, spec = who.removeprefix("plan:"), value["spec"]
            name = str(spec.get("name") or "")
            if query and query.lower() not in name.lower():
                continue
            pub = await self.store.get_pref(who, "published") or {}
            used = [m for m in await self.store.get_pref(who, "used_by") or []
                    if pid in subplan_ids(await self._spec(m) or {})]
            rows.append({"plan_id": pid, "name": name, "start": spec.get("start"),
                         "tasks": len(spec.get("tasks") or []), "subplans": subplan_ids(spec),
                         "included_in": used, **({"viewer": pub["url"]} if pub.get("url") else {})})
        rows.sort(key=lambda r: r["name"].lower())
        return {"plans": rows[:200], "count": len(rows)}

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        conv = getattr(ctx, "conversation_id", None) or ""
        if getattr(ctx, "restricted", False):
            return json.dumps({"error": "not available"})
        try:
            if name == "make_plan":
                spec = args.get("plan") or {}
                if isinstance(spec, str):
                    spec = json.loads(spec)
                plan_id = (args.get("plan_id") or "").strip() or uuid.uuid4().hex[:10]
                if plan_id in subplan_ids(spec):
                    raise PlanError("a plan can't include itself as a sub-plan")
                s, included = await self._build(spec, plan_id)
                await self.store.set_pref(f"plan:{plan_id}", "spec", {"spec": spec, "conv": conv,
                                                                     "actor": getattr(ctx, "actor", None)})
                await self._link_subplans(plan_id, spec)
                out = {"plan_id": plan_id, "name": s.plan.name, **summarise(s)}
                if included:
                    out["subplans"] = included
                if self.files and ctx is not None:
                    try:
                        out["msproject_xml"] = await self._xml(plan_id, s, ctx, conv, (args.get("site") or "").strip(),
                                                               (args.get("folder_path") or "").strip(),
                                                               args.get("replace_edited") is True)
                    except Exception as exc:  # the plan is saved; say the XML wasn't
                        out["msproject_xml"] = {"error": f"the MS Project XML wasn't saved: {str(exc)[:300]}"}
                if self.apps and await self.store.get_pref(f"plan:{plan_id}", "published"):
                    try:
                        out["published"] = await self._publish(plan_id, s)
                    except Exception as exc:  # the plan is saved; say the viewer is stale
                        out["publish_error"] = f"the published viewer wasn't refreshed: {str(exc)[:300]}"
                masters = await self._refresh_masters(plan_id)
                if masters:
                    out["masters"] = masters  # the programme plans that include this one
                return json.dumps(out)
            if name == "edit_plan":
                saved = await self._spec(args.get("plan_id", ""))
                if not saved:
                    return json.dumps({"error": "no such plan: list_plans shows the saved plans"})
                spec, notes = self._edit(saved, args)
                result = json.loads(await self.call_tool("make_plan", {
                    "plan": spec, "plan_id": args["plan_id"],
                    **{k: args[k] for k in ("site", "folder_path") if args.get(k)},
                    **({"replace_edited": True} if args.get("replace_edited") is True else {})}, ctx))
                if notes and "error" not in result:
                    result["changes"] = notes[:40]
                return json.dumps(result)
            if name == "get_plan":
                spec = await self._spec(args.get("plan_id", ""))
                if not spec:
                    return json.dumps({"error": "no such plan: list_plans shows the saved plans"})
                under = (args.get("under") or "").strip()
                out = {"plan_id": args["plan_id"], "spec": spec}
                if under:
                    branch = self._branch(spec, under)
                    if not branch:
                        return json.dumps({"error": f"no task {under!r} in that plan"})
                    out["spec"] = {k: v for k, v in spec.items() if k not in ("tasks",)} | {"tasks": branch}
                    out["branch_of"] = under
                out["subplans"] = subplan_ids(spec)
                out["included_in"] = [m for m in await self.store.get_pref(f"plan:{args['plan_id']}", "used_by") or []
                                      if args["plan_id"] in subplan_ids(await self._spec(m) or {})]
                text = json.dumps(out)
                if len(text) > 60000:
                    return json.dumps({"error": "too big to return whole: pass under=<a summary task id> to read "
                                                "one branch at a time",
                                       "top_level": [{"id": t.get("id"), "name": t.get("name")}
                                                     for t in spec.get("tasks") or [] if not t.get("parent")]})
                return text
            if name == "list_plans":
                return json.dumps(await self._list(str(args.get("query") or "")))[:60000]
            if name in ("plan_deliverables", "plan_map_deliverables", "plan_gate_pack"):
                spec = await self._spec(args.get("plan_id", ""))
                if not spec:
                    return json.dumps({"error": "no such plan: list_plans shows the saved plans"})
                s, _ = await self._build(spec)
                if name == "plan_map_deliverables":
                    from ..method import method_deliverables
                    props = [p for p in propose_tags(s, method_deliverables()) if p["candidates"]]
                    return json.dumps({"plan_id": args["plan_id"], "proposals": props,
                                       "next": "confirm with the person, then edit_plan upsert_tasks with deliverable"})[:60000]
                d = await self._deliverables(args["plan_id"], spec, s, refresh=args.get("refresh") is True)
                if not d:
                    return json.dumps({"error": "this plan has no register (spec.register) and no task tagged with a "
                                                "deliverable: set register with edit_plan, or tag tasks first"})
                if name == "plan_deliverables":
                    if args.get("stage") is not None:
                        d["stages"] = [x for x in d["stages"] if x["stage"] == int(args["stage"])]
                    return json.dumps(d)[:60000]
                gate = str(args.get("gate") or "").upper()
                stg = next((x for x in d["stages"] if x["gate"] == gate), None)
                if not stg:
                    return json.dumps({"error": f"no stage ending at {gate} in this plan"})
                if not self.files:
                    return json.dumps({"error": "files aren't available here"})
                from ..sheets import xlsx_from_content
                rows = [[r["id"], r["name"], r["producer"], r["approver"],
                         "; ".join(f"{doc['number']} Rev {doc['rev']}".strip() for doc in r["documents"]),
                         r["status"] + (" (late)" if r["late"] else " (at risk)" if r["at_risk"] else ""),
                         r["due"] or "", "; ".join(t["name"] for t in r["tasks"]),
                         "; ".join(doc["url"] for doc in r["documents"] if doc.get("url"))] for r in stg["rows"]]
                spec_x = {"title": f"{s.plan.name}: {gate} evidence", "sheets": [{
                    "name": f"{gate} evidence", "title": f"{gate} {stg['gate_name'] or ''} · {stg['approved']} of "
                                                          f"{stg['total']} approved",
                    "columns": ["Method ID", "Deliverable", "Producer", "Approver", "Documents", "Status", "Due",
                                "Tasks", "Folder"], "rows": rows,
                    "notes": [f"Register: {d['register'].get('file', 'not read')}", "Method: " + str((d.get('method') or {}).get('cite_as', ''))]}]}
                blob = xlsx_from_content(json.dumps(spec_x))
                fname = _slug(f"{s.plan.name} {gate} evidence") + ".xlsx"
                return json.dumps({"gate": gate, "approved": stg["approved"], "total": stg["total"],
                                   "file": {"file": await self.files._put_outbox(conv, fname, blob), "name": fname},
                                   "missing": [f"{r['id']} {r['name']} ({r['status']})" for r in stg["rows"] if not r["done"]][:40]})
            if name == "plan_overview":
                spec = await self._spec(args.get("plan_id", ""))
                if not spec:
                    return json.dumps({"error": "no such plan: list_plans shows the saved plans"})
                if not self.files:
                    return json.dumps({"error": "files aren't available here"})
                s, _ = await self._build(spec)
                data = overview_data(s, int(args.get("level") or 1))
                files = {}
                for fmt in args.get("formats") or ["pptx", "png"]:
                    if fmt not in OVERVIEW_FORMATS:
                        return json.dumps({"error": f"unknown format {fmt!r}: use {', '.join(OVERVIEW_FORMATS)}"})
                    blob = {"pptx": overview_pptx, "png": overview_png, "svg": overview_svg}[fmt](data)
                    filename = _slug(s.plan.name) + OVERVIEW_FORMATS[fmt]
                    files[fmt] = {"file": await self.files._put_outbox(conv, filename, blob), "name": filename,
                                  "size": len(blob)}
                return json.dumps({"plan_id": args["plan_id"], "files": files,
                                   "overview": {k: data[k] for k in ("name", "start", "finish", "duration")},
                                   "workstreams": [{k: w[k] for k in ("name", "start", "finish", "span", "critical")}
                                                   for w in data["workstreams"]],
                                   "milestones": [{k: m[k] for k in ("name", "date", "workstream", "late")}
                                                  for m in data["milestones"]],
                                   "next": "preview_file to check it; send_file / email_file / "
                                           "save_file_to_sharepoint; edit_deck to add it to another deck"})
            if name == "publish_plan":
                spec = await self._spec(args.get("plan_id", ""))
                if not spec:
                    return json.dumps({"error": "no such plan: build it with make_plan first"})
                if not self.apps:
                    return json.dumps({"error": "publishing isn't available here"})
                s, _ = await self._build(spec)
                try:
                    url = await self._publish(args["plan_id"], s)
                except Exception as exc:
                    return json.dumps({"error": f"publishing failed: {str(exc)[:500]}"})
                out = {"plan_id": args["plan_id"], "url": url, "download": url + "plan.xml",
                       "note": f"{org().name} sign-in required; live in about 30 seconds"}
                if args.get("site"):
                    if not self.files:
                        out["sharepoint"] = {"error": "SharePoint isn't available here"}
                    else:
                        filename = f"{_slug(s.plan.name)} - plan viewer.url"
                        fid = await self.files._put_outbox(conv, filename, link_file(url))
                        saved = await self.files.call_tool("save_file_to_sharepoint", {
                            "file": fid, "site": args["site"], "folder_path": args.get("folder_path", ""),
                            "filename": filename}, ctx)
                        out["sharepoint"] = json.loads(saved)
                        if not (await self.store.get_pref(f"plan:{args['plan_id']}", "xml") or {}).get("site"):
                            out["msproject_xml"] = await self._xml(args["plan_id"], s, ctx, conv, args["site"],
                                                                   args.get("folder_path", ""))
                return json.dumps(out)
            if name == "export_plan":
                spec = await self._spec(args.get("plan_id", ""))
                if not spec:
                    return json.dumps({"error": "no such plan: build it with make_plan first"})
                if not self.files:
                    return json.dumps({"error": "files aren't available here"})
                s, _ = await self._build(spec)
                levelled = args.get("levelled", True) is not False
                out = {}
                for fmt in args.get("formats") or ["msproject_xml", "html"]:
                    if fmt not in FORMATS:
                        return json.dumps({"error": f"unknown format {fmt!r}: use {', '.join(FORMATS)}"})
                    data = {"msproject_xml": lambda: export_xml(s, levelled), "html": lambda: html_viewer(s),
                            "mermaid": lambda: mermaid(s, levelled).encode(),
                            "mermaid_html": lambda: mermaid_page(s, levelled)}[fmt]()
                    filename = _slug(s.plan.name) + FORMATS[fmt]
                    out[fmt] = {"file": await self.files._put_outbox(conv, filename, data), "name": filename,
                                "size": len(data)}
                return json.dumps({"plan_id": args["plan_id"], "files": out,
                                   "next": "send_file / attach_to_jira / email_file / save_file_to_sharepoint"})
            if name == "plan_to_jira":
                return json.dumps(await self._jira(args, ctx))[:60000]
            if name == "plan_to_confluence":
                return json.dumps(await self._confluence(args, ctx, conv))
            if name == "import_project_xml":
                if not self.files:
                    return json.dumps({"error": "files aren't available here"})
                data, filename, _ = await self.files.resolve(args.get("file", ""), conv)
                spec = import_xml(data)
                s, _ = await self._build(spec)
                plan_id = uuid.uuid4().hex[:10]
                await self.store.set_pref(f"plan:{plan_id}", "spec", {"spec": spec, "conv": conv, "source": filename})
                return json.dumps({"plan_id": plan_id, "name": s.plan.name, "spec": spec, **summarise(s)})[:60000]
        except (PlanError, ValueError) as exc:
            return json.dumps({"error": str(exc)})
        raise ValueError(f"unknown tool {name}")
