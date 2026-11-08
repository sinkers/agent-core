"""Deliverables by phase: what each stage must produce, joined from three sources so nothing is typed twice.

  the method        what each stage and gate needs: ID, name, producer and approver roles (method.method_deliverables)
  the plan          who produces it and when: tasks tagged `deliverable: "D2.02"` (their finish is the due date)
  the register      the documents that satisfy it: number, revision, status, folder (the project register workbook,
                    e.g. PRJ-GEN-REG-0005: Register tab, plus its Deliverables tab's method ID -> document numbers)

A plan names its register in the spec:
  "register": {"site": "3.0 Projects", "folder": "1. Project Delivery/PRJ/01 Concept Design/Concept A",
               "prefix": "PRJ-GEN-REG-0005_", "library_root": "1. Project Delivery/PRJ"}
The latest revision in that folder is read (and cached for REGISTER_CACHE_S) at publish time and on request.

  build(s, method, register, today) -> {stages: [...], gates: {G2: {...}}, findings: [...], register: {...}}
"""

from __future__ import annotations

import base64
import io
import re
import time
from datetime import date, timedelta
from urllib.parse import quote

from ..org import org

REGISTER_CACHE_S = 900
SHAREPOINT = org().sharepoint_url.rstrip("/") + "/sites/{site}/Shared%20Documents/"
# Register statuses, least to most advanced. A deliverable's status is its least advanced live document's.
ORDER = ["not yet produced", "draft", "draft for review", "in check", "in review", "issued for review",
         "in approval", "approved", "issued"]
DONE = {"approved", "issued"}
GONE = {"superseded", "cancelled"}
AT_RISK_DAYS = 14
_ITEM = re.compile(r"^- 📄 (.+?) \([\d.]+ [KMG]?B")
_REV = re.compile(r"_([A-Z]{1,2}|\d+(?:\.\d+)?)_")


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def _did(s) -> list[str]:
    from .model import _deliverables
    return _deliverables(str(s or ""))


# ---- reading the register ------------------------------------------------------------------------------------------
async def read_register(client, cfg: dict) -> dict:
    """The latest revision of the project register in cfg["folder"] (files named cfg["prefix"]…). Returns
    {file, url, revision, read_at, docs: {number: {...}}, by_deliverable: {D2.02: [numbers]}} or {"error": ...}."""
    from ..documents import _parse_markdown_result
    site, folder, prefix = cfg.get("site", "3.0 Projects"), cfg.get("folder", ""), cfg.get("prefix", "")
    listing = await client.call_tool("sharepoint_get", {"site": site, "path": folder})
    names = [m.group(1) for line in str(listing).splitlines() if (m := _ITEM.match(line.strip()))]
    names = [n for n in names if n.startswith(prefix) and n.lower().endswith(".xlsx")]
    if not names:
        return {"error": f"no register named {prefix}… in {site} › {folder}"}

    def rev_key(n):
        m = _REV.search(n[len(prefix) - 1:])
        r = m.group(1) if m else ""
        return (1, float(r)) if r.replace(".", "").isdigit() else (0, len(r), r)
    name = max(names, key=rev_key)
    raw = await client.call_tool("sharepoint_get", {"site": site, "path": f"{folder}/{name}",
                                                     "max_inline_bytes": 5_000_000})
    body = _parse_markdown_result(raw) if isinstance(raw, str) else None
    if not body or not body.get("content_base64"):
        return {"error": f"couldn't read {name}"}
    out = parse_register(base64.b64decode(body["content_base64"]), cfg)
    m = _REV.search(name[len(prefix) - 1:])
    out.update(file=name, url=body.get("webUrl") or body.get("web_url") or "", revision=m.group(1) if m else "",
               read_at=time.time())
    return out


