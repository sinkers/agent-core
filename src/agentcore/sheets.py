"""Excel workbooks in the brand's style, and edits to any workbook (files.py `create_file`, `edit_file`).

Building (`xlsx_from_content`), from any of:
  - JSON: {"title"?: str, "sheets": [{"name", "title"?, "columns": [str | {"header", "width"?, "format"?}],
           "rows": [[...]], "totals"?: {"label"?: str, "sum": [column headers]}, "notes"?: [str]}]}
  - Markdown: one table per sheet, each under a "## Sheet name" heading (a single table needs no heading)
  - CSV text (one sheet)
Values: "=SUM(B2:B9)" stays a formula; "$1,250.50" becomes a number in currency format; "45%" a percentage;
"2026-10-14" or "14/10/2026" a date; "1,234" a number. Formats per column: currency, percent, date, integer,
number, text. A new workbook starts from the brand pack's Excel master (its theme colours and fonts), with its
"START HERE" guidance tab removed, and follows its rules: titles in the heading font, bold 14, body and labels in the
body font 12, numbers in the mono font 12. Header row in the primary colour with white text,
thin rules, frozen header, filters on, columns sized to their content, and the workbook set to recalculate when
it's opened.

Editing (`edit_xlsx`) uses openpyxl, which keeps values, formulas, styles, merged cells, filters and column widths
but DROPS charts, pivot tables, images, slicers and form controls when it saves. A workbook that has any of those
is refused unless the caller passes allow_losses (the result names what was lost).
"""

from __future__ import annotations

import io
import json
import re
from datetime import date, datetime
from .brandpack import brand

_P = brand().palette
PRIMARY, INK, PAGE, RULE, TINT = _P.primary, _P.ink, _P.page, _P.rule_doc or _P.rule, _P.tint
HEADING_FONT, BODY_FONT, MONO_FONT = brand().fonts.heading, brand().fonts.body, brand().fonts.mono
FORMATS = {"currency": '"$"#,##0.00;[Red]-"$"#,##0.00', "currency0": '"$"#,##0;[Red]-"$"#,##0',
           "percent": "0.0%", "date": "d mmm yyyy", "integer": "#,##0", "number": "#,##0.00", "text": "@"}
LOSSY_PARTS = {"xl/charts/": "charts", "xl/pivotTables/": "pivot tables", "xl/drawings/": "drawings or images",
               "xl/slicers/": "slicers", "xl/ctrlProps/": "form controls", "xl/media/": "images"}
_MONEY = re.compile(r"^\(?-?\$\s?-?[\d,]+(\.\d+)?\)?$")
_PCT = re.compile(r"^-?[\d.]+\s?%$")
_NUM = re.compile(r"^-?[\d,]*\.?\d+$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DMY = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")


def parse_value(v):
    """(value, number format or None) for a cell written as text."""
    if not isinstance(v, str):
        return v, None
    s = v.strip()
    if not s:
        return None, None
    if s.startswith("="):
        return s, None
    if _MONEY.match(s):
        neg = s.startswith("(") or "-" in s
        n = float(re.sub(r"[^\d.]", "", s))
        return (-n if neg else n), FORMATS["currency" if "." in s else "currency0"]
    if _PCT.match(s):
        return float(s.rstrip("% ").strip()) / 100, FORMATS["percent"]
    if _ISO.match(s):
        try:
            return datetime.strptime(s, "%Y-%m-%d").date(), FORMATS["date"]
        except ValueError:
            return s, None
    if _DMY.match(s):
        try:
            return datetime.strptime(s, "%d/%m/%Y").date(), FORMATS["date"]
        except ValueError:
            return s, None
    if _NUM.match(s) and not (len(s) > 1 and s.startswith("0") and not s.startswith("0.")):  # keep "007" as text
        n = float(s.replace(",", ""))
        return (int(n) if n.is_integer() and "." not in s else n), (FORMATS["integer"] if "," in s else None)
    return s, None


# ---- reading what the agent wrote ------------------------------------------------------------------------
def _md_cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def spec_from_content(content: str) -> dict:
    text = (content or "").strip()
    if text.startswith("{"):
        try:
            spec = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"the workbook JSON doesn't parse: {exc}") from None
        if not spec.get("sheets"):
            raise ValueError('the workbook JSON needs "sheets": [{"name", "columns", "rows"}]')
        return spec
    lines = text.splitlines()
    if any(ln.strip().startswith("|") for ln in lines):
        sheets, name, title = [], None, None
        i = 0
        while i < len(lines):
            ln = lines[i].strip()
            if m := re.match(r"^#\s+(.+)$", ln):
                title = m.group(1).strip()
            elif m := re.match(r"^#{2,}\s+(.+)$", ln):
                name = m.group(1).strip()
            elif ln.startswith("|"):
                block = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    if not re.match(r"^\s*\|?\s*:?-{2,}", lines[i]):
                        block.append(_md_cells(lines[i]))
                    i += 1
                sheets.append({"name": name or f"Sheet{len(sheets) + 1}", "columns": block[0], "rows": block[1:]})
                name = None
                continue
            i += 1
        return {"title": title, "sheets": sheets}
    import csv
    rows = [r for r in csv.reader(io.StringIO(text))]
    if not rows:
        raise ValueError("an .xlsx needs a table: a Markdown table, CSV lines, or the JSON workbook spec")
    return {"sheets": [{"name": "Sheet1", "columns": rows[0], "rows": rows[1:]}]}


