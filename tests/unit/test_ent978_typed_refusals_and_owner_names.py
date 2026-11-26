"""trinity-enterprise#978 items 2-5: the turn-tier fence's last four follow-ups.

2. "Could not check" is not a credential failure. When a Gemini CLI or Codex
   agent cannot ask the platform whose turn it is, it answered 503, and every
   backend path read a 503 as AUTH: the subscription walk ran and the AUTH
   dispatch breaker counted it. Both runtimes (and Claude Code, whose #4743
   check already answered 502 untyped) now answer 502 with
   ``error_code: "turn_check_unavailable"``; every path reads that code, and an
   agent still on the older image is recognised by its own sentence. The async
   record names it instead of ``empty_result`` (trinity#4778 AC 2).
3. ``TURN_REFUSED`` only when the body says ``turn_refused``: a bare 403 is not
   a refusal.
4. ``visitor_shell`` fails closed when the setting cannot be read (ruling
   2026-11-25, ent#978): an error, broken JSON, a non-object or a non-boolean
   value turns the shell off for that turn and is logged. A missing setting
   keeps the default (on).
5. An instruction file name at any depth is owner-tier (same ruling): a nested
   ``CLAUDE.md`` / ``CLAUDE.local.md`` / ``AGENTS.md`` / ``GEMINI.md`` is in the
   instructions class, so the share tier may not write or delete it.

Error-code assertions compare ``.value`` (the str-Enum members compare equal).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import HTTPException

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from agent_server.services import managed_hooks, pull_worker, result_callback, turn_scope

from services import file_path_policy as policy
from services.execution_classification import classify_switch_failure
from test_ent403_workspace_model import AGENT as PORTAL_AGENT  # noqa: F401 — `svc` is a fixture
from test_ent403_workspace_model import EMAIL as PORTAL_EMAIL
from test_ent403_workspace_model import SESSION as PORTAL_SESSION
from test_ent403_workspace_model import _failed_turn, svc  # noqa: F401
from test_ent403_workspace_model import _run as _portal_run

pytestmark = pytest.mark.unit

CODE = "turn_check_unavailable"
AGENT = "agent-a"


def _could_not_check(runtime: str) -> str:
    return turn_scope.COULD_NOT_CHECK.format(runtime=runtime)


# The body an agent on the image BEFORE this change answers: a 503 whose detail
# is the bare sentence (turn_scope.py, ent#926).
_OLD_IMAGE_503 = _could_not_check("Gemini CLI")


# =========================================================================== #
# Item 2 — the agent's answer
# =========================================================================== #


def _scope_refusal(monkeypatch, runtime: str) -> HTTPException:
    async def unreachable(agent, run):
        return "unreachable", False, None, None
    monkeypatch.setattr(turn_scope, "fetch_turn", unreachable)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: AGENT)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(turn_scope.require_owner_tier(runtime, "exec-1"))
    return exc.value


def _hooks_refusal(monkeypatch) -> HTTPException:
    async def unreachable(agent, run):
        return "unreachable", False, None, None
    monkeypatch.setattr(turn_scope, "fetch_turn", unreachable)
    monkeypatch.setattr(turn_scope, "_agent_name", lambda: AGENT)
    monkeypatch.setattr(managed_hooks, "state", lambda: managed_hooks.SKIPPED)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(managed_hooks.require_hooks_or_owner("exec-1"))
    return exc.value


@pytest.mark.parametrize("runtime", ["Gemini CLI", "Codex"])
def test_gemini_and_codex_answer_a_typed_502_never_a_503(monkeypatch, runtime):
    exc = _scope_refusal(monkeypatch, runtime)
    assert exc.status_code == 502
    assert exc.detail == {"message": _could_not_check(runtime), "error_code": CODE}


def test_claude_code_answers_the_same_typed_502(monkeypatch):
    exc = _hooks_refusal(monkeypatch)
    assert exc.status_code == 502
    assert exc.detail == {"message": _could_not_check("Claude Code"), "error_code": CODE}


def test_both_runtimes_name_the_one_code():
    assert turn_scope.UNAVAILABLE_ERROR_CODE == CODE


@pytest.mark.parametrize("producer", ["gemini", "claude"])
def test_the_async_record_names_it_not_empty_result(monkeypatch, producer):
    """trinity#4778 AC 2: read from the typed body, not the 502 alone."""
    exc = _scope_refusal(monkeypatch, "Gemini CLI") if producer == "gemini" else _hooks_refusal(monkeypatch)
    envelope = result_callback._envelope_from_http_exception(exc)
    assert envelope["error_code"] == CODE
    assert envelope["terminal_reason"] == CODE


