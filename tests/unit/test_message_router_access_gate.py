"""
Characterization tests for the cross-channel access gate in
ChannelMessageRouter._handle_message_inner (#311 / #1026).

These drive the *real* pipeline with a fully-mocked adapter + patched
collaborators and pin the observable behavior of the 5b access-policy gate:
who reaches execute_task, who is prompted/denied, and whether a pending
access-request is recorded. They are green against the current inline gate
and must stay green after it is extracted into `_enforce_access_policy`.

Paths covered:
- DM open_access → executes
- DM require_email + no verified email → prompt_auth, no execution
- DM verified + has access → executes
- DM verified + restrictive policy → records pending request, no execution
- group any_verified already unlocked → executes
- group any_verified locked + no verifier → prompt_group_auth, no execution
- group any_verified + sender verifies → unlocks group, executes
- group none → executes (legacy permissive)
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from adapters.message_router import ChannelMessageRouter
from adapters.base import NormalizedMessage

_MR = sys.modules[ChannelMessageRouter.__module__]


def _make_adapter(channel: str = "telegram") -> MagicMock:
    a = MagicMock()
    a.channel_type = channel
    # awaited methods
    a.get_agent_name = AsyncMock(return_value="agent1")
    a.enrich_message = AsyncMock(return_value=None)  # #350 async identity hook
    a.handle_verification = AsyncMock(return_value=True)
    a.resolve_verified_email = AsyncMock(return_value=None)
    a.is_group_verified = AsyncMock(return_value=False)
    a.set_group_verified = AsyncMock()
    a.prompt_group_auth = AsyncMock()
    a.prompt_auth = AsyncMock()
    a.indicate_processing = AsyncMock()
    a.indicate_done = AsyncMock()
    a.send_response = AsyncMock()
    a.on_response_sent = AsyncMock()
    # sync methods
    a.get_bot_token = MagicMock(return_value="tok")
    a.get_rate_key = MagicMock(return_value="rk")
    a.get_session_identifier = MagicMock(return_value="sid")
    a.get_source_identifier = MagicMock(return_value="src@example.com")
    return a


def _make_message(is_group: bool = False) -> NormalizedMessage:
    return NormalizedMessage(
        sender_id="u1",
        text="hello",
        channel_id="c1",
        timestamp="2026-01-01T00:00:00Z",
        metadata={"is_group": is_group},
    )


@contextmanager
def _env(policy: dict, rate_ok: bool = True, container_status: str = "running"):
    """Patch every collaborator the pipeline touches; yield the db + service mocks.

    `rate_ok=False` simulates a rate-limited caller; `container_status` overrides
    the agent container state (e.g. "stopped" for the availability gate).
    """
    db = MagicMock()
    db.get_access_policy.return_value = policy
    db.email_has_agent_access.return_value = False
    db.get_or_create_public_chat_session.return_value = {"id": "s1"}
    db.build_public_chat_context.return_value = "ctx-prompt"
    db.get_or_create_public_user_memory.return_value = {}
    db.increment_public_user_memory_count.return_value = 0

    container = MagicMock()
    container.status = container_status

    result = MagicMock()
    result.status = "success"
    result.response = "agent reply"
    result.error = None
    result.cost = 0.0
    result.execution_id = "e1"
    service = MagicMock()
    service.execute_task = AsyncMock(return_value=result)

    with patch.object(_MR, "db", db), \
         patch.object(_MR, "get_agent_container", return_value=container), \
         patch.object(_MR, "get_task_execution_service", return_value=service), \
         patch.object(_MR, "_check_rate_limit", return_value=rate_ok), \
         patch.object(_MR, "process_voice", new=AsyncMock(return_value="")), \
         patch.object(_MR, "format_user_memory_block", return_value=None), \
         patch.object(_MR, "summarize_user_memory_background", new=AsyncMock()):
        yield db, service


def _run(router, adapter, message):
    asyncio.run(router._handle_message_inner(adapter, message))


# --------------------------------------------------------------------------- DM

def test_dm_open_access_executes():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    with _env({"require_email": False, "open_access": True, "group_auth_mode": "none"}) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()
    adapter.prompt_auth.assert_not_awaited()


def test_dm_require_email_no_email_prompts_and_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.resolve_verified_email = AsyncMock(return_value=None)
    with _env({"require_email": True, "open_access": False, "group_auth_mode": "none"}) as (db, service):
        _run(router, adapter, message)
    adapter.prompt_auth.assert_awaited_once()
    service.execute_task.assert_not_awaited()


def test_dm_verified_with_access_executes():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.resolve_verified_email = AsyncMock(return_value="a@b.com")
    with _env({"require_email": True, "open_access": False, "group_auth_mode": "none"}) as (db, service):
        db.email_has_agent_access.return_value = True
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()
    db.email_has_agent_access.assert_called()


def test_dm_verified_no_access_records_pending_request():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.resolve_verified_email = AsyncMock(return_value="a@b.com")
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "none"}) as (db, service):
        db.email_has_agent_access.return_value = False
        _run(router, adapter, message)
    db.upsert_access_request.assert_called_once()
    service.execute_task.assert_not_awaited()


# ------------------------------------------------------------------------- group

def test_group_any_verified_unlocked_executes():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message(is_group=True)
    adapter.is_group_verified = AsyncMock(return_value=True)
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "any_verified"}) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()
    adapter.prompt_group_auth.assert_not_awaited()


def test_group_any_verified_locked_prompts_and_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message(is_group=True)
    adapter.is_group_verified = AsyncMock(return_value=False)
    adapter.resolve_verified_email = AsyncMock(return_value=None)
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "any_verified"}) as (db, service):
        _run(router, adapter, message)
    adapter.prompt_group_auth.assert_awaited_once()
    service.execute_task.assert_not_awaited()


def test_group_any_verified_sender_unlocks_and_executes():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message(is_group=True)
    adapter.is_group_verified = AsyncMock(return_value=False)
    adapter.resolve_verified_email = AsyncMock(return_value="a@b.com")
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "any_verified"}) as (db, service):
        _run(router, adapter, message)
    adapter.set_group_verified.assert_awaited_once()
    service.execute_task.assert_awaited_once()


def test_group_none_executes():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message(is_group=True)
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "none"}) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()


# --------------------------------------------------------------- entry gates (#1026)

_OPEN = {"require_email": False, "open_access": True, "group_auth_mode": "none"}


def test_no_agent_resolved_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.get_agent_name = AsyncMock(return_value=None)
    with _env(_OPEN) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()
    adapter.send_response.assert_not_awaited()  # step 1 returns silently


def test_no_bot_token_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.get_bot_token = MagicMock(return_value=None)
    with _env(_OPEN) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()
    adapter.send_response.assert_not_awaited()  # step 2 returns silently


def test_rate_limited_dm_replies_and_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    with _env(_OPEN, rate_ok=False) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()
    adapter.send_response.assert_awaited_once()  # "too quickly" notice


def test_rate_limited_group_silent_drop():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message(is_group=True)
    with _env({"require_email": False, "open_access": False, "group_auth_mode": "none"},
              rate_ok=False) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()
    adapter.send_response.assert_not_awaited()  # groups drop silently


def test_container_not_running_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    with _env(_OPEN, container_status="exited") as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()
    adapter.send_response.assert_awaited_once()  # "not available" notice


def test_not_verified_aborts():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    adapter.handle_verification = AsyncMock(return_value=False)
    with _env(_OPEN) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_not_awaited()


def test_happy_path_persists_and_sends():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    with _env(_OPEN) as (db, service):
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()
    adapter.send_response.assert_awaited_once()      # agent reply delivered
    adapter.on_response_sent.assert_awaited_once()   # post-response hook
    assert db.add_public_chat_message.call_count == 2  # user + assistant persisted


def test_execution_failed_replies_error_and_skips_finalize():
    router, adapter, message = ChannelMessageRouter(), _make_adapter(), _make_message()
    with _env(_OPEN) as (db, service):
        failed = MagicMock()
        failed.status = "failed"
        failed.error = "boom"
        failed.response = None
        failed.cost = 0.0
        failed.execution_id = "e1"
        service.execute_task = AsyncMock(return_value=failed)
        _run(router, adapter, message)
    service.execute_task.assert_awaited_once()
    adapter.send_response.assert_awaited_once()        # error reply sent
    adapter.on_response_sent.assert_not_awaited()      # finalize NOT reached
    db.add_public_chat_message.assert_not_called()     # nothing persisted on failure
