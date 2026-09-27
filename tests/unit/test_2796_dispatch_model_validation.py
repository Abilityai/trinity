"""#2796 — a caller-supplied `model` is shape-checked at the request boundary.

Before this, all three interactive entry points took `model` as an unvalidated
free string and passed it to the runtime as a `--model` argv element. A value
that cannot name a model came back as
`[claude-code:unrecognized_model] {"model":"admin"}` — exit code 1, no output,
and no field named anywhere in the message.

The reported diagnosis was that Trinity passed the CALLER'S ROLE into the model
slot. It does not, and `test_2796_role_does_not_reach_the_model_slot` below is
the guard for that: with the body the UI actually sends, the slot is `None`, and
the only `"admin"` in scope is `source_user_email` — the admin account has no
email, so it falls back to the fixed username, and it lands in its own column.
The value reaches `model` only when the request body carries it, which is what
these tests close.

Every call-site is covered, per CONTRIBUTING's bug-fix rule:

  * `POST /api/agents/{name}/task`                       (ParallelTaskRequest)
  * `POST /api/agents/{name}/chat`                       (ChatMessageRequest)
  * `POST /api/agents/{name}/sessions/{id}/message`      (SessionMessageRequest)

plus `run_resumable_turn`, which splats one value into `execute_task` TWICE
(initial attempt + cold retry) — validating the router's single source is what
makes both safe, and the last test proves the second call is not missed.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import services.chat_execution_service as _CE
import services.dispatch_admission_service as _DISPATCH
import services.session_turn_service as _STS
from models import (
    ChatMessageRequest,
    ParallelTaskRequest,
    SessionMessageRequest,
    TaskExecutionStatus,
)
from routers.chat import chat_with_agent as CHAT_ENDPOINT
from routers.chat import execute_parallel_task as TASK_ENDPOINT
from routers.sessions import send_session_message as SESSION_ENDPOINT
from services import chat_persistence_service
from services.chat_signals import ChatAdmissionReplay
from services.model_catalog import (
    InvalidModelError,
    MODEL_CATALOG,
    validate_dispatch_model,
)
from services.model_context import _FAMILY_PREFIX_WINDOWS

pytestmark = pytest.mark.unit

_CHAT_MOD = sys.modules[TASK_ENDPOINT.__module__]      # routers.chat
_SESSION_MOD = sys.modules[SESSION_ENDPOINT.__module__]  # routers.sessions

# The value from the report: the operator's role, and also the fixed admin
# username. Neither is a model id.
BAD_MODEL = "admin"


def _admin():
    """The reporting caller: role 'admin', and no email — so `email or username`
    falls back to the literal string "admin" (CLAUDE.md: username fixed as
    'admin'). That fallback is what makes this the right fixture for #2796."""
    u = MagicMock()
    u.id = 1
    u.email = None
    u.username = "admin"
    u.role = "admin"
    u.agent_name = None
    u.mcp_key_id = None
    u.mcp_key_name = None
    return u


def _result():
    r = MagicMock()
    r.status = TaskExecutionStatus.SUCCESS
    r.response = "done"
    r.error = None
    r.cost = 0.01
    r.context_used = 10
    r.context_max = 200000
    r.raw_response = {"response": "done"}
    r.execution_id = "exec1"
    r.session_id = None
    return r


# ---------------------------------------------------------------------------
# the validator itself
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, "", "   "])
def test_2796_blank_normalises_to_inherit(raw):
    """Blank means *inherit the platform default*, never *invalid* — the picker's
    default option submits "". Normalising before validating is load-bearing."""
    assert validate_dispatch_model(raw) is None


@pytest.mark.parametrize(
    "raw",
    [
        "sonnet", "opus", "haiku", "fable",          # short aliases
        "claude-sonnet-5",
        "claude-sonnet-4-6[1m]",                      # documented free-text suffix
        "claude-some-model-that-does-not-exist-yet",  # id shipped after this catalog
        "gemini-3-pro",                               # Gemini-runtime agents
        "gpt-5.1-codex",                              # Codex-runtime agents
        "gpt-5.6-sol",
        "codex",                                      # the bare Codex id
        "Claude-Opus-5",                              # case-folded, like model_context
        "  claude-opus-5  ",                          # stripped, not rejected
    ],
)
def test_2796_accepts_anything_that_can_name_a_model(raw):
    assert validate_dispatch_model(raw) == raw.strip()