# ---- building ---------------------------------------------------------------------------------------------
def _sheet_name(name: str, used: set) -> str:
    clean = re.sub(r"[\[\]:*?/\\]", " ", name or "Sheet").strip()[:31] or "Sheet"
    base, n = clean, 2
    while clean.lower() in used:
        clean = f"{base[:28]} {n}"
        n += 1
    used.add(clean.lower())
    return clean


def master_path():
    """The brand's Excel master (its theme), or a path that doesn't exist."""
    from pathlib import Path
    return brand().file(brand().templates.excel) or Path("/nonexistent/master.xlsx")


def xlsx_from_content(content: str) -> bytes:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter

    spec = spec_from_content(content)
    master = master_path()
    wb = load_workbook(master) if master.exists() else Workbook()  # the master carries the brand theme
    template_sheets = list(wb.worksheets)
    used: set = set()
    head_font = Font(name=BODY_FONT, size=12, bold=True, color="FFFFFF")
    head_fill = PatternFill("solid", fgColor=PRIMARY)
    body_font = Font(name=BODY_FONT, size=12, color=INK)
    number_font = Font(name=MONO_FONT, size=12, color=INK)
    rule = Border(bottom=Side(style="thin", color=RULE))
    for sheet in spec["sheets"]:
        ws = wb.create_sheet(_sheet_name(sheet.get("name") or "Sheet", used))
        cols = [c if isinstance(c, dict) else {"header": str(c)} for c in sheet.get("columns") or []]
        if not cols:
            raise ValueError(f"sheet {ws.title!r} has no columns")
        top = 1
        title = sheet.get("title") or (spec.get("title") if len(spec["sheets"]) == 1 else None)
        if title:
            ws.cell(1, 1, title).font = Font(name=HEADING_FONT, size=14, bold=True, color=INK)
            top = 3
        for j, col in enumerate(cols, 1):
            c = ws.cell(top, j, col.get("header", ""))
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        widths = [len(str(col.get("header", ""))) for col in cols]
        rows = sheet.get("rows") or []
        for i, row in enumerate(rows, top + 1):
            for j, raw in enumerate(list(row)[:len(cols)], 1):
                value, fmt = parse_value(raw)
                c = ws.cell(i, j, value)
                numeric = isinstance(value, (int, float)) and not isinstance(value, bool) or \
                    (isinstance(value, str) and value.startswith("="))
                c.font, c.border = (number_font if numeric else body_font), rule
                want = cols[j - 1].get("format")
                if want in FORMATS:
                    c.number_format = FORMATS[want]
                elif fmt:
                    c.number_format = fmt
                widths[j - 1] = max(widths[j - 1], min(len(str(raw)), 60))
        last = top + len(rows)
        totals = sheet.get("totals")
        if totals and rows:
            t = last + 1
            ws.cell(t, 1, totals.get("label", "Total")).font = Font(name=BODY_FONT, size=12, bold=True, color=INK)
            for j, col in enumerate(cols, 1):
                if col.get("header") in (totals.get("sum") or []):
                    letter = get_column_letter(j)
                    c = ws.cell(t, j, f"=SUM({letter}{top + 1}:{letter}{last})")
                    c.font = Font(name=MONO_FONT, size=12, bold=True, color=INK)
                    c.number_format = ws.cell(last, j).number_format
                    c.border = Border(top=Side(style="thin", color=PRIMARY))
        for j, width in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(j)].width = cols[j - 1].get("width") or min(max(11, width + 5), 70)
        ws.freeze_panes = ws.cell(top + 1, 1)
        if rows:
            ws.auto_filter.ref = f"A{top}:{get_column_letter(len(cols))}{last}"
        for k, note in enumerate(sheet.get("notes") or [], start=(last + (3 if totals else 2))):
            ws.cell(k, 1, note).font = Font(name=BODY_FONT, size=10, italic=True, color="8A847A")
        ws.sheet_view.showGridLines = False
    for old in template_sheets:  # the master's START HERE guidance and empty tab never go out
        wb.remove(old)
    wb.active = 0
    wb.calculation.fullCalcOnLoad = True  # Excel works out every formula when it opens the file
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---- outline and editing --------------------------------------------------------------------------------
def lossy_parts(data: bytes) -> list[str]:
    import zipfile
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    return sorted({what for prefix, what in LOSSY_PARTS.items() if any(n.startswith(prefix) for n in names)})


def outline(data: bytes, max_rows: int = 8) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data))  # formulas as written
    sheets = []
    for ws in wb.worksheets:
        sample = []
        for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, max_rows), values_only=True):
            sample.append([("" if v is None else str(v))[:40] for v in row[:12]])
        sheets.append({"sheet": ws.title, "rows": ws.max_row, "columns": ws.max_column,
                       "range": ws.dimensions, "first_rows": sample})
    return {"kind": "Excel workbook", "sheets": sheets, "would_lose_on_edit": lossy_parts(data)}


