"""Spoken progress while the agent works on a live call (voice_llm.stream_turn).

The agent loop reports what it is doing as events (agent.handle's on_progress): the model's interim words, each
tool call with its arguments, and each result. Every few seconds while the caller waits, the Narrator turns the
events since the last update into one short spoken sentence: what it's doing now and anything concrete found so
far ("I've got the twelve open PRJ tickets; checking which are overdue now."). A fast model writes it; if that
is slow or fails, a template names the step instead. There is no fixed count of updates: they continue until the
answer is ready (each one also keeps ElevenLabs from timing the turn out).

Unverified phone callers get the template only: nothing from a tool result is read out to them.
"""

from __future__ import annotations

import json
import re
from .org import org

MAX_EVENTS_IN_PROMPT = 8
RESULT_CHARS = 700

# Plain words for what a tool is doing, keyed by tool name (server__tool); {arg} fills from the call's arguments.
PHRASES = {
    "jira__search_issues": "searching Jira{for_jql}",
    "jira__count_issues": "counting the Jira tickets{for_jql}",
    "jira__get_issue": "reading {key}",
    "jira__add_comment": "adding a comment on {key}",
    "jira__create_issue": "creating the ticket",
    "jira__update_issue": "updating {key}",
    "jira__transition_issue": "moving {key}",
    "jira__timeline_audit": "checking the timeline",
    "openclaw__search_docs": "searching SharePoint{for_query}",
    "openclaw__sharepoint_get": "opening the document",
    "attachments__read_attachment": "reading {name}",
    "attachments__view_attachment": "looking at {name}",
    "confluence__search_pages": "searching Confluence{for_query}",
    "confluence__get_page": "reading the Confluence page",
    "web__web_search": "searching the web{for_query}",
    "web__read_web_page": "reading a web page",
    "library__search_references": "checking the reference library{for_query}",
    "library__read_reference": "reading {doc}",
    "files__create_file": "writing {filename}",
    "files__edit_file": "editing the document",
    "plan__make_plan": "building the plan",
    "plan__export_plan": "exporting the plan",
    "plan__publish_plan": "publishing the plan",
    "plan__edit_plan": "updating the plan",
    "plan__plan_deliverables": "checking the deliverables against the register",
    "plan__plan_gate_pack": "building the gate evidence list",
    "method__method_search": f"checking the {org().name} method" + "{for_query}",
    "method__method_page": f"reading the {org().name} method",
    "method__method_template": "getting the method's template",
    "plan__get_plan": "reading the plan",
    "plan__list_plans": "checking the saved plans",
    "plan__plan_overview": "drawing the plan overview",
    "skills__load": "getting my notes on {name}",
    "teams__find_person": "looking up {name}",
    "mail__send_email": "sending the email",
    "escalation__chase_status": "checking who's been chased",
    "history__get_call_transcript": "reading the call transcript",
    "schedule__list_scheduled_tasks": "checking the scheduled tasks",
}


def _fmt(template: str, args: dict) -> str:
    query = str(args.get("query") or args.get("q") or "").strip()
    jql = str(args.get("jql") or "")
    project = re.search(r"project\s*(?:=|in)\s*\(?\s*\"?([A-Z][A-Z0-9]+)", jql)
    fields = {
        "for_query": f" for {query[:60]}" if query else "",
        "for_jql": f" in {project.group(1)}" if project else "",
        "key": str(args.get("key") or args.get("issue") or "the ticket"),
        "name": str(args.get("name") or args.get("query") or "it"),
        "doc": str(args.get("doc") or "the reference"),
        "filename": str(args.get("filename") or "the file"),
    }
    try:
        return template.format(**fields)
    except (KeyError, IndexError):
        return template


def describe(name: str, args: dict | None) -> str:
    """'searching Jira in PRJ', 'reading PRJ-479': what a tool call is doing, in plain words."""
    args = args or {}
    phrases = {**PHRASES, **org().progress_phrases}
    if name in phrases:
        return _fmt(phrases[name], args)
    tool = name.split("__", 1)[-1].replace("_", " ")
    return f"working on {tool}"


def found(name: str, content: str) -> str:
    """A few words on what a result held, when it's easy to tell (a count, a title); else ''."""
    text = (content or "").strip()
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        data = None
    if isinstance(data, dict):
        for key in ("total", "count"):
            if isinstance(data.get(key), int):
                return f"{data[key]} found"
        for key in ("issues", "results", "pages", "items", "files"):
            if isinstance(data.get(key), list):
                return f"{len(data[key])} found"
        if data.get("summary"):
            return str(data["summary"])[:80]
    if isinstance(data, list):
        return f"{len(data)} found"
    m = re.search(r"Found (\d+) relevant", text)
    return f"{m.group(1)} documents found" if m else ""


