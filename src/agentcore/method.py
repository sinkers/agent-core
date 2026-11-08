"""The organisation's method (its own repository) as tools: read-only, from the snapshot
`scripts/method-sync.py` bakes into the image at METHOD_DIR (/app/method).

Versions follow the method repository's release rules: an approved release is a vX.Y.Z tag, the latest approved
release is the official method, and `draft` is `main` (the preview site, work in progress, never official).

  method_versions()                          which versions are here, which one is official, what each changed
  method_search(query, version?, limit?)     pages matching the words (deliverable, gate, role, stage ids rank first)
  method_page(path | id, version?)           one page as text: a deliverable (D1.03), gate (G2), role (R-05),
                                             stage (stage 3), workstream (FIN), or a site path
  method_template(deliverable, version?, file?)
                                             a deliverable's template folder; with file, that file in the outbox
  method_changes(since?, version?)           the CHANGELOG between two versions

`version` is "official" (default; the latest approved release, or the draft while none is approved), "draft", or a
release like "v1.2.0". Every result says which version it came from and whether that version is approved.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .org import org, localise

MAX_PAGE_CHARS = 40_000
STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "for", "on", "at", "is", "what", "who", "how", "does",
        "do", "which", "with", "by", "be", "it", "method", org().name.lower()}

TOOLS = [
    {"name": "method_versions",
     "description": ("The {org} method versions available: the approved releases (vX.Y.Z), which one is official, "
                     "and the draft (main, work in progress), with dates, commits and what each changed. Call this "
                     "before quoting the method as {org}'s position."),
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "method_search",
     "description": ("Search the {org} project delivery method ({method_doc}): stages, gates, deliverables, "
                     "workstreams, roles, management and gating, companions, open items. Returns matching pages "
                     "with their site links. Ids rank first: D1.03, G2, R-05, O-12, FIN."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "version": {"type": "string", "description": "official (default), draft or vX.Y.Z"},
         "limit": {"type": "integer"}}, "required": ["query"]}},
    {"name": "method_page",
     "description": ("Read one page of the {org} method as text, with its link: give an id (deliverable D1.03, gate "
                     "G2, role R-05, stage 'stage 3', workstream FIN, open item O-12) or a site path from "
                     "method_search (e.g. governance/business-case/)."),
     "inputSchema": {"type": "object", "properties": {
         "id": {"type": "string"}, "version": {"type": "string"}}, "required": ["id"]}},
    {"name": "method_template",
     "description": ("A deliverable's template folder in the {org} method (e.g. D1.01): its README (what the "
                     "deliverable must contain) and files. With file, puts that template file in the outbox for "
                     "edit_file, send_file or save_file_to_sharepoint."),
     "inputSchema": {"type": "object", "properties": {
         "deliverable": {"type": "string"}, "file": {"type": "string"}, "version": {"type": "string"}},
         "required": ["deliverable"]}},
    {"name": "method_changes",
     "description": ("What changed in the {org} method: the CHANGELOG of a version (default the official one), or "
                     "every release after `since` (e.g. the version a project was set up against) up to `version`."),
     "inputSchema": {"type": "object", "properties": {
         "since": {"type": "string"}, "version": {"type": "string"}}}},
]
TOOLS = localise(TOOLS)

_ID = re.compile(r"\b(?:(D)(\d)\.(\d{1,2})|(G)(\d)|(R)-?(\d{1,2})|(O)-?(\d{1,2}))\b", re.I)


def _norm_id(text: str) -> str | None:
    """'D1.3' -> 'deliverables/d1-03/', 'G2' -> 'gates/g2/', 'R-5' -> 'roles/r-05/', 'O-12' -> open item."""
    m = _ID.search(text or "")
    if not m:
        return None
    if m.group(1):
        return f"deliverables/d{m.group(2)}-{int(m.group(3)):02d}/"
    if m.group(4):
        return f"gates/g{m.group(5)}/"
    if m.group(6):
        return f"roles/r-{int(m.group(7)):02d}/"
    return "open-items/"


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9][a-z0-9.\-]*", (text or "").lower()) if w not in STOP]


def site_base(root: str | None = None) -> str | None:
    """The method site to link to: the approved site once there is an approved release, else the draft site;
    None when the method isn't in this build."""
    try:
        idx = json.loads((Path(root or os.environ.get("METHOD_DIR", "/app/method")) / "index.json").read_text())
    except (OSError, ValueError):
        return None
    m = org().method
    return (m.approved_site if idx.get("official") else m.draft_site) or None


def _table_cell(text: str, label: str) -> str:
    m = re.search(rf"^\| {re.escape(label)} \| (.*?) \|$", text or "", re.M)
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", m.group(1)).strip() if m else ""