def test_a_real_empty_result_keeps_its_label():
    """The #678 reader-race 502 names no code of its own."""
    empty = HTTPException(502, {"message": "Execution completed without a result message",
                                "metadata": {"num_turns": 1}, "raw_message_count": 0,
                                "parse_failure_count": 0, "recovery_attempted": True})
    envelope = result_callback._envelope_from_http_exception(empty)
    assert envelope["terminal_reason"] == "empty_result"
    assert envelope["error_code"] is None


@pytest.mark.parametrize("producer", ["gemini", "claude"])
def test_the_pull_record_names_it(monkeypatch, producer):
    exc = _scope_refusal(monkeypatch, "Codex") if producer == "gemini" else _hooks_refusal(monkeypatch)
    assert pull_worker._failed_result_body_from_http("tok", exc)["error_code"] == CODE


def test_the_code_is_in_the_execution_vocabulary_and_the_pull_taxonomy():
    from models import PullTaskResultRequest
    from services.execution_envelope import TaskExecutionErrorCode

    assert TaskExecutionErrorCode(CODE).value == CODE
    assert PullTaskResultRequest(claim_token="t", status="failed", error_code=CODE).error_code == CODE


# =========================================================================== #
# Item 2 — the backend's reading, every path
# =========================================================================== #


def _resp(status: int, detail) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    body = {"detail": detail}
    resp.text = json.dumps(body)
    resp.json.return_value = body
    return resp


_TYPED_502 = {"message": _could_not_check("Codex"), "error_code": CODE}
_UNTYPED_502 = {"message": _could_not_check("Claude Code")}  # #4743's answer, before this change


def test_the_sentence_marker_is_the_agents_own():
    """The older image's text is recognised by a marker the agent's sentence
    must keep carrying; a copy edit that drops it fails here, not in the field."""
    from services.failure_classifier import is_turn_check_unavailable

    for runtime in ("Gemini CLI", "Codex", "Claude Code"):
        assert is_turn_check_unavailable(_could_not_check(runtime))
    assert not is_turn_check_unavailable("token expired")
    assert not is_turn_check_unavailable("")


@pytest.mark.parametrize("status,detail", [
    (502, _TYPED_502),
    (503, _OLD_IMAGE_503),
    (502, _UNTYPED_502),
], ids=["typed-502", "old-image-503", "untyped-502"])
def test_no_subscription_walk_reads_it_as_a_credential_failure(status, detail):
    assert classify_switch_failure(_resp(status, detail)) is None


def test_a_genuine_503_still_walks():
    assert classify_switch_failure(_resp(503, "service unavailable")) == "auth"


@pytest.mark.parametrize("status,detail", [
    (502, _TYPED_502),
    (503, _OLD_IMAGE_503),
], ids=["typed-502", "old-image-503"])
def test_the_task_path_codes_it_and_never_switches(status, detail):
    from test_792_subscription_retry import _resp as _agent_resp
    from test_792_subscription_retry import _run

    from services.task_execution_service import TaskExecutionStatus

    legacy_switch = AsyncMock(return_value={"switched": True, "new_subscription": "sub-b"})
    fallback = AsyncMock(return_value={"switched": True})
    with (
        patch("services.subscription_auto_switch.handle_subscription_failure", legacy_switch),
        patch("services.subscription_auto_switch.fallback_to_api_key", fallback),
    ):
        result, ctx = _run(responses=[_agent_resp(status, {"detail": detail})],
                           switch_result={"switched": True, "new_subscription": "sub-b"})

    assert result.status == TaskExecutionStatus.FAILED
    assert ctx.agent_call_count == 1
    ctx.switch.assert_not_awaited()
    legacy_switch.assert_not_awaited()
    fallback.assert_not_awaited()
    assert (result.error_code.value if result.error_code else None) == CODE