def parse_register(data: bytes, cfg: dict | None = None) -> dict:
    """Register and Deliverables tabs of a project register workbook (header row found by its first column)."""
    from openpyxl import load_workbook
    cfg = cfg or {}
    wb = load_workbook(io.BytesIO(data), data_only=True)
    docs: dict[str, dict] = {}
    by_deliverable: dict[str, list[str]] = {}
    root = cfg.get("library_root", "")
    base = SHAREPOINT.format(site=quote(cfg.get("site", "3.0 Projects").replace(" ", ""))) if cfg.get("site") else ""

    def rows(ws, first: str):
        head = None
        for row in ws.iter_rows(values_only=True):
            cells = [_norm(c) for c in row]
            if head is None:
                if cells and cells[0].lower() == first.lower():
                    head = [c.lower() for c in cells]
                continue
            if any(cells):
                yield dict(zip(head, cells))

    def col(r, *names):
        for n in names:
            for k, v in r.items():
                if k.startswith(n):
                    return v
        return ""
    if "Register" in wb.sheetnames:
        for r in rows(wb["Register"], "Document number"):
            num = col(r, "document number")
            if not num or num.lower().startswith("(unnumbered"):
                continue
            folder = col(r, "approved folder") or col(r, "working folder")
            link = (base + quote("/".join(x.strip("/") for x in (root, folder) if x.strip("/")))) if base and folder else ""
            docs[num] = {"number": num, "title": col(r, "description"), "rev": col(r, "rev"),
                         "status": col(r, "status") or "", "stage": col(r, "stage"), "folder": folder, "url": link,
                         "approval": col(r, "approval"), "deliverable": _did(col(r, "method deliverable"))}
            for d in docs[num]["deliverable"]:
                by_deliverable.setdefault(d, []).append(num)
    if "Deliverables" in wb.sheetnames:
        for r in rows(wb["Deliverables"], "Method ID"):
            ids = _did(col(r, "method id"))
            nums = [n for n in re.split(r"[,;\s]+", " ".join((col(r, "document number"), col(r, "other document"))))
                    if re.match(r"[A-Z0-9]+-[A-Z]{2,4}-[A-Z]{2,4}-\d{3,4}$", n)]
            for d in ids:
                for n in nums:
                    if n not in by_deliverable.setdefault(d, []):
                        by_deliverable[d].append(n)
    return {"docs": docs, "by_deliverable": by_deliverable}


# ---- joining ------------------------------------------------------------------------------------------------------
def _rank(status: str) -> int:
    s = status.lower()
    for i, k in enumerate(ORDER):
        if s == k or s.startswith(k):
            return i
    return 1  # unknown words count as draft


def status_of(doc_statuses: list[str]) -> str:
    live = [s for s in doc_statuses if s and s.lower() not in GONE]
    if not live:
        return ""
    return min(live, key=_rank)


