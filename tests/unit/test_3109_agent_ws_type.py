"""#3109 — agent lifecycle broadcasts carry `type` beside `event`.

The dashboard store (`src/frontend/src/stores/network.js`) dispatches on
`data.type`. `agent_created` and `agent_deleted` were sent with `event` only, so
the dashboard never heard a new agent (it waited for the 30 s poll) and its
`agent_deleted` branch was dead. `agent_started`/`agent_stopped` already sent
both keys; the other two now do as well.

Two tests: the real `_broadcast_agent_created` payload, and a guard over every
`agent_*` broadcast dict literal in the backend, whose live consumer is that
dispatcher — so a new lifecycle event added with `event` only fails here.
"""
from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


@pytest.mark.asyncio
async def test_agent_created_broadcast_carries_type():
    try:
        from services.agent_service.crud import _broadcast_agent_created
        from models import AgentStatus
    except ImportError:
        pytest.skip("backend venv required")

    sent = []

    class _Manager:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    status = AgentStatus(name="scout", status="running", port=2222,
                         created=datetime.now(timezone.utc), resources={})
    await _broadcast_agent_created(status, _Manager())
    assert sent and sent[0]["event"] == "agent_created"
    assert sent[0]["type"] == "agent_created"
    assert sent[0]["data"]["name"] == "scout"


def _agent_event_dicts():
    """Every dict literal under src/backend whose "event" is an `agent_*` string."""
    for path in sorted(_BACKEND.rglob("*.py")):
        if "tests" in path.parts or "enterprise" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            keys = {k.value: v for k, v in zip(node.keys, node.values)
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            ev = keys.get("event")
            if isinstance(ev, ast.Constant) and isinstance(ev.value, str) and ev.value in LIFECYCLE:
                yield path.relative_to(_BACKEND), node.lineno, ev.value, keys.get("type")


# The lifecycle events the dashboard dispatcher handles by `type`.
LIFECYCLE = {"agent_created", "agent_deleted", "agent_started", "agent_stopped"}


def test_every_agent_lifecycle_broadcast_sends_type():
    found = list(_agent_event_dicts())
    names = {ev for _, _, ev, _ in found}
    assert {"agent_created", "agent_deleted"} <= names, "the scan must see the broadcasts it guards"
    missing = [f"{p}:{line} {ev}" for p, line, ev, t in found
               if not (isinstance(t, ast.Constant) and t.value == ev)]
    assert not missing, "agent lifecycle broadcasts without a matching `type`: " + ", ".join(missing)
