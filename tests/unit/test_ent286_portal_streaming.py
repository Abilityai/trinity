"""The Workspace can watch a turn while it runs (ent#286).

The Workspace had no way to show tool activity for one structural reason: the
client never learned an execution id, because the chat call only returned once
the turn was already over. The agent has streamed its log all along, and the
backend already proxies exactly that for public links — the missing piece was
an id, early.

The tests worth writing are not "does SSE work" (that machinery predates this
change and is exercised by the public-link path). They are about the two things
this change actually decides:

  * the turn must be IDENTICAL whether it is watched or not — same resume, same
    persistence, same cache write. A second, drifting implementation of a
    billed turn is the failure this design exists to avoid, and it would show up
    as "streaming conversations quietly stop remembering".
  * who may watch. Executions are agent-scoped, so a shared agent's clients can
    reach each other's execution ids; the caller check is the only thing
    standing between that and reading someone else's conversation.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import types
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault(
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent286.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent286-logs")
)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "bob@example.com"
OTHER = "eve@example.com"
SESSION = "sess-1"
EXEC_ID = "exec-abc"


@pytest.fixture()
def portal(monkeypatch):
    from client_portal import service as svc
    from database import db as core_db

    state = types.SimpleNamespace(chat_calls=[], created=[], row=None, availability="ready")

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: s or SESSION)

    # `availability` (#2196): `start_portal_turn` resolved the state for its own
    # gate and hands it down, so the streamed turn costs ONE Docker read rather
    # than one at dispatch and another inside `portal_chat`.
    async def _fake_chat(agent_name, message, email, session_id=None,
                        include_owned=False, execution_id=None,
                        turn_timeout_seconds=None, availability=None, **kw):
        state.chat_calls.append({
            "agent": agent_name, "message": message, "email": email,
            "session_id": session_id, "execution_id": execution_id,
            "availability": availability,
        })
        return {"response": "done", "cost": 0.01, "session_id": session_id}

    monkeypatch.setattr(svc, "portal_chat", _fake_chat)

    def _create(**kwargs):
        state.created.append(kwargs)
        return types.SimpleNamespace(id=EXEC_ID)

    monkeypatch.setattr(core_db, "create_task_execution", _create)
    monkeypatch.setattr(core_db, "get_agent_subscription_id", lambda a: "sub-1")
    monkeypatch.setattr(core_db, "get_execution", lambda eid: state.row)

    # Default: the agent is up. A turn is refused for an agent that cannot run,
    # so every test that isn't ABOUT that needs the healthy case; the ones that
    # are override this.
    #
    # Patched on the SEAM, not on services.docker_service: a sibling module
    # installs a MagicMock stub for that module at collection time, and patching
    # one copy while the code resolves another is why these tests passed alone
    # and failed in the full suite.
    #
    # #2196 moved the seam from the boolean `_agent_is_running` to the tri-state
    # `_agent_availability`, because the turn paths need the STATE — one Docker
    # read has to answer both "may this dispatch?" and "what do we tell the
    # client?". Patching the old boolean here would now be INERT, which is worse
    # than no patch: the real read would run against whatever Docker the machine
    # happens to have, and these tests would pass or fail by daemon.
    async def _available(name):
        return state.availability

    state.availability = "ready"
    monkeypatch.setattr(svc, "_agent_availability", _available)
    return svc, state


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The watched turn is the same turn
# ---------------------------------------------------------------------------


def test_the_id_comes_back_before_the_turn_finishes(portal):
    """The whole feature in one assertion: the caller gets something to
    subscribe to while the agent is still working."""
    svc, state = portal
    out = _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))

    assert out["execution_id"] == EXEC_ID
    assert out["session_id"] == SESSION


def test_the_streamed_turn_runs_the_same_code_path(portal):
    """It delegates to `portal_chat` with the pre-created id — it does not
    reimplement the turn. A second implementation is how the resume, the
    persistence and the UUID cache write silently diverge."""
    svc, state = portal
    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert len(state.chat_calls) == 1
    call = state.chat_calls[0]
    assert call["execution_id"] == EXEC_ID   # same row the client is watching
    assert call["message"] == "hello"
    assert call["session_id"] == SESSION


def test_the_thread_is_resolved_before_dispatch(portal):
    """The client must be able to adopt the thread immediately — a refresh
    mid-turn has to reattach to it, not open a second conversation."""
    svc, state = portal
    out = _run(svc.start_portal_turn(AGENT, "hello", EMAIL, None))
    assert out["session_id"] == SESSION


def test_an_off_roster_agent_never_mints_an_execution(portal, monkeypatch):
    """The scope gate runs BEFORE the row is created. Otherwise a caller
    outside scope could mint execution rows (and stream ids) at will."""
    svc, state = portal
    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: False)

    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))

    assert excinfo.value.status_code == 404
    assert state.created == []


def test_a_row_that_cannot_be_created_fails_loudly(portal, monkeypatch):
    """No id means no stream. Better a clear error than a turn the client can
    never watch and never sees the end of."""
    svc, _ = portal
    from database import db as core_db
    monkeypatch.setattr(core_db, "create_task_execution", lambda **kw: None)

    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))
    assert excinfo.value.status_code == 500


# ---------------------------------------------------------------------------
# Who may watch
# ---------------------------------------------------------------------------


def _execution(agent_name=AGENT, email=EMAIL):
    return types.SimpleNamespace(agent_name=agent_name, source_user_email=email)


def test_a_caller_may_watch_their_own_turn(portal):
    svc, state = portal
    state.row = _execution()
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is True
    # Case-insensitively — a verified email is not case-sensitive identity.
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL.upper()) is True


def test_a_caller_may_not_watch_someone_elses_turn(portal):
    """The load-bearing one. Executions are agent-scoped, so two clients of one
    shared agent can reach each other's ids; only this check stops one of them
    reading the other's conversation."""
    svc, state = portal
    state.row = _execution(email=OTHER)
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is False


