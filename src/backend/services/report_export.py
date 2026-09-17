"""Report export — render a stored report as .xlsx or .pdf (#1536).

Pure builders: `(payload, display_hint, title) -> bytes`. No DB, no HTTP, no
access control — the router owns all three, so these stay unit-testable against
real library output rather than a mock that would prove nothing about whether a
cell landed in the right column.

Two decisions worth knowing:

* **The libraries are imported lazily**, inside the builders. They are pinned in
  the backend image, but an instance that pulls new code without rebuilding
  (#1814 — `start.sh` does not rebuild on an in-place upgrade) would otherwise
  fail at module import and take the whole reports router down with it. Lazy
  import turns that into one endpoint answering 503 with a rebuild hint.
* **Shape mismatch degrades, never 500s.** A `kpi` payload asked for as a
  spreadsheet is not an error; it is a two-column sheet. Anything unrecognized
  falls back to pretty-printed JSON in a single cell / a preformatted PDF block,
  because a stakeholder holding a slightly ugly file is better served than one
  holding a stack trace.
"""
from __future__ import annotations

import io
import json
from typing import Any, Dict, List, Optional, Tuple

# Sheet/marker text kept here so the router and tests agree on it.
FALLBACK_SHEET_TITLE = "Report"
JSON_FALLBACK_HEADER = "payload (JSON)"


class ExportUnavailable(RuntimeError):
    """An export library is not installed in this image (#1814 upgrade path)."""


def _rows_for_table(payload: Dict[str, Any]) -> Tuple[List[str], List[List[Any]]]:
    """`{columns, rows}` → header + normalized rows.

    A row may be a list (positional) or an object keyed by column — the same
    duality `ReportTable.vue` renders, mirrored here so an exported file matches
    what the user saw on screen.
    """
    columns = [str(c) for c in payload.get("columns", [])]
    out: List[List[Any]] = []
    for row in payload.get("rows", []):
        if isinstance(row, dict):
            out.append([row.get(c) for c in columns])
        elif isinstance(row, (list, tuple)):
            out.append(list(row))
        else:  # scalar row — keep it rather than dropping data
            out.append([row])
    return columns, out


def _tabular_view(payload: Any, display_hint: Optional[str]) -> Tuple[List[str], List[List[Any]]]:
    """Best tabular projection of any payload, for the spreadsheet path.

    Every branch returns SOMETHING renderable: the fallback is one JSON cell, so
    a caller never has to handle "this payload can't be exported".
    """
    if not isinstance(payload, dict):
        return [JSON_FALLBACK_HEADER], [[json.dumps(payload, indent=2, default=str)]]

    if display_hint == "table" or ("columns" in payload and "rows" in payload):
        return _rows_for_table(payload)

    if display_hint == "kpi" or "tiles" in payload:
        rows = []
        for tile in payload.get("tiles", []):
            if isinstance(tile, dict):
                value = tile.get("value")
                unit = tile.get("unit")
                rows.append([tile.get("label"), value, unit])
        if rows:
            return ["label", "value", "unit"], rows

    if display_hint == "timeline" or "events" in payload:
        rows = []
        for ev in payload.get("events", []):
            if isinstance(ev, dict):
                rows.append([ev.get("ts"), ev.get("label"), ev.get("detail")])
        if rows:
            return ["ts", "label", "detail"], rows

    if display_hint == "markdown" or "markdown" in payload:
        text = payload.get("markdown")
        if isinstance(text, str):
            return ["markdown"], [[line] for line in text.splitlines() or [""]]

    return [JSON_FALLBACK_HEADER], [[json.dumps(payload, indent=2, default=str)]]


