import json

import httpx
import pytest

from agentcore_mcp_jira.adf import to_adf, to_text
from agentcore_mcp_jira.client import JiraClient, JiraError
from agentcore_mcp_jira.server import JiraTools


def test_checklist_becomes_task_list_without_paragraph():
    doc = to_adf("Deliverables\n- [ ] Draft [SLD](https://sp/sld)\n- [x] Load schedule")
    para, tasks = doc["content"]
    assert para["type"] == "paragraph"
    assert tasks["type"] == "taskList" and [t["attrs"]["state"] for t in tasks["content"]] == ["TODO", "DONE"]
    item = tasks["content"][0]
    assert item["content"][0]["type"] == "text"  # taskItem holds inline nodes, not a paragraph
    link = item["content"][1]
    assert link["text"] == "SLD" and link["marks"][0]["attrs"]["href"] == "https://sp/sld"


def test_bullets_headings_bold_and_roundtrip_text():
    doc = to_adf("## Status\n- **Blocked** on [PRJ-161](https://x/browse/PRJ-161)\n- second\n\n1. one\n2. two")
    kinds = [n["type"] for n in doc["content"]]
    assert kinds == ["heading", "bulletList", "orderedList"]
    text = to_text(doc)
    assert "- Blocked on PRJ-161 <https://x/browse/PRJ-161>" in text


def make_tools(handler):
    def route(request: httpx.Request):
        if request.url.host == "auth.atlassian.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer t"
        return handler(request)
    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    return JiraTools(JiraClient("id", "secret", "cloud", "https://site.atlassian.net", http=http))


TRANSITIONS = {"transitions": [
    {"id": "21", "name": "Start", "to": {"name": "In Progress", "statusCategory": {"key": "indeterminate"}}},
    {"id": "31", "name": "Finish", "to": {"name": "Done", "statusCategory": {"key": "done"}}},
]}


async def test_transition_refuses_done_but_resolve_allows():
    posted = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/transitions"):
            return httpx.Response(200, json=TRANSITIONS)
        posted.append(json.loads(req.content))
        return httpx.Response(204)

    tools = make_tools(handler)
    with pytest.raises(JiraError, match="resolve_issue"):
        await tools.transition_issue("WOW-1", "Done")
    ok = await tools.transition_issue("WOW-1", "in progress", comment="Starting now")
    assert ok == {"key": "WOW-1", "url": "https://site.atlassian.net/browse/WOW-1", "status": "In Progress"}
    assert posted[0]["transition"]["id"] == "21" and posted[0]["update"]["comment"][0]["add"]["body"]["type"] == "doc"
    done = await tools.resolve_issue("WOW-1")
    assert done["status"] == "Done" and posted[1]["transition"]["id"] == "31"


async def test_search_paginates_with_next_page_token_and_returns_links():
    pages = iter([
        {"issues": [{"key": "A-1", "fields": {"summary": "one", "status": {"name": "To Do"}}}], "nextPageToken": "p2"},
        {"issues": [{"key": "A-2", "fields": {"summary": "two", "status": {"name": "Done"}}}]},
    ])
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json=next(pages))

    out = await field_aware(handler).search_issues("project = A", limit=5)
    assert [i["key"] for i in out["issues"]] == ["A-1", "A-2"]
    assert out["issues"][0]["url"] == "https://site.atlassian.net/browse/A-1"
    assert "nextPageToken" not in seen[0] and seen[1]["nextPageToken"] == "p2"


async def test_create_issue_converts_markdown_description():
    captured = {}

    def handler(req):
        captured.update(json.loads(req.content))
        return httpx.Response(201, json={"key": "WOW-9"})

    out = await make_tools(handler).create_issue("WOW", "READY: test", description="- [ ] item", labels=["TODAY"])
    assert out["url"].endswith("/browse/WOW-9")
    assert captured["fields"]["description"]["content"][0]["type"] == "taskList"
    assert captured["fields"]["labels"] == ["TODAY"]


async def test_errors_surface_jira_messages():
    tools = make_tools(lambda req: httpx.Response(400, json={"errorMessages": ["The value 'NOPE' does not exist"]}))
    with pytest.raises(JiraError, match="does not exist"):
        await tools.search_issues("project = NOPE")


