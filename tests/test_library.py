"""Reference library (library.py) and skill reference files (skills.load_reference)."""

import json

import pytest

from agentcore.library import LibraryTools, ReferenceLibrary, _key
from agentcore.skills import SkillLoader
from agentcore.tools import CallContext, ToolGateway

from .conftest import SKILLS

CTX = CallContext("c1", "aad-a", [])

SOPA = """# Part 3 Procedure for recovering progress payments

## Division 1 Payment claims and payment schedules

### 17 Payment claims

(1) A person who is or who claims to be entitled to a progress payment may serve a payment claim on the person who
under the construction contract concerned is or may be liable to make the payment.

### 18 Payment schedules

(1) A respondent may reply to a payment claim by providing a payment schedule to the claimant.
(4) A respondent must provide a payment schedule within 10 business days after the payment claim is served.

### 18A Something inserted later

Text of an inserted section.

# Part 4 Adjudication

### 21 Adjudication applications

A claimant may apply for adjudication of a payment claim.
"""

NCC = """# Section C Fire resistance

## Part C2 Fire resistance and stability

### C2D2 Type of construction required

The minimum Type of fire-resisting construction of a building must be that specified in Table C2D2.

### C2D3 Calculation of rise in storeys

Rise in storeys is counted from the ground.
"""


def make_library(tmp_path, *, pending=False):
    sopa = tmp_path / "tas-sopa-2009"
    sopa.mkdir()
    (sopa / "source.yaml").write_text(
        "id: tas-sopa-2009\ntitle: Building and Construction Industry Security of Payment Act 2009 (Tas)\n"
        "short: SOPA 2009 (Tas)\nedition: Version current from 1 July 2025\nsource_url: https://legislation.tas.gov.au/x\n"
        "licence: CC BY 4.0\nattribution: State of Tasmania\nretrieved: 2026-10-10\n")
    (sopa / "01-act.md").write_text(SOPA)
    ncc = tmp_path / "ncc-2022-vol1"
    ncc.mkdir()
    (ncc / "source.yaml").write_text("id: ncc-2022-vol1\nshort: NCC 2022 Vol 1\nedition: NCC 2022\nlicence: CC BY 4.0\n")
    (ncc / "01-section-c.md").write_text(NCC)
    if pending:
        p = tmp_path / "as-4000-1997"
        p.mkdir()
        (p / "source.yaml").write_text("id: as-4000-1997\nshort: AS 4000-1997\nstatus: not_loaded\n"
                                       "how_to_get: Bought copy only; per-user licence.\n")
    return LibraryTools(ReferenceLibrary(tmp_path))


async def call(tools, name, args=None):
    return json.loads(await tools.call_tool(name, args or {}, CTX))


@pytest.mark.parametrize("text,key", [
    ("17 Payment claims", "17"), ("s 17", "17"), ("section 17", "17"), ("reg. 291", "291"), ("cl 34.2", "34.2"),
    ("C2D2 Type of construction", "c2d2"), ("Part 4 Adjudication", "part 4"), ("PART 4—ADJUDICATION", "part 4"),
    ("3.2 Safe work method statements", "3.2"), ("18A", "18a"), ("Spec 5", "specification 5"),
    ("Payment claims", ""),
])
def test_section_keys(text, key):
    assert _key(text) == key


async def test_list_shows_edition_licence_and_documents_not_yet_held(tmp_path):
    tools = make_library(tmp_path, pending=True)
    docs = {d["id"]: d for d in (await call(tools, "list_references"))["documents"]}
    assert docs["tas-sopa-2009"]["edition"] == "Version current from 1 July 2025"
    assert docs["tas-sopa-2009"]["licence"] == "CC BY 4.0" and docs["tas-sopa-2009"]["sections"] == 4
    assert docs["as-4000-1997"]["status"].startswith("not loaded") and "per-user" in docs["as-4000-1997"]["how_to_get"]
    out = await call(tools, "read_reference", {"doc": "as-4000-1997", "section": "34"})
    assert "no text is held" in out["error"]