def _agent_error(status: int, detail) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://agent-a:8000/api/chat")
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _finalize_chat(err):
    """The real /chat finalizer + the real SUB-003 step; (raised, switch)."""
    import services.chat_execution_service as ces
    from services.failure_classifier import is_auth_failure

    mdb = MagicMock()
    mdb.get_execution.return_value = None
    switch = AsyncMock(return_value={"new_subscription": "sub-b"})
    fake = SimpleNamespace(handle_subscription_failure=switch, is_auth_failure=is_auth_failure)
    with (
        patch.object(ces, "db", mdb),
        patch.object(ces, "get_staged_values", lambda: []),
        patch.object(ces, "activity_service", MagicMock(complete_activity=AsyncMock())),
        patch.dict("sys.modules", {"services.subscription_auto_switch": fake}),
    ):
        with pytest.raises(ces.ChatDispatchError) as exc:
            asyncio.run(ces._finalize_http_failure(
                name=AGENT, e=err, task_execution_id="exec-978",
                chat_activity_id="ca", collaboration_activity_id=None,
            ))
    return exc.value, switch


@pytest.mark.parametrize("status,detail", [
    (502, _TYPED_502),
    (503, _OLD_IMAGE_503),
], ids=["typed-502", "old-image-503"])
def test_the_chat_path_codes_it_answers_502_and_never_switches(status, detail):
    from services.chat_execution_service import ERROR_CODE_HEADER

    from services.chat_execution_service import TURN_CHECK_UNAVAILABLE_MESSAGE

    raised, switch = _finalize_chat(_agent_error(status, detail))
    switch.assert_not_awaited()
    assert raised.status_code == 502
    assert raised.headers[ERROR_CODE_HEADER] == CODE
    # A 5xx answers fixed words, never the agent's relayed body (#4284).
    assert raised.detail == TURN_CHECK_UNAVAILABLE_MESSAGE


def test_the_task_path_answer_to_a_chat_caller_is_502_with_the_code():
    import services.chat_execution_service as ces
    from services.execution_envelope import TaskExecutionErrorCode as C

    result = SimpleNamespace(status="failed", error=_could_not_check("Codex"),
                             error_code=C.TURN_CHECK_UNAVAILABLE, execution_id="e1")
    with patch.object(ces.idempotency_service, "fail"), pytest.raises(ces.ChatDispatchError) as exc:
        ces._map_task_failure(AGENT, result, idem=object())
    assert exc.value.status_code == 502
    assert exc.value.headers[ces.ERROR_CODE_HEADER] == CODE
    assert exc.value.detail == ces.TURN_CHECK_UNAVAILABLE_MESSAGE


def test_an_old_image_async_record_is_recoded_from_its_sentence():
    """The #1083 callback from the older image labels the 503 ``auth``."""
    from services.execution_envelope import terminal_from_callback_payload

    payload = SimpleNamespace(status="failed", error_code="auth", terminal_reason="auth",
                              error=_OLD_IMAGE_503, response=None, metadata={},
                              execution_log=None, session_id=None, cost=None,
                              context_used=None, context_max=None)
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == CODE

    payload.error = "token expired"
    assert terminal_from_callback_payload(payload, "exec-1").error_code.value == "auth"


