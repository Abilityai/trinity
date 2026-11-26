"""#4475 — a terminate that ended the work writes its audit row even when
another writer records the turn's terminal status first.

On a real instance the synchronous `/task` or `/chat` caller that started the
turn sees the agent stop and writes the terminal row before the terminate's own
CANCELLED compare-and-swap runs (the agent's terminate route waits up to 7 s
for the process to exit before it answers). The terminate then loses the CAS.
The audit row is decided on what the agent answered this request
(`terminated`), deduplicated by a first-claim per agent and execution id, so a
repeat or concurrent terminate still writes one row.

Shares the #3180 world: the real routes, the real credential resolver, a real
SQLite store and fakeredis behind every Redis lookup.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_3180_operational_write_audit import (  # noqa: E402,F401 (fixtures)
    AGENT,
    OWNER,
    TERMINATE,
    _assert_terminate_row,
    _owner_user_key,
    _rows,
    _seed,
    _session,
    _status_of,
    _terminate_path,
    chat_router,
    db,
    db_backend,
    redis_breaker_util,
    world,
)

pytestmark = pytest.mark.unit

ce = chat_router.chat_execution_service


def _inflight_writer_wins(monkeypatch, status: str, error: str) -> list:
    """Run the starting request's terminal write just before the terminate's
    CANCELLED CAS, as the sync `/task` (CANCELLED) or `/chat` (FAILED) caller
    does on a live instance."""
    real = db.update_execution_status
    raced: list = []

    def _sync_caller_first(*args, **kwargs):
        if not raced:
            eid = kwargs.get("execution_id") or args[0]
            raced.append(real(execution_id=eid, status=status,
                              result=ce.ExecutionResult(error=error)))
        return real(*args, **kwargs)

    monkeypatch.setattr(db, "update_execution_status", _sync_caller_first)
    return raced


class TestTheInflightWriterWinsTheStatusRace:
    def test_a_task_turn_cancelled_by_its_caller_still_records_the_terminate(
        self, world, monkeypatch
    ):
        eid = _seed("s2-4475-task-raced", "running")
        raced = _inflight_writer_wins(monkeypatch, "cancelled", "Execution cancelled by user")

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "terminated"
        assert raced == [True]
        assert _status_of(eid) == "cancelled"
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], None, eid, "terminated")

    def test_a_chat_turn_failed_by_its_caller_still_records_the_terminate(
        self, world, monkeypatch
    ):
        eid = _seed("s2-4475-chat-raced", "running")
        raced = _inflight_writer_wins(monkeypatch, "failed", "agent returned 502")

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert raced == [True]
        assert _status_of(eid) == "failed"
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], None, eid, "terminated")

    def test_a_parked_turn_finalized_by_its_dispatcher_still_records_the_cancel(
        self, world, monkeypatch
    ):
        """The parked dispatcher reads the cancel key and writes CANCELLED
        itself (task_execution_service, BackendAgentCallCancelled); it can get
        there before the terminate's own CAS."""
        eid = _seed("s2-4475-parked-raced", "running")
        world.redis.set(
            ce.agent_call_limiter._marker_key(eid),
            json.dumps({"agent": AGENT, "phase": "parked"}),
        )
        raced = _inflight_writer_wins(monkeypatch, "cancelled", "cancelled while queued")

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert res.json()["status"] == "cancelled_while_parked"
        assert raced == [True]
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], None, eid, "cancelled_while_parked")

    def test_with_redis_down_the_row_is_still_written(self, world, monkeypatch):
        """Fail-open: a duplicate security record costs less than a missing one."""
        eid = _seed("s2-4475-no-redis", "running")
        _inflight_writer_wins(monkeypatch, "cancelled", "Execution cancelled by user")
        monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: None)

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert len(_rows(TERMINATE)) == 1

    def test_with_the_redis_claim_raising_the_row_is_still_written(self, world, monkeypatch):
        """After boot the client is cached, so an outage shows up as `.set`
        raising rather than as a `None` client."""
        eid = _seed("s2-4475-redis-raises", "running")
        _inflight_writer_wins(monkeypatch, "cancelled", "Execution cancelled by user")

        class _Down:
            def set(self, *a, **k):
                raise ConnectionError("redis unreachable")

        monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: _Down())

        res = world.client.post(_terminate_path(eid), headers=_session(OWNER))

        assert res.status_code == 200, res.text
        assert len(_rows(TERMINATE)) == 1


