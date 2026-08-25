"""ent#155 — Escape / Stop cancels an in-flight chat turn.

The cancel MACHINERY already existed end to end (terminate endpoint →
agent-server process registry SIGINT → CANCELLED terminal, #679/#1332) and was
already used by the Tasks panel. What did not exist was a trigger on the three
conversation surfaces, and two of those three have no `users` row to
authenticate with — a public-link visitor and a Workspace client are both real
people stopping a turn they themselves started.

So the suite is about the boundary, not about the click:

1. **Who may stop what.** The public link scopes per LINK (its token is the
   credential, exactly as for the status and stream routes it sits beside); the
   Workspace scopes per CALLER (roster + started-by-this-caller), because
   executions are agent-scoped and two clients can share one agent.
2. **The cancel semantics are not re-implemented.** Both new routes delegate to
   the one `terminate_execution` path, so CANCELLED-not-FAILED, the CAS guard
   and breaker-neutrality are inherited rather than restated.
3. **Losing the race is not an error.** A cancel arriving after the turn ended
   answers `already_terminal`, because the client races its own poll and a
   person cannot act on having lost.
"""
from __future__ import annotations

import inspect

import pytest

pytestmark = pytest.mark.unit


def test_terminate_no_longer_requires_a_platform_user():
    """The gate moved to the ROUTE, so the service takes an optional principal.

    A public visitor and a Workspace client have no `users` row; requiring one
    here would have forced a second cancel path for them, which is how two
    cancel semantics get born.
    """
    from services import chat_execution_service as mod
    sig = inspect.signature(mod.terminate_execution)
    assert sig.parameters["current_user"].default is None
    assert sig.parameters["actor_kind"].default == "operator"


def test_a_null_user_id_is_recorded_as_a_kind_not_left_mysterious():
    """`user_id` is legitimately NULL for those two principals, so the activity
    row says WHO acted instead of leaving an unexplained blank."""
    from services import chat_execution_service as mod
    src = inspect.getsource(mod)
    assert 'user_id=getattr(current_user, "id", None)' in src
    assert '"actor_kind": actor_kind' in src


def test_neither_new_route_re_implements_cancellation():
    """Both delegate to the one terminate path. A surface that wrote its own
    CAS, its own CANCELLED write or its own breaker handling would be a second
    cancel semantics for one product.
    """
    from routers import public as public_router
    from client_portal import service as portal_service

    public_src = inspect.getsource(public_router.public_terminate_execution)
    portal_src = inspect.getsource(portal_service.terminate_portal_turn)

    assert "_terminate_execution(" in public_src
    assert "terminate_execution(" in portal_src
    for src in (public_src, portal_src):
        # No status write, no CAS, no breaker call of its own — the words
        # "CANCELLED" appear in the docstrings, which is the point: they
        # DESCRIBE semantics owned elsewhere rather than restating them.
        assert "update_execution_status" not in src
        assert "cancel_queued_execution" not in src
        assert "TaskExecutionStatus" not in src
        assert "record_outcome" not in src


def test_the_public_route_scopes_by_link_and_by_agent():
    """The token is the credential — the same one required to start the turn —
    and the execution must belong to the agent behind THIS link."""
    src = inspect.getsource(__import__("routers.public", fromlist=["public"]).public_terminate_execution)
    assert "check_public_link_rate_limit" in src
    assert "_validate_public_link(token)" in src
    assert "execution.agent_name != agent_name" in src
    assert 'actor_kind="public_link"' in src


def test_the_portal_route_scopes_by_caller_not_only_by_agent():
    """The load-bearing gate. Executions are agent-scoped, so without
    `execution_belongs_to_caller` any client of a shared agent could stop
    another client's turn by guessing an id — strictly worse than reading one.
    """
    from client_portal import router as mod
    src = inspect.getsource(mod.portal_terminate_execution)
    assert "_require_roster(agent_name, email, include_owned)" in src
    assert "service.execution_belongs_to_caller(execution_id, agent_name, email)" in src
    # Uniform 404, exactly like the stream route — never confirm an execution
    # exists to someone who may not touch it.
    assert "404" in src
    assert "rate_limiter.enforce" in src


def test_the_portal_route_reuses_the_streams_own_gate_function():
    """Not a re-implementation of the same predicate: the identical function
    the stream route uses, so the two cannot drift on who may reach a turn."""
    from client_portal import router as mod
    stream_src = inspect.getsource(mod.portal_stream_execution)
    cancel_src = inspect.getsource(mod.portal_terminate_execution)
    assert "execution_belongs_to_caller" in stream_src
    assert "execution_belongs_to_caller" in cancel_src


