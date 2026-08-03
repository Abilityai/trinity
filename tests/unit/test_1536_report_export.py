"""Report export to .xlsx / .pdf (#1536).

The builders are exercised against the REAL libraries and the output is read
back — a mocked workbook would assert that we called a method, not that a value
landed in the right cell, which is the entire question an export feature raises.

Locked here:
  * a `table` payload becomes real cells, with both row encodings (positional
    list and column-keyed object) landing in the same columns
  * `kpi` / `timeline` project to sensible sheets instead of erroring
  * an unmappable payload degrades to JSON in a cell — never a 500
  * a title that Excel would reject (>31 chars, `[]:*?/\\`) does not raise
  * agent-authored content is escaped before reportlab parses it as markup
  * the export route reuses the detail route's 404-not-403, and answers 503 —
    not 500 — when the libraries are missing (#1814 upgrade path)
"""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

openpyxl = pytest.importorskip("openpyxl", reason="export deps not installed")


def _export():
    try:
        from services import report_export
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return report_export


def _sheet(data: bytes):
    return openpyxl.load_workbook(io.BytesIO(data), read_only=True).active


def _rows(data: bytes):
    return [list(r) for r in _sheet(data).iter_rows(values_only=True)]


# ---------------------------------------------------------------------------
# xlsx
# ---------------------------------------------------------------------------

def test_table_payload_becomes_real_cells():
    x = _export()
    payload = {"columns": ["id", "score"], "rows": [["lead-1", 7], ["lead-2", 14]]}
    rows = _rows(x.build_xlsx(payload, "table", "Leads"))
    assert rows[0] == ["id", "score"]
    assert rows[1] == ["lead-1", 7]
    assert rows[2][1] == 14  # a number stays a number, not "14"


def test_object_rows_land_in_the_declared_column_order():
    """`ReportTable.vue` accepts column-keyed rows; an export that ignored the
    key mapping would silently transpose data."""
    x = _export()
    payload = {
        "columns": ["id", "score"],
        "rows": [{"score": 7, "id": "lead-1"}],  # deliberately reversed
    }
    rows = _rows(x.build_xlsx(payload, "table", "Leads"))
    assert rows[1] == ["lead-1", 7]


def test_kpi_payload_projects_to_label_value_unit():
    x = _export()
    payload = {"tiles": [{"label": "Leads", "value": 14, "unit": "new"}]}
    rows = _rows(x.build_xlsx(payload, "kpi", "Week"))
    assert rows[0] == ["label", "value", "unit"]
    assert rows[1] == ["Leads", 14, "new"]


def test_timeline_payload_projects_to_event_columns():
    x = _export()
    payload = {"events": [{"ts": "2026-07-28T09:00:00Z", "label": "Deal closed", "detail": "ACME"}]}
    rows = _rows(x.build_xlsx(payload, "timeline", "Timeline"))
    assert rows[0] == ["ts", "label", "detail"]
    assert rows[1][1] == "Deal closed"


def test_unmappable_payload_degrades_to_json_not_an_error():
    x = _export()
    rows = _rows(x.build_xlsx({"something": {"deeply": ["odd"]}}, None, "Odd"))
    assert rows[0] == [x.JSON_FALLBACK_HEADER]
    assert "deeply" in rows[1][0]


def test_non_dict_payload_still_exports():
    x = _export()
    rows = _rows(x.build_xlsx([1, 2, 3], None, "List"))
    assert "1" in rows[1][0]


def test_nested_cell_value_is_serialized_rather_than_raising():
    """openpyxl raises on an unsupported cell type; a report can legitimately
    carry a dict inside a row."""
    x = _export()
    payload = {"columns": ["id", "meta"], "rows": [["a", {"k": "v"}]]}
    rows = _rows(x.build_xlsx(payload, "table", "T"))
    assert rows[1][1] == '{"k": "v"}'


def test_title_that_excel_would_reject_does_not_raise():
    x = _export()
    data = x.build_xlsx({"columns": ["a"], "rows": [[1]]}, "table", "A/B:C*D?[very long title]" * 3)
    ws = _sheet(data)
    assert len(ws.title) <= 31
    assert not set(ws.title) & set("[]:*?/\\")


