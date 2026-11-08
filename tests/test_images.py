import base64
import io
import json
import random

import pytest
from PIL import Image

from agentcore.images import MAX_EDGE, prepare_image, visual


def noisy_png(w=900, h=600) -> bytes:
    """A picture that doesn't compress to nothing (a real screenshot or photo, not a flat colour)."""
    random.seed(1)
    img = Image.new("RGB", (w, h))
    img.putdata([(random.randrange(256), random.randrange(256), random.randrange(256)) for _ in range(w * h)])
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def pdf(pages=3) -> bytes:
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument.new()
    for _ in range(pages):
        doc.new_page(1191, 842)  # A3 landscape, like a drawing sheet
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def test_big_screenshots_are_downscaled_and_small_ones_untouched():
    buf = io.BytesIO()
    Image.new("RGB", (3024, 1964), "white").save(buf, "PNG")
    fmt, out, info = prepare_image(buf.getvalue())
    assert info["resized"] and max(Image.open(io.BytesIO(out)).size) == MAX_EDGE
    small = io.BytesIO()
    Image.new("RGB", (400, 300), "white").save(small, "PNG")
    assert prepare_image(small.getvalue()) == ("png", small.getvalue(), {"width": 400, "height": 300, "resized": False})
    with pytest.raises(ValueError):
        prepare_image(b"not an image")


def test_pdf_pages_render_for_drawings():
    shown, info = visual(pdf(7), "PRJ-ELE-SLD-001.pdf", pages="2-3")
    assert info == {"kind": "pdf", "pages": 7, "shown": [2, 3]}
    assert [label for _, _, label in shown] == ["PRJ-ELE-SLD-001.pdf page 2", "PRJ-ELE-SLD-001.pdf page 3"]
    assert max(Image.open(io.BytesIO(shown[0][1])).size) <= MAX_EDGE
    default, info = visual(pdf(9), "set.pdf")
    assert info["shown"] == [1, 2, 3, 4, 5]  # at most 5 per call


def test_pictures_inside_word_documents_but_not_logos():
    from docx import Document
    from docx.shared import Inches
    d = Document()
    d.add_paragraph("Figure 1 shows the layout.")
    logo = io.BytesIO()
    Image.new("RGB", (40, 40), "red").save(logo, "PNG")
    d.add_picture(io.BytesIO(logo.getvalue()), width=Inches(0.3))
    d.add_picture(io.BytesIO(noisy_png()), width=Inches(5))
    out = io.BytesIO()
    d.save(out)
    shown, info = visual(out.getvalue(), "design.docx")
    assert info["pictures"] == 1 and len(shown) == 1 and shown[0][2] == "design.docx picture 1"


async def test_view_attachment_returns_images_to_the_gateway_and_read_attachment_shows_images():
    from agentcore.attachments import AttachmentTools, remember_attachments
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext
    store = MemoryStore()
    await remember_attachments(store, "chat-a", [{"name": "Screenshot 2026-10-06.png", "type": "image/png", "source": "mail",
                                                  "message_id": "m", "attachment_id": "a", "size": 1000},
                                                 {"name": "SLD.pdf", "type": "application/pdf", "source": "mail",
                                                  "message_id": "m", "attachment_id": "b", "size": 1000}])
    blobs = {"a": noisy_png(), "b": pdf(2)}

    async def mail_fetch(entry):
        return blobs[entry["attachment_id"]], entry["name"]
    tools = AttachmentTools(store, mail_fetch=mail_fetch)
    ctx = CallContext("chat-a", "u", [])
    out = await tools.call_tool("read_attachment", {"name": "Screenshot"}, ctx)
    assert isinstance(out, dict) and out["images"][0][2] == "Screenshot 2026-10-06.png"
    out = await tools.call_tool("view_attachment", {"name": "SLD.pdf", "pages": [2]}, ctx)
    assert json.loads(out["text"])["shown"] == [2] and len(out["images"]) == 1


async def test_images_reach_the_model_as_image_blocks(cfg):
    from agentcore.agent import Turn
    from .conftest import make_agent

    class ImageTool:
        in_process = True

        async def list_tools(self):
            return [{"name": "view_attachment", "description": "x", "inputSchema": {"type": "object"}}]

        async def call_tool(self, name, args, ctx):
            return {"text": '{"name": "SLD.pdf"}', "images": [("png", b"PNGDATA", "SLD.pdf page 1")]}
    agent, model, _, tools = make_agent(cfg, [[("attachments__view_attachment", {"name": "SLD.pdf"})], "The SLD shows two feeders."])
    tools.clients["attachments"] = ImageTool()
    await tools.discover()
    await agent.handle(Turn("c1", "check the SLD", "u1", "Erin"))
    result = model.calls[1]["messages"][-1]["content"][0]["toolResult"]["content"]
    assert {"image": {"format": "png", "source": {"bytes": b"PNGDATA"}}} in result
    assert {"text": "[image: SLD.pdf page 1]"} in result


