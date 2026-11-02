"""#3549 — the `permissions_set` audit row records the saved target list.

The handler read `body["permissions"] or body["targets"]`, keys nobody sends;
the request key is `permitted_agents`, so `details.targets` was always null.
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = [pytest.mark.unit]


def _put(monkeypatch, body):
    from routers import agent_files

    audits = []

    async def _logic(agent_name, body, current_user, request):
        return {"status": "updated", "permitted_count": len(body.get("permitted_agents", []))}

    async def _audit(**kw):
        audits.append(kw)

    monkeypatch.setattr(agent_files, "set_agent_permissions_logic", _logic)
    monkeypatch.setattr(agent_files, "reject_agent_principal", lambda u: None)
    monkeypatch.setattr(agent_files.platform_audit_service, "log", _audit)

    request = SimpleNamespace(
        client=SimpleNamespace(host="127.0.0.1"),
        scope={"path": "/api/agents/a/permissions"},
        state=SimpleNamespace(),
    )
    user = SimpleNamespace(username="admin")
    asyncio.run(agent_files.set_agent_permissions("a", request, body, user))
    assert len(audits) == 1
    assert audits[0]["event_action"] == "permissions_set"
    return audits[0]["details"]


def test_audit_carries_saved_targets(monkeypatch):
    assert _put(monkeypatch, {"permitted_agents": ["b", "c"]}) == {"targets": ["b", "c"]}


def test_cleared_set_is_empty_list_not_null(monkeypatch):
    assert _put(monkeypatch, {"permitted_agents": []}) == {"targets": []}
