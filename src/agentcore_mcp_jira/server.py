"""`jira` MCP server: Jira Cloud as one agent's own Atlassian service account.

One deployment per agent identity; e.g. Ada's runs as the "Ada (AI)" service account.
Tools return browse links, never bare keys (post-to-jira skill section 4), and accept
bodies as markdown-ish text (converted to ADF) or as an ADF document.

Moving an issue into a Done-category status is a separate tool, `resolve_issue`, so the
agent's allow-list can tag it `confirm` while ordinary transitions stay free.

Environment: JIRA_CLIENT_ID, JIRA_CLIENT_SECRET, JIRA_CLOUD_ID, JIRA_SITE_URL,
MCP_SERVER_TOKEN (bearer required from callers), PORT (default 8080).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from typing import Any

import re

from agentcore.org import org

from .adf import to_adf, to_text
from .client import JiraClient, JiraError
from . import timeline
from .fields import (EPIC_LINK_NAME, SPRINT_NAME, STORY_POINT_ALIASES, FieldCatalog, coerce, is_empty, is_noisy,
                     simple)

log = logging.getLogger(__name__)

SUMMARY_FIELDS = ["summary", "status", "assignee", "reporter", "priority", "duedate", "updated", "labels", "issuetype", "parent"]
DETAIL_FIELDS = ["created", "resolution", "resolutiondate", "components", "fixVersions", "versions", "environment",
                 "timetracking"]
SAFE_PATH = re.compile(r"^/(?!/)[A-Za-z0-9_\-./~%:=,@+]*$")
MAX_GET_CHARS = 60_000


def _iso(value: str) -> str:
    """YYYY-MM-DD (Jira date fields take no time or zone)."""
    from datetime import date as _date
    try:
        return _date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError:
        raise JiraError(f"{value!r} isn't a date: use YYYY-MM-DD") from None


def _body(value: str | dict | None) -> dict | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    stripped = value.strip()
    if stripped.startswith("{"):
        try:
            doc = json.loads(stripped)
            if doc.get("type") == "doc":
                return doc
        except ValueError:
            pass
    return to_adf(value)


class JiraTools:
    """Plain async methods; `build_server` registers them as MCP tools."""

    def __init__(self, jira: JiraClient):
        self.jira = jira
        self.catalog = FieldCatalog(jira)

    async def _core_ids(self) -> dict[str, list[str]]:
        return {"story_points": await self.catalog.story_point_ids(),
                "sprint": await self.catalog.ids_named(SPRINT_NAME),
                "epic": await self.catalog.ids_named(EPIC_LINK_NAME)}

    @staticmethod
    def _agile(f: dict, core: dict[str, list[str]]) -> dict:
        """Story points, current sprint and epic, whichever field ids this site uses."""
        points = next((f[i] for i in core["story_points"] if f.get(i) is not None), None)
        sprints = next((f[i] for i in core["sprint"] if f.get(i)), None) or []
        current = next((s for s in sprints if isinstance(s, dict) and s.get("state") == "active"), None)
        current = current or (sprints[-1] if sprints and isinstance(sprints[-1], dict) else None)
        epic = next((f[i] for i in core["epic"] if f.get(i)), None)
        parent = f.get("parent") or {}
        if not epic and ((parent.get("fields") or {}).get("issuetype") or {}).get("name") == "Epic":
            epic = parent.get("key")
        return {"story_points": points, "sprint": (current or {}).get("name"), "epic": epic}

    def _brief(self, issue: dict, core: dict[str, list[str]] | None = None) -> dict:
        f = issue.get("fields", {})
        agile = self._agile(f, core) if core is not None else {}
        return {
            "key": issue["key"], "url": self.jira.browse_url(issue["key"]),
            "summary": f.get("summary"), "status": (f.get("status") or {}).get("name"),
            "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
            "type": (f.get("issuetype") or {}).get("name"),
            "assignee": (f.get("assignee") or {}).get("displayName"),
            "priority": (f.get("priority") or {}).get("name"), "due": f.get("duedate"),
            "updated": f.get("updated"), "labels": f.get("labels", []),
            "parent": (f.get("parent") or {}).get("key"),
            **agile,
        }

    async def search_issues(self, jql: str, limit: int = 20, fields: list[str] | None = None) -> dict:
        core = await self._core_ids()
        extra = [await self.catalog.resolve(n) for n in fields or []]
        wanted = SUMMARY_FIELDS + [i for ids in core.values() for i in ids] + [x["id"] for x in extra]
        issues = await self.jira.search(jql, list(dict.fromkeys(wanted)), max(1, min(limit, 100)))
        out = []
        for issue in issues:
            brief = self._brief(issue, core)
            if extra:
                f = issue.get("fields", {})
                brief["fields"] = {x["name"]: simple(f.get(x["id"])) for x in extra}
            out.append(brief)
        pointed = [b["story_points"] for b in out if isinstance(b.get("story_points"), (int, float))]
        result = {"count": len(out), "issues": out,
                  "totals": {"story_points": float(sum(pointed)), "issues_with_points": len(pointed),
                             "issues_without_points": len(out) - len(pointed)}}
        if len(out) >= max(1, min(limit, 100)):
            result["note"] = "limit reached; there may be more issues. count_issues gives the total."
        return result

    async def count_issues(self, jql: str) -> dict:
        body = await self.jira.request("POST", "/search/approximate-count", json={"jql": jql})
        return {"jql": jql, "count": body.get("count")}

    async def get_issue(self, key: str, comments: int = 10) -> dict:
        issue = await self.jira.request("GET", f"/issue/{key}?fields=*all")
        core = await self._core_ids()
        names = {i: x.get("name", i) for i, x in (await self.catalog.by_id()).items()}
        out = self._brief(issue, core)
        f = issue.get("fields", {})
        out["description"] = to_text(f.get("description")).strip()
        out["estimate_hours"] = round(f["timeoriginalestimate"] / 3600, 1) if f.get("timeoriginalestimate") else None
        sprint_values = next((f[i] for i in core["sprint"] if f.get(i)), None)
        out["sprints"] = simple(sprint_values) if sprint_values else []
        for name in DETAIL_FIELDS:
            if not is_empty(f.get(name)):
                out[name] = simple(f[name])
        cs = (f.get("comment") or {}).get("comments", [])[-comments:]
        out["comments"] = [{"author": (c.get("author") or {}).get("displayName"), "created": c.get("created"),
                            "body": to_text(c.get("body")).strip()} for c in cs]
        out["attachments"] = [_attachment(a) for a in (f.get("attachment") or [])][-MAX_LISTED_ATTACHMENTS:]
        out["links"] = [{"type": (l.get("type") or {}).get("name"),
                         "key": (l.get("outwardIssue") or l.get("inwardIssue") or {}).get("key")}
                        for l in f.get("issuelinks", [])]
        known = {i for ids in core.values() for i in ids}
        out["other_fields"] = {names.get(k, k): simple(v) for k, v in f.items()
                               if k.startswith("customfield_") and k not in known and not is_empty(v)
                               and not is_noisy(names.get(k, k))}
        return out

    async def list_fields(self, query: str = "") -> dict:
        q = query.strip().lower()
        found = [x for x in await self.catalog.all()
                 if not q or q in x.get("name", "").lower() or q == x["id"].lower()]
        return {"fields": [{"id": x["id"], "name": x.get("name"), "type": (x.get("schema") or {}).get("type"),
                            "custom": x.get("custom", False)} for x in found[:50]],
                "more": max(0, len(found) - 50)}

    async def jira_get(self, path: str, params: dict | None = None) -> Any:
        """Any read-only Jira REST v3 GET, for things the other tools don't cover."""
        if not isinstance(path, str) or ".." in path or not SAFE_PATH.match(path):
            raise JiraError("path must be a Jira REST v3 path such as /project/PRJ/versions (query in params)")
        body = await self.jira.request("GET", path, params={k: str(v) for k, v in (params or {}).items()})
        text = json.dumps(body)
        if len(text) > MAX_GET_CHARS:
            return {"truncated": True, "chars": len(text), "json": text[:MAX_GET_CHARS]}
        return body

    async def create_issue(self, project: str, summary: str, issue_type: str = "Task", description: str | dict = "",
                           labels: list[str] | None = None, assignee_account_id: str | None = None,
                           parent: str | None = None, due_date: str | None = None,
                           start_date: str | None = None) -> dict:
        fields: dict[str, Any] = {"project": {"key": project}, "summary": summary, "issuetype": {"name": issue_type}}
        if start_date:
            fields[await self._start_field()] = _iso(start_date)
        if (doc := _body(description)) is not None:
            fields["description"] = doc
        if labels:
            fields["labels"] = labels
        if assignee_account_id:
            fields["assignee"] = {"accountId": assignee_account_id}
        if parent:
            fields["parent"] = {"key": parent}
        if due_date:
            fields["duedate"] = _iso(due_date)
        created = await self.jira.request("POST", "/issue", json={"fields": fields})
        return {"key": created["key"], "url": self.jira.browse_url(created["key"])}

    async def update_issue(self, key: str, summary: str | None = None, description: str | dict | None = None,
                           labels_add: list[str] | None = None, labels_remove: list[str] | None = None,
                           due_date: str | None = None, assignee_account_id: str | None = None,
                           fields: dict[str, Any] | None = None, start_date: str | None = None,
                           parent: str | None = None, fix_versions_add: list[str] | None = None,
                           fix_versions_remove: list[str] | None = None) -> dict:
        extra, fields = fields or {}, {}
        update: dict[str, list] = {}
        for name, value in extra.items():
            if name.strip().lower() in STORY_POINT_ALIASES:
                target = (await self.catalog.by_id())[await self.catalog.story_point_field_for(key)]
            else:
                target = await self.catalog.resolve(name)
            fields[target["id"]] = coerce(target, value)
        if summary:
            fields["summary"] = summary
        if (doc := _body(description)) is not None:
            fields["description"] = doc
        if due_date:
            fields["duedate"] = None if due_date == "none" else _iso(due_date)
        if start_date:
            fields[await self._start_field()] = None if start_date == "none" else _iso(start_date)
        if parent:
            fields["parent"] = None if parent == "none" else {"key": parent}
        versions = [{"add": {"name": v}} for v in fix_versions_add or []] + [{"remove": {"name": v}} for v in fix_versions_remove or []]
        if versions:
            update["fixVersions"] = versions
        if assignee_account_id is not None:
            fields["assignee"] = {"accountId": assignee_account_id or None}
        ops = [{"add": l} for l in labels_add or []] + [{"remove": l} for l in labels_remove or []]
        if ops:
            update["labels"] = ops
        if not fields and not update:
            return {"key": key, "url": self.jira.browse_url(key), "changed": False}
        await self.jira.request("PUT", f"/issue/{key}", json={"fields": fields, "update": update})
        return {"key": key, "url": self.jira.browse_url(key), "changed": True}

    async def _start_field(self) -> str:
        fid = await self.catalog.start_date_id()
        if not fid:
            raise JiraError("this Jira site has no Start date field, so timeline bars can only use due dates")
        return fid

    async def link_issues(self, blocker: str, blocked: str, link_type: str = timeline.BLOCKS) -> dict:
        """`blocker` blocks `blocked` (the timeline's dependency arrow). Read back and fixed if Jira stored it
        reversed; an existing identical link is left alone."""
        if blocker == blocked:
            raise JiraError("an issue can't block itself")
        current = await self.jira.request("GET", f"/issue/{blocker}?fields=issuelinks")

        def outward_to(issue, key):
            return [l for l in (issue.get("fields") or {}).get("issuelinks") or []
                    if (l.get("type") or {}).get("name") == link_type and (l.get("outwardIssue") or {}).get("key") == key]

        def inward_from(issue, key):
            return [l for l in (issue.get("fields") or {}).get("issuelinks") or []
                    if (l.get("type") or {}).get("name") == link_type and (l.get("inwardIssue") or {}).get("key") == key]
        if outward_to(current, blocked):
            return {"blocker": blocker, "blocked": blocked, "type": link_type, "created": False, "note": "already linked"}
        if inward_from(current, blocked):
            raise JiraError(f"{blocked} already {link_type.lower()}s {blocker} (the opposite way); that would make a loop."
                            " Remove one of them in Jira if the order changed")
        for attempt in ("inward-is-blocker", "outward-is-blocker"):
            body = ({"inwardIssue": {"key": blocker}, "outwardIssue": {"key": blocked}} if attempt == "inward-is-blocker"
                    else {"inwardIssue": {"key": blocked}, "outwardIssue": {"key": blocker}})
            await self.jira.request("POST", "/issueLink", json={"type": {"name": link_type}, **body})
            after = await self.jira.request("GET", f"/issue/{blocker}?fields=issuelinks")
            if outward_to(after, blocked):
                return {"blocker": blocker, "blocked": blocked, "type": link_type, "created": True,
                        "check": f"read back: {blocker} {link_type.lower()} {blocked}"}
            for wrong in inward_from(after, blocked):  # stored reversed: remove it and try the other way
                await self.jira.request("DELETE", f"/issueLink/{wrong['id']}")
        raise JiraError(f"couldn't make {blocker} {link_type.lower()} {blocked}: the link didn't read back either way")

    async def rank_issues(self, keys: list[str], after: str | None = None, before: str | None = None) -> dict:
        """Put `keys` in this order in the backlog and timeline, after (or before) an anchor issue; with no anchor
        the first key stays put and the rest follow it."""
        keys = [k for k in dict.fromkeys(keys or []) if k]
        if not keys or (after and before):
            raise JiraError("give the keys in the order wanted, and at most one of after / before")
        order = list(keys)
        if not after and not before:  # the first key is the anchor; the rest follow it
            after, keys = keys[0], keys[1:]
        for n, i in enumerate(range(0, len(keys), 50)):
            chunk = keys[i:i + 50]
            where = {"rankBeforeIssue": before} if before and n == 0 else {"rankAfterIssue": after}
            await self.jira.agile("PUT", "/issue/rank", json={"issues": chunk, **where})
            after = chunk[-1]  # later chunks follow the previous one, so the whole list stays in order
        return {"ranked": len(order), "order": order}

    async def timeline_audit(self, project: str | None = None, epic: str | None = None,
                             keys: list[str] | None = None, limit: int = 500) -> dict:
        """The timeline as Jira will draw it (rank order) and what is wrong with it."""
        if keys:
            jql = f"key in ({','.join(keys)}) ORDER BY Rank ASC"
        elif epic:
            jql = f"(key = {epic} OR parent = {epic}) ORDER BY Rank ASC"
        elif project:
            jql = f"project = {project} AND statusCategory != Done OR project = {project} AND resolved >= -30d ORDER BY Rank ASC"
        else:
            raise JiraError("give a project, an epic or keys")
        start = await self.catalog.start_date_id()
        sprint_ids = await self.catalog.ids_named(SPRINT_NAME)
        fields = ["summary", "issuetype", "status", "assignee", "duedate", "parent", "fixVersions", "issuelinks",
                  *([start] if start else []), *sprint_ids]
        raw = await self.jira.search(jql, fields, max(1, min(limit, 1000)))
        items = [timeline.item(i, start, self.jira.browse_url) for i in raw]
        in_sprint = {i["key"] for i in raw if any((i.get("fields") or {}).get(s) for s in sprint_ids)}
        for it in items:
            it["in_sprint"] = it["key"] in in_sprint
        findings = timeline.audit(items, sprint_keys=in_sprint)
        sev = {"error": 0, "warning": 0, "info": 0}
        for f in findings:
            sev[f["severity"]] += 1
        return {"jql": jql, "start_date_field": start, "count": len(items), "summary": sev,
                "findings": findings[:150], "items": items[:limit]}

    async def add_comment(self, key: str, body: str | dict, mention_account_ids: list[str] | None = None) -> dict:
        doc = _body(body)
        if mention_account_ids:  # @mentions notify the people named (reach out via Jira)
            mentions: list[dict] = []
            for acct in mention_account_ids:
                mentions += [{"type": "mention", "attrs": {"id": acct}}, {"type": "text", "text": " "}]
            body = list(doc.get("content", []))
            if body and body[0].get("type") == "paragraph":  # "@Jane Citizen Thanks, ..." on one line
                body[0] = {**body[0], "content": mentions + list(body[0].get("content", []))}
            else:
                body = [{"type": "paragraph", "content": mentions[:-1]}] + body
            doc = {**doc, "content": body}
        c = await self.jira.request("POST", f"/issue/{key}/comment", json={"body": doc})
        return {"key": key, "url": f"{self.jira.browse_url(key)}?focusedCommentId={c.get('id')}", "comment_id": c.get("id")}

    async def add_attachment(self, key: str, filename: str, content_base64: str) -> dict:
        """Attach a file to an issue. Called by the agent's own file tools (files.py), which fetch the bytes;
        the model never passes file content itself."""
        import base64
        data = base64.b64decode(content_base64)
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise ValueError(f"{filename} is {len(data) // 1_048_576} MB; Jira attachments here are limited to "
                             f"{MAX_ATTACHMENT_BYTES // 1_048_576} MB")
        out = await self.jira.request("POST", f"/issue/{key}/attachments", headers={"X-Atlassian-Token": "no-check"},
                                      files={"file": (filename, data)})
        a = out[0] if isinstance(out, list) and out else {}
        return {"key": key, "url": self.jira.browse_url(key), "attachment_id": a.get("id"), "filename": a.get("filename", filename),
                "size": a.get("size", len(data))}

    async def get_attachment(self, key: str, name: str | None = None, attachment_id: str | None = None) -> dict:
        """One of an issue's attachments, with its bytes as base64. Called by the agent's attachments tools
        (attachments.py), which convert it to text or pictures; the model never sees the bytes."""
        import base64
        issue = await self.jira.request("GET", f"/issue/{key}?fields=attachment")
        files = (issue.get("fields") or {}).get("attachment") or []
        wanted = (name or "").strip().lower()
        if attachment_id:
            match = next((a for a in files if str(a.get("id")) == str(attachment_id)), None)
        else:  # newest first: a file re-attached under the same name means the latest version
            ordered = sorted(files, key=lambda a: a.get("created") or "", reverse=True)
            match = next((a for a in ordered if not wanted or a.get("filename", "").lower() == wanted), None) or next(
                (a for a in ordered if wanted and wanted in a.get("filename", "").lower()), None)
        if not match:
            raise JiraError(f"{key} has no attachment {name or attachment_id!r}; "
                            f"it has: {[a.get('filename') for a in files] or 'none'}")
        if (match.get("size") or 0) > MAX_DOWNLOAD_BYTES:
            raise JiraError(f"{match.get('filename')} is {match['size'] // 1_048_576} MB; files over "
                            f"{MAX_DOWNLOAD_BYTES // 1_048_576} MB can't be read here")
        data = await self.jira.download(f"/attachment/content/{match['id']}")
        return {**_attachment(match), "key": key, "content_base64": base64.b64encode(data).decode()}

    async def list_transitions(self, key: str) -> dict:
        t = await self.jira.request("GET", f"/issue/{key}/transitions")
        return {"key": key, "transitions": [{"id": x["id"], "name": x["name"], "to": x["to"]["name"],
                                             "to_category": x["to"]["statusCategory"]["key"]} for x in t.get("transitions", [])]}

    async def _transition(self, key: str, to_status: str, comment: str | None, allow_done: bool) -> dict:
        options = (await self.list_transitions(key))["transitions"]
        match = next((t for t in options if to_status.lower() in (t["to"].lower(), t["name"].lower())), None)
        if not match:
            raise JiraError(f"no transition to '{to_status}' from the current status; options: {[t['to'] for t in options]}")
        if match["to_category"] == "done" and not allow_done:
            raise JiraError(f"'{match['to']}' is a Done status: use resolve_issue, which needs a confirm")
        body: dict[str, Any] = {"transition": {"id": match["id"]}}
        if comment:
            body["update"] = {"comment": [{"add": {"body": _body(comment)}}]}
        await self.jira.request("POST", f"/issue/{key}/transitions", json=body)
        return {"key": key, "url": self.jira.browse_url(key), "status": match["to"]}

    async def transition_issue(self, key: str, to_status: str, comment: str | None = None) -> dict:
        return await self._transition(key, to_status, comment, allow_done=False)

    async def resolve_issue(self, key: str, to_status: str = "Done", comment: str | None = None) -> dict:
        return await self._transition(key, to_status, comment, allow_done=True)

    async def find_user(self, query: str) -> dict:
        users = await self.jira.request("GET", "/user/search", params={"query": query, "maxResults": 10})
        return {"users": [{"account_id": u["accountId"], "name": u.get("displayName"), "type": u.get("accountType")}
                          for u in users if u.get("active")]}


MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024  # the agent's document reader limit (documents.py)
MAX_LISTED_ATTACHMENTS = 30


def _attachment(a: dict) -> dict:
    return {"id": a.get("id"), "filename": a.get("filename"), "type": a.get("mimeType"), "size": a.get("size"),
            "created": a.get("created"), "author": (a.get("author") or {}).get("displayName")}


TOOL_DOCS = {
    "add_attachment": "Attach a file (base64) to an issue. Agents use their files tools (attach_to_jira) instead.",
    "get_attachment": ("Download one of an issue's attachments (base64). Agents use their attachments tools "
                       "(read_attachment / view_attachment with issue) instead."),
    "search_issues": ("Search Jira with JQL. Each issue has key, browse url, summary, status, assignee, due, labels, "
                      "story_points, sprint and epic; totals sums the story points. `fields` adds any other fields "
                      "by name (e.g. ['Tempo Team', 'Start date']). Up to 100 issues; JQL can filter on custom "
                      "fields too, e.g. '\"Story Points\" is EMPTY' or 'sprint in openSprints()'."),
    "count_issues": "Count the issues matching a JQL query (no 100 limit).",
    "get_issue": ("Get one issue with description, story points, sprint(s), epic, recent comments, attachments "
                  "(files on the ticket, including ones pasted into comments: '[attachment: name]'), links and every "
                  "other non-empty field by name (other_fields), as plain text."),
    "list_fields": "Find Jira fields by name (e.g. 'point', 'sprint', 'team') to get their ids and types.",
    "jira_get": ("Read-only escape hatch: GET any Jira REST v3 path, e.g. /project/PRJ/versions, "
                 "/issue/PRJ-328/changelog, /project/PRJ/components. Query string goes in params."),
    "create_issue": "Create an issue. description accepts markdown (checklists '- [ ]' become Jira task lists) or an ADF doc.",
    "update_issue": ("Edit summary, description, labels, start_date and due_date (YYYY-MM-DD, or 'none' to clear; "
                     "the timeline draws these two), parent (epic key), fix_versions_add/remove (release names), assignee, and any other field by name through "
                     "`fields`, e.g. {'Story Points': 3} (sets whichever story point field the issue uses), "
                     "{'Sprint': <sprint id>}, {'Start date': '2026-10-06'}. null clears a field."),
    "add_comment": "Add a comment. body accepts markdown or an ADF doc. mention_account_ids (from find_user) @mentions and notifies people. Link tickets and documents, never bare keys or paths.",
    "list_transitions": "List the statuses an issue can move to next.",
    "link_issues": ("Make `blocker` block `blocked` (the Jira timeline's dependency arrow; only Blocks links are drawn). "
                    "Checked by reading it back and fixed if Jira stored it reversed; an existing link is left alone."),
    "rank_issues": ("Order issues in the backlog and timeline: keys in the order wanted, after (or before) an anchor "
                    "issue; with no anchor the first key stays and the rest follow. Epics rank among epics, children "
                    "within their epic."),
    "timeline_audit": ("Check a project's (or one epic's, or some keys') Jira timeline as it will be drawn, in rank order: "
                       "missing or one-sided dates, children outside their epic, red dependency arrows (blocked starts "
                       "before its blocker is due), reversed dates, overdue, orphans, no owner, rank out of date order, "
                       "subtasks (never shown). Returns findings and every item with start, due, parent, links."),
    "transition_issue": "Move an issue to a non-Done status (e.g. In Progress, In review). Refuses Done statuses.",
    "resolve_issue": "Move an issue to a Done-category status. Destructive: needs a human confirm.",
    "find_user": "Find Jira users by name or email to get an account id.",
}


