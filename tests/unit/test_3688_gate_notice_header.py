"""trinity#3688 — a gated request's ending notice in a Workspace thread does
not read "Finished".

After #3653 the requester is woken (`triggered_by="skill_gate"`) with how its
gated request ended, and the reply is reported into the Workspace thread it
came from. `_portal_body` chose its header from the wake-up row's own run
status, which completes normally for a rejected, expired or not-run request —
so the first word the requester read was "Finished" above "Your request was
rejected."

A `skill_gate` row that completed now gets a neutral header; its text carries
the verdict. A wake-up that itself failed still says it didn't finish, and the
approved run's own completion (it keeps its requester's trigger) is unchanged.

Driven through the real `report_completion` → `_resolve_portal` →
`add_portal_message` path with the ent#457 harness (only the execution row and
the portal DB are stubbed).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ent457_portal_completion_report import (  # noqa: E402
    OTHER, _SESSION, _Row, _drive, ccr,  # noqa: F401 — `ccr` is a fixture
)

pytestmark = pytest.mark.unit


def _body(written):
    assert len(written) == 1
    return written[0]["args"][4]


@pytest.mark.parametrize("notice", [
    "Your request to run pay-invoice was rejected.",
    "Your request to run pay-invoice expired before anyone decided it.",
    "Your request to run pay-invoice was approved but did not run.",
])
def test_an_ending_notice_does_not_say_finished(ccr, monkeypatch, notice):
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by="skill_gate"),
                         session=_SESSION, summary_or_error=notice)
    body = _body(written)
    assert ok is True
    assert "Finished" not in body
    assert body.startswith("**Approval update**")
    assert notice in body                       # the verdict is the text's


def test_the_approved_runs_own_completion_is_unchanged(ccr, monkeypatch):
    # An approved run keeps its requester's trigger; it is never `skill_gate`.
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by="mcp"),
                         session=_SESSION, summary_or_error="Paid INV-8.")
    assert _body(written).startswith("**Finished**")


def test_a_wake_up_that_itself_failed_still_says_so(ccr, monkeypatch):
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by="skill_gate"),
                         session=_SESSION, status="failed", summary_or_error="boom")
    assert _body(written).startswith("**Didn't finish** — failed")


def test_a_delegated_notice_still_names_the_agent(ccr, monkeypatch):
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by="skill_gate", agent_name=OTHER),
                         session=_SESSION, summary_or_error="Your request was rejected.")
    assert _body(written).startswith(f"**Approval update ({OTHER})**")


def test_the_trigger_reaches_every_resolver(ccr):
    """The call shape is shared (ent#457 review): a field added for one
    resolver is passed to all of them, so none silently misses it."""
    import inspect
    for resolver in ccr._CHANNEL_RESOLVERS.values():
        assert "triggered_by" in inspect.signature(resolver).parameters, resolver.__name__


# The Inbox pill is not read from the text: it is the message `source`, set by
# the same test as the header. A notice left at `completion:done` would still
# show a green "Finished" pill above "Your request was rejected."

def _source(written):
    return written[0]["kwargs"]["source"]


@pytest.mark.parametrize("trigger,status,source", [
    ("skill_gate", "success", "completion:notice"),
    ("mcp", "success", "completion:done"),          # the approved run itself
    ("skill_gate", "failed", "completion:failed"),  # the wake-up itself failed
])
def test_the_pill_source_agrees_with_the_header(ccr, monkeypatch, trigger, status, source):
    ok, written = _drive(ccr, monkeypatch, _Row(triggered_by=trigger), session=_SESSION,
                         status=status, summary_or_error="Your request was rejected.")
    assert _source(written) == source


def test_a_notice_shows_no_outcome_pill_in_the_inbox():
    from client_portal import chat_previews
    assert chat_previews._outcome("completion:notice") is None
    assert chat_previews._outcome("completion:done") == "done"


def test_a_notice_is_narrated_as_a_background_report():
    from client_portal import service
    assert "completion:notice" in service._COMPLETION_SOURCES