FIELDS = [
    {"id": "summary", "name": "Summary", "custom": False, "schema": {"type": "string"}},
    {"id": "customfield_10028", "name": "Story Points", "custom": True, "schema": {"type": "number"}},
    {"id": "customfield_10016", "name": "Story point estimate", "custom": True, "schema": {"type": "number"}},
    {"id": "customfield_10020", "name": "Sprint", "custom": True, "schema": {"type": "array", "items": "json"}},
    {"id": "customfield_10014", "name": "Epic Link", "custom": True, "schema": {"type": "any"}},
    {"id": "customfield_10046", "name": "Tempo Team", "custom": True, "schema": {"type": "option2"}},
    {"id": "customfield_10050", "name": "Discipline", "custom": True, "schema": {"type": "option"}},
    {"id": "customfield_10051", "name": "Reviewers", "custom": True, "schema": {"type": "array", "items": "user"}},
]
SPRINT = [{"id": 1, "name": "Sprint 3", "state": "closed"}, {"id": 2, "name": "PRJ-1 Sprint 2026-4", "state": "active"}]


def field_aware(handler):
    def route(req):
        if req.url.path.endswith("/field"):
            return httpx.Response(200, json=FIELDS)
        return handler(req)
    return make_tools(route)


async def test_search_includes_story_points_sprint_epic_and_totals():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"issues": [
            {"key": "PRJ-328", "fields": {"summary": "a", "customfield_10028": 2.0, "customfield_10020": SPRINT,
                                            "customfield_10014": "PRJ-322"}},
            {"key": "PRJ-329", "fields": {"summary": "b", "customfield_10016": 3}},  # team-managed style
            {"key": "PRJ-330", "fields": {"summary": "c"}},
        ]})

    out = await field_aware(handler).search_issues("project = PRJ")
    assert {"customfield_10028", "customfield_10016", "customfield_10020", "customfield_10014"} <= set(seen["fields"])
    first = out["issues"][0]
    assert first["story_points"] == 2.0 and first["sprint"] == "PRJ-1 Sprint 2026-4" and first["epic"] == "PRJ-322"
    assert out["issues"][1]["story_points"] == 3 and out["issues"][2]["story_points"] is None
    assert out["totals"] == {"story_points": 5.0, "issues_with_points": 2, "issues_without_points": 1}


async def test_search_extra_fields_by_name_and_unknown_field_suggests():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"issues": [
            {"key": "A-1", "fields": {"customfield_10046": {"value": "Electrical"},
                                      "customfield_10051": [{"displayName": "Erin Hall"}]}}]})

    tools = field_aware(handler)
    out = await tools.search_issues("project = A", fields=["tempo team", "Reviewers"])
    assert "customfield_10046" in seen["fields"]
    assert out["issues"][0]["fields"] == {"Tempo Team": "Electrical", "Reviewers": ["Erin Hall"]}
    with pytest.raises(JiraError, match="Story Points"):
        await tools.search_issues("project = A", fields=["story pints"])


async def test_get_issue_returns_other_fields_by_name():
    def handler(req):
        return httpx.Response(200, json={"key": "PRJ-328", "fields": {
            "summary": "x", "customfield_10028": 2.0, "customfield_10020": SPRINT,
            "customfield_10050": {"value": "HV"}, "customfield_10019": "0|i021y2:", "customfield_10000": "{}"}})

    out = await field_aware(handler).get_issue("PRJ-328")
    assert out["story_points"] == 2.0 and out["sprint"] == "PRJ-1 Sprint 2026-4"
    assert out["sprints"] == ["Sprint 3 (closed)", "PRJ-1 Sprint 2026-4 (active)"]
    assert out["other_fields"]["Discipline"] == "HV"
    assert "Story Points" not in out["other_fields"]  # already top level