def test_an_old_image_pull_record_neither_switches_nor_counts_as_auth():
    from services import pull_coordination_service as pcs

    assert pcs._switch_failure_kind("auth", f"[auth] {_OLD_IMAGE_503}") is None
    assert pcs._switch_failure_kind("auth", "token expired") == "auth"

    mock_db = MagicMock()
    mock_db.get_execution.return_value = SimpleNamespace(
        id="exec-978", agent_name="worker-a", status="running", triggered_by="schedule",
        duration_ms=None, cost=None, fan_out_id=None, loop_id=None,
        claude_session_id="dispatched_async",
    )
    mock_db.update_execution_status.return_value = True
    with (
        patch.object(pcs, "db", mock_db),
        patch.object(pcs, "event_dispatch_service", MagicMock()),
        patch.object(pcs, "activity_service", MagicMock()),
        patch.object(pcs, "subscription_auto_switch", MagicMock()),
        patch.object(pcs, "_spawn_breaker_verdict") as verdict,
    ):
        outcome = pcs.apply_task_result(
            "exec-978", "tok", status="failed", content=_OLD_IMAGE_503, error_code="auth",
        )
    assert outcome.kind == "applied"
    assert verdict.call_args.args[2] is False
    stored = mock_db.update_execution_status.call_args.kwargs["result"].error
    assert stored.startswith(f"[{CODE}] ")


@pytest.mark.parametrize("code", ["TURN_CHECK_UNAVAILABLE", "turn_check_unavailable"])
def test_a_workspace_person_is_told_to_send_it_again(svc, monkeypatch, code):
    """Nothing ran and nothing was billed, so Retry is right; never the
    usage-limit copy an AUTH code would give, and never the raw sentence."""
    from client_portal.service import ClientPortalError
    from services.execution_envelope import TaskExecutionErrorCode

    typed = TaskExecutionErrorCode(code) if code.islower() else code
    _failed_turn(svc, monkeypatch, code=typed, error=_could_not_check("Codex"))
    with pytest.raises(ClientPortalError) as exc:
        _portal_run(svc.portal_chat(PORTAL_AGENT, "hi", PORTAL_EMAIL, session_id=PORTAL_SESSION))

    assert exc.value.category in svc.PORTAL_FAILURE_CATEGORIES
    assert exc.value.category == "agent_unavailable"
    assert exc.value.retryable is True
    assert "usage limit" not in exc.value.detail
    assert "Codex" not in exc.value.detail and "Trinity could not" not in exc.value.detail
    assert "again" in exc.value.detail


def test_an_owed_wake_stays_owed_when_the_turn_check_could_not_ask():
    """ent#1108 AC #4: no turn started, so the wake an answered ask owes is
    kept for the agent's next start. The old 503 kept it as `auth`; the typed
    code must not turn that into "a turn ran"."""
    from services import operator_resume_service as ors
    from services.execution_envelope import TaskExecutionErrorCode as C

    assert CODE in ors.NO_TURN_ERROR_CODES
    result = SimpleNamespace(status="failed", error_code=C.TURN_CHECK_UNAVAILABLE)
    assert ors._never_reached_the_agent(result) is True
    assert "turn_refused" not in ors.NO_TURN_ERROR_CODES   # a refusal will not change


def test_the_scheduler_mirror_carries_the_marker():
    """Byte identity is pinned by test_904's parity class; this names the list."""
    mirror = (Path(__file__).resolve().parents[2] / "src/scheduler/failure_classifier.py").read_text()
    assert "TURN_CHECK_UNAVAILABLE_MARKERS" in mirror


# =========================================================================== #
# Item 3 — TURN_REFUSED only from the typed body
# =========================================================================== #


def test_the_chat_path_does_not_call_a_bare_403_a_refusal():
    from services.chat_execution_service import ERROR_CODE_HEADER

    raised, _ = _finalize_chat(_agent_error(403, "nope"))
    assert raised.status_code != 403
    assert raised.headers.get(ERROR_CODE_HEADER) != "turn_refused"


def test_the_chat_path_still_answers_a_typed_refusal_403():
    from services.chat_execution_service import ERROR_CODE_HEADER

    refusal = {"message": turn_scope.REFUSED.format(runtime="Codex"), "error_code": "turn_refused"}
    raised, switch = _finalize_chat(_agent_error(403, refusal))
    switch.assert_not_awaited()
    assert raised.status_code == 403
    assert raised.headers[ERROR_CODE_HEADER] == "turn_refused"


