"""#2426 — the synchronous Workspace turn must carry its session binding.

ent#457 gave the Workspace a report-back: an agent that delegates during a chat
turn gets the completion posted into that thread. `report_completion` gates on
``if not source_channel_chat_id``, so the parent row MUST carry the session or
the feature is silently inert.

It was inert on the synchronous path. ent#457 passes the binding to
`execute_task`, which persists it only inside ``if not execution_id:`` — and
ent#365's `_precreate_sync_execution` had already created the row and handed the
id down, so that branch never ran, and the pre-create stamped only
`source_channel`. Measured on a dev instance: 5 of 8 portal rows had a NULL
`chat_id`, split exactly by path (browser/streaming stamped, `POST /chat` not).

WHY THIS FILE EXISTS AT THIS LAYER. Neither feature is wrong alone and both have
passing tests — ent#457's mock the engine and assert the kwargs are *passed*
(they are), ent#365's assert no orphan `running` row (still true). Nothing
asserted the PERSISTED ROW, which is the only place the two meet. That is the
same lesson `test_ent457_portal_turn_kwargs.py` states about itself: "the only
place the two signatures meet is here."
"""
from __future__ import annotations

import ast
import inspect

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def portal_service():
    from client_portal import service as m
    return m


# ---------------------------------------------------------------------------
# The behaviour: the row the pre-create writes
# ---------------------------------------------------------------------------
def test_precreated_row_carries_the_session_and_the_client(portal_service, monkeypatch):
    """The regression. Before the fix these two arrived as None."""
    captured = {}

    class _FakeDb:
        def get_agent_subscription_id(self, agent_name):
            return "sub_1"

        def create_task_execution(self, **kwargs):
            captured.update(kwargs)
            return type("Row", (), {"id": "exec_abc"})()

    import database
    monkeypatch.setattr(database, "db", _FakeDb(), raising=False)

    out = portal_service._precreate_sync_execution(
        "agent-a", "hello", "x@example.com", "ps_session_1",
    )

    assert out == "exec_abc"
    assert captured["source_channel_chat_id"] == "ps_session_1", (
        "without the session id, report_completion suppresses every report from "
        "this path at its `if not source_channel_chat_id` gate"
    )
    assert captured["source_channel_client"] == "x@example.com", (
        "_resolve_portal refuses a report whose chain does not name the thread's "
        "client, so a missing client fails closed just as hard as a missing session"
    )
    # Unchanged by this fix — pinned so the row stays otherwise identical.
    assert captured["source_channel"] == portal_service.PORTAL_SOURCE_CHANNEL
    assert captured["triggered_by"] == "public"
    assert captured["source_user_email"] == "x@example.com"


def test_still_fails_soft_when_the_row_cannot_be_written(portal_service, monkeypatch):
    """A turn must never be refused because this bookkeeping failed.

    On None, `run_resumable_turn` creates the row itself — and that path DOES
    stamp both fields, so the fallback is correct rather than merely tolerable.
    """
    class _BoomDb:
        def get_agent_subscription_id(self, agent_name):
            return None

        def create_task_execution(self, **kwargs):
            raise RuntimeError("db down")

    import database
    monkeypatch.setattr(database, "db", _BoomDb(), raising=False)

    assert portal_service._precreate_sync_execution(
        "agent-a", "hello", "x@example.com", "ps_1",
    ) is None


def test_subscription_lookup_failure_does_not_lose_the_binding(portal_service, monkeypatch):
    """Usage tracking is best-effort; the binding is not."""
    captured = {}

    class _PartialDb:
        def get_agent_subscription_id(self, agent_name):
            raise RuntimeError("no subscription service")

        def create_task_execution(self, **kwargs):
            captured.update(kwargs)
            return type("Row", (), {"id": "exec_x"})()

    import database
    monkeypatch.setattr(database, "db", _PartialDb(), raising=False)

    portal_service._precreate_sync_execution("a", "m", "x@example.com", "ps_2")
    assert captured["subscription_id"] is None
    assert captured["source_channel_chat_id"] == "ps_2"
    assert captured["source_channel_client"] == "x@example.com"


# ---------------------------------------------------------------------------
# The two writers must agree — that is the actual invariant
# ---------------------------------------------------------------------------
def _create_call_kwargs(fn) -> set[str]:
    """Keyword names passed to a `create_task_execution(...)` call inside `fn`."""
    tree = ast.parse(inspect.getsource(fn).lstrip())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            if name == "create_task_execution":
                return {kw.arg for kw in node.keywords if kw.arg}
    return set()


def test_the_sync_pre_create_stamps_what_the_streaming_path_stamps(portal_service):
    """`_precreate_sync_execution`'s docstring claims the two paths produce
    "indistinguishable rows". This asserts the claim instead of trusting it —
    the claim was false, and it read as true.

    Derived from the real call sites, so a THIRD channel field added to one
    writer and forgotten in the other fails here rather than shipping as another
    silently-inert report path.
    """
    sync = _create_call_kwargs(portal_service._precreate_sync_execution)
    streaming = _create_call_kwargs(portal_service.start_portal_turn)

    assert sync, "no create_task_execution call found in _precreate_sync_execution"
    assert streaming, "no create_task_execution call found in start_portal_turn"

    channel_fields = {k for k in streaming if k.startswith("source_channel")}
    assert channel_fields, "the streaming path stamps no channel fields — check this test's premise"
    missing = channel_fields - sync
    assert not missing, (
        f"the synchronous path omits {sorted(missing)} that the streaming path stamps; "
        "a report delegated from a sync turn cannot be joined back to its chat"
    )


def test_execute_task_persists_the_binding_only_when_it_creates_the_row(portal_service):
    """The other half of the collision, pinned so the fix is not silently undone.

    If someone later teaches `execute_task` to UPDATE an adopted row, this test
    should be revisited deliberately — not deleted because it went red.
    """
    from services import task_execution_service

    src = inspect.getsource(task_execution_service.TaskExecutionService.execute_task)
    tree = ast.parse(src.lstrip())

    guarded = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        # `if not execution_id:`
        if not (isinstance(node.test, ast.UnaryOp) and isinstance(node.test.op, ast.Not)):
            continue
        for inner in ast.walk(node):
            if isinstance(inner, ast.Call):
                name = inner.func.attr if isinstance(inner.func, ast.Attribute) else getattr(inner.func, "id", "")
                if name == "create_task_execution":
                    kws = {kw.arg for kw in inner.keywords if kw.arg}
                    if "source_channel_chat_id" in kws:
                        guarded = True
    assert guarded, (
        "execute_task no longer writes source_channel_chat_id inside its "
        "`if not execution_id` create — if that moved, the pre-create is no "
        "longer the only writer for an adopted row and #2426's reasoning changes"
    )