def test_empty_table_exports_headers_only():
    x = _export()
    rows = _rows(x.build_xlsx({"columns": ["id"], "rows": []}, "table", "Empty"))
    assert rows == [["id"]]


# ---------------------------------------------------------------------------
# pdf
# ---------------------------------------------------------------------------

def test_pdf_is_a_pdf_and_contains_content():
    pytest.importorskip("reportlab", reason="export deps not installed")
    x = _export()
    data = x.build_pdf({"columns": ["id"], "rows": [["lead-1"]]}, "table", "Leads")
    assert data.startswith(b"%PDF-")
    assert len(data) > 500


def test_pdf_escapes_agent_authored_markup():
    """reportlab parses a mini-HTML dialect, so an agent-authored `<b>` (or a
    malformed tag) would reflow — or crash — the document."""
    pytest.importorskip("reportlab", reason="export deps not installed")
    x = _export()
    data = x.build_pdf({"markdown": "Alert <b>unclosed <tag"}, "markdown", "T")
    assert data.startswith(b"%PDF-")


def test_pdf_row_cap_is_reported_not_silent():
    """Asserting on rendered bytes proves nothing — reportlab compresses content
    streams — so the truncation decision is tested where it is made."""
    x = _export()
    small = [[f"lead-{i}"] for i in range(10)]
    assert x.cap_rows_for_pdf(small) == (small, None)

    big = [[f"lead-{i}"] for i in range(x.PDF_MAX_ROWS + 500)]
    capped, note = x.cap_rows_for_pdf(big)
    assert len(capped) == x.PDF_MAX_ROWS
    assert note and "xlsx" in note and f"{x.PDF_MAX_ROWS:,}" in note


def test_pdf_of_a_large_table_still_builds():
    pytest.importorskip("reportlab", reason="export deps not installed")
    x = _export()
    rows = [[f"lead-{i}", i] for i in range(x.PDF_MAX_ROWS + 500)]
    data = x.build_pdf({"columns": ["id", "score"], "rows": rows}, "table", "Big")
    assert data.startswith(b"%PDF-")


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------

def _user(username="admin"):
    return SimpleNamespace(id=1, username=username, agent_name=None)


def _report(agent="scout"):
    return {
        "id": "r1",
        "agent_name": agent,
        "title": "Leads",
        "display_hint": "table",
        "payload": {"columns": ["id"], "rows": [["lead-1"]]},
    }


def _reports_module():
    try:
        from routers import reports
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")
    return reports


def test_export_streams_a_spreadsheet_with_a_download_filename(monkeypatch):
    reports = _reports_module()
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _report())
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    resp = asyncio.run(reports.export_report(report_id="r1", format="xlsx", current_user=_user()))
    assert resp.status_code == 200
    assert "spreadsheetml" in resp.media_type
    assert 'attachment; filename="Leads-r1.xlsx"' in resp.headers["content-disposition"]
    assert resp.headers["x-content-type-options"] == "nosniff"


def test_export_is_404_for_a_report_the_caller_cannot_reach(monkeypatch):
    """Same shape as the detail route — an export URL must not become the
    existence oracle that route refuses to be."""
    from fastapi import HTTPException

    reports = _reports_module()
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _report())
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: False)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(reports.export_report(report_id="r1", format="xlsx", current_user=_user()))
    assert exc.value.status_code == 404


def test_missing_export_library_is_503_not_500(monkeypatch):
    """The libraries are pinned in the image, but an instance that upgrades code
    without rebuilding (#1814) must get a rebuild hint, not a stack trace."""
    from fastapi import HTTPException

    reports = _reports_module()
    x = _export()
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _report())
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    def _boom(*a, **k):
        raise x.ExportUnavailable("openpyxl is not installed")

    monkeypatch.setattr(reports.report_export, "build_xlsx", _boom)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(reports.export_report(report_id="r1", format="xlsx", current_user=_user()))
    assert exc.value.status_code == 503
    assert "Rebuild the backend image" in exc.value.detail


def test_export_filename_strips_content_disposition_breakers():
    reports = _reports_module()
    name = reports._export_filename('Q3 "leads"\nreport; drop', "abcdef1234", "xlsx")
    assert '"' not in name and "\n" not in name and ";" not in name
    assert name.endswith(".xlsx")