async def test_update_issue_sets_custom_fields_by_name():
    put = {}

    def handler(req):
        if req.method == "GET":  # editmeta: company-managed project, classic Story Points field
            return httpx.Response(200, json={"fields": {"customfield_10028": {}, "customfield_10050": {}}})
        put.update(json.loads(req.content))
        return httpx.Response(204)

    tools = field_aware(handler)
    out = await tools.update_issue("PRJ-328", fields={"Story Points": "3", "Discipline": "HV"})
    assert out["changed"] and put["fields"] == {"customfield_10028": 3.0, "customfield_10050": {"value": "HV"}}
    with pytest.raises(JiraError, match="Sprint"):
        await tools.update_issue("PRJ-328", fields={"Sprint": "next"})


async def test_story_points_alias_writes_the_field_the_issue_uses():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.method == "GET":  # editmeta: this issue's screen has only the team-managed field
            return httpx.Response(200, json={"fields": {"customfield_10016": {}}})
        return httpx.Response(204)

    await field_aware(handler).update_issue("TM-1", fields={"story points": 5})
    assert calls[-1][2]["fields"] == {"customfield_10016": 5.0}


async def test_list_fields_filters_by_name():
    out = await field_aware(lambda req: httpx.Response(404)).list_fields("point")
    assert [f["name"] for f in out["fields"]] == ["Story Points", "Story point estimate"]


async def test_count_issues_uses_approximate_count():
    def handler(req):
        assert req.url.path.endswith("/search/approximate-count") and json.loads(req.content) == {"jql": "project = A"}
        return httpx.Response(200, json={"count": 412})

    assert await make_tools(handler).count_issues("project = A") == {"jql": "project = A", "count": 412}


async def test_jira_get_is_read_only_and_scoped_to_the_api():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        return httpx.Response(200, json={"values": []})

    tools = make_tools(handler)
    assert await tools.jira_get("/project/PRJ/versions", {"expand": "x"}) == {"values": []}
    assert seen[0].endswith("/rest/api/3/project/PRJ/versions?expand=x")
    for bad in ["https://evil.example/x", "/../../admin", "//evil.example/x", "project?x=1#frag\n"]:
        with pytest.raises(JiraError):
            await tools.jira_get(bad)


async def test_add_attachment_posts_multipart_with_no_check_header():
    import base64
    import httpx
    from agentcore_mcp_jira.client import JiraClient
    from agentcore_mcp_jira.server import JiraTools
    seen = {}

    def handler(req):
        if "oauth" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.update(url=str(req.url), token=req.headers.get("x-atlassian-token"), ctype=req.headers.get("content-type"),
                    body=req.content)
        return httpx.Response(200, json=[{"id": "10001", "filename": "plan.docx", "size": 3}])
    jira = JiraClient("id", "secret", "cloud", "https://x.atlassian.net", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    out = await JiraTools(jira).add_attachment("PRJ-328", "plan.docx", base64.b64encode(b"doc").decode())
    assert out["attachment_id"] == "10001" and seen["url"].endswith("/issue/PRJ-328/attachments")
    assert seen["token"] == "no-check" and seen["ctype"].startswith("multipart/form-data") and b"plan.docx" in seen["body"]


ATTACHMENTS = [
    {"id": "100", "filename": "site-plan.pdf", "mimeType": "application/pdf", "size": 11, "created": "2026-10-01T09:00:00.000+1000",
     "author": {"displayName": "Sam"}},
    {"id": "101", "filename": "site-plan.pdf", "mimeType": "application/pdf", "size": 11, "created": "2026-10-07T09:00:00.000+1000",
     "author": {"displayName": "Sam"}},
    {"id": "102", "filename": "image-20261007.png", "mimeType": "image/png", "size": 4, "created": "2026-10-07T10:00:00.000+1000"},
]


async def test_get_attachment_downloads_the_latest_file_of_that_name_without_leaking_the_bearer():
    seen = []

    def route(req: httpx.Request):
        if req.url.host == "auth.atlassian.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append((req.url.host, req.url.path, req.headers.get("authorization")))
        if req.url.path.endswith("/issue/PRJ-371"):
            return httpx.Response(200, json={"fields": {"attachment": ATTACHMENTS}})
        if "/attachment/content/" in req.url.path:
            return httpx.Response(303, headers={"location": f"https://api.media.atlassian.com/file/{req.url.path[-3:]}?token=signed"})
        if req.url.host == "api.media.atlassian.com":
            return httpx.Response(200, content=b"%PDF-" + req.url.path[-3:].encode())
        return httpx.Response(404)

    http = httpx.AsyncClient(transport=httpx.MockTransport(route))
    tools = JiraTools(JiraClient("id", "secret", "cloud", "https://site.atlassian.net", http=http))
    out = await tools.get_attachment("PRJ-371", name="SITE-PLAN.pdf")
    import base64
    assert out["id"] == "101" and base64.b64decode(out["content_base64"]) == b"%PDF-101"
    assert seen[-1] == ("api.media.atlassian.com", "/file/101", None)  # bearer dropped on the cross-host redirect
    assert (await tools.get_attachment("PRJ-371", name="image"))["filename"] == "image-20261007.png"  # partial name
    with pytest.raises(JiraError, match="has: \\['site-plan.pdf'"):
        await tools.get_attachment("PRJ-371", name="layout.dwg")


async def test_get_issue_lists_attachments_and_comments_show_pasted_files():
    comment = {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "See the markup:"}]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "uuid", "type": "file", "alt": "image-20261007.png"}}]}]}

    def handler(req):
        if req.url.path.endswith("/field"):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"key": "PRJ-371", "fields": {
            "summary": "Site plan", "attachment": ATTACHMENTS,
            "comment": {"comments": [{"author": {"displayName": "Sam"}, "body": comment}]}}})

    out = await make_tools(handler).get_issue("PRJ-371")
    assert [a["filename"] for a in out["attachments"]] == ["site-plan.pdf", "site-plan.pdf", "image-20261007.png"]
    assert "[attachment: image-20261007.png]" in out["comments"][0]["body"]