def test_2796_every_catalog_id_survives_the_gate():
    """The gate must never refuse an id this repo ships — the failure mode of
    using the 3-id Workspace allow-list on the operator surface."""
    for entry in MODEL_CATALOG:
        assert validate_dispatch_model(entry.id) == entry.id


def test_2796_gate_covers_every_family_model_context_knows():
    """The gate's family list is mirrored from `model_context`, which is the
    platform's existing "do we recognise this id?" registry. If a runtime is
    added there and not here, its agents become undispatchable — a Codex agent
    (`gpt-*`) is exactly the case this catches. Drift fails the build."""
    for prefix, _window in _FAMILY_PREFIX_WINDOWS:
        probe = prefix + "probe"
        assert validate_dispatch_model(probe) == probe, (
            f"model_context recognises the {prefix!r} family but the #2796 gate "
            f"refuses it — add it to _MODEL_FAMILY_PREFIXES in model_catalog.py"
        )


@pytest.mark.parametrize(
    "raw",
    [
        BAD_MODEL,
        "user",
        "--dangerously-skip-permissions",  # argv smuggling, closed in passing
        "-p",
    ],
)
def test_2796_rejects_what_cannot_name_a_model(raw):
    with pytest.raises(InvalidModelError):
        validate_dispatch_model(raw)


def test_2796_refusal_names_the_offending_value_and_bounds_it():
    """The issue asks for an error 'naming the offending value'. It must also be
    bounded: the field is unbounded at the payload layer."""
    with pytest.raises(InvalidModelError) as exc:
        validate_dispatch_model(BAD_MODEL)
    assert BAD_MODEL in str(exc.value)

    with pytest.raises(InvalidModelError) as exc:
        validate_dispatch_model("z" * 5000)
    assert len(str(exc.value)) < 300
    assert "…" in str(exc.value)


# ---------------------------------------------------------------------------
# POST /api/agents/{name}/task  — ParallelTaskRequest
# ---------------------------------------------------------------------------

@contextmanager
def _task_env():
    idem = MagicMock(replay=False, in_flight=False, execution_id="e0", snapshot=None)
    db = MagicMock()
    db.get_execution_timeout.return_value = 3600
    db.get_max_parallel_tasks.return_value = 3
    db.get_agent_subscription_id.return_value = None
    db.create_task_execution.return_value = MagicMock(id="exec1")
    db.resume_session_belongs_to_user.return_value = True
    isvc = MagicMock()
    isvc.begin.return_value = idem
    cap = MagicMock()
    cap.acquire = AsyncMock(return_value=MagicMock(state="admitted", queue_position=0))
    cap.release = AsyncMock()
    cap.force_release = AsyncMock()
    task_service = MagicMock()
    task_service.execute_task = AsyncMock(return_value=_result())
    with patch.object(_CHAT_MOD, "get_agent_container", return_value=MagicMock(status="running")), \
         patch.object(_CHAT_MOD, "db", db), \
         patch.object(_CE, "db", db), \
         patch.object(_CE, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "idempotency_service", isvc), \
         patch.object(_DISPATCH, "platform_audit_service", MagicMock(log=AsyncMock())), \
         patch.object(_CE, "get_capacity_manager", return_value=cap), \
         patch.object(_CE, "dispatch_breaker_active", return_value=False), \
         patch.object(_CE, "get_task_execution_service", return_value=task_service), \
         patch.object(_CE, "activity_service",
                      MagicMock(track_activity=AsyncMock(return_value="a1"),
                                complete_activity=AsyncMock())), \
         patch.object(chat_persistence_service, "persist_chat_session",
                      AsyncMock(return_value="cs1")), \
         patch.object(_CE, "wait_for_sync_terminal", AsyncMock(return_value=None)):
        yield {"db": db, "task_service": task_service, "idem": isvc}