class Narrator:
    """Collects one turn's progress events and turns the new ones into a spoken update."""

    def __init__(self, request: str, verified: bool = True, meeting: bool = False):
        self.request, self.verified, self.meeting = request, verified, meeting
        self.events: list[dict] = []
        self.spoken: list[str] = []
        self._seen = 0

    async def add(self, event: dict) -> None:
        self.events.append(event)

    def _fresh(self) -> list[dict]:
        return self.events[self._seen:]

    def template_line(self) -> str:
        """No model: name the latest step (and a count it returned, if any)."""
        calls = [e for e in self.events if e.get("type") == "tool"]
        results = [e for e in self.events if e.get("type") == "result"]
        if not calls:
            return "Still working it out"
        doing = describe(calls[-1]["name"], calls[-1].get("args"))
        line = f"Now {doing}"
        if self.verified and results:
            got = found(results[-1]["name"], results[-1].get("content", ""))
            if got and results[-1]["name"] != calls[-1]["name"]:
                line = f"{got}; now {doing}"
        if self.events and self.events[-1].get("type") == "result":
            line = f"Going through what came back from {doing.split(' for ')[0]}"
        for alt in (line, f"Still {doing}", f"{doing[0].upper()}{doing[1:]} is taking a moment",
                    f"Bear with me, {doing}"):
            if alt not in self.spoken[-3:]:  # never the same words twice running
                return alt
        return f"Still {doing}"

    def prompt(self) -> str:
        """The fast model's instructions for the next update."""
        lines = []
        for e in self.events[-MAX_EVENTS_IN_PROMPT:]:
            if e.get("type") == "note" and e.get("text"):
                lines.append(f"- you noted: {e['text'][:200]}")
            elif e.get("type") == "tool":
                lines.append(f"- you started {describe(e['name'], e.get('args'))} ({e['name']})")
            elif e.get("type") == "result":
                body = (e.get("content") or "")[:RESULT_CHARS] if self.verified else "(not shown)"
                lines.append(f"- {e['name']} returned ({e.get('status')}): {body}")
        said = "\n".join(f"- {s}" for s in self.spoken[-4:]) or "- (nothing yet beyond your first acknowledgement)"
        return (
            f"You are on a live {'meeting' if self.meeting else 'call'} and still working on this request: "
            f"\"{self.request[:300]}\"\n\nWhat you've done so far, latest last:\n" + "\n".join(lines) +
            f"\n\nUpdates you've already spoken:\n{said}\n\n"
            + ("Nothing new has come back since your last update: say what you're waiting on or checking, in "
               "different words.\n" if not self._fresh() and self.spoken else "") +
            "Write the next spoken update: ONE sentence, at most 20 words, saying what you're doing right now and "
            "anything concrete you've found so far (a count, a name, a date, a ticket key said naturally). Write numbers as "
            "digits. A figure is said only with exactly what the result says it applies to; if that's unclear, name "
            "what you found instead of the number. Never say what a ticket or document contains until its result "
            "is listed above. Don't "
            "repeat an earlier update, don't give the final answer or a conclusion you haven't checked, no filler "
            "like 'still on it' or 'nearly there', and no promises about timing. Plain spoken words only. Reply "
            "with the sentence only.")

    def checked(self, line: str) -> bool:
        """A model-written update may only quote numbers that a result actually returned, and may not describe a
        ticket or document whose result hasn't come back (Haiku once read 'PRJ-349 shows...' before reading it)."""
        returned = " ".join(e.get("content", "") for e in self.events if e.get("type") == "result")
        standalone = r"(?<![A-Za-z\d-])\d[\d,.]*"  # 36, 1,200, 2.5; not the digits in AB12 or AB12-349
        nums = set(re.findall(standalone, returned))
        for n in re.findall(standalone, line):
            if n.rstrip(".,") not in {x.rstrip(".,") for x in nums}:
                return False
        pending = {str((e.get("args") or {}).get("key")) for e in self.events if e.get("type") == "tool"} - \
            {str(k) for e in self.events if e.get("type") == "result" for k in re.findall(r"[A-Z][A-Z0-9]+-\d+", e.get("content", ""))}
        for key in pending - {"None"}:
            if re.search(rf"{re.escape(key)}\W+(shows|says|has|lists|confirms)", line):
                return False
        return True

    async def next_line(self, write=None) -> str:
        """The next update. `write(prompt) -> str | None` is the fast model (agent.progress_line)."""
        prompt = self.prompt()  # before marking events seen: it says whether anything is new
        self._seen = len(self.events)
        line = None
        if write and self.verified:  # even with nothing new, it can say what he's waiting on (without repeating)
            try:
                line = await write(prompt)
            except Exception:
                line = None
        line = re.sub(r"\s+", " ", (line or "").strip().strip('"')).rstrip(".…") if line else ""
        if not line or line in self.spoken[-3:] or len(line) > 200 or not self.checked(line):
            line = self.template_line()
        self.spoken.append(line)
        return line
