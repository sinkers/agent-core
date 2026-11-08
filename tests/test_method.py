"""The Acme method tools (method.py) and the snapshot converter (scripts/method-sync.py)."""

import json
from pathlib import Path

import pytest

from agentcore.method import MethodTools



def snapshot(tmp_path, approved=()):
    """A small snapshot: the draft, plus any approved releases."""
    versions = []
    for vid, kind in [*((v, "approved") for v in approved), ("draft", "draft")]:
        d = tmp_path / vid
        (d / "templates/stage-1/d1-02-project-brief").mkdir(parents=True)
        (d / "templates/stage-1/d1-02-project-brief/README.md").write_text("# D1.02 Project brief\nMust contain: scope.")
        (d / "templates/stage-1/d1-02-project-brief/Brief.docx").write_bytes(b"PK docx")
        base = "https://apps.example.com/method/" if kind == "approved" else "https://apps.example.com/method-dev/"
        pages = [{"path": "gates/g2/", "title": "G2 — Preliminary investment", "url": base + "gates/g2/",
                  "text": f"# G2\nAuthority: Acme Board ({vid}). Exit: basis of design approved."},
                 {"path": "deliverables/d1-02/", "title": "D1.02 — Project brief", "url": base + "deliverables/d1-02/",
                  "text": "# D1.02 — Project brief\nApprover: Board at G1."},
                 {"path": "lifecycle/stage-3/", "title": "Stage 3 — Spatial Coordination", "url": base + "lifecycle/stage-3/",
                  "text": "Coordinated schematic set; connection application."},
                 {"path": "workstreams/fin/", "title": "FIN — Offtake and project finance", "url": base + "workstreams/fin/",
                  "text": "Term sheet at G1."},
                 {"path": "governance/change-control/", "title": "Change control", "url": base + "governance/change-control/",
                  "text": "Change requests above project manager authority go to the sponsor."}]
        (d / "pages.json").write_text(json.dumps(pages))
        (d / "templates.json").write_text(json.dumps([
            {"path": "stage-1/d1-02-project-brief/README.md", "size": 40},
            {"path": "stage-1/d1-02-project-brief/Brief.docx", "size": 7}]))
        versions.append({"id": vid, "kind": kind, "version": vid, "sha": "abc1234", "date": "2026-10-16",
                         "pages": len(pages), "changes": f"- MINOR: changes in {vid}"})
    (tmp_path / "index.json").write_text(json.dumps({"doc_number": "Acme-GEN-RPT-0003", "repo": "github.com/example/method",
                                                     "official": approved[-1] if approved else None,
                                                     "versions": versions}))
    return MethodTools(root=str(tmp_path))


async def call(tools, name, args):
    return json.loads(await tools.call_tool(name, args))


async def test_with_no_approved_release_everything_is_marked_draft(tmp_path):
    tools = snapshot(tmp_path)
    v = await call(tools, "method_versions", {})
    assert v["official"] is None and "no approved release" in v["note"]
    page = await call(tools, "method_page", {"id": "G2"})
    assert page["approved"] is False and "DRAFT" in page["warning"] and "not approved" in page["cite_as"]
    assert page["url"].startswith("https://apps.example.com/method-dev/")


async def test_the_latest_approved_release_is_official_and_older_ones_say_superseded(tmp_path):
    tools = snapshot(tmp_path, approved=("v1.0.0", "v1.1.0"))
    page = await call(tools, "method_page", {"id": "G2"})
    assert page["version"] == "v1.1.0" and page["official"] and "warning" not in page
    assert page["cite_as"] == "Acme method v1.1.0 (Acme-GEN-RPT-0003)"
    old = await call(tools, "method_page", {"id": "G2", "version": "1.0.0"})
    assert "superseded" in old["cite_as"] and "v1.0.0" in old["text"]
    draft = await call(tools, "method_page", {"id": "G2", "version": "draft"})
    assert draft["approved"] is False and draft["based_on"] == "v1.1.0"


@pytest.mark.parametrize("ident,path", [("D1.2", "deliverables/d1-02/"), ("d1.02", "deliverables/d1-02/"),
                                        ("G2", "gates/g2/"), ("stage 3", "lifecycle/stage-3/"),
                                        ("FIN", "workstreams/fin/"), ("governance/change-control/", "governance/change-control/"),
                                        ("https://apps.example.com/method-dev/gates/g2/", "gates/g2/")])
async def test_pages_by_id_or_path(tmp_path, ident, path):
    tools = snapshot(tmp_path)
    assert (await call(tools, "method_page", {"id": ident}))["id"] == path


async def test_search_ranks_ids_and_titles_first(tmp_path):
    tools = snapshot(tmp_path)
    res = await call(tools, "method_search", {"query": "who approves the project brief D1.02"})
    assert res["results"][0]["id"] == "deliverables/d1-02/"
    res = await call(tools, "method_search", {"query": "change control sponsor"})
    assert res["results"][0]["id"] == "governance/change-control/"


async def test_unknown_page_suggests_and_unknown_version_lists_what_exists(tmp_path):
    tools = snapshot(tmp_path)
    miss = await call(tools, "method_page", {"id": "change"})
    assert "did_you_mean" in miss
    bad = await call(tools, "method_page", {"id": "G2", "version": "v9.9.9"})
    assert "available draft" in bad["error"]


async def test_template_lists_readme_and_puts_a_file_in_the_outbox(tmp_path):
    from agentcore.files import FileTools
    from agentcore.store import MemoryStore
    tools = snapshot(tmp_path)
    tools.files = FileTools(MemoryStore())
    out = await call(tools, "method_template", {"deliverable": "D1.02"})
    assert "Must contain" in out["readme"] and out["files"] == [{"name": "Brief.docx", "size": 7}]
    got = json.loads(await tools.call_tool("method_template", {"deliverable": "D1.02", "file": "Brief.docx"},
                                           type("C", (), {"conversation_id": "chat-a"})()))
    data, name, _ = await tools.files.resolve(got["file"]["file"], "chat-a")
    assert name == "Brief.docx" and data == b"PK docx"


async def test_changes_since_a_projects_version(tmp_path):
    tools = snapshot(tmp_path, approved=("v1.0.0", "v1.1.0", "v2.0.0"))
    out = await call(tools, "method_changes", {"since": "v1.0.0"})
    assert [r["version"] for r in out["releases"]] == ["v1.1.0", "v2.0.0"]


async def test_no_snapshot_says_so(tmp_path):
    tools = MethodTools(root=str(tmp_path / "missing"))
    assert "isn't loaded" in (await call(tools, "method_page", {"id": "G2"}))["error"]