def method_deliverables(version: str | None = None, root: str | None = None) -> dict:
    """{"version": {...status...}, "items": [{id, name, stage, stage_name, gate, producer, approver, url}]} from the
    snapshot: every D<stage>.<nn> deliverable page. Empty items when the method isn't in this build."""
    tools = MethodTools(root=root)
    try:
        v = tools._resolve(version)
    except LookupError:
        return {"version": None, "items": []}
    pages = {p["path"]: p for p in tools.pages(v)}
    stage_name = {n: pages.get(f"lifecycle/stage-{n}/", {}).get("title", f"Stage {n}") for n in range(8)}
    items = []
    for path, p in pages.items():
        m = re.match(r"deliverables/d(\d)-(\d\d)/$", path)
        if not m:
            continue
        n = int(m.group(1))
        items.append({"id": f"D{n}.{m.group(2)}", "name": p["title"].split(" — ", 1)[-1], "stage": n,
                      "stage_name": stage_name[n].replace("Stage ", "St").replace(" — ", " "),
                      "gate": f"G{n}" if n < 7 else "", "producer": _table_cell(p["text"], "Producer (role)"),
                      "approver": _table_cell(p["text"], "Approver"), "url": p["url"]})
    items.sort(key=lambda d: (d["stage"], int(d["id"].split(".")[1])))
    return {"version": tools._status(v), "items": items}


