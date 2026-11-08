"""Markdown-ish text -> Atlassian Document Format (post-to-jira skill, section 1-2).

Jira isn't markdown: whatever isn't converted shows as literal characters (a table became one paragraph of
pipes). Handles what the agents' comments use: paragraphs (a single newline is a line break), `#`
headings, `- ` / `* ` bullets, `1. ` numbered lists, `- [ ]` / `- [x]` checklists (taskList, the case generic
converters break), `| a | b |` tables (a `|---|` row after the first makes it the header), `---` rules,
`> ` quotes, `> [!info]` panels (info, note, warning, error, success), fenced
code blocks, **bold**, *italic* / _italic_, ~~strike~~, `code`, [text](url) links and
bare URLs. Anything else stays literal text.
"""

from __future__ import annotations

import re
import uuid

INLINE_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)|\*\*(.+?)\*\*|`([^`]+)`|(https?://[^\s)|]+)"
                       r"|~~(.+?)~~|(?<![\w*])\*(?!\s)([^*]+?)(?<!\s)\*(?![\w*])|(?<![\w_])_(?!\s)([^_]+?)(?<!\s)_(?![\w_])")
TASK_RE = re.compile(r"^\s*[-*]\s+\[( |x|X)\]\s+(.*)$")
BULLET_RE = re.compile(r"^\s*[-*•]\s+(.*)$")
ORDERED_RE = re.compile(r"^\s*\d+[.)]\s+(.*)$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_RE = re.compile(r"^\s*\|.*\|\s*$")
SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
RULE_RE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
PANEL_RE = re.compile(r"^\[!(info|note|warning|error|success|tip|caution|important)\]\s*(.*)$", re.I)
PANEL_TYPES = {"tip": "success", "caution": "warning", "important": "note"}
FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")


def inline(text: str, marks: list[dict] | None = None) -> list[dict]:
    """Inline nodes for one line; nested marks (bold around a link) are kept."""
    marks = marks or []
    nodes: list[dict] = []
    pos = 0

    def plain(t: str):
        if t:
            nodes.append({"type": "text", "text": t, **({"marks": marks} if marks else {})})

    for m in INLINE_RE.finditer(text):
        plain(text[pos:m.start()])
        label, href, bold, code, bare, strike, star_em, under_em = m.groups()
        if href:
            nodes += inline(label, marks + [{"type": "link", "attrs": {"href": href}}])
        elif bold:
            nodes += inline(bold, marks + [{"type": "strong"}])
        elif code:
            nodes.append({"type": "text", "text": code, "marks": [{"type": "code"}]})
        elif bare:
            nodes.append({"type": "text", "text": bare, "marks": marks + [{"type": "link", "attrs": {"href": bare}}]})
        elif strike:
            nodes += inline(strike, marks + [{"type": "strike"}])
        else:
            nodes += inline(star_em or under_em, marks + [{"type": "em"}])
        pos = m.end()
    plain(text[pos:])
    return nodes or [{"type": "text", "text": " "}]


def _cells(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|") and not line.endswith("\\|"):
        line = line[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", line)]


def _table(rows: list[str]) -> dict:
    header = len(rows) > 1 and bool(SEPARATOR_RE.match(rows[1]))
    body = [r for i, r in enumerate(rows) if not (header and i == 1)]
    grid = [_cells(r) for r in body]
    width = max(len(r) for r in grid)
    out = []
    for i, cells in enumerate(grid):
        kind = "tableHeader" if header and i == 0 else "tableCell"
        cells = cells + [""] * (width - len(cells))
        out.append({"type": "tableRow", "content": [
            {"type": kind, "content": [{"type": "paragraph", "content": inline(c) if c else []}]} for c in cells]})
    return {"type": "table", "attrs": {"isNumberColumnEnabled": False, "layout": "default"}, "content": out}


def to_adf(markdown: str) -> dict:
    content: list[dict] = []
    para: list[str] = []
    quote: list[str] = []
    table: list[str] = []
    lst: dict | None = None
    code: list[str] | None = None
    code_lang = ""

    def flush_para():
        nonlocal para
        if para:
            nodes: list[dict] = []
            for i, line in enumerate(para):  # a single newline is a line break, as people mean it in a comment
                if i:
                    nodes.append({"type": "hardBreak"})
                nodes += inline(line)
            content.append({"type": "paragraph", "content": nodes})
            para = []

    def flush_list():
        nonlocal lst
        if lst:
            content.append(lst)
            lst = None

    def flush_quote():
        nonlocal quote
        if quote:
            panel = PANEL_RE.match(quote[0].strip())
            lines = ([panel.group(2)] if panel.group(2) else []) + quote[1:] if panel else quote
            inner = to_adf("\n".join(lines))["content"]
            allowed = ("paragraph", "bulletList", "orderedList", "heading", "taskList", "rule", "codeBlock") if panel \
                else ("paragraph", "bulletList", "orderedList", "codeBlock")
            body = [n for n in inner if n["type"] in allowed] or [{"type": "paragraph", "content": []}]
            if panel:
                kind = panel.group(1).lower()
                content.append({"type": "panel", "attrs": {"panelType": PANEL_TYPES.get(kind, kind)}, "content": body})
            else:
                content.append({"type": "blockquote", "content": body})
            quote = []

    def flush_table():
        nonlocal table
        if table:
            content.append(_table(table))
            table = []

    def flush_all():
        flush_para(); flush_list(); flush_quote(); flush_table()

    for raw in markdown.splitlines():
        line = raw.rstrip()
        if code is not None:  # inside a fenced block: everything is literal until the closing fence
            if FENCE_RE.match(line):
                content.append({"type": "codeBlock", **({"attrs": {"language": code_lang}} if code_lang else {}),
                                "content": [{"type": "text", "text": "\n".join(code)}] if code else []})
                code = None
            else:
                code.append(raw)
            continue
        if m := FENCE_RE.match(line):
            flush_all()
            code, code_lang = [], m.group(1)
            continue
        if not line.strip():
            flush_all()
            continue
        if TABLE_RE.match(line) or (table and SEPARATOR_RE.match(line)):
            if not table:
                flush_para(); flush_list(); flush_quote()
            table.append(line)
            continue
        flush_table()
        if m := QUOTE_RE.match(line):
            flush_para(); flush_list()
            quote.append(m.group(1))
            continue
        flush_quote()
        if RULE_RE.match(line):
            flush_para(); flush_list()
            content.append({"type": "rule"})
        elif m := TASK_RE.match(line):
            flush_para()
            if not lst or lst["type"] != "taskList":
                flush_list()
                lst = {"type": "taskList", "attrs": {"localId": uuid.uuid4().hex[:12]}, "content": []}
            lst["content"].append({"type": "taskItem",  # taskItem holds inline nodes directly, no paragraph
                                   "attrs": {"localId": uuid.uuid4().hex[:12], "state": "DONE" if m.group(1).lower() == "x" else "TODO"},
                                   "content": inline(m.group(2))})
        elif (m := BULLET_RE.match(line)) or (m2 := ORDERED_RE.match(line)):
            flush_para()
            kind = "bulletList" if m else "orderedList"
            text = (m or m2).group(1)
            if not lst or lst["type"] != kind:
                flush_list()
                lst = {"type": kind, "content": []}
            lst["content"].append({"type": "listItem", "content": [{"type": "paragraph", "content": inline(text)}]})
        elif m := HEADING_RE.match(line):
            flush_para(); flush_list()
            content.append({"type": "heading", "attrs": {"level": min(len(m.group(1)), 6)}, "content": inline(m.group(2))})
        else:
            flush_list()
            para.append(line.strip())
    if code is not None:  # an unclosed fence: keep what was written
        content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(code)}] if code else []})
    flush_all()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


def to_text(node: dict | list | None) -> str:
    """ADF -> plain text for the model (lists as '- ', checklists as '[ ]'/'[x]', links as 'text <url>')."""
    if node is None:
        return ""
    if isinstance(node, list):
        return "".join(to_text(n) for n in node)
    t = node.get("type")
    if t == "text":
        text = node.get("text", "")
        for mark in node.get("marks", []):
            if mark.get("type") == "link":
                href = mark.get("attrs", {}).get("href", "")
                if href and href != text:
                    text = f"{text} <{href}>"
        return text
    if t in ("inlineCard", "blockCard"):
        return node.get("attrs", {}).get("url", "")
    if t == "mention":
        return node.get("attrs", {}).get("text", "@user")
    if t == "hardBreak":
        return "\n"
    if t == "media":  # a file or image pasted into the description or a comment; the bytes are an issue attachment
        name = node.get("attrs", {}).get("alt") or ""
        return f"[attachment: {name}]" if name else "[attachment]"
    if t == "rule":
        return "---\n"
    if t == "tableRow":
        return "| " + " | ".join(to_text(c.get("content")).strip().replace("\n", " ") for c in node.get("content", [])) + " |\n"
    if t == "table":
        return "".join(to_text(r) for r in node.get("content", [])) + "\n"
    inner = to_text(node.get("content"))
    if t == "panel":
        return f"[{node.get('attrs', {}).get('panelType', 'info')}] {inner.strip()}\n"
    if t == "blockquote":
        return "".join(f"> {l}\n" for l in inner.strip().splitlines())
    if t == "codeBlock":
        return f"```\n{inner}\n```\n"
    if t == "listItem":
        return f"- {inner.strip()}\n"
    if t == "taskItem":
        return f"[{'x' if node.get('attrs', {}).get('state') == 'DONE' else ' '}] {inner.strip()}\n"
    if t in ("paragraph", "heading"):
        return inner + "\n"
    return inner
