"""ent#457 review — channel context is inherited only from work still running.

WHY THIS EXISTS. The first version of the cross-client fix compared the
inherited `source_channel_client` against the session's `client_email` and
called that a binding. It is not: BOTH values are read off the same parent row,
which the CALLER names, so in the attack it was written to stop —

    agent A is shared with clients X and Y; while serving Y, A passes the
    execution id of one of X's portal turns as `parent_execution_id`

— the child inherits X's session AND X's email, they match, and the report is
delivered into X's thread. A comparison between two values from one source
cannot fail. That is the same defect the fix was meant to close, one level up,
and the test that "proved" it hand-built a mismatch no writer in the tree can
produce.

The only input here the caller does not choose is TIME. ent#265's premise is
that A delegates *during* a turn it is serving, so a finished parent is not
that. This bound is what removes "any historical execution of any client of
this agent" from the attack surface.

RESIDUAL, stated rather than implied: if X has a turn genuinely in flight at
that moment, A can still name it. "All history" → "a concurrent live turn" is a
real narrowing and not a proof; closing it needs the child to learn its client
from something other than the caller's argument.
"""
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def mod():
    from services import chat_execution_service as m
    return m


def _parent(status="running", **over):
    base = dict(
        agent_name="agent-a",
        status=status,
        source_channel="portal",
        source_channel_chat_id="ps_xxxxxxxx",
        source_channel_thread=None,
        source_channel_agent="agent-a",
        source_channel_client="x@example.com",
        source_user_email="x@example.com",
    )
    base.update(over)
    return SimpleNamespace(**base)


def _drive(mod, monkeypatch, parent):
    monkeypatch.setattr(mod.db, "get_execution", lambda _id: parent)
    request = SimpleNamespace(parent_execution_id="exec-parent")
    caller = SimpleNamespace(agent_name="agent-a", connector_agent=None,
                             username="agent-a")
    return mod._inherited_channel_context(request, current_user=caller)


def test_a_running_parent_still_passes_its_context_down(mod, monkeypatch):
    """The feature has to keep working: A delegating mid-turn is the whole
    point of ent#224/ent#265."""
    channel, chat_id, thread, binding, client = _drive(mod, monkeypatch, _parent())
    assert channel == "portal"
    assert chat_id == "ps_xxxxxxxx"
    assert binding == "agent-a"
    assert client == "x@example.com"


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "queued", ""])
def test_a_parent_that_is_not_running_passes_nothing_down(mod, monkeypatch, status):
    """The bound. A finished turn's destination is exactly what an agent would
    cite to reach a client it is not currently serving."""
    assert _drive(mod, monkeypatch, _parent(status=status)) == mod._NO_INHERITED_CONTEXT


def test_the_cross_client_case_is_refused_when_the_cited_turn_has_finished(mod, monkeypatch):
    """The reported attack, at the point it becomes reachable.

    A is serving Y and cites one of X's PAST portal turns. Every identity check
    passes — same agent, and the inherited client matches the inherited session
    because they are the same row — so nothing downstream can catch it. It is
    refused here, on the one property the caller does not control.
    """
    finished_turn_of_another_client = _parent(
        status="success",
        source_channel_chat_id="ps_client_x",
        source_channel_client="x@example.com",
    )
    assert _drive(mod, monkeypatch, finished_turn_of_another_client) == \
        mod._NO_INHERITED_CONTEXT


def test_a_missing_status_is_treated_as_not_running(mod, monkeypatch):
    """Fail closed on an unreadable status: a row shape that cannot answer
    'is this live' must not be read as 'yes'."""
    parent = _parent()
    del parent.status
    assert _drive(mod, monkeypatch, parent) == mod._NO_INHERITED_CONTEXT