class TestOneRowPerEndedTurn:
    def test_a_second_task_execution_id_for_the_same_agent_process_writes_no_second_row(
        self, world
    ):
        """The claim is keyed on the id the agent verified (`execution_id`),
        never on the caller-supplied `task_execution_id`, so naming another row
        neither adds a row nor spends that row's own claim."""
        eid = _seed("s2-4475-process", "running")
        other = _seed("s2-4475-sibling-row", "running")
        repeat_headers, _ = _owner_user_key()

        first = world.client.post(_terminate_path(eid), headers=_session(OWNER))
        second = world.client.post(
            _terminate_path(eid) + f"?task_execution_id={other}", headers=repeat_headers
        )

        assert first.status_code == second.status_code == 200, (first.text, second.text)
        rows = _rows(TERMINATE)
        assert len(rows) == 1
        _assert_terminate_row(rows[0], None, eid, "terminated")

        # The sibling's own terminate still records itself.
        third = world.client.post(_terminate_path(other), headers=_session(OWNER))
        assert third.status_code == 200, third.text
        assert len(_rows(TERMINATE)) == 2


class TestAFailedAuditWrite:
    def test_the_real_hook_gives_the_claim_back_so_a_repeat_records_the_turn(
        self, world, monkeypatch
    ):
        """`platform_audit_service.log` swallows a failed insert and returns
        None, so the hook never raises; the claim is released on that None."""
        eid = _seed("s2-4475-audit-down", "running")
        failing = {"left": 1}

        def _wrap(real):
            def _maybe_fail(*a, **k):
                if failing["left"]:
                    failing["left"] -= 1
                    raise RuntimeError("audit store down")
                return real(*a, **k)
            return _maybe_fail

        monkeypatch.setattr(db, "create_audit_entry", _wrap(db.create_audit_entry))
        monkeypatch.setattr(db, "create_audit_entry_chained", _wrap(db.create_audit_entry_chained))

        first = world.client.post(_terminate_path(eid), headers=_session(OWNER))
        assert first.status_code == 200, first.text
        assert _rows(TERMINATE) == []
        assert world.redis.get(ce._terminate_audit_key(AGENT, eid)) is None

        second = world.client.post(_terminate_path(eid), headers=_session(OWNER))
        assert second.status_code == 200, second.text
        assert len(_rows(TERMINATE)) == 1


class TestTheClaimHelper:
    def test_a_failed_audit_write_gives_the_claim_back(self, world):
        calls: list = []

        async def _boom(outcome):
            calls.append(outcome)
            raise RuntimeError("audit store down")

        with pytest.raises(RuntimeError):
            asyncio.run(ce._audit_terminate_once(AGENT, "s2-4475-boom", _boom, "terminated"))
        assert world.redis.get(ce._terminate_audit_key(AGENT, "s2-4475-boom")) is None

        async def _ok(outcome):
            calls.append(outcome)
            return "event-id"

        asyncio.run(ce._audit_terminate_once(AGENT, "s2-4475-boom", _ok, "terminated"))
        asyncio.run(ce._audit_terminate_once(AGENT, "s2-4475-boom", _ok, "terminated"))
        assert calls == ["terminated", "terminated"]

    def test_no_hook_takes_no_claim(self, world):
        asyncio.run(ce._audit_terminate_once(AGENT, "s2-4475-nohook", None, "terminated"))
        assert world.redis.get(ce._terminate_audit_key(AGENT, "s2-4475-nohook")) is None
