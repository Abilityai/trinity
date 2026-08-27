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

    Review finding NEW-7: this was `inspect.getsource` plus `assert
    'triggered_by' in src`, which a guard inverted to `== "public"` satisfies
    just as happily. A security narrowing has to be exercised, so this drives
    the route with a `triggered_by="schedule"` row and asserts the refusal.
    """
    import asyncio
    from types import SimpleNamespace

    import pytest as _pytest
    from fastapi import HTTPException
    from routers import public as public_router

    def _row(triggered_by):
        return SimpleNamespace(
            id="exec-1", agent_name="scribe", triggered_by=triggered_by,
            status="running", source_user_email=None,
        )

    def _arrange(monkeypatch, row, *, terminated):
        monkeypatch.setattr(public_router, "_get_client_ip", lambda request: "1.2.3.4")
        monkeypatch.setattr(public_router, "check_public_link_rate_limit", lambda ip: None)
        monkeypatch.setattr(
            public_router, "_validate_public_link",
            lambda token: {"id": "link-1", "agent_name": "scribe"},
        )
        monkeypatch.setattr(public_router, "_agent_requires_email", lambda name: False)
        monkeypatch.setattr(public_router.db, "get_execution", lambda eid: row)

        async def _term(**kwargs):
            terminated.append(kwargs)
            return {"status": "terminated", "execution_id": kwargs["execution_id"]}
        monkeypatch.setattr(public_router, "_terminate_execution", _term)

    def _call():
        return asyncio.run(public_router.public_terminate_execution(
            token="tok", execution_id="exec-1", request=object(), session_token=None,
        ))

    # A scheduled run on the same agent — the owner's autonomous work — is
    # refused, and refused with the SAME uniform 404 as an unknown execution, so
    # the route is not an oracle for "this id exists on this agent".
    with _pytest.MonkeyPatch.context() as mp:
        terminated = []
        _arrange(mp, _row("schedule"), terminated=terminated)
        with _pytest.raises(HTTPException) as e:
            _call()
        assert e.value.status_code == 404
        assert e.value.detail == "Execution not found"
        assert terminated == [], "a scheduled run must never reach the terminator"

    # ...while the turn the link itself produced still cancels, so the guard
    # cannot be satisfied by refusing everything.
    with _pytest.MonkeyPatch.context() as mp:
        terminated = []
        _arrange(mp, _row("public"), terminated=terminated)
        assert _call()["status"] == "terminated"
        assert len(terminated) == 1

# --------------------------------------------------------------------------
# Review findings (@obasilakis) — the durable verdict and the blast radius
# --------------------------------------------------------------------------

def test_a_cancelled_portal_turn_is_classified_as_cancelled_not_failed():
    """NEW-1: the suppression was client-side and in-memory, so it shielded
    exactly one tab until its next load. `loadThread` and `reattach` both call
    `markLastUserTurnFailed(last_turn_outcome)`, and `_run` records that verdict
    durably — so stopping a turn and then switching threads (or reloading) put
    the user's own message back in red under "Something went wrong", for
    something they did on purpose.

    The arm belongs in the classifier, ahead of the failure ladder.
    """
    import inspect
    from client_portal import service as mod
    src = inspect.getsource(mod.portal_chat)
    cancelled_at = src.index('status == "cancelled"')
    failed_at = src.index('status == "failed"')
    assert cancelled_at < failed_at, (
        "the cancelled arm must precede the failure ladder, or a cancellation "
        "is classified as an agent error"
    )
    assert 'category="cancelled"' in src


def test_the_client_keys_on_the_durable_category_not_its_own_memory():
    """The point of moving it server-side: the browser arm must read the
    recorded verdict, which survives a reload, rather than the in-memory set,
    which does not."""
    from pathlib import Path
    sfc = Path(__file__).resolve().parents[2] / "src/frontend/src/components/portal/PortalConversation.vue"
    body = sfc.read_text()
    assert "outcome.category === 'cancelled'" in body


def test_cancelling_one_turn_does_not_clear_the_whole_agents_capacity():
    """N1: `force_release(name)` is documented in-tree as "Emergency: clear all
    running slots and the in-memory queue" — it DELs the agent's whole slot
    ZSET plus the overflow list. ent#155 hands that path to a public-link
    visitor and a Workspace client, so on an agent with max_parallel_tasks > 1
    one person stopping their own turn dropped slot accounting for every other
    in-flight execution.

    It also fired on `already_finished`, where nothing was cancelled at all.
    """
    import inspect
    from services import chat_execution_service as mod
    # The public `terminate_execution` is a thin wrapper; the capacity
    # handling lives in the implementation it delegates to.
    src = inspect.getsource(mod._proxy_terminate_and_finalize)
    # The emergency clear is gone from this path entirely — it survives only on
    # the explicit operator force-release endpoint, which is where "clear
    # everything" belongs.
    assert "capacity.force_release(" not in src
    assert "release_if_matches" in src
    # And `already_finished` releases nothing: the branch is terminated-only.
    assert 'if result.get("status") == "terminated" and task_execution_id:' in src


def test_public_terminate_is_not_weaker_than_the_route_that_creates_the_turn():
    """Re-review finding. On a `require_email` link `POST /chat` demands a
    `session_token`, and `source_user_email` IS populated for the verified
    visitor — so the identity the old docstring said did not exist does exist
    on exactly those links. Without this, one visitor could stop another's turn
    with the link token alone: a destructive write gated more weakly than the
    write that created its target.

    Open links are unchanged — there really is no visitor identity there.
    """
    import inspect
    from routers import public as mod
    src = inspect.getsource(mod.public_terminate_execution)
    assert "session_token" in src, "no session gate on the terminate route"
    assert "_agent_requires_email(agent_name)" in src, (
        "the gate must apply only to links that HAVE a visitor identity"
    )
    assert "source_user_email" in src, "the turn is not bound to its own visitor"
    # Uniform 404 on a mismatch, never a distinguishable 403 (Invariant #8).
    body = src[src.index("_agent_requires_email(agent_name)"):]
    assert "404" in body, "a mismatch must not be distinguishable from absence"


def test_the_2320_cancelled_row_comment_matches_its_value():
    """Re-review nit: the comment beside the cancelled row said retryable is
    TRUE while the value is False. The value is right — a cancellation is not
    one of the two verdicts where nothing reached the agent — so the comment
    was the error."""
    from pathlib import Path
    body = Path(__file__).resolve().parents[1].joinpath(
        "unit/test_2320_portal_failed_turn_visibility.py").read_text()
    row = [l for l in body.splitlines() if "generic_cancelled" in l and "409" in l]
    assert row, "the cancelled row moved"
    assert "False" in row[0], "the cancelled verdict must not be retryable"
