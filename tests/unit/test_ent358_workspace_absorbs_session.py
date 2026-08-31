"""The Workspace absorbs the Session surface — without losing continuity (ent#358).

Removing the Agent Detail Session surface is a two-line change. What makes it
safe is everything else, because the surface being removed was the *more
capable* one: it resumed a real Claude session (`--resume`), while Workspace
chat replayed prior messages to the agent as prompt text. Replay recovers what
was said; it does not recover tool results, mid-skill state, or reasoning state.

So the tests here are about the downgrade NOT happening, and about the two ways
it could happen silently:

  * a Workspace turn that quietly stops resuming (no error — just an agent that
    forgot), and
  * the JSONL reaper deleting a live Workspace session file, which produces the
    same amnesia an hour later and from a completely different direction.

The prompt-composition tests look cosmetic and are not: sending the history
block on a RESUMED turn re-pays for context the session already holds, and puts
a summary of the conversation next to the conversation, inviting the model to
treat the summary as the record. Sending it on a COLD turn is the only
continuity that turn has. Same block, opposite correctness — which is why both
directions are pinned.
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
    "TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent358.db")
)
os.environ.setdefault(
    "LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent358-logs")
)

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "scribe"
EMAIL = "bob@example.com"
SESSION = "sess-1"
CACHED_UUID = "11111111-2222-3333-4444-555555555555"
FRESH_UUID = "99999999-8888-7777-6666-555555555555"


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Result:
    """Stand-in for TaskExecutionResult — only the fields portal_chat reads."""

    def __init__(self, status="success", response="ok", error=None, session_id=FRESH_UUID):
        self.status = status
        self.response = response
        self.error = error
        self.session_id = session_id
        self.cost = 0.01


class _Recorder:
    """Captures every execute_task call so a test can assert on the sequence."""

    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    async def execute_task(self, **kwargs):
        self.calls.append(kwargs)
        return self._results.pop(0) if self._results else _Result()


@pytest.fixture(autouse=True)
def _pin_container_state(monkeypatch):
    """#2196: pin the container-state seam for every test in this module.

    `portal_chat` gained a liveness gate (it had none, and its 502 fired only
    AFTER the user's message was durably written). Without this fixture the gate
    would consult the developer's real Docker — where these fixture agents have
    no container — so the module would pass in a Docker-less CI container and
    fail on every workstation, or the reverse. Patched on the consuming module's
    own attribute, which is why that read has a named seam at all.
    """
    from client_portal import service as svc

    async def _map(names):
        return {n: "ready" for n in names}

    async def _one(name):
        return "ready"

    monkeypatch.setattr(svc, "_availability_map", _map)
    monkeypatch.setattr(svc, "_agent_availability", _one)


@pytest.fixture()
def portal(monkeypatch):
    """`client_portal.service` with its DB, roster and execution stack stubbed.

    Everything faked here is a boundary the turn logic calls out to; the
    composition and resume decisions under test are the real code.
    """
    from client_portal import service as svc
    from client_portal import db as portal_db
    from services import session_turn_service

    state = types.SimpleNamespace(
        cached=None,
        cleared=False,
        failures=0,
        cached_writes=[],
        history=[],
        recorder=None,
    )

    monkeypatch.setattr(svc, "agent_on_roster", lambda a, e, include_owned=False: True)
    monkeypatch.setattr(svc, "_build_portal_system_prompt", lambda a, e: None)
    monkeypatch.setattr(svc, "_resolve_session_id", lambda a, e, s, **kw: SESSION)
    monkeypatch.setattr(svc, "_spawn_title_generation", lambda *a, **kw: None)

    async def _no_inbox(agent, email, message):
        return ([], [], [])

    monkeypatch.setattr(svc, "_collect_inbox_for_turn", _no_inbox)

    monkeypatch.setattr(portal_db, "get_portal_session", lambda *a, **kw: {"title": "t"})
    monkeypatch.setattr(portal_db, "get_portal_messages",
                        lambda *a, **kw: state.history)
    monkeypatch.setattr(portal_db, "add_portal_message", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "touch_portal_session", lambda *a, **kw: None)
    monkeypatch.setattr(portal_db, "get_cached_claude_session_id",
                        lambda sid: state.cached)

    def _clear(sid):
        state.cleared = True
        state.cached = None

    def _mark(sid):
        state.failures += 1
        return state.failures

    monkeypatch.setattr(portal_db, "clear_cached_claude_session_id", _clear)
    monkeypatch.setattr(portal_db, "mark_resume_failure", _mark)
    monkeypatch.setattr(portal_db, "update_cached_claude_session_id",
                        lambda sid, uuid: state.cached_writes.append((sid, uuid)))

    # Claude-like runtime unless a test says otherwise; no Docker in a unit test.
    monkeypatch.setattr(session_turn_service, "supports_session_resume", lambda a: True)
    monkeypatch.setattr(session_turn_service, "resolve_lock_ttl", lambda a: 60)

    class _NoLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(session_turn_service, "ResumeLock", _NoLock)

    def _install(results):
        state.recorder = _Recorder(results)
        monkeypatch.setattr(
            session_turn_service,
            "get_task_execution_service",
            lambda: state.recorder,
            raising=False,
        )
        # run_resumable_turn imports the accessor lazily from the module, so the
        # patch has to land where it looks it up.
        import services.task_execution_service as tes
        monkeypatch.setattr(tes, "get_task_execution_service", lambda: state.recorder)
        return state.recorder

    state.install = _install
    return svc, state


def _run(coro):
    """`asyncio.run`, not `get_event_loop().run_until_complete`.

    The latter passes when this file runs alone and fails in a full suite: a
    sibling module that closes the global loop leaves `get_event_loop()`
    handing back a closed one. A fresh loop per call has no such dependency on
    what ran before.
    """
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Continuity: a Workspace turn resumes the way a Session turn did
# ---------------------------------------------------------------------------


def test_cached_thread_resumes_its_claude_session(portal):
    """The whole point of the change. A thread with a cached id reattaches."""
    svc, state = portal
    state.cached = CACHED_UUID
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "and then?", EMAIL, SESSION))

    assert len(rec.calls) == 1
    assert rec.calls[0]["resume_session_id"] == CACHED_UUID
    # Even a resumed turn must persist, or the NEXT turn has no JSONL to find.
    assert rec.calls[0]["persist_session"] is True


def test_cold_thread_runs_without_resume_but_still_persists(portal):
    """A first turn has nothing to resume — but must write the JSONL that makes
    turn two resumable. This is the step whose omission looks fine forever and
    then loses every second turn."""
    svc, state = portal
    state.cached = None
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert rec.calls[0]["resume_session_id"] is None
    assert rec.calls[0]["persist_session"] is True


def test_successful_turn_caches_the_id_it_ran_under(portal):
    """Without this write the next turn goes cold — continuity silently resets
    every single turn, with a successful-looking chat the whole way."""
    svc, state = portal
    state.cached = None
    state.install([_Result(session_id=FRESH_UUID)])

    _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert state.cached_writes == [(SESSION, FRESH_UUID)]


def test_a_failed_cache_write_does_not_fail_an_already_billed_turn(portal, monkeypatch):
    """The turn ran and cost money. Losing the cache costs the NEXT turn its
    continuity; losing the reply costs the user their answer."""
    svc, state = portal
    from client_portal import db as portal_db

    def _boom(sid, uuid):
        raise RuntimeError("db down")

    monkeypatch.setattr(portal_db, "update_cached_claude_session_id", _boom)
    state.install([_Result(response="the answer")])

    out = _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert out["response"] == "the answer"


# ---------------------------------------------------------------------------
# Prompt composition: the history block belongs on cold turns only
# ---------------------------------------------------------------------------


def test_resumed_turn_does_not_replay_history_into_the_prompt(portal):
    """The session already holds it. Replaying is double-billing plus an
    invitation to treat the summary as the record."""
    svc, state = portal
    state.cached = CACHED_UUID
    state.history = [
        {"role": "user", "content": "what is the deadline"},
        {"role": "assistant", "content": "friday"},
    ]
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "and the budget?", EMAIL, SESSION))

    assert "Conversation so far" not in rec.calls[0]["message"]
    assert "and the budget?" in rec.calls[0]["message"]


def test_cold_turn_keeps_replaying_history(portal):
    """A cold turn has no session memory, so the replay is the ONLY continuity
    it has. Dropping it here would be the regression this change exists to
    avoid, arriving through the other door."""
    svc, state = portal
    state.cached = None
    state.history = [
        {"role": "user", "content": "what is the deadline"},
        {"role": "assistant", "content": "friday"},
    ]
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "and the budget?", EMAIL, SESSION))

    assert "Conversation so far" in rec.calls[0]["message"]
    assert "friday" in rec.calls[0]["message"]


# ---------------------------------------------------------------------------
# Resume failure: the JSONL is gone
# ---------------------------------------------------------------------------


def test_missing_jsonl_falls_back_to_one_cold_retry_with_history_restored(portal):
    """The reaped-JSONL path. The retry has no session memory, so the history
    block the resumed attempt correctly omitted has to come BACK for it —
    otherwise a reaped session answers its next question with no context at
    all, which reads to the user as the agent losing the thread."""
    svc, state = portal
    state.cached = CACHED_UUID
    state.history = [{"role": "assistant", "content": "friday"}]
    rec = state.install([
        _Result(status="failed", error="No conversation found with session ID: x"),
        _Result(status="success"),
    ])

    out = _run(svc.portal_chat(AGENT, "and the budget?", EMAIL, SESSION))

    assert len(rec.calls) == 2
    assert rec.calls[0]["resume_session_id"] == CACHED_UUID
    assert "Conversation so far" not in rec.calls[0]["message"]

    assert rec.calls[1]["resume_session_id"] is None
    assert "Conversation so far" in rec.calls[1]["message"]
    assert "friday" in rec.calls[1]["message"]

    # The stale id is dropped and the failure counted, so the next turn starts
    # clean instead of re-failing against the same missing file.
    assert state.cleared is True
    assert state.failures == 1
    assert out["response"]


def test_a_real_agent_error_is_not_treated_as_a_resume_failure(portal):
    """Only the missing-JSONL marker may trigger a retry. Retrying a genuine
    failure doubles the spend and the latency for nothing."""
    svc, state = portal
    state.cached = CACHED_UUID
    rec = state.install([_Result(status="failed", error="the agent exploded")])

    with pytest.raises(svc.ClientPortalError):
        _run(svc.portal_chat(AGENT, "hi", EMAIL, SESSION))

    assert len(rec.calls) == 1
    assert state.cleared is False


# ---------------------------------------------------------------------------
# Runtimes without --resume
# ---------------------------------------------------------------------------


def test_codex_runtime_runs_stateless_and_keeps_its_history_replay(portal, monkeypatch):
    """Codex has no `--resume`. Passing it a cached id would produce failure
    text this code reads as Claude's, so the id is dropped — and because the
    turn is then cold, the history replay must come back with it."""
    svc, state = portal
    from services import session_turn_service

    monkeypatch.setattr(session_turn_service, "supports_session_resume", lambda a: False)
    state.cached = CACHED_UUID
    state.history = [{"role": "assistant", "content": "friday"}]
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "and the budget?", EMAIL, SESSION))

    assert rec.calls[0]["resume_session_id"] is None
    assert "Conversation so far" in rec.calls[0]["message"]


# ---------------------------------------------------------------------------
# Lock contention
# ---------------------------------------------------------------------------


def test_a_busy_thread_answers_429_not_a_500(portal, monkeypatch):
    """Two turns on one thread race on the same JSONL. The second must get a
    retryable answer in the Workspace's own error vocabulary, not a stack
    trace — the engine raises an HTTP-shaped error the Session router wants."""
    svc, state = portal
    from services import session_turn_service

    class _Busy:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            raise session_turn_service.ResumeLockBusy("session_lock:x")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(session_turn_service, "ResumeLock", _Busy)
    state.cached = CACHED_UUID
    state.install([_Result()])

    with pytest.raises(svc.ClientPortalError) as excinfo:
        _run(svc.portal_chat(AGENT, "hi", EMAIL, SESSION))

    assert excinfo.value.status_code == 429


# ---------------------------------------------------------------------------
# The reaper must not delete live Workspace sessions
# ---------------------------------------------------------------------------


def test_reaper_keep_set_covers_workspace_threads(monkeypatch):
    """The quietest way to undo this whole feature: keep resuming correctly,
    but let the 6h sweep delete the JSONL an hour after it is written. Every
    Workspace conversation then goes cold with no error anywhere.

    Three old files, all past the age guard: one held by a Session-tab row, one
    held by a Workspace thread, one held by nobody. Only the last may be
    deleted."""
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from database import db as core_db

    session_uuid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    workspace_uuid = "11111111-2222-3333-4444-555555555555"
    orphan_uuid = "99999999-9999-9999-9999-999999999999"

    monkeypatch.setattr(core_db, "list_active_claude_session_ids",
                        lambda agent: [session_uuid])
    monkeypatch.setattr(portal_db, "list_active_claude_session_ids",
                        lambda agent: [workspace_uuid])

    old = "1000000000"  # long past any age guard
    listing = "\n".join(
        f"{u}.jsonl {old}" for u in (session_uuid, workspace_uuid, orphan_uuid)
    )
    removed = []

    async def _fake_exec(container, cmd, timeout=30):
        if cmd.startswith("rm -f"):
            removed.append(cmd)
            return {"exit_code": 0, "output": ""}
        return {"exit_code": 0, "output": listing}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _fake_exec)

    svc = cleanup.SessionCleanupService()
    per = _run(svc._sweep_agent(AGENT))

    assert per["deleted"] == 1
    assert any(orphan_uuid in c for c in removed)
    assert not any(workspace_uuid in c for c in removed), (
        "a live Workspace session's JSONL was reaped — every thread on this "
        "agent just silently lost its memory"
    )
    assert not any(session_uuid in c for c in removed)


def test_reaper_skips_the_sweep_when_the_workspace_keep_set_cannot_load(monkeypatch):
    """Fail-closed. Skipping a cycle costs disk space; reaping against a
    partial keep set costs users their conversations."""
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from database import db as core_db

    monkeypatch.setattr(core_db, "list_active_claude_session_ids",
                        lambda agent: ["session-tab-uuid"])

    def _boom(agent):
        raise RuntimeError("db down")

    monkeypatch.setattr(portal_db, "list_active_claude_session_ids", _boom)

    reached = {"container": False}

    async def _fake_exec(container, cmd, timeout=30):
        reached["container"] = True
        return {"exit_code": 0, "output": ""}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _fake_exec)

    svc = cleanup.SessionCleanupService()
    per = _run(svc._sweep_agent(AGENT))

    assert reached["container"] is False, "must not list (or reap) files blind"
    assert per["errors"] == 1
    assert per["deleted"] == 0


def test_a_failed_turn_does_not_cache_its_session_id(portal):
    """A failed turn can still have written a JSONL. Caching that id would aim
    every later turn at a session whose last act was to fail — so the cache
    write sits AFTER the success gate, as it does on the Session surface."""
    svc, state = portal
    state.cached = None
    state.install([_Result(status="failed", error="the agent exploded",
                           session_id="half-written-uuid")])

    with pytest.raises(svc.ClientPortalError):
        _run(svc.portal_chat(AGENT, "hi", EMAIL, SESSION))

    assert state.cached_writes == []


# ---------------------------------------------------------------------------
# Scope: what you can DO must equal what you can SEE
# ---------------------------------------------------------------------------


@pytest.fixture()
def roster(monkeypatch):
    """Stub the two roster reads the scope gate is built from."""
    from client_portal import service as svc
    from client_portal import db as portal_db

    monkeypatch.setattr(portal_db, "get_shared_roster",
                        lambda email: [{"agent_name": "shared-with-me"}])
    monkeypatch.setattr(portal_db, "get_owned_roster",
                        lambda email: [{"agent_name": "my-own-agent"}])
    return svc


def test_an_owner_can_act_on_their_own_agent(roster):
    """The bug that made the Workspace unusable.

    `get_roster(include_owned=True)` (ent#357's one-click platform entry) shows
    a platform user the agents they OWN, but the scope gate read only the
    SHARED roster — and Trinity refuses a self-share, so an owner's agents are
    never in that set. Result: every agent visible in the sidebar 404'd on
    every action, with no way to grant yourself access.
    """
    assert roster.agent_on_roster("my-own-agent", "me@example.com", include_owned=True) is True
    assert roster.agent_on_roster("shared-with-me", "me@example.com", include_owned=True) is True


def test_a_client_session_never_reaches_owned_agents(roster):
    """The other half, and the reason this is opt-in rather than defaulted.

    An external client's scope is exactly what was shared with them. If the
    flag defaulted on, a portal-token session for an email that also owns
    agents would silently gain access to every one of them — the hazard
    `get_roster`'s own docstring warns about.
    """
    assert roster.agent_on_roster("my-own-agent", "me@example.com") is False
    assert roster.agent_on_roster("my-own-agent", "me@example.com", include_owned=False) is False
    # What WAS shared still works, both ways.
    assert roster.agent_on_roster("shared-with-me", "me@example.com") is True


def test_an_agent_on_neither_roster_is_refused(roster):
    for include_owned in (True, False):
        assert roster.agent_on_roster("stranger", "me@example.com", include_owned=include_owned) is False


def test_the_current_message_is_not_replayed_as_its_own_context(portal):
    """Ordering guard (ent#286 fallout).

    Persisting the user's message early — so a mid-turn reload doesn't look
    like message loss — put it in the table BEFORE the history read that builds
    the context block, so a cold turn arrived as "Client: hello" followed by
    "hello". The reads that must not see it (thread title for first-exchange
    detection, history for context) now happen first, deliberately.
    """
    svc, state = portal
    state.cached = None
    state.history = []          # nothing prior — this is turn one
    rec = state.install([_Result()])

    _run(svc.portal_chat(AGENT, "hello", EMAIL, SESSION))

    assert rec.calls[0]["message"] == "hello", (
        "the turn's own message was replayed back to it as conversation context"
    )


# ---------------------------------------------------------------------------
# Review fixes
# ---------------------------------------------------------------------------


def test_a_retry_after_a_failed_turn_does_not_duplicate_the_user_message(portal, monkeypatch):
    """Review finding: persisting the user's half BEFORE the turn (so a reload
    never looks like data loss) meant a failed turn left the row behind, and the
    UI's Retry wrote it again — the thread showed it twice, message_count
    double-counted, and the duplicate was replayed into the next cold turn's
    context, telling the model the client asked twice.
    """
    svc, state = portal
    from client_portal import db as portal_db

    written = []
    monkeypatch.setattr(portal_db, "add_portal_message",
                        lambda mid, agent, email, role, content, cost, ts, session_id=None:
                        written.append((role, content)))
    # The state a failed turn leaves: the client's message is last, unanswered.
    monkeypatch.setattr(portal_db, "get_portal_messages",
                        lambda *a, **kw: [{"role": "user", "content": "draft the invoice"}])

    svc._persist_user_turn(AGENT, EMAIL, SESSION, "draft the invoice")
    assert written == [], "the retry re-persisted the same user message"

    # A genuinely new message still lands.
    svc._persist_user_turn(AGENT, EMAIL, SESSION, "actually, make it two")
    assert written == [("user", "actually, make it two")]


def test_the_same_text_after_a_reply_is_not_treated_as_a_retry(portal, monkeypatch):
    """The guard must not swallow someone deliberately repeating themselves —
    only the exact state a failed turn leaves (their message last, unanswered)."""
    svc, state = portal
    from client_portal import db as portal_db

    written = []
    monkeypatch.setattr(portal_db, "add_portal_message",
                        lambda mid, agent, email, role, content, cost, ts, session_id=None:
                        written.append((role, content)))
    monkeypatch.setattr(portal_db, "get_portal_messages",
                        lambda *a, **kw: [{"role": "assistant", "content": "done"}])

    svc._persist_user_turn(AGENT, EMAIL, SESSION, "do it again")
    assert written == [("user", "do it again")]


def test_a_turn_that_never_started_gets_a_terminal(portal, monkeypatch):
    """Review finding: the execution row is created BEFORE the background turn
    so the client has something to subscribe to, but nothing wrote a terminal
    for a raise that happened before execute_task — leaving the row RUNNING
    forever and inviting the cleanup watchdog to fabricate a 'silent launch
    failure' against a healthy agent."""
    svc, _ = portal
    from database import db as core_db

    calls = []
    monkeypatch.setattr(core_db, "update_execution_status",
                        lambda eid, status, **kw: calls.append((eid, status, kw.get("error"))))

    svc._fail_unstarted_execution("exec-1", "This conversation is already handling a message.")

    assert len(calls) == 1
    eid, status, error = calls[0]
    assert (eid, status) == ("exec-1", "failed")
    assert "already handling" in error


def test_finalizing_an_unstarted_execution_never_raises(portal, monkeypatch):
    """It runs on the failure path of a background task; raising there would
    replace a useful log line with an unhandled-exception traceback."""
    svc, _ = portal
    from database import db as core_db

    def _boom(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(core_db, "update_execution_status", _boom)
    svc._fail_unstarted_execution("exec-1", "reason")   # must not raise