def _call_task(request, user=None):
    return asyncio.run(TASK_ENDPOINT(
        request=request, name="agent1", current_user=user or _admin(),
        x_source_agent=None, x_via_mcp=None, idempotency_key="k1",
        x_event_trigger=None, x_internal_secret=None,
    ))


def test_2796_task_refuses_a_non_model_before_it_reaches_the_agent():
    with _task_env() as m:
        with pytest.raises(Exception) as exc:
            _call_task(ParallelTaskRequest(message="hi", model=BAD_MODEL))
    assert getattr(exc.value, "status_code", None) == 422
    assert BAD_MODEL in str(getattr(exc.value, "detail", ""))
    m["task_service"].execute_task.assert_not_awaited()
    m["db"].create_task_execution.assert_not_called()
    # Refused before the admission gate, so no idempotency key is burned.
    m["idem"].begin.assert_not_called()


def test_2796_task_still_dispatches_a_legitimate_model():
    with _task_env() as m:
        _call_task(ParallelTaskRequest(message="hi", model="claude-sonnet-5"))
    assert m["task_service"].execute_task.await_args.kwargs["model"] == "claude-sonnet-5"
    assert m["db"].create_task_execution.call_args.kwargs["model_used"] == "claude-sonnet-5"


def test_2796_task_keeps_the_documented_free_text_passthrough():
    """ModelSelector.vue passes free text through on purpose, including the
    `[1m]` extended-context suffix. The gate must not close that door."""
    with _task_env() as m:
        _call_task(ParallelTaskRequest(message="hi", model="claude-sonnet-4-6[1m]"))
    assert m["task_service"].execute_task.await_args.kwargs["model"] == "claude-sonnet-4-6[1m]"


def test_2796_role_does_not_reach_the_model_slot():
    """The guard for the CORRECTION, not the fix.

    With the body the Chat tab sends by default (no model), an admin caller
    leaves the model slot `None`; the only "admin" in play is the
    `source_user_email` fallback, in its own column. If this ever fails, the
    reported role-into-model mix-up has become real and the diagnosis in this
    file's docstring is out of date.
    """
    with _task_env() as m:
        _call_task(ParallelTaskRequest(message="hi"))
    kwargs = m["task_service"].execute_task.await_args.kwargs
    assert kwargs["model"] is None
    assert kwargs["triggered_by"] == "manual"
    assert kwargs["source_user_email"] == "admin"
    assert m["db"].create_task_execution.call_args.kwargs["model_used"] is None


def test_2796_task_treats_blank_as_inherit_not_as_invalid():
    with _task_env() as m:
        _call_task(ParallelTaskRequest(message="hi", model="   "))
    assert m["task_service"].execute_task.await_args.kwargs["model"] is None


# ---------------------------------------------------------------------------
# POST /api/agents/{name}/chat  — ChatMessageRequest
# ---------------------------------------------------------------------------

def _call_chat(request):
    return asyncio.run(CHAT_ENDPOINT(
        request=request, name="agent1", current_user=_admin(),
        x_source_agent=None, x_via_mcp=None, idempotency_key="k1",
    ))


def test_2796_chat_refuses_a_non_model_before_the_admission_gate():
    admit = AsyncMock()
    with patch.object(_CHAT_MOD, "get_agent_container", return_value=MagicMock(status="running")), \
         patch.object(_DISPATCH, "admit_chat_request", admit):
        with pytest.raises(Exception) as exc:
            _call_chat(ChatMessageRequest(message="hi", model=BAD_MODEL))
    assert getattr(exc.value, "status_code", None) == 422
    assert BAD_MODEL in str(getattr(exc.value, "detail", ""))
    admit.assert_not_awaited()


