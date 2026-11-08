"""Excel workbooks in Acme's style, and edits to any workbook (sheets.py)."""
import io
import json
import zipfile
from datetime import date

import pytest
from openpyxl import Workbook, load_workbook

from agentcore.filemaker import make_file
from agentcore.sheets import edit_xlsx, outline, parse_value, xlsx_from_content

BUDGET = """# PRJ budget

## Summary

| Line | Budget | Spent | Share |
|---|---|---|---|
| Transformers | $410,000 | $120,500.50 | 29% |
| PTUs | $2,400,000 | 0 | 0% |
| Total | =SUM(B4:B5) | =SUM(C4:C5) | |

## Dates

| Milestone | Date |
|---|---|
| Energisation | 2027-06-17 |
"""


def test_values_become_numbers_dates_and_formulas():
    assert parse_value("$1,250.50") == (1250.5, '"$"#,##0.00;[Red]-"$"#,##0.00')
    assert parse_value("45%")[0] == pytest.approx(0.45)
    assert parse_value("2026-10-14") == (date(2026, 10, 14), "d mmm yyyy")
    assert parse_value("14/10/2026")[0] == date(2026, 10, 14)
    assert parse_value("=SUM(A1:A3)") == ("=SUM(A1:A3)", None)
    assert parse_value("1,234") == (1234, "#,##0") and parse_value("007") == ("007", None)


def test_edits_change_cells_rows_and_sheets():
    data = make_file("budget.xlsx", BUDGET)
    new, done = edit_xlsx(data, [
        {"op": "set_cells", "sheet": "Summary", "cells": {"C3": "$50,000", "E1": "Note"}},
        {"op": "append_rows", "sheet": "Summary", "rows": [["UPS", "$90,000", "0", "0%"]]},
        {"op": "add_sheet", "name": "Risks", "rows": [["Risk", "Owner"], ["Grid date", "AS"]]},
        {"op": "rename_sheet", "sheet": "Dates", "to": "Milestones"},
        {"op": "delete_rows", "sheet": "Milestones", "from": 2, "count": 1}])
    wb = load_workbook(io.BytesIO(new))
    ws = wb["Summary"]
    assert ws["C3"].value == 50000 and ws["E1"].value == "Note" and ws["A5"].value == "UPS"
    assert ws["A5"].font.name == "Calibri"  # the new row takes the row above's style
    assert wb.sheetnames == ["Summary", "Milestones", "Risks"] and wb["Milestones"].max_row == 1
    assert len(done) == 5
    with pytest.raises(ValueError, match="Sheets: Summary"):
        edit_xlsx(data, [{"op": "set_cells", "sheet": "Nope", "cells": {"A1": 1}}])
    with pytest.raises(ValueError, match="unknown op"):
        edit_xlsx(data, [{"op": "pivot"}])


def test_a_workbook_with_charts_is_refused_unless_losses_are_allowed():
    from openpyxl.chart import BarChart, Reference
    wb = Workbook()
    ws = wb.active
    for row in [["a", 1], ["b", 2]]:
        ws.append(row)
    chart = BarChart()
    chart.add_data(Reference(ws, min_col=2, min_row=1, max_row=2))
    ws.add_chart(chart, "D2")
    buf = io.BytesIO()
    wb.save(buf)
    data = buf.getvalue()
    assert any(n.startswith("xl/charts/") for n in zipfile.ZipFile(io.BytesIO(data)).namelist())
    assert "charts" in outline(data)["would_lose_on_edit"]
    with pytest.raises(ValueError, match="charts"):
        edit_xlsx(data, [{"op": "set_cells", "cells": {"A1": "x"}}])
    _, done = edit_xlsx(data, [{"op": "set_cells", "cells": {"A1": "x"}}], allow_losses=True)
    assert "dropped" in done[-1]


def test_outline_shows_sheets_and_first_rows():
    o = outline(make_file("budget.xlsx", BUDGET))
    assert [s["sheet"] for s in o["sheets"]] == ["Summary", "Dates"] and o["sheets"][0]["first_rows"][0][0] == "Line"
