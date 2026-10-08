"""
Gated skills — a public-link visitor's held request ends the wait (trinity#3274, item 9).

The public link's async path never answers 202: it pre-creates the row and the
`execute_task` backstop holds the request in the background, closing the row
`skipped` with the gate's notice. The status route returned no text for a
`skipped` row, and `PublicChat.vue` polled it for 30 minutes before saying
"Request timed out". It now returns the notice for a `skipped` row the public
link itself started (`triggered_by == "public"`) — the same access check as
every other status read (the link token, and the row on the link's agent).

Driven: `routers.public.public_execution_status`, with the link check, the rate
limit and the row read stubbed.
"""
import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit

NOTICE = "Not run: the skill pay-invoice on finance needs approval before it can run."


def _status(monkeypatch, *, status, triggered_by="public", agent="finance", record=None):
    import routers.public as pub

    row = SimpleNamespace(id="e1", agent_name=agent, status=status, triggered_by=triggered_by,
                          response=None, error=NOTICE)
    monkeypatch.setattr(pub, "_get_client_ip", lambda request: "1.2.3.4")
    monkeypatch.setattr(pub, "check_public_link_rate_limit", lambda ip: None)
    monkeypatch.setattr(pub, "_validate_public_link", lambda token: {"agent_name": "finance"})
    monkeypatch.setattr(pub.db, "get_execution", lambda eid: row)
    monkeypatch.setattr(pub.db, "get_gate_requests_by_origin_executions",
                        lambda ids: {"e1": record} if record else {})
    return asyncio.run(pub.public_execution_status("tok", "e1", SimpleNamespace()))


def test_a_held_public_turn_answers_with_the_gates_notice(monkeypatch):
    out = _status(monkeypatch, status="skipped", record={"state": "pending"})
    assert (out["status"], out["error"], out["gate"]) == ("skipped", NOTICE, "held")


def test_a_refused_public_turn_is_named_a_refusal_not_a_wait(monkeypatch):
    """A refusal creates no pending record; the page shows it as an error."""
    out = _status(monkeypatch, status="skipped", record=None)
    assert (out["error"], out["gate"]) == (NOTICE, "refused")


def test_a_skipped_row_another_trigger_started_says_nothing_here(monkeypatch):
    out = _status(monkeypatch, status="skipped", triggered_by="schedule", record={"state": "pending"})
    assert (out["error"], out["gate"]) == (None, None)


def test_a_running_row_still_carries_no_text(monkeypatch):
    assert _status(monkeypatch, status="running")["error"] is None