def build_server(tools: JiraTools):
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name="jira", instructions=f"Jira Cloud for {org().name}, acting as the agent's own service account.")
    for name, doc in TOOL_DOCS.items():
        server.tool(name=name, description=doc)(getattr(tools, name))
    return server


def create_app():
    """Starlette app: MCP at /mcp behind a bearer check, plus /healthz."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Mount, Route

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    logging.getLogger("httpx").setLevel(logging.WARNING)
    jira = JiraClient(os.environ["JIRA_CLIENT_ID"], os.environ["JIRA_CLIENT_SECRET"],
                      os.environ["JIRA_CLOUD_ID"], os.environ["JIRA_SITE_URL"])
    mcp_app = build_server(JiraTools(jira)).streamable_http_app(streamable_http_path="/", stateless_http=True,
                                                                host="0.0.0.0")
    expected = os.environ["MCP_SERVER_TOKEN"]

    class BearerGuard:
        def __init__(self, app):
            self.app = app

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                auth = dict(scope.get("headers", [])).get(b"authorization", b"").decode()
                if not hmac.compare_digest(auth.removeprefix("Bearer ").strip(), expected):
                    await JSONResponse({"error": "unauthorised"}, status_code=401)(scope, receive, send)
                    return
            await self.app(scope, receive, send)

    async def healthz(request):
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/healthz", healthz), Mount("/mcp", app=BearerGuard(mcp_app))],
                     lifespan=getattr(mcp_app.router, "lifespan_context", None))


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
