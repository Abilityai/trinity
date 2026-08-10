"""Tag WS broadcast is a THIN trigger — no tag values on the wire (ent#305).

Same class as ``test_918_report_broadcast.py``: ``/ws`` is SCOPE_ALL and
unfiltered, so every logged-in browser receives a SCOPE_ALL event. Org-overlay
tags (``dept-*``, ``reports-to-*``) ARE the org chart — department membership
and manager→report edges — so a broadcast carrying tag values would hand any
authenticated ``/ws`` client every tenant's org structure, including agents it
cannot see in ``GET /api/agents``. The broadcast must carry ONLY
``{type, agent_name}``; listeners refetch through the access-controlled
``GET /api/agents/{name}/tags``.
"""
from __future__ import annotations

import asyncio
import inspect
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


def _load_router():
    try:
        from routers import tags
    except ImportError:
        pytest.skip("backend venv required")
    return tags


def test_broadcast_event_is_thin():
    tags = _load_router()
    mgr = _FakeManager()
    tags.set_websocket_manager(mgr)
    try:
        asyncio.run(tags._broadcast_tags_changed("a1"))
    finally:
        tags.set_websocket_manager(None)

    event = json.loads(mgr.messages[0])
    assert event["type"] == "agent_tags_changed"
    assert event["agent_name"] == "a1"
    # The leak guard: the org chart never rides the unfiltered socket.
    assert set(event.keys()) == {"type", "agent_name"}


def test_broadcast_signature_cannot_take_tags():
    """The helper's signature is the structural guard: a future caller cannot
    hand it tag values to forward without changing the signature this test
    pins."""
    tags = _load_router()
    params = list(inspect.signature(tags._broadcast_tags_changed).parameters)
    assert params == ["agent_name"]