async def test_a_model_that_refuses_images_gets_a_note_instead(cfg):
    from agentcore.agent import Turn
    from .conftest import make_agent

    class ImageTool:
        in_process = True

        async def list_tools(self):
            return [{"name": "view_attachment", "description": "x", "inputSchema": {"type": "object"}}]

        async def call_tool(self, name, args, ctx):
            return {"text": "{}", "images": [("png", b"P", "x page 1")]}
    agent, model, _, tools = make_agent(cfg, [[("attachments__view_attachment", {})], "I couldn't see the image."])
    tools.clients["attachments"] = ImageTool()
    await tools.discover()
    original = model.converse

    async def picky(**kw):
        if any("image" in c for m in kw["messages"] for b in m["content"] if "toolResult" in b
               for c in b["toolResult"]["content"]):
            raise RuntimeError("ValidationException: this model doesn't support image input")
        return await original(**kw)
    model.converse = picky
    res = await agent.handle(Turn("c1", "look", "u1", "Erin"))
    assert "couldn't see" in res.reply
    assert "could not be shown to this model" in str(model.calls[-1]["messages"])


def test_sharepoint_image_files_come_back_as_pictures():
    from agentcore.documents import readable_tool_result_with_images
    body = "**name:** site-photo.jpg\n**size:** 99 bytes\n**content_base64:**\n" + base64.b64encode(noisy_png(200, 100)).decode()
    text, images = readable_tool_result_with_images("openclaw__sharepoint_get", body)
    assert "shown to you below" in text and len(images) == 1
    doc = "**name:** report.pdf\n**size:** 9 bytes\n**content_base64:**\n" + base64.b64encode(pdf(1)).decode()
    text, images = readable_tool_result_with_images("openclaw__sharepoint_get", doc)
    assert images == [] and "view_attachment" in text


async def test_emailed_screenshots_are_kept_but_signature_logos_skipped():
    import httpx
    from agentcore.mailbox import MailboxPoller
    from agentcore.store import MemoryStore
    items = [{"@odata.type": "#microsoft.graph.fileAttachment", "id": "1", "name": "image001.png", "contentType": "image/png", "size": 481, "isInline": True},
             {"@odata.type": "#microsoft.graph.fileAttachment", "id": "2", "name": "screen.png", "contentType": "image/png", "size": 250000, "isInline": False},
             {"@odata.type": "#microsoft.graph.fileAttachment", "id": "3", "name": "pasted.png", "contentType": "image/png", "size": 180000, "isInline": True}]

    def handler(req):
        if "login" in str(req.url):
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(200, json={"value": items})
    p = MailboxPoller(agent=None, store=MemoryStore(), mailbox="eve@acme.example", app_id="a", app_password="p", tenant_id="t",
                      people=None, owner_email="x", http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    files, _ = await p.email_attachments("m1")
    assert [f["name"] for f in files] == ["screen.png", "pasted.png"]


async def test_an_image_pasted_into_teams_opens_although_it_has_no_name():
    """Teams sends a pasted image as contentType and name "image/*", served as application/octet-stream."""
    import httpx

    from agentcore.attachments import AttachmentTools, file_attachments, remember_attachments
    from agentcore.store import MemoryStore
    from agentcore.tools import CallContext
    pasted = {"contentType": "image/*", "name": "image/*",
              "contentUrl": "https://smba.trafficmanager.net/au/t1/v3/attachments/0-aue-d1/views/original"}
    store = MemoryStore()
    await remember_attachments(store, "chat-p", file_attachments({"attachments": [pasted]}))

    def handler(req):
        return httpx.Response(200, content=noisy_png(), headers={"content-type": "application/octet-stream"})

    async def bot_token():
        return "bot-token"
    tools = AttachmentTools(store, bot_token=bot_token, http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    ctx = CallContext("chat-p", "u", [])
    listed = json.loads(await tools.call_tool("list_attachments", {}, ctx))["attachments"]
    assert "/" not in listed[0]["name"]  # a usable name, not "image/*"
    for out in (await tools.call_tool("read_attachment", {}, ctx), await tools.call_tool("view_attachment", {}, ctx)):
        assert isinstance(out, dict) and out["images"][0][0] in ("png", "jpeg"), out
        assert out["images"][0][2].endswith(".png")
    by_name = await tools.call_tool("view_attachment", {"name": listed[0]["name"]}, ctx)
    assert isinstance(by_name, dict) and by_name["images"]


def test_file_type_is_sniffed_when_the_name_has_none():
    from agentcore.images import sniff_ext
    assert sniff_ext(noisy_png(20, 20)) == "png"
    assert sniff_ext(b"\xff\xd8\xff\xe0rest") == "jpg"
    assert sniff_ext(b"GIF89a....") == "gif"
    assert sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "webp"
    assert sniff_ext(b"%PDF-1.7") == "pdf"
    assert sniff_ext(b"\x00\x00\x00\x18ftypheic") == "heic"
    assert sniff_ext(b"hello") == ""