def test_an_execution_of_another_agent_is_refused(portal):
    svc, state = portal
    state.row = _execution(agent_name="someone-else")
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is False


def test_an_unknown_or_unattributed_execution_is_refused(portal):
    """A row with no `source_user_email` belongs to nobody in portal terms —
    a schedule, a webhook, an agent-to-agent call. It is not the caller's to
    watch, and an empty-string comparison must not make it so."""
    svc, state = portal
    state.row = None
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is False
    state.row = _execution(email=None)
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is False
    state.row = _execution(email="")
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, "") is False


def test_a_lookup_failure_refuses_rather_than_admits(portal, monkeypatch):
    """Fail closed: an unreadable row is not proof of ownership."""
    svc, _ = portal
    from database import db as core_db

    def _boom(eid):
        raise RuntimeError("db down")

    monkeypatch.setattr(core_db, "get_execution", _boom)
    assert svc.execution_belongs_to_caller(EXEC_ID, AGENT, EMAIL) is False


async def _drain(coro):
    """Await the start, then let its background task run to completion."""
    out = await coro
    await asyncio.sleep(0)          # let the task start
    from client_portal import service as svc
    while svc._INFLIGHT_TURNS:
        await asyncio.sleep(0.01)
    return out


# ---------------------------------------------------------------------------
# Reattach after a reload
# ---------------------------------------------------------------------------


class _FakeRedis:
    def __init__(self, data=None, broken=False):
        self.data = dict(data or {})
        self.broken = broken

    def set(self, k, v, ex=None):
        if self.broken:
            raise RuntimeError("redis down")
        self.data[k] = v

    def get(self, k):
        if self.broken:
            raise RuntimeError("redis down")
        return self.data.get(k)

    def delete(self, k):
        if self.broken:
            raise RuntimeError("redis down")
        self.data.pop(k, None)

    def scan_iter(self, match=None, count=None):
        if self.broken:
            raise RuntimeError("redis down")
        return iter(list(self.data.keys()))