def _ws(wb, name):
    if not name:
        return wb.active
    for ws in wb.worksheets:
        if ws.title.lower() == str(name).lower():
            return ws
    raise ValueError(f"no sheet {name!r}. Sheets: {', '.join(ws.title for ws in wb.worksheets)}")


def _put(ws, ref: str, raw):
    from copy import copy
    value, fmt = parse_value(raw)
    cell = ws[ref]
    cell.value = value
    if fmt and cell.number_format in ("General", None):
        cell.number_format = fmt
    return cell


def edit_xlsx(data: bytes, ops: list[dict], allow_losses: bool = False) -> tuple[bytes, list[str]]:
    from copy import copy

    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    lost = lossy_parts(data)
    if lost and not allow_losses:
        raise ValueError(f"this workbook has {', '.join(lost)}, which my spreadsheet library drops when it saves. "
                         "Ask the person whether that's acceptable (then pass allow_losses), or describe the change "
                         "for them to make in Excel")
    wb = load_workbook(io.BytesIO(data))
    done: list[str] = []
    for n, op in enumerate(ops or [], 1):
        kind = (op.get("op") or "").strip()
        if kind == "set_cells":
            ws = _ws(wb, op.get("sheet"))
            cells = op.get("cells") or {}
            if not cells:
                raise ValueError(f"edit {n}: set_cells needs `cells`, e.g. {{\"B4\": 1200, \"C4\": \"=B4*1.1\"}}")
            for ref, raw in cells.items():
                if not re.fullmatch(r"[A-Za-z]{1,3}\d{1,7}", ref):
                    raise ValueError(f"edit {n}: {ref!r} isn't a cell reference like B4")
                _put(ws, ref.upper(), raw)
            done.append(f"{ws.title}: set {len(cells)} cell(s) ({', '.join(list(cells)[:6])})")
        elif kind in ("append_rows", "insert_rows"):
            ws = _ws(wb, op.get("sheet"))
            rows = op.get("rows") or []
            at = int(op.get("at") or 0) if kind == "insert_rows" else ws.max_row + 1
            if kind == "insert_rows":
                if not 1 <= at <= ws.max_row + 1:
                    raise ValueError(f"edit {n}: `at` must be a row from 1 to {ws.max_row + 1}")
                ws.insert_rows(at, len(rows))
            style_row = max(at - 1, 1)
            for k, row in enumerate(rows):
                for j, raw in enumerate(row, 1):
                    c = _put(ws, f"{get_column_letter(j)}{at + k}", raw)
                    src = ws.cell(style_row, j)
                    if src.has_style and style_row > 1:
                        c.font, c.border, c.alignment = copy(src.font), copy(src.border), copy(src.alignment)
                        if c.number_format == "General":
                            c.number_format = src.number_format
            if ws.auto_filter.ref and kind == "append_rows":
                start = ws.auto_filter.ref.split(":")[0]
                ws.auto_filter.ref = f"{start}:{get_column_letter(ws.max_column)}{ws.max_row}"
            done.append(f"{ws.title}: {'inserted' if kind == 'insert_rows' else 'added'} {len(rows)} row(s) at row {at}")
            if kind == "insert_rows":
                done.append("note: formulas elsewhere that point below the inserted rows aren't shifted; check them")
        elif kind == "delete_rows":
            ws = _ws(wb, op.get("sheet"))
            start, count = int(op.get("from") or 0), int(op.get("count") or 1)
            if not 1 <= start <= ws.max_row:
                raise ValueError(f"edit {n}: `from` must be a row from 1 to {ws.max_row}")
            ws.delete_rows(start, count)
            done.append(f"{ws.title}: deleted {count} row(s) from row {start}")
        elif kind == "add_sheet":
            used = {ws.title.lower() for ws in wb.worksheets}
            ws = wb.create_sheet(_sheet_name(op.get("name") or "Sheet", used))
            for row in op.get("rows") or []:
                ws.append([parse_value(v)[0] for v in row])
            done.append(f"added sheet {ws.title!r}")
        elif kind == "rename_sheet":
            ws = _ws(wb, op.get("sheet"))
            old, ws.title = ws.title, _sheet_name(op.get("to") or "", {s.title.lower() for s in wb.worksheets})
            done.append(f"renamed sheet {old!r} to {ws.title!r}")
        elif kind == "set_column_width":
            ws = _ws(wb, op.get("sheet"))
            ws.column_dimensions[str(op.get("column", "A")).upper()].width = float(op.get("width") or 12)
            done.append(f"{ws.title}: column {str(op.get('column', 'A')).upper()} width {op.get('width')}")
        else:
            raise ValueError(f"edit {n}: unknown op {kind!r}. Ops: set_cells, append_rows, insert_rows, delete_rows, "
                             f"add_sheet, rename_sheet, set_column_width")
    wb.calculation.fullCalcOnLoad = True
    buf = io.BytesIO()
    wb.save(buf)
    if lost:
        done.append(f"dropped from the copy: {', '.join(lost)}")
    return buf.getvalue(), done