async def test_search_ranks_the_section_and_says_how_to_read_it(tmp_path):
    tools = make_library(tmp_path)
    out = await call(tools, "search_references", {"query": "payment schedule business days"})
    top = out["results"][0]
    assert top["doc"] == "tas-sopa-2009" and top["section"] == "18 Payment schedules"
    assert top["read"] == {"doc": "tas-sopa-2009", "section": "18"} and "**" in top["snippet"]
    assert "Division 1" in top["within"]
    # stemming, OR fallback when not every word is present, and the doc filter
    out = await call(tools, "search_references", {"query": "claims adjudicated zebra"})
    assert {r["section"] for r in out["results"]} >= {"21 Adjudication applications"}
    out = await call(tools, "search_references", {"query": "construction", "doc": "ncc-2022-vol1"})
    assert out["results"] and all(r["doc"] == "ncc-2022-vol1" for r in out["results"])
    assert "error" in await call(tools, "search_references", {"query": "x", "doc": "nope"})
    assert "note" in await call(tools, "search_references", {"query": "zebra giraffe"})


async def test_read_by_number_title_or_part_with_a_citation(tmp_path):
    tools = make_library(tmp_path)
    out = await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "s 18"})
    assert out["section"] == "18 Payment schedules" and "10 business days" in out["text"]
    assert out["cite"].startswith("SOPA 2009 (Tas), 18 Payment schedules — Version current from 1 July 2025")
    assert "https://legislation.tas.gov.au/x" in out["cite"] and out["attribution"] == "State of Tasmania"
    assert (await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "18A"}))["section"].startswith("18A")
    part = await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "Part 3"})
    assert "17 Payment claims" in part["text"] and "18A" in part["text"] and "Adjudication applications" not in part["text"]
    ncc = await call(tools, "read_reference", {"doc": "ncc-2022-vol1", "section": "C2D2"})
    assert "Table C2D2" in ncc["text"]
    by_title = await call(tools, "read_reference", {"doc": "ncc-2022-vol1", "section": "rise in storeys"})
    assert by_title["section"].startswith("C2D3")
    miss = await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "19"})
    assert "no section" in miss["error"]


async def test_long_sections_page(tmp_path, monkeypatch):
    import agentcore.library as lib
    monkeypatch.setattr(lib, "MAX_READ_CHARS", 100)
    tools = make_library(tmp_path)
    first = await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "Part 3"})
    assert len(first["text"]) == 100 and first["more"]["next_offset"] == 100
    second = await call(tools, "read_reference", {"doc": "tas-sopa-2009", "section": "Part 3", "offset": 100})
    assert second["text"] and second["text"] != first["text"]


async def test_missing_folder_is_an_empty_library(tmp_path):
    tools = LibraryTools(ReferenceLibrary(tmp_path / "absent"))
    assert (await call(tools, "list_references"))["documents"] == []



async def test_ada_gets_the_library_through_the_gateway(cfg):
    skills = SkillLoader(SKILLS, cfg.agent.name, cfg.skills.always, cfg.skills.on_demand)
    gw = ToolGateway(cfg, skills, {})
    assert isinstance(gw.clients.get("library"), LibraryTools)
    await gw.discover()
    assert {"library__list_references", "library__search_references", "library__read_reference"} <= set(gw.specs)


def test_skill_reference_files_load_only_from_references(tmp_path):
    skill = tmp_path / "demo"
    (skill / "references").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: d\nversion: 1.0.0\napplies_to: [ada]\n---\nBody")
    (skill / "references" / "detail.md").write_text("Detail text")
    (skill / "secret.md").write_text("not a reference")
    loader = SkillLoader(tmp_path, "ada", [], ["demo"])
    assert loader.list()[0]["references"] == ["references/detail.md"]
    for ref in ("detail", "detail.md", "references/detail.md"):
        assert "Detail text" in loader.load_reference("demo", ref)
    for bad in ("../secret.md", "secret.md", "references/../secret.md"):
        with pytest.raises(KeyError):
            loader.load_reference("demo", bad)


async def test_skills_load_with_a_reference_through_the_gateway(cfg, tmp_path):
    skills = SkillLoader(SKILLS, cfg.agent.name, cfg.skills.always, cfg.skills.on_demand)
    gw = ToolGateway(cfg, skills, {})
    name = next(n for n in skills.skills if skills.references(n))
    ref = skills.references(name)[0]
    out = await gw.call("skills__load", {"name": name, "reference": ref}, CTX, True)
    assert out.status == "ok" and out.content.startswith(f'<skill_reference skill="{name}"')
    out = await gw.call("skills__load", {"name": name, "reference": "nope.md"}, CTX, True)
    assert out.status == "error"
