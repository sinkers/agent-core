"""The sales fact register (the pre-sales agent).

Every claim the pre-sales agent makes comes from here. A fact is a statement with its value, unit and
conditions, its source (document, revision, link), an area (product line) and a status:

  draft      proposed (by a sweep or a person); never used in customer material
  internal   true and usable for the agent's own reasoning, never quoted to a customer
  approved   approved for customers by one of the area's approvers (sales.fact_approvers)
  queried    something about it is ambiguous or possibly wrong: an open query to the approver
  superseded replaced by a newer fact

Sweeps check correctness as well as change: conflicting sources, numbers without
units, conditions or source, superseded revisions, stale material, single-source vendor claims. Each becomes
a query (raise_query) to the approver, and the fact stays out of customer material until it is resolved.

In-process `facts` tools:
  find_facts(query?, area?, status?)          search the register
  propose_fact(statement, area, source, ...)   add a draft (or internal) fact; `supersedes` replaces one
  raise_query(kind, summary, sources, ...)     something ambiguous or possibly incorrect: query the approver
  list_queries(status?)                        open (or all) queries
  approve_fact(fact_id, note?)                 approvers only: approved for customers
  resolve_query(query_id, resolution, ...)     approvers only: close a query, optionally set the fact's status
  check_claims(text)                           every number and fact id in a draft, checked against approved facts
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from .org import localise, org

STATUSES = ("draft", "internal", "approved", "queried", "superseded")
QUERY_KINDS = ("conflict", "ambiguous", "unsourced", "superseded_source", "stale", "unconfirmed", "other")
FACT_ID_RE = re.compile(r"\bF-\d{4,}\b")
UNITS = (r"%|months?|mths?|weeks?|wks?|days?|years?|yrs?|hours?|hrs?|h|GW|MW|kW|GWh|MWh|kWh|kVA|MVA|kV|V|A|"
         r"km|m2|m²|sqm|m|mm|t|kg|tonnes?|GPUs?|racks?|units?|x|°C|k|m|bn|M")
# a claim number as sales material writes it: 14–18 months, 2.5 MW, A$1.2m, 99.99%, US$2.10/GPU-hour
NUMBER_RE = re.compile(r"(?<![\w.])((?:A|US|AU)?\$)?(\d+(?:[.,]\d+)*(?:\s?[–-]\s?\d+(?:[.,]\d+)*)?)"
                       r"(?:\s?(" + UNITS + r")(?![a-zA-Z]))?")

TOOLS = [
    {"name": "find_facts",
     "description": ("Search the sales fact register. Only facts with status 'approved' may appear in anything "
                     "for a customer; 'internal' facts inform your advice but are never quoted; 'draft' and "
                     "'queried' are not usable yet. Returns id, statement, value, unit, conditions, source, status, "
                     "area, verified date."),
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "area": {"type": "string"},
         "status": {"type": "string", "enum": list(STATUSES)}}}},
    {"name": "propose_fact",
     "description": ("Add a fact as a draft (or as internal-only). Give the exact source: document number and "
                     "revision, or a Jira/Confluence/SharePoint link. Use supersedes=<fact id> when a newer source "
                     "changes an existing fact. A fact with no unit, no conditions or no source is ambiguous: "
                     "raise_query instead of guessing."),
     "inputSchema": {"type": "object", "properties": {
         "statement": {"type": "string"}, "area": {"type": "string"}, "value": {"type": "string"},
         "unit": {"type": "string"}, "conditions": {"type": "string"}, "source": {"type": "string"},
         "source_rev": {"type": "string"}, "status": {"type": "string", "enum": ["draft", "internal"]},
         "supersedes": {"type": "string"}}, "required": ["statement", "area", "source"]}},
    {"name": "raise_query",
     "description": ("Record something ambiguous or possibly incorrect for the fact approver to resolve: "
                     "conflicting sources, a number without unit/conditions/source, a superseded revision still in "
                     "use, stale sales material, a single unconfirmed vendor or competitor source. Link every source. "
                     "The fact (if any) is held back from customer material until resolved. Then tell the approver "
                     "on the related Jira ticket (comment with @mention) or in Teams."),
     "inputSchema": {"type": "object", "properties": {
         "kind": {"type": "string", "enum": list(QUERY_KINDS)}, "summary": {"type": "string"},
         "sources": {"type": "array", "items": {"type": "string"}}, "fact_id": {"type": "string"},
         "area": {"type": "string"}}, "required": ["kind", "summary", "sources"]}},
    {"name": "list_queries",
     "description": "Queries on the register (default: open ones), with who must resolve them.",
     "inputSchema": {"type": "object", "properties": {"status": {"type": "string", "enum": ["open", "resolved", "all"]}}}},
    {"name": "approve_fact",
     "description": ("Approve a fact for customers. Only the area's fact approvers may: the person asking must be "
                     "one of them, otherwise this refuses and says who can."),
     "inputSchema": {"type": "object", "properties": {"fact_id": {"type": "string"}, "note": {"type": "string"}},
                     "required": ["fact_id"]}},
    {"name": "resolve_query",
     "description": ("Close a query with the approver's answer, optionally setting the fact's status (approved, "
                     "internal, superseded or draft). Approvers only."),
     "inputSchema": {"type": "object", "properties": {
         "query_id": {"type": "string"}, "resolution": {"type": "string"},
         "fact_status": {"type": "string", "enum": ["approved", "internal", "superseded", "draft"]}},
         "required": ["query_id", "resolution"]}},
    {"name": "check_claims",
     "description": ("Check a draft (answer, quote text, slide, one-pager) before it goes to anyone outside {org}: "
                     "every number and every [F-xxxx] fact id must trace to an approved fact. Returns the claims "
                     "with no approved fact behind them; fix or remove those, or raise_query."),
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
]
TOOLS = localise(TOOLS)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm_number(s: str) -> str:
    return re.sub(r"\s+", "", s.replace("–", "-").replace(",", "")).lower()


def numbers_in(text: str) -> list[str]:
    """Numbers that make a claim (with currency and unit), skipping bare years, fact ids and list markers."""
    out = []
    for m in NUMBER_RE.finditer(FACT_ID_RE.sub(" ", text or "")):
        cur, num, unit = m.group(1) or "", m.group(2), m.group(3) or ""
        if not cur and not unit and (re.fullmatch(r"(19|20)\d\d", num) or re.fullmatch(r"\d", num)):
            continue  # a year, or "1." / "step 2"
        out.append(f"{cur}{num}{' ' if unit and unit not in ('%', 'k', 'm', 'bn', 'M') else ''}{unit}".strip())
    return out


class FactTools:
    in_process = True

    def __init__(self, store, approvers: list[dict], owner: str = ""):
        """approvers: [{name, email, aad_id?, jira_account_id?, areas?: [..] (empty = every area)}]"""
        self.store = store
        self.approvers = approvers or []
        self.owner = owner

    async def list_tools(self) -> list[dict]:
        return TOOLS

    # ---- storage: one JSON document per fact / query in the agent's prefs table ----
    async def _all(self, kind: str) -> list[dict]:
        rows = await self.store.prefs_with_key(kind)
        return sorted((v for _, v in rows if isinstance(v, dict)), key=lambda d: d["id"])

    async def _get(self, kind: str, item_id: str) -> dict | None:
        return await self.store.get_pref(item_id, kind)

    async def _put(self, kind: str, item: dict) -> None:
        await self.store.set_pref(item["id"], kind, item)

    async def _next_id(self, kind: str, prefix: str) -> str:
        items = await self._all(kind)
        n = max((int(i["id"].split("-")[1]) for i in items), default=0) + 1
        return f"{prefix}-{n:04d}"

    # ---- who may approve ----
    def approvers_for(self, area: str | None) -> list[dict]:
        return [a for a in self.approvers if not a.get("areas") or (area and area.lower() in
                                                                      [x.lower() for x in a["areas"]])]

    def _is_approver(self, ctx, area: str | None) -> bool:
        who = {(getattr(ctx, "actor", "") or "").lower()}
        actor = (getattr(ctx, "actor", "") or "")
        if actor.startswith("jira:"):
            who.add(actor[5:].lower())
        if actor.startswith("mail:"):
            who.add(actor[5:].lower())
        for a in self.approvers_for(area):
            ids = {str(a.get(k, "")).lower() for k in ("aad_id", "jira_account_id", "email") if a.get(k)}
            if who & ids:
                return True
        return False

    def _names(self, area: str | None) -> str:
        return ", ".join(a.get("name") or a.get("email", "") for a in self.approvers_for(area)) or self.owner

    # ---- tools ----
    async def call_tool(self, name: str, args: dict, ctx=None) -> str:
        if getattr(ctx, "restricted", False):
            return json.dumps({"error": "not available"})
        if name == "find_facts":
            q = (args.get("query") or "").lower()
            words = [w for w in re.split(r"\W+", q) if len(w) > 2]
            facts = [f for f in await self._all("fact")
                     if (not args.get("area") or f["area"].lower() == args["area"].lower())
                     and (not args.get("status") or f["status"] == args["status"])
                     and (not words or all(w in json.dumps(f).lower() for w in words))]
            return json.dumps({"count": len(facts), "facts": facts[:60]})
        if name == "propose_fact":
            for field in ("statement", "area", "source"):
                if not (args.get(field) or "").strip():
                    return json.dumps({"error": f"{field} is required"})
            fid = await self._next_id("fact", "F")
            fact = {"id": fid, "statement": args["statement"].strip(), "area": args["area"].strip(),
                    "value": args.get("value"), "unit": args.get("unit"), "conditions": args.get("conditions"),
                    "source": args["source"].strip(), "source_rev": args.get("source_rev"),
                    "status": args.get("status") if args.get("status") in ("draft", "internal") else "draft",
                    "proposed_by": getattr(ctx, "actor_name", None) or getattr(ctx, "actor", None),
                    "created": _now(), "verified_on": None, "history": []}
            note = None
            if args.get("supersedes"):
                old = await self._get("fact", args["supersedes"])
                if not old:
                    return json.dumps({"error": f"no fact {args['supersedes']}"})
                old["status"] = "superseded"
                old["history"].append({"at": _now(), "event": f"superseded by {fid}"})
                await self._put("fact", old)
                fact["supersedes"] = old["id"]
                note = f"{old['id']} is now superseded; {fid} needs approval before customers see it"
            missing = [f for f in ("value", "unit", "conditions") if not args.get(f)]
            await self._put("fact", fact)
            out = {"fact": fact, "approvers": self._names(fact["area"])}
            if note:
                out["note"] = note
            if missing and args.get("value") is not None:
                out["warning"] = f"no {', '.join(missing)}: consider raise_query (ambiguous)"
            return json.dumps(out)
        if name == "raise_query":
            kind = args.get("kind")
            if kind not in QUERY_KINDS:
                return json.dumps({"error": f"kind must be one of {', '.join(QUERY_KINDS)}"})
            if not args.get("sources"):
                return json.dumps({"error": "link the sources (documents, tickets, pages) the query is about"})
            area = args.get("area")
            fact = await self._get("fact", args["fact_id"]) if args.get("fact_id") else None
            if args.get("fact_id") and not fact:
                return json.dumps({"error": f"no fact {args['fact_id']}"})
            if fact:
                area = area or fact["area"]
                if fact["status"] not in ("superseded",):
                    fact["history"].append({"at": _now(), "event": f"queried ({kind}), was {fact['status']}"})
                    fact["status_before_query"], fact["status"] = fact["status"], "queried"
                    await self._put("fact", fact)
            qid = await self._next_id("factquery", "Q")
            query = {"id": qid, "kind": kind, "summary": args["summary"].strip(), "sources": args["sources"],
                     "fact_id": fact["id"] if fact else None, "area": area, "status": "open", "raised": _now(),
                     "raised_by": getattr(ctx, "actor_name", None) or "sweep", "approvers": self._names(area)}
            await self._put("factquery", query)
            return json.dumps({"query": query, "next": f"tell {query['approvers']} (Jira comment with @mention on "
                                                       "the related ticket, or Teams) with the sources linked"})
        if name == "list_queries":
            status = args.get("status") or "open"
            qs = [q for q in await self._all("factquery") if status == "all" or q["status"] == status]
            return json.dumps({"count": len(qs), "queries": qs[:80]})
        if name == "approve_fact":
            fact = await self._get("fact", args.get("fact_id", ""))
            if not fact:
                return json.dumps({"error": f"no fact {args.get('fact_id')}"})
            if not self._is_approver(ctx, fact["area"]):
                return json.dumps({"error": f"only {self._names(fact['area'])} can approve {fact['area']} facts for "
                                            "customers", "refused": True})
            if fact["status"] == "queried":
                return json.dumps({"error": "this fact has an open query: resolve_query first"})
            if fact["status"] == "superseded":
                return json.dumps({"error": "this fact is superseded; approve the fact that replaced it"})
            fact["status"], fact["verified_on"] = "approved", date.today().isoformat()
            fact["approved_by"] = getattr(ctx, "actor_name", None) or getattr(ctx, "actor", None)
            fact["history"].append({"at": _now(), "event": f"approved by {fact['approved_by']}",
                                    "note": args.get("note")})
            await self._put("fact", fact)
            return json.dumps({"fact": fact})
        if name == "resolve_query":
            query = await self._get("factquery", args.get("query_id", ""))
            if not query:
                return json.dumps({"error": f"no query {args.get('query_id')}"})
            if not self._is_approver(ctx, query.get("area")):
                return json.dumps({"error": f"only {self._names(query.get('area'))} can resolve this query",
                                   "refused": True})
            query.update({"status": "resolved", "resolution": args.get("resolution"), "resolved": _now(),
                          "resolved_by": getattr(ctx, "actor_name", None) or getattr(ctx, "actor", None)})
            await self._put("factquery", query)
            fact = await self._get("fact", query["fact_id"]) if query.get("fact_id") else None
            if fact:
                new = args.get("fact_status") or fact.get("status_before_query") or "draft"
                fact["status"] = new
                if new == "approved":
                    fact["verified_on"] = date.today().isoformat()
                fact["history"].append({"at": _now(), "event": f"query {query['id']} resolved: {new}",
                                        "note": args.get("resolution")})
                await self._put("fact", fact)
            return json.dumps({"query": query, "fact": fact})
        if name == "check_claims":
            return json.dumps(await self.check(args.get("text", "")))
        raise ValueError(f"unknown tool {name}")

    async def check(self, text: str) -> dict:
        facts = await self._all("fact")
        approved = [f for f in facts if f["status"] == "approved"]
        by_id = {f["id"]: f for f in facts}
        unapproved_ids = []
        for fid in sorted(set(FACT_ID_RE.findall(text or ""))):
            f = by_id.get(fid)
            if not f or f["status"] != "approved":
                unapproved_ids.append({"fact_id": fid, "status": f["status"] if f else "unknown"})
        haystack = [_norm_number(" ".join(str(f.get(k) or "") for k in ("statement", "value", "unit")))
                    for f in approved]
        unsupported = []
        for token in numbers_in(text):
            m = re.search(r"\d+(?:[.,]\d+)*(?:\s?[–-]\s?\d+(?:[.,]\d+)*)?", token)
            core = _norm_number(m.group(0)) if m else ""
            if not any(core and core in h for h in haystack):
                unsupported.append(token)
        ok = not unapproved_ids and not unsupported
        return {"ok": ok, "unsupported_numbers": unsupported, "fact_ids_not_approved": unapproved_ids,
                "approved_facts": len(approved),
                "next": "ready for review and a salesperson's yes" if ok else
                        f"remove or source these before it leaves {org().name} (or raise_query)"}