@pytest.fixture()
def redis_stub(monkeypatch):
    import redis_breaker_util
    fake = _FakeRedis()
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: fake)
    return fake


def test_the_inflight_marker_carries_the_execution_id(redis_stub):
    """A bare "busy" flag would not be enough: a client that reloaded has lost
    the id it was streaming, and this is where it gets it back."""
    from client_portal import service as svc

    svc.mark_turn_inflight(SESSION, EXEC_ID)
    assert svc.get_turn_inflight(SESSION) == EXEC_ID

    svc.clear_turn_inflight(SESSION)
    assert svc.get_turn_inflight(SESSION) is None


def test_a_running_turn_is_distinguishable_from_a_finished_one(redis_stub):
    """The agent answers 404 for both "not registered yet" and "already over".
    Only the marker tells them apart — and the proxy needs that to know whether
    to keep waiting or end the stream cleanly."""
    from client_portal import service as svc

    svc.mark_turn_inflight(SESSION, EXEC_ID)
    assert svc.get_turn_inflight_matches(EXEC_ID) is True
    assert svc.get_turn_inflight_matches("some-other-execution") is False

    svc.clear_turn_inflight(SESSION)
    assert svc.get_turn_inflight_matches(EXEC_ID) is False


def test_marker_lookups_fail_open_toward_waiting(monkeypatch):
    """Redis is unavailable. Waiting a bounded extra moment for a turn that
    might be streaming beats cutting off one that is."""
    import redis_breaker_util
    from client_portal import service as svc

    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: _FakeRedis(broken=True))
    assert svc.get_turn_inflight_matches(EXEC_ID) is True
    # ...but the per-session read has no safe default to invent, so it says
    # "nothing in flight" and the client picks the reply up from history.
    assert svc.get_turn_inflight(SESSION) is None


def test_a_started_turn_is_marked_and_then_cleared(portal, redis_stub):
    """End to end over the marker: set at dispatch, gone once the turn ends —
    on every exit path, or the UI reattaches forever to a turn that is over."""
    from client_portal import service as svc

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))
    assert svc.get_turn_inflight(SESSION) is None


def test_the_marker_is_set_while_the_turn_is_still_running(portal, redis_stub, monkeypatch):
    """The half that matters for a reload: while the turn runs, the marker
    names it."""
    from client_portal import service as svc

    seen = {}

    async def _slow_chat(agent_name, message, email, session_id=None,
                         include_owned=False, execution_id=None,
                         turn_timeout_seconds=None, availability=None):
        seen["during"] = svc.get_turn_inflight(session_id)
        return {"response": "done", "cost": 0.0, "session_id": session_id}

    monkeypatch.setattr(svc, "portal_chat", _slow_chat)
    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert seen["during"] == EXEC_ID


def test_a_stopped_agent_is_refused_before_anything_is_created(portal, monkeypatch):
    """A turn the agent cannot run must fail at dispatch, not after.

    Accepting it returned 202, so the client believed a turn had started; the
    stream then 503'd, leaving an orphan execution row, a doomed background
    task, and a client with nothing to show the user. Worse, a client that
    treats a failed stream as a failed dispatch re-sends — and the observed
    result was the same message persisted twice, 320ms apart.
    """
    svc, state = portal
    state.availability = "stopped"

    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))

    assert excinfo.value.status_code == 502
    # #2196: the copy names the state and the next action, and drops "try
    # again" — for these two states retrying cannot work.
    assert "start it" in excinfo.value.detail.lower()
    assert "try again" not in excinfo.value.detail.lower()
    assert state.created == [], "an execution row was created for a turn that cannot run"


