"""Acme decks on the brand team's master PowerPoint (deck.py), from specs and Markdown, and edits to any deck."""

import io
import json
import zipfile

import pytest

from agentcore.deck import build_deck, deck_outline, edit_deck, image_refs_in_deck, markdown_to_deck, template_path


def png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (42, 90, 61)).save(buf, "PNG")
    return buf.getvalue()


EVERY_KIND = {
    "title": "PRJ *programme* update", "subtitle": "Where we are", "author": "Ada (AI) • Project manager",
    "slides": [
        {"kind": "section", "title": "Status", "subtitle": "Programme and procurement"},
        {"kind": "bullets", "title": "Headlines", "bullets": ["On track", {"text": "Offer due Nov", "level": 1}],
         "notes": "Lead with the date"},
        {"kind": "two_column", "title": "Then and now", "left_title": "Then", "left": ["a"], "right": ["b"]},
        {"kind": "statement", "text": "Power first, then compute."},
        {"kind": "image", "title": "Site", "body": "The north yard", "image": "outbox:abc"},
        {"kind": "comparison", "title": "Options", "left": {"label": "Hire", "headline": "Hire sets", "body": "4 wk"},
         "right": {"label": "Buy", "headline": "Wait", "body": "17 wk"}},
        {"kind": "table", "title": "RFQs", "columns": ["Package", "Value"], "rows": [["PTU", "$4.0M"]]},
        {"kind": "numbers", "title": "Figures", "items": [{"value": "36 wk", "label": "lead"},
                                                          {"value": "10 MW", "label": "build"}]},
        {"kind": "process", "title": "How we buy", "steps": [{"heading": "Issue", "body": "RFQ"},
                                                             {"heading": "Rank", "body": "Score"}]},
        {"kind": "timeline", "title": "Milestones", "items": [{"date": "Nov", "heading": "Offer", "body": "x"},
                                                              {"date": "Jun", "heading": "Deliver", "current": True}]},
        {"kind": "chart", "chart": "bar", "title": "Spend", "body": "$k", "categories": ["Jul", "Aug"],
         "series": [{"name": "Spend", "values": [1, 2]}]},
        {"kind": "chart", "chart": "line", "title": "Trend", "categories": ["Jul", "Aug"],
         "series": [{"name": "A", "values": [1, 2]}, {"name": "B", "values": [2, 1]}]},
        {"kind": "closing", "text": "Questions? *Talk to us.*", "presenter": "Ada (AI)", "email": "ada@acme.example"},
    ]}


def xml_text(data: bytes) -> str:
    z = zipfile.ZipFile(io.BytesIO(data))
    return "\n".join(z.read(n).decode("utf8", "ignore") for n in z.namelist()
                     if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def test_long_lists_and_tables_continue_on_new_slides():
    from pptx import Presentation
    spec = {"title": "T", "slides": [{"title": "Many", "bullets": [f"b{i}" for i in range(10)]},
                                     {"kind": "table", "title": "Big", "columns": ["a"], "rows": [[i] for i in range(12)]}]}
    titles = [o["title"] for o in deck_outline(build_deck(spec))]
    assert titles[1:] == ["Many", "Many (cont.)", "Big", "Big (cont.)"]
    with pytest.raises(ValueError, match="unknown slide kind"):
        build_deck({"title": "T", "slides": [{"kind": "wobble"}]})
    assert Presentation(io.BytesIO(build_deck({"title": "Only a title", "slides": []}))).slides


def test_markdown_becomes_a_deck_spec():
    md = """# PRJ *update*
Where we are

## Status
> Programme and procurement

## Headlines
- On track
  - Offer due Nov
Notes: Lead with the date.

## Figures
- **36 wk** PTU lead
- **10 MW** first build

## RFQs
| Package | Value |
|---|---|
| PTU | $4.0M |

---
### Spend
Monthly, $k
```chart line
Month,Spend,Budget
Jul,1,2
Aug,2,2
```

## Site
![North yard](outbox:abc)
Looking north.

## Questions?
"""
    spec = markdown_to_deck(md)
    assert spec["title"] == "PRJ *update*" and spec["subtitle"] == "Where we are"
    kinds = [s["kind"] for s in spec["slides"]]
    assert kinds == ["section", "bullets", "numbers", "table", "chart", "image", "closing"]
    s = spec["slides"]
    assert s[1]["bullets"][1] == {"text": "Offer due Nov", "level": 1} and s[1]["notes"] == "Lead with the date."
    assert s[2]["items"][0] == {"value": "36 wk", "label": "PTU lead"}
    assert s[3]["columns"] == ["Package", "Value"] and s[3]["rows"] == [["PTU", "$4.0M"]]
    assert s[4]["chart"] == "line" and s[4]["series"][1] == {"name": "Budget", "values": ["2", "2"]}
    assert s[4]["title"] == "Spend" and s[5]["image"] == "outbox:abc" and "Looking north." in s[5]["body"]
    assert build_deck(spec, {"outbox:abc": ("x.png", png_bytes())})
    assert markdown_to_deck(json.dumps({"title": "J", "slides": []}))["title"] == "J"


def test_replace_text_keeps_run_formatting():
    from pptx import Presentation
    data = build_deck({"title": "T", "slides": [{"title": "Head", "bullets": ["Budget is $12M today"]}]})
    new, _ = edit_deck(data, [{"op": "replace_text", "find": "$12M", "replace": "$13M"}])
    run = next(r for s in Presentation(io.BytesIO(new)).slides for sh in s.shapes if sh.has_text_frame
               for p in sh.text_frame.paragraphs for r in p.runs if "$13M" in r.text)
    assert run.font.name == "Calibri" and run.text == "Budget is $13M today"


def test_edits_work_on_any_deck():
    from pptx import Presentation
    prs = Presentation()  # a plain Office deck, no Acme layouts
    s = prs.slides.add_slide(prs.slide_layouts[1])
    s.shapes.title.text = "Old plan"
    s.placeholders[1].text = "First point"
    buf = io.BytesIO()
    prs.save(buf)
    new, changes = edit_deck(buf.getvalue(), [
        {"op": "add_slides", "slides": [
            {"kind": "bullets", "title": "More", "bullets": ["a", "b"]},
            {"kind": "table", "title": "Costs", "columns": ["Item", "$"], "rows": [["PTU", "4.0"]]},
            {"kind": "chart", "title": "Trend", "categories": ["a", "b"], "series": [{"name": "s", "values": [1, 2]}]}]},
        {"op": "set_bullets", "slide": 1, "bullets": ["Replaced point"]}])
    outline = deck_outline(new)
    assert [o["title"] for o in outline] == ["Old plan", "More", "Costs", "Trend"]
    assert outline[2]["table"] == 1 and outline[3]["chart"] and "Replaced point" in outline[0]["text"]


def test_image_refs_the_content_needs():
    md = "# T\n## Site\n![North yard](outbox:abc)\n## Again\n![Same](outbox:abc)\n![Plan](https://x.sharepoint.com/a.png)\n"
    assert image_refs_in_deck(md) == ["outbox:abc", "https://x.sharepoint.com/a.png"]
    spec = {"title": "T", "slides": [{"kind": "image", "image": "site.jpg"}, {"kind": "bullets"}]}
    assert image_refs_in_deck(json.dumps(spec)) == ["site.jpg"]
    assert image_refs_in_deck("{not json") == [] and image_refs_in_deck("") == []
