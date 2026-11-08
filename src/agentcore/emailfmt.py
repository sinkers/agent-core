"""Rich-text email: the agents write Markdown, recipients get formatted HTML.

Headings, bold and italics, bullet and numbered lists, links, tables and code are kept; raw HTML in the text is
escaped first, so nothing pasted from a document or tool result can add scripts, styles or tracking images. Styles
are inline because Outlook ignores <style> blocks.
"""

from __future__ import annotations

import html
import re

import markdown as md

FONT = "font-family:Segoe UI,Calibri,Arial,sans-serif;font-size:11pt;line-height:1.45;color:#1f2328"
_AUTOLINK = re.compile(r"<((?:https?://|mailto:)[^\s<>]+|[^\s<>@]+@[^\s<>@]+\.[^\s<>@]+)>")
_STYLES = {
    "table": "border-collapse:collapse;margin:8px 0",
    "th": "border:1px solid #d0d7de;padding:4px 8px;background:#f6f8fa;text-align:left",
    "td": "border:1px solid #d0d7de;padding:4px 8px;vertical-align:top",
    "code": "font-family:Consolas,monospace;background:#f6f8fa;padding:1px 4px",
    "h1": "font-size:16pt;margin:12px 0 6px", "h2": "font-size:14pt;margin:12px 0 6px",
    "h3": "font-size:12pt;margin:10px 0 4px", "p": "margin:0 0 8px",
    "ul": "margin:0 0 8px 20px;padding:0", "ol": "margin:0 0 8px 20px;padding:0",
    "a": "color:#1B4B8A",
}


def _escape_raw_html(text: str) -> str:
    """Escape < and > except Markdown autolinks (<https://…>, <name@domain>)."""
    keep: list[str] = []

    def stash(m: re.Match) -> str:
        keep.append(m.group(0))
        return f"\x00{len(keep) - 1}\x00"

    text = _AUTOLINK.sub(stash, text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def markdown_to_html(text: str, footer: str | None = None) -> str:
    body = md.markdown(_escape_raw_html((text or "").strip()), extensions=["tables", "sane_lists", "fenced_code"],
                       output_format="html")
    for tag, style in _STYLES.items():
        body = re.sub(rf"<{tag}(?=[\s>])", f'<{tag} style="{style}"', body)
    # No remote images (they can be tracking pixels): an image becomes a link to it.
    def image_to_link(m: re.Match) -> str:
        src = re.search(r'src="([^"]*)"', m.group(0))
        alt = re.search(r'alt="([^"]*)"', m.group(0))
        return f'<a href="{src.group(1) if src else "#"}">{(alt.group(1) if alt else "") or "image"}</a>'
    body = re.sub(r"<img\b[^>]*>", image_to_link, body)
    # Only web and mail links: a [link](javascript:…) written by the model, or pasted from a document, is dropped.
    body = re.sub(r'href="(?!https?://|mailto:)[^"]*"', 'href="#"', body)
    if footer:
        body += f'<p style="margin:12px 0 0;color:#57606a;font-size:9pt"><em>{html.escape(footer)}</em></p>'
    return f'<div style="{FONT}">{body}</div>'