ADA_REPLY = """I've opened both screenshots and the spreadsheet.

**Governance**

| George Town role | High-level description | Zerra roles it replaces |
|---|---|---|
| Executive / Project director | Owns the business case, approves **spend** | Executive Project Director |
| Senior user | Accepts handover | Senior Operations Director, DC Operations Director |

## Not carried across
- Program Management Office Lead
- dedicated Planning Managers

> [!warning] Author and checker can't be the same person
> (PRC-0001 section 6).

> A quoted line from [RPT-0015](https://acme.sharepoint.com/x.docx)

---
```bash
kubectl get pods
```
Line one
line two with *em*, ~~old~~ and `code`."""


def test_ada_style_reply_converts_to_valid_adf_with_tables_panels_and_breaks():
    import pathlib
    doc = to_adf(ADA_REPLY)
    kinds = [n["type"] for n in doc["content"]]
    assert kinds == ["paragraph", "paragraph", "table", "heading", "bulletList", "panel", "blockquote", "rule",
                     "codeBlock", "paragraph"]
    table = doc["content"][2]
    assert [c["type"] for c in table["content"][0]["content"]] == ["tableHeader"] * 3
    assert len(table["content"]) == 3  # header + 2 rows; the |---| row is gone
    assert doc["content"][5]["attrs"]["panelType"] == "warning"
    assert any(n["type"] == "hardBreak" for n in doc["content"][-1]["content"])
    text = to_text(doc)
    assert "| Executive / Project director | Owns the business case, approves spend | Executive Project Director |" in text
    assert "|---|" not in text and "**" not in text
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((pathlib.Path(__file__).parent / "fixtures" / "adf-schema-v1.json").read_text())
    jsonschema.Draft4Validator(schema).validate(doc)


async def test_mention_goes_on_the_first_line_of_the_reply():
    posted = []

    def handler(req):
        posted.append(json.loads(req.content))
        return httpx.Response(201, json={"id": "9"})

    await make_tools(handler).add_comment("PRJ-1", "Thanks, done.\n\n- one", mention_account_ids=["acct-1"])
    first = posted[0]["body"]["content"][0]
    assert first["type"] == "paragraph" and first["content"][0]["type"] == "mention"
    assert first["content"][-1]["text"] == "Thanks, done."