def test_2796_chat_lets_a_legitimate_model_through_to_admission():
    """Reaching the admission gate is the assertion: the replay branch returns
    without needing the rest of the turn machinery stubbed."""
    admit = AsyncMock(return_value=ChatAdmissionReplay(
        execution_id="exec1", in_flight=False, snapshot={"ok": True}))
    with patch.object(_CHAT_MOD, "get_agent_container", return_value=MagicMock(status="running")), \
         patch.object(_DISPATCH, "admit_chat_request", admit):
        response = _call_chat(ChatMessageRequest(message="hi", model="claude-sonnet-5"))
    assert response.headers["X-Idempotent-Replay"] == "true"
    assert admit.await_args.kwargs["request"].model == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# POST /api/agents/{name}/sessions/{id}/message  — SessionMessageRequest
# ---------------------------------------------------------------------------

class _Reached(Exception):
    """Marker: execution got past the model gate."""


def _call_session(body):
    return asyncio.run(SESSION_ENDPOINT(
        name="agent1", session_id="sess1", body=body, current_user=_admin(),
    ))


@contextmanager
def _session_env():
    db = MagicMock()
    db.get_session.return_value = MagicMock(id="sess1", user_id=1, agent_name="agent1",
                                            subscription_id=None)
    with patch.object(_SESSION_MOD, "is_session_tab_enabled", return_value=True), \
         patch.object(_SESSION_MOD, "db", db):
        yield db


def test_2796_session_refuses_a_non_model_before_persisting_the_user_turn():
    with _session_env() as db:
        with pytest.raises(Exception) as exc:
            _call_session(SessionMessageRequest(message="hi", model=BAD_MODEL))
    assert getattr(exc.value, "status_code", None) == 422
    assert BAD_MODEL in str(getattr(exc.value, "detail", ""))
    # The user's message must not be stranded in the thread with no reply.
    db.add_session_message.assert_not_called()


def test_2796_session_lets_a_legitimate_model_through():
    with _session_env() as db:
        db.add_session_message.side_effect = _Reached
        with pytest.raises(_Reached):
            _call_session(SessionMessageRequest(message="hi", model="claude-sonnet-5"))


# ---------------------------------------------------------------------------
# run_resumable_turn — one validated value, two execute_task call sites
# ---------------------------------------------------------------------------

def test_2796_resumable_turn_carries_one_model_to_both_call_sites():
    """The cold retry is a SECOND `execute_task` call fed by the same
    `**execute_kwargs`. Validating the router's single source is only sufficient
    because both calls read it — assert that, so a future split cannot leave the
    retry unguarded."""
    calls = []

    def _res(status, error=None):
        r = MagicMock()
        r.status = status
        r.error = error
        r.session_id = "uuid-1"
        return r

    async def _execute_task(**kw):
        calls.append(kw)
        if len(calls) == 1:
            return _res("failed", error="No conversation found with session ID: abc")
        return _res("success")

    class _Lock:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    svc = MagicMock()
    svc.execute_task = _execute_task
    # String target, not `patch.object` on a locally imported module: under the
    # test conftest `services.task_execution_service` resolves to a DIFFERENT
    # module object than a module-scope `import` here binds, and
    # `run_resumable_turn` imports it lazily at call time — so only the
    # sys.modules entry patch is the one it will see.
    with patch("services.task_execution_service.get_task_execution_service",
               return_value=svc), \
         patch.object(_STS, "ResumeLock", _Lock), \
         patch.object(_STS, "supports_session_resume", return_value=True), \
         patch.object(_STS, "resolve_lock_ttl", return_value=60), \
         patch.object(_STS, "is_resume_not_found",
                      lambda e: "No conversation found" in (e or "")):
        turn = asyncio.run(_STS.run_resumable_turn(
            agent_name="agent1", session_key="sess1", message="hi",
            cached_uuid="abc", triggered_by="session",
            source_user_id=1, source_user_email="admin",
            model="claude-sonnet-5",
        ))

    assert turn.fallback_fired is True
    assert len(calls) == 2, "expected the initial attempt and the cold retry"
    assert [c["model"] for c in calls] == ["claude-sonnet-5", "claude-sonnet-5"]
