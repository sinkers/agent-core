import base64
import io
import json

from agentcore.documents import document_text, readable_tool_result
from agentcore.teams import message_text


def test_teams_link_cards_and_html_hrefs_reach_the_agent():
    url = "https://acme.sharepoint.com/:w:/r/sites/PRJ/Shared%20Documents/Brief/PRJ-GEN-RPT-0001_C.docx?d=w1&csf=1"
    activity = {"text": "Hi can you review this PRJ-GEN-RPT-0001_C_Project-brief.docx",
                "attachments": [
                    {"contentType": "text/html", "content": f'<p>Hi can you review this <a href="{url}">PRJ-GEN-RPT-0001_C_Project-brief.docx</a></p>'},
                    {"contentType": "application/vnd.microsoft.teams.file.download.info", "name": "notes.pdf",
                     "content": {"downloadUrl": "https://dl.example/notes.pdf", "fileType": "pdf"}},
                ]}
    text = message_text(activity, "bot")
    assert url.replace("&", "&") in text and "notes.pdf" in text
    assert text.startswith("Hi can you review this")


def docx_bytes(paragraphs):
    import docx
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "Item", "Value"
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def test_docx_becomes_text_with_tables():
    text = document_text(docx_bytes(["1. Scope", "Supply 2 x 1.5 MVA transformers."]), "brief.docx")
    assert "1. Scope" in text and "1.5 MVA" in text and "Item | Value" in text


def test_pptx_text_includes_tables_groups_charts_and_notes():
    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "Structures compared"
    table = slide.shapes.add_table(2, 3, Inches(1), Inches(2), Inches(6), Inches(1)).table
    for c, v in enumerate(["Structure", "Risk to Acme", "Price certainty"]):
        table.cell(0, c).text = v
    for c, v in enumerate(["EPC (FIDIC Silver)", "Low", "High"]):
        table.cell(1, c).text = v
    group = slide.shapes.add_group_shape()
    group.shapes.add_textbox(Inches(1), Inches(4), Inches(2), Inches(1)).text_frame.text = "Grouped caption"
    data = CategoryChartData()
    data.categories = ["CM", "EPC"]
    data.add_series("Cost", (10, 12))
    chart = slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(5), Inches(4), Inches(2), data).chart
    chart.has_title = True
    chart.chart_title.text_frame.text = "Cost by structure"
    slide.notes_slide.notes_text_frame.text = "Speaker: stress the EPC wrap"
    buf = io.BytesIO()
    prs.save(buf)
    text = document_text(buf.getvalue(), "pack.pptx")
    assert "Structures compared" in text
    assert "Structure | Risk to Acme | Price certainty" in text and "EPC (FIDIC Silver) | Low | High" in text
    assert "Grouped caption" in text
    assert "Cost by structure" in text and "CM" in text and "Cost: 10, 12" in text
    assert "Speaker: stress the EPC wrap" in text


def test_sharepoint_get_result_is_made_readable():
    raw = json.dumps({"name": "brief.docx", "size": 1234, "web_url": "https://sp/brief.docx",
                      "content_base64": base64.b64encode(docx_bytes(["Hello review"])).decode()})
    out = json.loads(readable_tool_result("openclaw__sharepoint_get", raw))
    assert "content_base64" not in out and "Hello review" in out["text"] and out["name"] == "brief.docx"
    assert readable_tool_result("jira__get_issue", "{}") == "{}"  # other tools untouched


def test_openclaw_markdown_result_is_made_readable():
    b64 = base64.b64encode(docx_bytes(["1 Document control", "8 Next steps"])).decode()
    raw = ("**name:** brief.docx\n**size:** 112439 bytes\n**mimeType:** application/vnd.openxmlformats\n"
           "**webUrl:** https://acme.sharepoint.com/x\n**last_modified:** 2026-09-30\n"
           "**download_url:** https://secret.example/download?tempauth=abc\n"
           "(download_url is a direct, pre-authenticated link — valid ~1 hour, no auth header needed)\n\n"
           f"**content_base64:**\n{b64}")
    out = readable_tool_result("openclaw__sharepoint_get", raw)
    assert "8 Next steps" in out and "tempauth" not in out and b64[:40] not in out
    assert "**name:** brief.docx" in out