def build(s, method: dict, register: dict | None, today: date | None = None) -> dict:
    """The deliverables-by-phase table for a scheduled plan (see the module docstring)."""
    plan = s.plan
    today = today or plan.status_date or date.today()
    items = {d["id"]: d for d in (method or {}).get("items", [])}
    tagged: dict[str, list] = {}
    for tid in plan.order:
        t = plan.tasks[tid]
        for d in t.deliverable:
            tagged.setdefault(d, []).append(t)
    gates = {t.gate: t for t in plan.tasks.values() if t.gate}
    in_plan = {int(g[1:]) for g in gates if g[1:].isdigit()} | {int(d[1]) for d in tagged}
    reg = register or {}
    docs, by_d = reg.get("docs", {}), reg.get("by_deliverable", {})
    stages: dict[int, dict] = {}
    findings: list[dict] = []
    for did in sorted(set(items) | set(tagged), key=lambda x: (int(x[1]), int(x.split(".")[1]))):
        n = int(did[1])
        if n not in in_plan:
            continue
        m = items.get(did, {"id": did, "name": "(not in the method)", "stage": n, "producer": "", "approver": "",
                            "url": "", "stage_name": f"St{n}", "gate": f"G{n}"})
        tasks = tagged.get(did, [])
        due = max((s.finish_date(t) for t in tasks), default=None)
        started = any(s.start_date(t) <= today for t in tasks)
        numbers = list(dict.fromkeys(by_d.get(did, []) + [t.document for t in tasks
                                                           if t.document and re.match(r"\w+-\w+-\w+-\d+", t.document)]))
        documents = [docs.get(num, {"number": num, "status": "", "rev": "", "url": "", "title": ""}) for num in numbers]
        st = status_of([d.get("status", "") for d in documents if d.get("number") in docs])
        if not st:
            st = ("Not in register" if numbers else "In progress (no number)" if started else
                  "Not started" if tasks else "Not planned")
        done = st.lower() in DONE
        gate = gates.get(f"G{n}")
        gate_date = s.start_date(gate) if gate else None
        late = not done and ((due and due < today) or (gate_date and gate_date < today))
        risk = not done and not late and gate_date is not None and gate_date <= today + timedelta(days=AT_RISK_DAYS)
        row = {"id": did, "name": m["name"], "producer": m.get("producer", ""), "approver": m.get("approver", ""),
               "url": m.get("url", ""), "tasks": [{"id": t.id, "name": t.name} for t in tasks],
               "owners": ", ".join(dict.fromkeys(plan.resources[a.resource].name for t in tasks
                                                 for a in t.assignments)),
               "due": due.isoformat() if due else None, "status": st, "done": done, "late": bool(late),
               "at_risk": bool(risk),
               "documents": [{k: d.get(k, "") for k in ("number", "title", "rev", "status", "url")} for d in documents]}
        stage = stages.setdefault(n, {"stage": n, "name": m.get("stage_name") or f"St{n}", "gate": f"G{n}" if n < 7 else "",
                                      "gate_name": gate.name if gate else "", "gate_date": gate_date.isoformat() if gate_date else None,
                                      "rows": []})
        stage["rows"].append(row)
    gate_summary = {}
    for n, stg in sorted(stages.items()):
        total = len(stg["rows"])
        approved = sum(r["done"] for r in stg["rows"])
        missing = [r for r in stg["rows"] if not r["done"]]
        stg.update(total=total, approved=approved, numbered=sum(1 for r in stg["rows"] if r["documents"]),
                   planned=sum(1 for r in stg["rows"] if r["tasks"]))
        if stg["gate"]:
            gate_summary[stg["gate"]] = {"approved": approved, "total": total, "late": sum(r["late"] for r in stg["rows"]),
                                         "at_risk": sum(r["at_risk"] for r in stg["rows"])}
        gd = date.fromisoformat(stg["gate_date"]) if stg["gate_date"] else None
        if gd and missing and gd <= today + timedelta(days=AT_RISK_DAYS):
            names = "; ".join(f"{r['id']} {r['name']} ({r['status']})" for r in missing[:5])
            findings.append({"check": "gate evidence", "severity": "error" if gd < today else "warning",
                             "message": f"{stg['gate']} on {gd:%d %b %y}: {len(missing)} of {total} deliverables not "
                                        f"approved: {names}" + (" …" if len(missing) > 5 else "")})
        unplanned = [r["id"] for r in stg["rows"] if not r["tasks"] and not r["done"]]
        if unplanned:
            findings.append({"check": "deliverable not planned", "severity": "info",
                             "message": f"{stg['name']}: {len(unplanned)} method deliverable(s) have no task tagged "
                                        f"to produce them: {', '.join(unplanned[:12])}"
                                        + (" …" if len(unplanned) > 12 else "")})
        nonumber = [r["id"] for r in stg["rows"] if r["tasks"] and not r["documents"] and r["status"].startswith("In progress")]
        if nonumber:
            findings.append({"check": "no document number", "severity": "warning",
                             "message": f"{stg['name']}: work has started on {', '.join(nonumber)} but no document "
                                        f"number is in the register: ask {org().register_keeper} for one"})
    return {"stages": [stages[n] for n in sorted(stages)], "gates": gate_summary, "findings": findings,
            "register": {k: reg.get(k) for k in ("file", "url", "revision", "read_at", "error") if reg.get(k)},
            "method": (method or {}).get("version")}


# ---- helping the agent tag tasks ----------------------------------------------------------------------------------
_STOP = {"and", "the", "of", "for", "a", "to", "with", "on", "in", "set", "plan", "report", "register", "draft",
         "concept", "stage", "site", "project"}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _STOP and len(w) > 2}


def propose_tags(s, method: dict, limit: int = 3) -> list[dict]:
    """For each method deliverable in the plan's stages that no task produces yet, the tasks whose names match it
    best (in the stage that ends at its gate), for a person to confirm before edit_plan tags them."""
    from .health import stage_of
    plan = s.plan
    gates = {t.gate: t for t in plan.tasks.values() if t.gate}
    done = {d for t in plan.tasks.values() for d in t.deliverable}
    out = []
    for m in (method or {}).get("items", []):
        gate = gates.get(m["gate"])
        if not gate or m["id"] in done:
            continue
        home = stage_of(plan, gate.id)
        pool = plan.descendants(home) if home else [t.id for t in plan.leaves()]
        want = _tokens(m["name"])
        scored = []
        for tid in pool + ([home] if home else []):
            t = plan.tasks[tid]
            if t.milestone:  # a deliverable is produced by work, not by a gate or milestone
                continue
            have = _tokens(t.name)
            if not want or not have:
                continue
            score = len(want & have) / len(want)
            if score >= 0.5:
                scored.append({"task": tid, "name": t.name, "score": round(score, 2)})
        scored.sort(key=lambda x: -x["score"])
        out.append({"deliverable": m["id"], "name": m["name"], "candidates": scored[:limit]})
    return out
