"""Report WS broadcast is a THIN trigger — no title/payload on the wire (#918).

This is the A1 leak regression. ``/ws`` is SCOPE_ALL and unfiltered, so every
logged-in browser receives a SCOPE_ALL event. The report broadcast must carry
ONLY trigger metadata (agent_name, report_id, report_type, created_at) so it
cannot leak report titles or payloads to users who can't access the agent.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


class _FakeManager:
    def __init__(self):
        self.messages = []

    async def broadcast(self, message):  # /ws (SCOPE_ALL) — receives a JSON string
        self.messages.append(message)

    async def broadcast_filtered(self, event):  # /ws/events (SCOPE_SCOPED) — receives a dict
        self.messages.append(event)


_FULL_REPORT = {
    "id": "r-1",
    "agent_name": "a1",
    "user_id": 1,
    "report_type": "recon.weekly_summary",
    "title": "TOP SECRET: Acme $2M deal closed",
    "payload": {"secret": "leads", "value": 2_000_000},
    "display_hint": "table",
    "schema_version": 1,
    "period_start": None,
    "period_end": None,
    "created_at": "2026-06-27T00:00:00Z",
}


def _load_service():
    try:
        from services import report_service
    except ImportError:
        pytest.skip("backend venv required")
    return report_service


def test_broadcast_event_is_thin():
    svc = _load_service()
    main_mgr, filtered_mgr = _FakeManager(), _FakeManager()
    svc.set_websocket_manager(main_mgr)
    svc.set_filtered_websocket_manager(filtered_mgr)

    asyncio.run(svc._broadcast_report(_FULL_REPORT))

    # /ws gets a JSON string; /ws/events gets a dict — both must be thin.
    main_event = json.loads(main_mgr.messages[0])
    filtered_event = filtered_mgr.messages[0]
    for event in (main_event, filtered_event):
        assert event["type"] == "agent_report"
        assert event["agent_name"] == "a1"
        assert event["report_id"] == "r-1"
        assert event["report_type"] == "recon.weekly_summary"
        # The leak guard: NO sensitive fields on the wire.
        assert "title" not in event
        assert "payload" not in event

    # And the secret string must not appear anywhere in the serialized payloads.
    serialized = json.dumps(main_mgr.messages[0]) + json.dumps(filtered_mgr.messages[0])
    assert "Acme" not in serialized
    assert "leads" not in serialized