def test_a_containerless_agent_is_refused_with_its_own_copy(portal):
    """#2196: "stopped" and "no container at all" are different refusals.

    The old single message said "it may be offline… Please try again", which is
    fair for a stopped agent and actively misleading for one whose container is
    gone — retrying never works there, and that is the state a Workspace client
    is most likely to hit (#1747 documents it as routine).
    """
    svc, state = portal
    state.availability = "unavailable"

    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))

    assert excinfo.value.status_code == 502
    assert excinfo.value.detail != svc._refusal_detail("stopped")
    assert "try again" not in excinfo.value.detail.lower()
    assert state.created == []


def test_a_running_agent_still_dispatches(portal):
    svc, state = portal
    state.availability = "ready"

    out = _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))
    assert out["execution_id"] == EXEC_ID


def test_an_unreadable_docker_still_dispatches(portal):
    """The fail-OPEN direction at the turn gate (#2196).

    `unknown` means "Docker could not be asked", which is not evidence the agent
    is down: a daemon restart or a socket-permission change leaves containers
    running and answering HTTP over the agent network. Refusing here would turn
    one unreadable socket into "no Workspace client can send a message".
    """
    svc, state = portal
    state.availability = "unknown"

    out = _run(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION))
    assert out["execution_id"] == EXEC_ID


def test_a_docker_hiccup_does_not_block_a_turn(portal, monkeypatch):
    """Fail OPEN on the running-check: Docker being briefly unreadable is not
    evidence the agent is down, and refusing a healthy turn is the worse error.

    #2196 repointed this at `agent_container_state`, the tri-state read
    `_agent_is_running` now uses. Left on `get_agent_container` it would have
    passed VACUOUSLY — the raiser would never be called, and the fix's most
    important behaviour would have lost its only guard.

    It also matters that this exercises the REAL predicate: before #2196 the
    docstring claimed fail-open while the code failed CLOSED, because
    `get_agent_container` had already swallowed the error and returned None, so
    the `except` branch was unreachable. A test that patched the boolean seam
    with a lambda could never have caught that.
    """
    svc, _ = portal

    def _boom(name):
        raise RuntimeError("docker socket down")

    # Patched on the sys.modules ENTRY, which is the exact object
    # `_agent_is_running`'s in-function import resolves —
    # `import services.docker_service as m` can bind a different copy when a
    # sibling module has stubbed it, which is the whole bug this file just hit.
    import sys
    module = sys.modules["services.docker_service"]
    monkeypatch.setattr(module, "agent_container_state", _boom)

    assert svc._agent_is_running(AGENT) is True


def test_the_running_check_reads_the_tri_state_and_fails_open_on_unknown(portal, monkeypatch):
    """The unreadable-Docker case that does NOT raise.

    `agent_container_state` returns `None` for "could not be asked" — it does
    not raise — so the fail-open rule has to hold on the RETURN value too, not
    only in the `except`. That is the exact shape of the bug #2196 fixed: the old
    implementation's swallowed-error path made its documented `except` branch
    dead code.
    """
    import sys
    svc, _ = portal
    module = sys.modules["services.docker_service"]

    monkeypatch.setattr(module, "agent_container_state", lambda name: None)
    assert svc._agent_is_running(AGENT) is True          # unreadable ⇒ allow

    monkeypatch.setattr(module, "agent_container_state", lambda name: "missing")
    assert svc._agent_is_running(AGENT) is False         # Docker answered: gone

    monkeypatch.setattr(module, "agent_container_state", lambda name: "stopped")
    assert svc._agent_is_running(AGENT) is False

    monkeypatch.setattr(module, "agent_container_state", lambda name: "running")
    assert svc._agent_is_running(AGENT) is True


def test_the_streamed_turn_does_not_re_read_docker(portal):
    """One Docker read per turn (#2196).

    `start_portal_turn` gates on the resolved state and `portal_chat` gates on it
    too — the `/chat` path has no other gate. Letting the background turn resolve
    it again would double the read on the hottest portal path for no new
    information, so the dispatch hands its answer down.
    """
    svc, state = portal

    _run(_drain(svc.start_portal_turn(AGENT, "hello", EMAIL, SESSION)))

    assert state.chat_calls[0]["availability"] == "ready"
