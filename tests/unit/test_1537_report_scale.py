"""Large-payload reporting — raised ceiling + paginated row reader (#1537).

Measured before designing: on a live fleet the existing reports averaged 201
bytes and the largest was 683 — four orders of magnitude under the 256 KiB cap.
So the cap was never a limit agents were hitting; it was the wall the FIRST real
tabular report would hit. That measurement is why this ships a raised ceiling
plus a row window rather than an off-row storage migration: there is no payload
in existence to justify the schema commitment (see REPORT_PAYLOAD_MAX_BYTES).

Locked here:
  * the ceiling is genuinely larger, and still enforced
  * the Content-Length pre-check rejects an oversized body on the header
  * the row reader windows a tabular payload and reports the true total
  * non-tabular payloads are refused with 400, not served a fake row axis
  * no-access answers 404, matching GET /reports/{id} — an id stays unprobeable
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _load():
    try:
        from fastapi import HTTPException
        from models import (
            REPORT_PAYLOAD_MAX_BYTES,
            REPORT_ROWS_PAGE_DEFAULT,
            REPORT_ROWS_PAGE_MAX,
            ReportCreate,
        )
        from routers import reports
    except ImportError:  # pragma: no cover - backend venv required
        pytest.skip("backend venv required")
    return (
        HTTPException,
        ReportCreate,
        REPORT_PAYLOAD_MAX_BYTES,
        REPORT_ROWS_PAGE_DEFAULT,
        REPORT_ROWS_PAGE_MAX,
        reports,
    )


def _user(uid=1, username="admin", agent_name=None):
    return SimpleNamespace(id=uid, username=username, agent_name=agent_name)


def _request(content_length=None):
    headers = {} if content_length is None else {"content-length": str(content_length)}
    return SimpleNamespace(headers=headers)


def _table_report(rows, agent="scout"):
    return {
        "id": "r1",
        "agent_name": agent,
        "user_id": 1,
        "report_type": "prospector.leads_found",
        "title": "leads",
        "payload": {"columns": ["id", "score"], "rows": rows},
        "display_hint": "table",
        "schema_version": 1,
        "period_start": None,
        "period_end": None,
        "created_at": "2026-07-28T00:00:00Z",
    }


# ---------------------------------------------------------------------------
# Ceiling
# ---------------------------------------------------------------------------

def test_ceiling_is_larger_than_the_old_cap():
    _, _, cap, _, _, _ = _load()
    assert cap > 256 * 1024, "the point of #1537 is that 256 KiB rejected the first real table"
    assert cap == 5 * 1024 * 1024


def test_content_length_over_the_ceiling_is_refused_on_the_header(monkeypatch):
    """Cheap rejection before serializing the parsed payload back to JSON."""
    HTTPException, ReportCreate, cap, _, _, reports = _load()
    monkeypatch.setattr(reports.rate_limiter, "enforce", lambda *a, **k: None)

    data = ReportCreate(report_type="t.x", title="t", payload={"a": 1})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            reports.create_report(
                data=data, name="scout", request=_request(cap + 1), current_user=_user()
            )
        )
    assert exc.value.status_code == 413


def test_unparseable_content_length_falls_through_to_the_exact_check(monkeypatch):
    """A lying/garbage header must not bypass the real cap — nor 500."""
    HTTPException, ReportCreate, cap, _, _, reports = _load()
    monkeypatch.setattr(reports.rate_limiter, "enforce", lambda *a, **k: None)

    oversized = {"rows": ["x" * 1024] * (6 * 1024)}  # > 5 MiB serialized
    data = ReportCreate(report_type="t.x", title="t", payload=oversized)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            reports.create_report(
                data=data, name="scout", request=_request("not-a-number"), current_user=_user()
            )
        )
    assert exc.value.status_code == 413


# ---------------------------------------------------------------------------
# Row reader
# ---------------------------------------------------------------------------

def test_rows_windows_a_large_payload_and_reports_the_true_total(monkeypatch):
    _, _, _, page_default, _, reports = _load()
    rows = [[f"lead-{i}", i] for i in range(12_000)]
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _table_report(rows))
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    out = asyncio.run(
        reports.get_report_rows(
            report_id="r1", offset=0, limit=page_default, current_user=_user()
        )
    )
    assert out["total"] == 12_000
    assert len(out["rows"]) == page_default
    assert out["columns"] == ["id", "score"]
    assert out["rows"][0] == ["lead-0", 0]


def test_rows_offset_returns_the_next_window(monkeypatch):
    _, _, _, _, _, reports = _load()
    rows = [[f"lead-{i}", i] for i in range(500)]
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _table_report(rows))
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    out = asyncio.run(
        reports.get_report_rows(report_id="r1", offset=100, limit=5, current_user=_user())
    )
    assert [r[0] for r in out["rows"]] == [f"lead-{i}" for i in range(100, 105)]
    assert out["offset"] == 100


def test_offset_past_the_end_is_an_empty_page_not_an_error(monkeypatch):
    _, _, _, _, _, reports = _load()
    monkeypatch.setattr(reports.db, "get_report", lambda rid: _table_report([[1, 2]]))
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    out = asyncio.run(
        reports.get_report_rows(report_id="r1", offset=9999, limit=10, current_user=_user())
    )
    assert out["rows"] == []
    assert out["total"] == 1


def test_non_tabular_payload_is_refused(monkeypatch):
    """A kpi/markdown payload has no row axis; inventing one would be worse than
    saying so."""
    HTTPException, _, _, _, _, reports = _load()
    report = _table_report([])
    report["payload"] = {"markdown": "# not a table"}
    monkeypatch.setattr(reports.db, "get_report", lambda rid: report)
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: True)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            reports.get_report_rows(report_id="r1", offset=0, limit=10, current_user=_user())
        )
    assert exc.value.status_code == 400


@pytest.mark.parametrize(
    "report, accessible",
    [
        (None, True),                      # unknown id
        ("present", False),                # exists, caller cannot access the agent
    ],
)
def test_missing_or_inaccessible_is_404(monkeypatch, report, accessible):
    """Same shape as GET /reports/{id}: 404 either way, so the sibling route
    can't be used to probe whether an id exists."""
    HTTPException, _, _, _, _, reports = _load()
    row = _table_report([[1, 2]]) if report == "present" else None
    monkeypatch.setattr(reports.db, "get_report", lambda rid: row)
    monkeypatch.setattr(reports.db, "can_user_access_agent", lambda u, a: accessible)

    with pytest.raises(HTTPException) as exc:
        asyncio.run(
            reports.get_report_rows(report_id="r1", offset=0, limit=10, current_user=_user())
        )
    assert exc.value.status_code == 404