@pytest.mark.parametrize("status", ["success", "failed", "cancelled", "skipped"])
def test_a_cancel_that_lost_the_race_is_a_no_op_success(status, monkeypatch):
    """AC: "a cancel landing after the turn already completed is a no-op".

    Not a 4xx — the client races its own poll, and losing that race is not
    something the person did or can fix. The reply is on screen.
    """
    import asyncio
    from client_portal import service as mod

    class _Row:
        def __init__(self, s): self.status = s; self.agent_name = "scribe"

    class _DB:
        def get_execution(self, eid): return _Row(status)

    monkeypatch.setitem(__import__("sys").modules, "database", type("M", (), {"db": _DB()}))

    out = asyncio.run(mod.terminate_portal_turn("scribe", "exec-1"))
    assert out["status"] == "already_terminal"
    assert out["execution_id"] == "exec-1"


@pytest.mark.parametrize("status", ["running", "queued"])
def test_a_live_turn_is_actually_terminated(status, monkeypatch):
    import asyncio
    from client_portal import service as mod

    class _Row:
        def __init__(self, s): self.status = s; self.agent_name = "scribe"

    class _DB:
        def get_execution(self, eid): return _Row(status)

    seen = {}

    async def _fake_terminate(**kwargs):
        seen.update(kwargs)
        return {"status": "terminated"}

    monkeypatch.setitem(__import__("sys").modules, "database", type("M", (), {"db": _DB()}))
    import services.chat_execution_service as ces
    monkeypatch.setattr(ces, "terminate_execution", _fake_terminate)

    out = asyncio.run(mod.terminate_portal_turn("scribe", "exec-1"))

    assert out == {"status": "terminated"}
    assert seen["name"] == "scribe"
    assert seen["current_user"] is None
    assert seen["actor_kind"] == "workspace_client"


def test_a_missing_execution_is_a_404_not_a_silent_success(monkeypatch):
    import asyncio
    from client_portal import service as mod
    from client_portal.service import ClientPortalError

    class _DB:
        def get_execution(self, eid): return None

    monkeypatch.setitem(__import__("sys").modules, "database", type("M", (), {"db": _DB()}))

    with pytest.raises(ClientPortalError) as e:
        asyncio.run(mod.terminate_portal_turn("scribe", "nope"))
    assert e.value.status_code == 404


def test_an_agent_side_failure_is_worded_for_a_client(monkeypatch):
    """The operator-facing detail names the agent host, which is not a client's
    business — but the refusal must still be honest that nothing stopped."""
    import asyncio
    from client_portal import service as mod
    from client_portal.service import ClientPortalError
    from services.chat_signals import ChatDispatchError

    class _Row:
        status = "running"; agent_name = "scribe"

    class _DB:
        def get_execution(self, eid): return _Row()

    async def _boom(**kwargs):
        raise ChatDispatchError(504, "Timeout connecting to agent 'scribe'")

    monkeypatch.setitem(__import__("sys").modules, "database", type("M", (), {"db": _DB()}))
    import services.chat_execution_service as ces
    monkeypatch.setattr(ces, "terminate_execution", _boom)

    with pytest.raises(ClientPortalError) as e:
        asyncio.run(mod.terminate_portal_turn("scribe", "exec-1"))
    assert e.value.status_code == 503
    assert "scribe" not in e.value.detail
    assert "stop" in e.value.detail.lower()


def test_a_public_link_can_only_cancel_what_a_public_link_produced():
    """Review finding: the link+agent pair is the right scope for a READ and the
    wrong one for a destructive write.

    `status` and `stream` let a link holder observe; this route lets them KILL,
    and every execution on that agent shares the agent name — the owner's own
    Agent Detail turn, a scheduled run, a loop iteration. Ids are 128-bit so
    this is not blind-guessable, but one leaked id (a screenshot, a log, a
    shared browser session) would otherwise let a visitor stop the owner's
    scheduled work. The symmetry-with-reading argument is sound for a read and
    does not carry to a write.
    """
    import inspect
    from routers import public as public_router
    src = inspect.getsource(public_router.public_terminate_execution)
    assert 'triggered_by' in src
    assert '"public"' in src
    # ...and it refuses with the SAME uniform 404 as an unknown execution, so
    # the route is not an oracle for "this id exists on this agent".
    assert src.count('detail="Execution not found"') >= 2
