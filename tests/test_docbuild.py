"""Word documents in Acme's style, and edits to any Word document (docbuild.py)."""
import io
from pathlib import Path
import shutil

import pytest
from docx import Document
from docx.oxml.ns import qn

from agentcore.docbuild import edit_docx, outline, split_front_matter
from agentcore.filemaker import make_file

REPORT = """---
type: Project report
title: PRJ-1 programme baseline
subtitle: Concept phase, first baseline for review
number: PRJ-GEN-RPT-nnnn
revision: A
date: 14 October 2026
status: Draft for review
author: Ada (AI) for A. Owner
scope: Long-lead equipment only. Construction packages are in the master RFQ list.
---

## 1 Purpose

This report sets the **first baseline** for [PRJ-1](https://acme.atlassian.net/browse/PRJ-1).

## 2 Long-lead items

| Item | Lead time | Cost |
|---|---|---|
| Transformer | 128 wk | $410,000 |
| PTU | 36 wk | $2.4m |

## 3 Risks

- Grid connection date
- Generator EPA approval
"""


def doc_of(data):
    return Document(io.BytesIO(data))


def test_front_matter_is_split_from_the_body():
    meta, body = split_front_matter(REPORT)
    assert meta["number"] == "PRJ-GEN-RPT-nnnn" and meta["type"] == "Project report"
    assert body.lstrip().startswith("## 1 Purpose")
    assert split_front_matter("# Just a title") == ({}, "# Just a title")


@pytest.fixture
def brand_without_templates(monkeypatch, tmp_path):
    """An example brand (palette, fonts) with no template files: documents are styled in code."""
    import yaml
    from agentcore import brandpack
    spec = yaml.safe_load((Path(__file__).resolve().parent / "fixtures" / "brand" / "brand.yaml").read_text())
    spec.pop("templates", None)
    (tmp_path / "brand.yaml").write_text(yaml.safe_dump(spec))
    monkeypatch.setenv("BRAND_DIR", str(tmp_path))
    brandpack.brand.cache_clear()
    yield
    monkeypatch.undo()
    brandpack.brand.cache_clear()


def test_the_first_heading_becomes_the_title_and_plain_skips_the_style():
    d = doc_of(make_file("notes.docx", "# Site visit notes\n\n## Findings\n\nAll good."))
    assert next(p for p in d.paragraphs if p.style.name == "Title").text == "Site visit notes"
    assert d.tables[0].rows[0].cells[0].text == "DATE"  # no front matter: just the date
    plain = doc_of(make_file("x.docx", "---\nstyle: plain\n---\n# Heading\n\nText"))
    assert plain.element.find(qn("w:background")) is None and not plain.tables
    assert plain.paragraphs[0].style.name == "Heading 1"


def test_xml_children_are_in_schema_order():
    """Word rejects out-of-order children as unreadable content."""
    from agentcore.docbuild import ORDER
    d = doc_of(make_file("baseline.docx", REPORT))
    for el in d.element.iter():
        local = el.tag.rsplit("}", 1)[-1]
        if local in ORDER:
            seq = ORDER[local].split()
            idx = [seq.index(c.tag.rsplit("}", 1)[-1]) for c in el if c.tag.rsplit("}", 1)[-1] in seq]
            assert idx == sorted(idx), f"{local} children out of order: {[c.tag.rsplit('}', 1)[-1] for c in el]}"


def test_edits_keep_formatting_and_report_what_changed():
    data = make_file("baseline.docx", REPORT)
    new, done = edit_docx(data, [
        {"op": "replace_text", "find": "first baseline", "replace": "approved baseline"},
        {"op": "insert", "after_heading": "3 Risks", "markdown": "- Transformer lead time"},
        {"op": "replace_section", "heading": "1 Purpose", "markdown": "New purpose text."},
        {"op": "set_table_cell", "table": 2, "row": 2, "col": 2, "text": "120 wk"},
        {"op": "add_table_row", "table": 2, "values": ["UPS", "14 wk", "$90,000"]},
        {"op": "comment", "on": "Generator EPA approval", "text": "Check with the EPA Level 2 rules"},
    ])
    d = doc_of(new)
    texts = [p.text for p in d.paragraphs]
    assert "New purpose text." in texts and not any("first baseline" in t for t in texts)
    assert texts.index("Transformer lead time") > texts.index("Generator EPA approval")
    t = d.tables[1]
    assert t.cell(1, 1).text == "120 wk" and t.rows[-1].cells[0].text == "UPS"
    assert [c.text for c in d.comments] == ["Check with the EPA Level 2 rules"]
    assert len(done) == 6 and "replaced" in done[0]
    with pytest.raises(ValueError, match="Headings"):
        edit_docx(data, [{"op": "delete_section", "heading": "No such heading"}])
    with pytest.raises(ValueError, match="isn't in the document"):
        edit_docx(data, [{"op": "replace_text", "find": "absent words", "replace": "x"}])


def test_replace_inside_bold_text_keeps_it_bold():
    d = doc_of(edit_docx(make_file("b.docx", REPORT), [{"op": "replace_text", "find": "first", "replace": "final"}])[0])
    body = next(p for p in d.paragraphs if p.text.startswith("This report"))
    run = next(r for r in body.runs if "final baseline" in r.text)
    assert run.bold


def test_tracked_changes_are_real_word_revisions_by_the_agent():
    new, _ = edit_docx(make_file("b.docx", REPORT), [
        {"op": "replace_text", "find": "first baseline", "replace": "approved baseline"},
        {"op": "delete_section", "heading": "3 Risks"},
        {"op": "insert", "after_heading": "2 Long-lead items", "markdown": "Added later."}],
        author="Ada (AI)", track_changes=True)
    xml = doc_of(new).element.body.xml
    assert xml.count("<w:del ") >= 2 and xml.count("<w:ins ") >= 2 and 'w:author="Ada (AI)"' in xml
    assert "<w:delText>first baseline</w:delText>" in xml.replace(' xml:space="preserve"', "")
    assert outline(new)["tracked_changes"] > 0


@pytest.mark.skipif(not shutil.which("soffice"), reason="LibreOffice not installed here")
def test_libreoffice_opens_it_and_renders_pages():
    from agentcore.office import render_pages
    shown, info = render_pages(make_file("baseline.docx", REPORT), "baseline.docx", [1])
    assert shown and info["pages"] >= 1