class MethodTools:
    in_process = True

    def __init__(self, files=None, root: str | None = None):
        self.files = files  # FileTools: outbox for template files
        self.root = Path(root or os.environ.get("METHOD_DIR", "/app/method"))
        self._index: dict | None = None
        self._pages: dict[str, list[dict]] = {}

    async def list_tools(self) -> list[dict]:
        return TOOLS

    # ---- the snapshot

    def index(self) -> dict:
        if self._index is None:
            try:
                self._index = json.loads((self.root / "index.json").read_text())
            except (OSError, ValueError):
                self._index = {"versions": [], "official": None}
        return self._index

    def _resolve(self, version: str | None) -> dict:
        idx = self.index()
        versions = {v["id"]: v for v in idx.get("versions") or []}
        if not versions:
            raise LookupError(f"the {org().name} method isn't loaded in this build (scripts/method-sync.py wasn't run)")
        want = (version or "official").strip().lower()
        if want in ("official", "approved", "latest", "current", ""):
            vid = idx.get("official") or "draft"
        elif want in ("draft", "main", "preview", "dev", "wip"):
            vid = "draft"
        else:
            vid = want if want.startswith("v") else f"v{want}"
        if vid not in versions:
            raise LookupError(f"no method version {version!r}: available {', '.join(versions)}")
        return versions[vid]

    def _status(self, v: dict) -> dict:
        idx = self.index()
        if v["kind"] == "approved":
            official = v["id"] == idx.get("official")
            return {"version": v["id"], "approved": True, "official": official, "commit": v["sha"], "date": v["date"],
                    "cite_as": f"{org().name} method {v['id']} ({idx.get('doc_number') or org().method.doc_number})"
                               + ("" if official else f" (superseded: the official version is {idx.get('official')})")}
        return {"version": "draft", "approved": False, "official": False, "commit": v["sha"], "date": v["date"],
                "based_on": idx.get("official") or "no approved release yet",
                "cite_as": f"{org().name} method draft (main, commit {v['sha']}, {v['date']}), not approved",
                "warning": (f"DRAFT, not an approved version of the {org().name} method"
                            + ("" if idx.get("official") else ": there is no approved release yet")
                            + ". Say so whenever you rely on it.")}

    def pages(self, v: dict) -> list[dict]:
        if v["id"] not in self._pages:
            self._pages[v["id"]] = json.loads((self.root / v["id"] / "pages.json").read_text())
        return self._pages[v["id"]]

    # ---- tools

    def versions(self) -> dict:
        idx = self.index()
        return {"official": idx.get("official"), "repo": idx.get("repo"), "synced_at": idx.get("synced_at"),
                "note": ("the official method is the latest approved release" if idx.get("official") else
                         "no approved release yet: only the draft exists, and it is not an approved method"),
                "versions": [{**self._status(v), "pages": v.get("pages"), "site": v.get("site") or
                              org().method.approved_site, "changes": (v.get("changes") or "")[:3000]}
                             for v in idx.get("versions") or []]}

    def search(self, query: str, version: str | None = None, limit: int = 8) -> dict:
        v = self._resolve(version)
        pages = self.pages(v)
        terms = _words(query)
        target = _norm_id(query)
        scored = []
        for p in pages:
            title, text = p["title"].lower(), p["text"].lower()
            score = 0.0
            if target and p["path"] == target:
                score += 100
            for t in terms:
                if t in title:
                    score += 8
                n = text.count(t)
                score += min(n, 20) ** 0.5
            if terms and all(t in text for t in terms):
                score += 5
            if score > 0:
                scored.append((score, p))
        scored.sort(key=lambda x: -x[0])
        out = []
        for score, p in scored[:max(1, min(int(limit or 8), 20))]:
            low = p["text"].lower()
            hit = min([low.find(t) for t in terms if t in low] or [0])
            start = max(0, hit - 160)
            out.append({"id": p["path"], "title": p["title"], "url": p["url"],
                        "snippet": ("…" if start else "") + re.sub(r"\s+", " ", p["text"][start:start + 420]) + "…"})
        return {**self._status(v), "query": query, "results": out}

    def page(self, ident: str, version: str | None = None) -> dict:
        v = self._resolve(version)
        pages = self.pages(v)
        by_path = {p["path"]: p for p in pages}
        key = (ident or "").strip()
        path = key.strip("/") + "/" if key.strip("/") else ""
        if key.startswith("http"):
            path = re.sub(r"^https?://[^/]+/[^/]+/", "", key).split("#")[0]
        found = by_path.get(path) or by_path.get(_norm_id(key) or "\0")
        if not found:
            m = re.fullmatch(r"(?:stage[\s-]*)(\d)", key.lower())
            if m:
                found = by_path.get(f"lifecycle/stage-{m.group(1)}/")
        if not found and re.fullmatch(r"[A-Za-z]{3}", key):
            found = by_path.get(f"workstreams/{key.lower()}/")
        if not found:
            hits = self.search(key, version, limit=5)["results"]
            return {**self._status(v), "error": f"no page {ident!r}", "did_you_mean": hits}
        text = found["text"]
        return {**self._status(v), "id": found["path"], "title": found["title"], "url": found["url"],
                "text": text[:MAX_PAGE_CHARS], **({"truncated": True} if len(text) > MAX_PAGE_CHARS else {})}

    async def template(self, deliverable: str, file: str | None, version: str | None, conv: str) -> dict:
        v = self._resolve(version)
        listing = json.loads((self.root / v["id"] / "templates.json").read_text())
        m = re.search(r"d(\d)[.\-](\d{1,2})", (deliverable or "").lower())
        prefix = (f"stage-{m.group(1)}/d{m.group(1)}-{int(m.group(2)):02d}-" if m else
                  (deliverable or "").strip("/").lower())
        files = [t for t in listing if t["path"].lower().startswith(prefix)]
        if not files:
            return {**self._status(v), "error": f"no template folder for {deliverable!r}"}
        folder = files[0]["path"].rsplit("/", 1)[0] + "/"
        readme_file = next((t for t in files if t["path"].lower().endswith("readme.md")), None)
        readme = ((self.root / v["id"] / "templates" / readme_file["path"]).read_text(errors="replace")[:8000]
                  if readme_file else "")
        out = {**self._status(v), "deliverable": deliverable, "folder": f"templates/{folder}",
               "readme": readme, "files": [{"name": t["path"].rsplit("/", 1)[-1], "size": t["size"]}
                                           for t in files if t is not readme_file]}
        if not out["files"]:
            out["note"] = f"placeholder: no {org().name} template file yet, only the README of what it must contain"
        if file:
            pick = next((t for t in files if t["path"].rsplit("/", 1)[-1].lower() == file.lower()), None)
            if not pick:
                out["error"] = f"no file {file!r} in that folder"
            elif pick.get("too_big") or not self.files:
                out["error"] = "that file isn't available here"
            else:
                data = (self.root / v["id"] / "templates" / pick["path"]).read_bytes()
                name = pick["path"].rsplit("/", 1)[-1]
                out["file"] = {"file": await self.files._put_outbox(conv, name, data), "name": name,
                               "next": "edit_file to fill it in, then send_file / save_file_to_sharepoint"}
        return out

    def changes(self, since: str | None, version: str | None) -> dict:
        idx = self.index()
        upto = self._resolve(version)
        approved = [v for v in idx.get("versions") or [] if v["kind"] == "approved"]
        if not since:
            return {**self._status(upto), "changes": upto.get("changes") or "(none recorded)"}
        lo = since.strip().lower()
        lo = lo if lo.startswith("v") else f"v{lo}"
        key = lambda vid: tuple(int(x) for x in vid[1:].split("."))  # noqa: E731
        if not re.fullmatch(r"v\d+\.\d+\.\d+", lo):
            return {"error": f"since must be a release like v1.0.0, not {since!r}"}
        top = key(upto["id"]) if upto["kind"] == "approved" else (999, 0, 0)
        picked = [v for v in approved if key(lo) < key(v["id"]) <= top]
        out = [{"version": v["id"], "date": v["date"], "changes": v.get("changes")} for v in picked]
        if upto["kind"] == "draft":
            out.append({"version": "draft (unreleased)", "date": upto["date"], "changes": upto.get("changes")})
        return {**self._status(upto), "since": lo, "releases": out}

    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        conv = getattr(ctx, "conversation_id", "") or ""
        try:
            if name == "method_versions":
                return json.dumps(self.versions())
            if name == "method_search":
                return json.dumps(self.search(str(args.get("query") or ""), args.get("version"),
                                              args.get("limit") or 8))
            if name == "method_page":
                return json.dumps(self.page(str(args.get("id") or args.get("path") or ""), args.get("version")))
            if name == "method_template":
                return json.dumps(await self.template(str(args.get("deliverable") or ""), args.get("file"),
                                                      args.get("version"), conv))
            if name == "method_changes":
                return json.dumps(self.changes(args.get("since"), args.get("version")))
        except LookupError as exc:
            return json.dumps({"error": str(exc)})
        raise ValueError(f"unknown tool {name}")