def build_xlsx(payload: Any, display_hint: Optional[str], title: str) -> bytes:
    """Render a report as a real spreadsheet — cells, not a JSON dump.

    Uses openpyxl's write_only workbook: rows are streamed to the zip as they
    are appended instead of being held as cell objects, which is what keeps a
    12,000-row export from materializing the whole grid in memory.
    """
    try:
        from openpyxl import Workbook
    except ImportError as e:  # pragma: no cover - exercised via the router test
        raise ExportUnavailable("openpyxl is not installed") from e

    columns, rows = _tabular_view(payload, display_hint)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet(title=_safe_sheet_title(title))
    if columns:
        ws.append(columns)
    for row in rows:
        ws.append([_cell(v) for v in row])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _safe_sheet_title(title: str) -> str:
    """Excel rejects >31 chars and []:*?/\\ in a sheet name — a title that is
    perfectly valid as a report would otherwise raise mid-export."""
    cleaned = "".join("-" if ch in "[]:*?/\\" else ch for ch in (title or "")).strip()
    return (cleaned or FALLBACK_SHEET_TITLE)[:31]


def _cell(value: Any) -> Any:
    """openpyxl writes str/int/float/bool/None/datetime natively; anything else
    (a nested dict in a cell, say) becomes compact JSON rather than raising."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return json.dumps(value, default=str)


def build_pdf(payload: Any, display_hint: Optional[str], title: str) -> bytes:
    """Render a report as a formatted PDF — a table stays a table, markdown stays
    prose. Not a JSON dump wrapped in a page border."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import (
            Paragraph,
            Preformatted,
            SimpleDocTemplate,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as e:  # pragma: no cover - exercised via the router test
        raise ExportUnavailable("reportlab is not installed") from e

    styles = getSampleStyleSheet()
    story = [Paragraph(_escape(title or "Report"), styles["Title"]), Spacer(1, 6 * mm)]

    if isinstance(payload, dict) and (display_hint == "markdown" or "markdown" in payload):
        for block in str(payload.get("markdown", "")).split("\n\n"):
            if block.strip():
                story.append(Paragraph(_escape(block.strip()), styles["BodyText"]))
                story.append(Spacer(1, 3 * mm))
    else:
        columns, rows = _tabular_view(payload, display_hint)
        if columns == [JSON_FALLBACK_HEADER]:
            # Unmappable shape: a readable monospaced dump beats an empty page.
            story.append(Preformatted(str(rows[0][0])[:20000], styles["Code"]))
        else:
            # Cap rows in the document itself: a 12,000-row PDF is not a document
            # anyone reads, and generating it costs minutes. The spreadsheet is
            # the right artifact for a full dataset, and the note says so.
            capped, cap_note = cap_rows_for_pdf(rows)
            data = [columns] + [[_pdf_cell(v) for v in r] for r in capped]
            table = Table(data, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f1f5f9")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f172a")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("FONTSIZE", (0, 0), (-1, -1), 7),
                        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.append(table)
            if cap_note:
                story.append(Spacer(1, 4 * mm))
                story.append(Paragraph(_escape(cap_note), styles["Italic"]))

    buf = io.BytesIO()
    SimpleDocTemplate(
        buf,
        pagesize=landscape(A4),
        title=title or "Report",
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    ).build(story)
    return buf.getvalue()


# A PDF is a document, not a dataset. Beyond this the spreadsheet is the answer.
PDF_MAX_ROWS = 2000


def cap_rows_for_pdf(rows: List[List[Any]]) -> Tuple[List[List[Any]], Optional[str]]:
    """Rows to draw, plus the note telling the reader what was left out.

    Split out so the truncation decision is testable directly: reportlab
    compresses content streams, so asserting on the rendered bytes proves
    nothing about whether the user was told.
    """
    if len(rows) <= PDF_MAX_ROWS:
        return rows, None
    return (
        rows[:PDF_MAX_ROWS],
        f"Showing first {PDF_MAX_ROWS:,} of {len(rows):,} rows — "
        f"export as .xlsx for the full dataset.",
    )


def _pdf_cell(value: Any) -> str:
    text = "" if value is None else (value if isinstance(value, str) else json.dumps(value, default=str))
    return _escape(text[:200])


def _escape(text: str) -> str:
    """reportlab's Paragraph parses a mini-HTML dialect, so report content —
    which is agent-authored — must be escaped or a stray `<b>` reflows the
    document (and a malformed tag raises mid-build)."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