@pytest.mark.parametrize("detail,expected", [
    ("nope", None),
    ({"message": "refused", "error_code": "turn_refused"}, "turn_refused"),
], ids=["bare-403", "typed-403"])
def test_the_task_path_codes_turn_refused_only_from_the_body(detail, expected):
    from test_792_subscription_retry import _resp as _agent_resp
    from test_792_subscription_retry import _run

    with (
        patch("services.subscription_auto_switch.handle_subscription_failure", AsyncMock(return_value=None)),
        patch("services.subscription_auto_switch.fallback_to_api_key", AsyncMock(return_value=None)),
    ):
        result, _ = _run(responses=[_agent_resp(403, {"detail": detail})] * 4, switch_result=None)
    code = result.error_code.value if result.error_code else None
    if expected is None:
        assert code != "turn_refused"
    else:
        assert code == expected


# =========================================================================== #
# Item 4 — visitor_shell fails closed when unreadable
# =========================================================================== #


def _db_reading(value=None, raises=None):
    db = MagicMock()
    if raises is not None:
        db.get_guardrails_config.side_effect = raises
    else:
        db.get_guardrails_config.return_value = value
    return db


@pytest.mark.parametrize("db_kwargs", [
    {"raises": RuntimeError("db down")},
    {"raises": ValueError("guardrails_config is not valid JSON")},
    {"value": ["visitor_shell"]},
    {"value": "on"},
    {"value": {"visitor_shell": "false"}},
    {"value": {"visitor_shell": None}},
    {"value": {"visitor_shell": 1}},
], ids=["read-error", "broken-json", "a-list", "a-string", "string-false", "null", "one"])
def test_an_unreadable_setting_turns_the_shell_off_and_is_logged(caplog, db_kwargs):
    from services import turn_tier_service as t

    db = _db_reading(**db_kwargs)
    with caplog.at_level(logging.WARNING, logger=t.logger.name):
        assert t.visitor_shell_allowed(db, AGENT) is False
        assert t.shell_allowed(db, AGENT, "share", "visitor") is False
        assert t.restricts_standing_work(db, AGENT, "share", "visitor") is True
    assert any("visitor_shell_unreadable" in r.getMessage() and AGENT in r.getMessage()
               for r in caplog.records)
    db.get_guardrails_config.assert_called_with(AGENT, strict=True)


@pytest.mark.parametrize("value,expected", [
    (None, True),                      # no row
    ({}, True),                        # nothing set: the default (on)
    ({"tool_access": {}}, True),       # other settings, not this one
    ({"visitor_shell": True}, True),
    ({"visitor_shell": False}, False),
])
def test_a_readable_setting_is_read_as_before(caplog, value, expected):
    from services import turn_tier_service as t

    with caplog.at_level(logging.WARNING, logger=t.logger.name):
        assert t.visitor_shell_allowed(_db_reading(value), AGENT) is expected
    assert not [r for r in caplog.records if "visitor_shell_unreadable" in r.getMessage()]


@pytest.fixture
def ownership_db(tmp_path, monkeypatch):
    """A real SQLite agent_ownership table through the Core layer the mixin uses."""
    from sqlalchemy import create_engine, insert

    import db.agent_settings.security as security_module
    import db.engine as engine_module
    import db.tables as tables

    engine = create_engine(f"sqlite:///{tmp_path / 'ownership.db'}")
    tables.agent_ownership.create(engine)
    monkeypatch.setattr(engine_module, "get_engine", lambda: engine)
    monkeypatch.setattr(security_module, "get_engine", lambda: engine)

    def put(blob):
        with engine.begin() as conn:
            conn.execute(tables.agent_ownership.delete())
            conn.execute(insert(tables.agent_ownership).values(agent_name=AGENT, owner_id=1,
                                                               guardrails_config=blob))
    return security_module.SecurityMixin(), put


def test_the_db_reader_reports_broken_json_only_when_asked(ownership_db):
    ops, put = ownership_db
    put("{not json")
    assert ops.get_guardrails_config(AGENT) == {}            # every other reader: unchanged
    with pytest.raises(ValueError):
        ops.get_guardrails_config(AGENT, strict=True)

    put(json.dumps({"visitor_shell": False}))
    assert ops.get_guardrails_config(AGENT, strict=True) == {"visitor_shell": False}
    put(None)
    assert ops.get_guardrails_config(AGENT, strict=True) == {}


