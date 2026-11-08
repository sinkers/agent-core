"""DXF drawings: the text a drawing review checks, and pictures of the sheets."""

import io
import json

import pytest

ezdxf = pytest.importorskip("ezdxf")


def drawing(binary: bool = False) -> bytes:
    doc = ezdxf.new("R2018", setup=True)
    doc.header["$INSUNITS"] = 4  # millimetres
    doc.layers.add("E-POWER", color=1)
    doc.layers.add("TITLE", color=7)
    msp = doc.modelspace()
    msp.add_line((0, 0), (4000, 0), dxfattribs={"layer": "E-POWER"})
    msp.add_lwpolyline([(0, 0), (4000, 0), (4000, 2500), (0, 2500)], close=True, dxfattribs={"layer": "E-POWER"})
    msp.add_text("TX1 2.5 MVA 22/0.433 kV", dxfattribs={"layer": "E-POWER", "height": 100}).set_placement((200, 1200))
    msp.add_mtext("NOTE 1: ALL CABLES TO AS/NZS 3008.1.1\\PNOTE 2: REFER PRJ-ELE-SLD-001", dxfattribs={"layer": "E-POWER"})
    dim = msp.add_linear_dim(base=(0, -400), p1=(0, 0), p2=(4000, 0), dxfattribs={"layer": "E-POWER"})
    dim.render()
    cb = doc.blocks.new("CB")  # breaker symbols in the model space must not crowd the title block out
    cb.add_circle((0, 0), 5)
    cb.add_attdef("SIZE", (6, 0))
    for n in range(12):
        msp.add_blockref("CB", (n * 300, 1800)).add_auto_attribs({"SIZE": "1000A" if n < 4 else "250A"})
    tb = doc.blocks.new("Acme_A1_TITLE")
    tb.add_lwpolyline([(0, 0), (180, 0), (180, 60), (0, 60)], close=True)
    for tag, y in (("DWG_NO", 40), ("REV", 30), ("TITLE", 20), ("DRAWN", 10)):
        tb.add_attdef(tag, (5, y), dxfattribs={"height": 3})
    sheet = doc.layouts.new("A1-01")
    ref = sheet.add_blockref("Acme_A1_TITLE", (230, 10), dxfattribs={"layer": "TITLE"})
    ref.add_auto_attribs({"DWG_NO": "PRJ-ELE-GA-0012", "REV": "C", "TITLE": "SWITCHROOM GENERAL ARRANGEMENT",
                          "DRAWN": "EH"})
    sheet.add_text("ISSUED FOR CONSTRUCTION", dxfattribs={"layer": "TITLE", "height": 5}).set_placement((230, 80))
    if binary:
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "b.dxf")
            doc.saveas(path, fmt="bin")
            with open(path, "rb") as f:
                return f.read()
    s = io.StringIO()
    doc.write(s)
    return s.getvalue().encode()


@pytest.mark.parametrize("binary", [False, True])
def test_dxf_text_has_what_a_drawing_review_checks(binary):
    from agentcore.documents import document_text
    text = document_text(drawing(binary), "PRJ-ELE-GA-0012_C.dxf")
    assert "units millimetres" in text and "Sheets (layouts): Model, A1-01" in text
    assert "DWG_NO = PRJ-ELE-GA-0012" in text and "REV = C" in text and "[A1-01] Acme_A1_TITLE" in text
    assert "Components in the model space (12 attributed symbols" in text and "CB (12): SIZE = 250A x8 | SIZE = 1000A x4" in text
    assert "TX1 2.5 MVA" in text and "REFER PRJ-ELE-SLD-001" in text and "ISSUED FOR CONSTRUCTION" in text
    assert "Dimensions (1" in text and "4000.00" in text
    assert "E-POWER (" in text and "LWPOLYLINE" in text


def test_dxf_sheets_are_drawn_as_pictures():
    from PIL import Image

    from agentcore.images import visual
    shown, info = visual(drawing(), "ga.dxf")
    assert info["kind"] == "dxf" and info["views"] == ["1 model space", "2 sheet A1-01"] and info["shown"] == [1, 2]
    assert [label for _, _, label in shown] == ["ga.dxf model space", "ga.dxf sheet A1-01"]
    img = Image.open(io.BytesIO(shown[0][1])).convert("L")
    assert max(img.size) <= 1568 and img.getextrema()[0] < 128  # something was drawn on the white sheet
    only_sheet, sheet_info = visual(drawing(), "ga.dxf", pages=[2])
    assert [label for _, _, label in only_sheet] == ["ga.dxf sheet A1-01"]
    assert "DWG_NO = PRJ-ELE-GA-0012" in sheet_info["drawing_text"]  # the text comes with any view


def test_dwg_and_broken_files_get_a_clear_message():
    from agentcore.documents import document_text
    from agentcore.images import visual
    with pytest.raises(ValueError, match="DWG"):
        document_text(b"AC1032" + b"\0" * 100, "x.dxf")
    with pytest.raises(ValueError, match="DWG"):
        visual(b"AC1032", "x.dwg")
    with pytest.raises(ValueError, match="couldn't read that DXF"):
        document_text(b"this is not a drawing", "x.dxf")


async def test_eve_reads_and_views_a_dxf_sent_by_email():
    from agentcore.attachments import AttachmentTools, remember_attachments
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext
    store = MemoryStore()
    await remember_attachments(store, "chat-d", [{"name": "PRJ-ELE-GA-0012_C.dxf", "type": "application/dxf",
                                                 "source": "mail", "message_id": "m", "attachment_id": "a", "size": 1}])
    data = drawing()

    async def mail_fetch(entry):
        return data, entry["name"]
    tools = AttachmentTools(store, mail_fetch=mail_fetch)
    ctx = CallContext("chat-d", "u", [])
    read = json.loads(await tools.call_tool("read_attachment", {"name": "PRJ-ELE-GA-0012_C.dxf"}, ctx))
    assert "DWG_NO = PRJ-ELE-GA-0012" in read["text"]
    seen = await tools.call_tool("view_attachment", {"name": "PRJ-ELE-GA-0012_C.dxf"}, ctx)
    assert isinstance(seen, dict) and len(seen["images"]) == 2
    assert "DWG_NO = PRJ-ELE-GA-0012" in json.loads(seen["text"])["drawing_text"]  # the exact data with the pictures



@pytest.mark.skipif(not __import__("sys").platform.startswith("linux"), reason="the memory cap is enforced on Linux")
def test_a_drawing_too_big_for_memory_fails_with_a_message_not_a_crash(monkeypatch):
    from agentcore import dxf
    monkeypatch.setattr(dxf, "CHILD_MEMORY_BYTES", 40 * 1024 * 1024)
    with pytest.raises(ValueError, match="too big|ran out of memory"):
        dxf.isolated("render", drawing(), "big.dxf", None, 1568)
