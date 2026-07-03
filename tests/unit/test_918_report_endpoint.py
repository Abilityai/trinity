"""Router-level guards for the agent-report create endpoint (#918).

The two most security-/abuse-sensitive checks live inline in
``routers.reports.create_report`` *before* the service call, so they're tested by
invoking the async endpoint function directly (no TestClient/auth stack needed —
``name`` is the already-resolved ``AuthorizedAgent`` string and ``current_user``
a lightweight stand-in):

  * **self-gate** — an agent-scoped key whose bound ``agent_name`` differs from
    the path agent → 403 (the sibling-spoof defense; ``AuthorizedAgent`` alone
    only proves the *owner* can access the path agent).
  * **413 cap** — a payload whose serialized JSON exceeds
    ``REPORT_PAYLOAD_MAX_BYTES`` is rejected before it reaches the DB.

The happy path is included to prove the gate lets a legitimate self-report
through to the service.
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
        from models import ReportCreate, REPORT_PAYLOAD_MAX_BYTES
        from routers import reports
    except ImportError:
        pytest.skip("backend venv required")
    return HTTPException, ReportCreate, REPORT_PAYLOAD_MAX_BYTES, reports


def _user(agent_name=None, uid=1):
    """Stand-in for the resolved ``current_user`` (only .id / .agent_name read)."""
    return SimpleNamespace(id=uid, agent_name=agent_name, username="u")


def test_self_gate_blocks_sibling_spoof():
    HTTPException, ReportCreate, _CAP, reports = _load()
    data = ReportCreate(report_type="recon.weekly_summary", title="t", payload={"k": 1})
    # Agent-scoped key bound to "other" reporting against path agent "a1" → 403.
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reports.create_report(data, name="a1", current_user=_user(agent_name="other")))
    assert exc.value.status_code == 403


def test_payload_over_cap_rejected_413():
    HTTPException, ReportCreate, CAP, reports = _load()
    # Self-gate passes (user-scoped: agent_name=None) so we reach the size check.
    big = {"blob": "a" * (CAP + 1000)}
    data = ReportCreate(report_type="ops.daily_health", title="t", payload=big)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(reports.create_report(data, name="a1", current_user=_user()))
    assert exc.value.status_code == 413


def test_self_report_reaches_service(monkeypatch):
    HTTPException, ReportCreate, _CAP, reports = _load()
    captured = {}

    async def _fake_create_report(**kwargs):
        captured.update(kwargs)
        return {
            "id": "r-1",
            "agent_name": kwargs["agent_name"],
            "user_id": kwargs["user_id"],
            "report_type": kwargs["report_type"],
            "title": kwargs["title"],
            "payload": kwargs["payload"],
            "display_hint": kwargs.get("display_hint"),
            "schema_version": kwargs.get("schema_version", 1),
            "period_start": kwargs.get("period_start"),
            "period_end": kwargs.get("period_end"),
            "created_at": "2026-06-27T00:00:00Z",
        }

    monkeypatch.setattr(reports.report_service, "create_report", _fake_create_report)
    data = ReportCreate(report_type="recon.weekly_summary", title="t", payload={"k": 1})
    # Agent-scoped key reporting as itself ("a1" == path "a1") is allowed.
    result = asyncio.run(reports.create_report(data, name="a1", current_user=_user(agent_name="a1")))
    assert result.id == "r-1"
    assert result.agent_name == "a1"
    assert captured["agent_name"] == "a1"
    assert captured["user_id"] == 1