def test_broken_json_in_the_row_turns_the_shell_off(ownership_db):
    from services import turn_tier_service as t

    ops, put = ownership_db
    put("{not json")
    assert t.visitor_shell_allowed(ops, AGENT) is False
    put(json.dumps({}))
    assert t.visitor_shell_allowed(ops, AGENT) is True


def test_the_db_facade_the_callers_pass_carries_strict_through(ownership_db):
    """Every caller passes `database.db`. A facade method that dropped the
    keyword would raise on every read, and fail-closed would then take the
    shell from every channel visitor on every agent."""
    from database import DatabaseManager
    from services import turn_tier_service as t

    ops, put = ownership_db
    facade = DatabaseManager.__new__(DatabaseManager)  # no engine, no init: only the delegation
    facade._agent_ops = ops
    put(json.dumps({}))
    assert t.visitor_shell_allowed(facade, AGENT) is True
    put(json.dumps({"visitor_shell": False}))
    assert t.visitor_shell_allowed(facade, AGENT) is False
    put("{not json")
    assert t.visitor_shell_allowed(facade, AGENT) is False
    assert facade.get_guardrails_config(AGENT) == {}


# =========================================================================== #
# Item 5 — instruction file names at any depth are owner-tier
# =========================================================================== #

_NAMES = ("CLAUDE.md", "CLAUDE.local.md", "AGENTS.md", "GEMINI.md")


def test_claude_local_md_is_in_the_name_list():
    """Eugene's check on the ruling."""
    assert set(_NAMES) <= set(policy.INSTRUCTION_FILE_NAMES)


@pytest.mark.parametrize("name", _NAMES)
@pytest.mark.parametrize("folder", ["docs", "workspace/app/src", "/home/developer/notes"])
def test_a_nested_instruction_file_is_instructions(folder, name):
    path = f"{folder}/{name}"
    assert policy.INSTRUCTIONS in policy.classes(path)
    assert policy.decide(policy.SHARE, policy.WRITE, path) == (False, policy.OWNER_TIER)
    assert policy.decide(policy.OWNER, policy.WRITE, path).allowed
    assert "write" in policy.restricted_verbs(policy.SHARE, path)


@pytest.mark.parametrize("name", ["CLAUDE.local.md", "AGENTS.md", "GEMINI.md"])
def test_the_share_tier_may_not_delete_one(name):
    assert policy.decide(policy.SHARE, policy.DELETE, f"docs/{name}") == (False, policy.OWNER_TIER)
    assert policy.decide(policy.OWNER, policy.DELETE, f"docs/{name}").allowed


@pytest.mark.parametrize("path", [
    "docs/template.yaml",          # only the agent's own template.yaml declares metrics
    "docs/claude.md",              # the runtimes load the exact name
    "docs/CLAUDE.md.bak",
    "docs/MY-CLAUDE.md",
    "docs/CLAUDE.md/notes.txt",    # a folder of that name holds ordinary files
    "workspace/notes.md",
])
def test_lookalikes_stay_ordinary_files(path):
    assert policy.INSTRUCTIONS not in policy.classes(path)
    assert policy.decide(policy.SHARE, policy.WRITE, path).allowed


def test_the_top_level_files_are_unchanged():
    for name in _NAMES + ("template.yaml",):
        assert policy.decide(policy.SHARE, policy.WRITE, name) == (False, policy.OWNER_TIER)


def test_an_agent_key_needs_instructions_manage_for_a_nested_one():
    """One class, one rule: the ent#164 self-change grant covers a nested
    instruction file as it covers the top-level one."""
    from services.agent_service.files import _touches_instructions

    assert _touches_instructions("docs/CLAUDE.md", include_ancestors=False) is True
    assert _touches_instructions("docs/AGENTS.md", include_ancestors=False) is True
    assert _touches_instructions("docs/notes.md", include_ancestors=False) is False
