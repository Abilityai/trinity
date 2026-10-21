"""#3487 — a PULLED turn walks the subscription pool, in place under one claim.

#3470 made a refused turn walk every subscription and then the platform API key
before it fails — on the PUSH paths, where the walk is a loop inside one
dispatch. On a pull pilot the agent's worker claims the row and POSTs the
result to `pull_coordination_service`, which ran the one-shot
`spawn_subscription_failure` and wrote FAILED: one Failed card per refused
subscription for a Workspace user on a piloted agent.

Now a refused attempt from a capable worker is answered with a `retry`
instruction — run the turn again, under the SAME claim and lease, on the next
credential — and the worker follows it. Pinned here:

  A. the walk round-trips through the row without its secret;
  B. the two DB ops: one attempt has one winner (token rotation), and the walk
     state is written only by the token that owns the row;
  C. the sink: refused → retry on the next rung (no terminal, no hooks);
     a duplicate POST gets the same answer and records nothing twice;
     exhausted → ONE FAILED row with the "Tried: …" trail and no one-shot switch;
     served on a trial → commit + SUB-004, the refused attempts' cost rolled in;
     an incapable worker, a terminated turn, a non-credential failure and an
     exhausted budget keep the pre-#3487 behaviour;
  D. the rebuilt result carries the walk summary (push parity);
  E. the agent's pull worker follows the instruction with the credential as a
     per-spawn `auth_override`, says what it can do on every result, and never
     follows one it cannot honour.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from unit.test_3470_subscription_walk import _sub, _switch_db, _wire_db, wired  # noqa: F401

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]


def _now_iso(delta_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_s)).isoformat().replace("+00:00", "Z")


# ===========================================================================
# A — the walk on the row, never the secret
# ===========================================================================
class TestWalkState:
    def test_every_field_round_trips_except_the_credential(self):
        m = __import__("services.subscription_auto_switch", fromlist=["x"])
        walk = m.SubscriptionWalk(
            agent_name="a", interactive=True, assigned_subscription_id="sub-a",
            assigned_subscription_name="SUB-A", tried_subscription_ids={"sub-a", "sub-b"},
            tried_api_key=True, attempts=[{"attempt": 1, "on": "SUB-A", "failure": "rate_limit"}],
            active=m.Rung(kind="subscription", subscription_id="sub-c", subscription_name="SUB-C",
                          credential="tok-SECRET", why={"last_resort": False}),
            override_supported=True, legacy_restart_used=False, max_rungs=5,
            committed={"switched": True}, stop_reason=None,
        )
        state = m.walk_to_state(walk)
        assert set(state) == {f.name for f in fields(m.SubscriptionWalk)}, \
            "a new SubscriptionWalk field must be persisted (or this test updated on purpose)"
        assert "tok-SECRET" not in json.dumps(state)

        back = m.walk_from_state(json.loads(json.dumps(state)))
        for f in fields(m.SubscriptionWalk):
            if f.name == "active":
                continue
            assert getattr(back, f.name) == getattr(walk, f.name), f.name
        assert back.active.kind == "subscription"
        assert back.active.subscription_id == "sub-c"
        assert back.active.credential is None, "the secret is read fresh, never stored"

    def test_rung_credential_is_read_fresh(self, wired):
        m = wired.m
        _wire_db(wired, _switch_db())
        assert m.rung_credential(m.Rung(kind="subscription", subscription_id="sub-b")) == "tok-sub-b"
        assert m.rung_credential(m.Rung(kind="api_key")) == "sk-ant-test-key"
        assert m.rung_credential(None) is None


# ===========================================================================
# B — the two DB ops, on real SQLite
# ===========================================================================
@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    from sqlalchemy import create_engine, insert

    import db.schedules.queue as queue_mod
    from db import tables as t

    engine = create_engine(f"sqlite:///{tmp_path / 'q.db'}")
    t.metadata.create_all(engine, tables=[t.schedule_executions])
    monkeypatch.setattr(queue_mod, "get_engine", lambda: engine)
    with engine.begin() as conn:
        conn.execute(insert(t.schedule_executions).values(
            id="exec-1", schedule_id="s", agent_name="a", status="running",
            started_at=_now_iso(), message="m", triggered_by="public",
            claim_token="t0", backlog_metadata="{}",
        ))
    from db.schedules import ScheduleOperations
    return SimpleNamespace(ops=ScheduleOperations(MagicMock(), MagicMock()), engine=engine, t=t)


def _row(q):
    from sqlalchemy import select
    with q.engine.connect() as conn:
        return dict(conn.execute(select(q.t.schedule_executions)).mappings().first())


class TestDbOps:
    def test_one_attempt_has_one_winner(self, queue_db):
        assert queue_db.ops.rotate_claim_token("exec-1", "t0", "t1") is True
        assert queue_db.ops.rotate_claim_token("exec-1", "t0", "t9") is False, \
            "a duplicate of the same attempt must lose"
        row = _row(queue_db)
        assert row["claim_token"] == "t1" and row["status"] == "running"

    def test_a_terminal_row_cannot_be_rotated(self, queue_db):
        from sqlalchemy import update
        with queue_db.engine.begin() as conn:
            conn.execute(update(queue_db.t.schedule_executions).values(status="failed"))
        assert queue_db.ops.rotate_claim_token("exec-1", "t0", "t1") is False

    def test_metadata_is_written_only_by_the_owning_token(self, queue_db):
        assert queue_db.ops.set_backlog_metadata_for_token("exec-1", "stale", '{"x":1}') is False
        assert queue_db.ops.set_backlog_metadata_for_token("exec-1", "t0", '{"x":2}') is True
        assert _row(queue_db)["backlog_metadata"] == '{"x":2}'


# ===========================================================================
# C — the sink
# ===========================================================================
class FakePullDB:
    """The pull sink's db for one row: the token CAS, the metadata write, the
    terminal CAS — stateful, so a walk can be driven attempt by attempt."""

    def __init__(self, *, started_delta_s=-5.0, timeout=3600):
        from models import TaskExecutionStatus as S
        self.S = S
        self.row = dict(
            id="exec-p", agent_name="worker-a", status=S.RUNNING, triggered_by="public",
            claim_token="t0", backlog_metadata="{}", started_at=_now_iso(started_delta_s),
            duration_ms=None, cost=None, fan_out_id=None, loop_id=None,
            claude_session_id=None,
        )
        self.timeout = timeout
        self.terminals = []
        self.attributed = []

    def get_execution(self, execution_id):
        # Like the real model: the row's claim_token is NOT exposed.
        if execution_id != self.row["id"]:
            return None
        return SimpleNamespace(**{k: v for k, v in self.row.items() if k != "claim_token"})

    def get_execution_timeout(self, agent_name):
        return self.timeout

    def rotate_claim_token(self, execution_id, old, new):
        if self.row["status"] == self.S.RUNNING and self.row["claim_token"] == old:
            self.row["claim_token"] = new
            return True
        return False

    def set_backlog_metadata_for_token(self, execution_id, token, meta):
        if self.row["claim_token"] != token:
            return False
        self.row["backlog_metadata"] = meta
        return True

    def update_execution_status(self, *, execution_id, status, result, claim_token):
        terminal = {self.S.SUCCESS, self.S.FAILED, self.S.CANCELLED, self.S.SKIPPED}
        if self.row["status"] in terminal or self.row["claim_token"] != claim_token:
            return False
        self.row["status"] = status
        self.terminals.append((status, result))
        return True

    def set_execution_subscription(self, execution_id, subscription_id):
        self.attributed.append(subscription_id)
        return True

    def walk_state(self):
        return json.loads(self.row["backlog_metadata"]).get("subscription_walk") or {}


@pytest.fixture
def sink(wired, monkeypatch):
    """The real sink over FakePullDB, the real walk over the #3470 fixture db,
    every fire-and-forget hook stubbed."""
    from services import pull_coordination_service as pcs
    import services.platform_audit_service as audit_mod

    pull_db = FakePullDB()
    switch_db = _wire_db(wired, _switch_db(viable=[_sub("sub-b")]))
    monkeypatch.setattr(pcs, "db", pull_db)
    monkeypatch.setattr(pcs, "event_dispatch_service", MagicMock())
    monkeypatch.setattr(pcs, "activity_service", MagicMock())
    monkeypatch.setattr(pcs, "channel_completion_report", MagicMock())
    monkeypatch.setattr(pcs, "_spawn_breaker_verdict", lambda *a, **k: None)
    monkeypatch.setattr(pcs, "_spawn_post_turn_delivery", lambda *a, **k: None)
    one_shot = MagicMock()
    monkeypatch.setattr(wired.m, "spawn_subscription_failure", one_shot)
    audit = AsyncMock()
    monkeypatch.setattr(audit_mod.platform_audit_service, "log", audit)
    return SimpleNamespace(pcs=pcs, db=pull_db, switch_db=switch_db, wired=wired,
                           one_shot=one_shot, audit=audit)


def _post(sink, *, token, error_code="billing", status="failed", applied=None,
          capable=True, cost=None, terminated=False, content="usage limit reached"):
    md = {"auth_override_capable": capable}
    if applied:
        md["auth_override"] = applied
    if terminated:
        md["terminated"] = True
    return asyncio.run(sink.pcs.handle_task_result(
        "exec-p", token, status=status, content=content if status != "success" else "done",
        error_code=error_code if status != "success" else None, cost=cost, metadata=md,
    ))


class TestTheSinkWalksInPlace:
    def test_a_refusal_answers_retry_on_the_next_subscription(self, sink):
        out = _post(sink, token="t0", cost=0.05)
        assert out.kind == "retry"
        assert out.retry["auth_override"] == {"oauth_token": "tok-sub-b"}
        assert out.retry["claim_token"] not in (None, "t0")
        assert out.retry["timeout_seconds"] and out.retry["timeout_seconds"] < 3600
        # In place: still running under the NEW token, and no terminal at all.
        assert sink.db.row["status"] == sink.db.S.RUNNING
        assert sink.db.row["claim_token"] == out.retry["claim_token"]
        assert sink.db.terminals == []
        assert sink.pcs.event_dispatch_service.spawn_task_terminal_event.call_count == 0
        # Attributed to the credential the attempt ran on; the secret never stored.
        sink.switch_db.record_rate_limit_event.assert_called_once()
        assert sink.switch_db.record_rate_limit_event.call_args.kwargs["subscription_id"] == "sub-a"
        assert "tok-sub-b" not in sink.db.row["backlog_metadata"]
        assert sink.one_shot.call_count == 0

    def test_a_duplicate_post_gets_the_same_answer_and_records_nothing_twice(self, sink):
        first = _post(sink, token="t0")
        again = _post(sink, token="t0")
        assert again.kind == "retry"
        assert again.retry == first.retry
        assert sink.switch_db.record_rate_limit_event.call_count == 1
        assert sink.db.terminals == [], "a lost 200 must never become a FAILED terminal"

    def test_the_walk_ends_in_one_failed_row_with_the_trail(self, sink):
        r1 = _post(sink, token="t0")                                   # SUB-A refused → SUB-B
        r2 = _post(sink, token=r1.retry["claim_token"], applied="oauth_token")  # SUB-B → the key
        assert r2.kind == "retry" and r2.retry["auth_override"] == {"api_key": "sk-ant-test-key"}
        r3 = _post(sink, token=r2.retry["claim_token"], applied="api_key", error_code="auth")
        assert r3.kind == "applied" and r3.status == sink.db.S.FAILED
        assert len(sink.db.terminals) == 1
        error = sink.db.terminals[0][1].error
        assert "Tried:" in error and "SUB-A" in error and "SUB-B" in error and "platform API key" in error
        assert sink.one_shot.call_count == 0, "the walk recorded every refusal; no one-shot switch"
        titles = [c.kwargs["data"].title for c in sink.switch_db.create_notification.call_args_list]
        assert titles.count("No subscription could serve a turn") == 1
        assert sink.db.walk_state()["summary"]["exhausted"] is True

    def test_served_on_a_trial_commits_and_rolls_the_cost_in(self, sink):
        r1 = _post(sink, token="t0", cost=0.05)
        out = _post(sink, token=r1.retry["claim_token"], status="success", applied="oauth_token", cost=0.10)
        assert out.kind == "applied" and out.status == sink.db.S.SUCCESS
        assert sink.wired.performed == ["sub-b"], "the serving subscription is committed once"
        assert sink.db.attributed == ["sub-b"], "SUB-004: the row bills the subscription that served"
        assert sink.db.terminals[0][1].cost == pytest.approx(0.15)
        assert sink.db.walk_state()["summary"]["switched"] is True

    def test_a_trial_the_worker_did_not_apply_is_the_baselines(self, sink):
        r1 = _post(sink, token="t0")
        _post(sink, token=r1.retry["claim_token"])  # no `auth_override` reported
        second = sink.switch_db.record_rate_limit_event.call_args_list[1]
        assert second.kwargs["subscription_id"] == "sub-a"

    @pytest.mark.parametrize("why", ["incapable", "terminated", "not_a_credential_failure"])
    def test_the_old_path_is_kept_where_the_walk_cannot_run(self, sink, why):
        kw = {"incapable": dict(capable=False),
              "terminated": dict(terminated=True),
              "not_a_credential_failure": dict(error_code="timeout")}[why]
        out = _post(sink, token="t0", **kw)
        assert out.kind == "applied" and out.status == sink.db.S.FAILED
        assert sink.db.row["claim_token"] == "t0", "nothing rotated"
        if why != "not_a_credential_failure":
            sink.one_shot.assert_called_once()

    def test_an_exhausted_budget_stops_without_an_exhausted_notice(self, sink):
        sink.db.row["started_at"] = _now_iso(-3590)
        out = _post(sink, token="t0")
        assert out.kind == "applied" and out.status == sink.db.S.FAILED
        assert "time budget" in sink.db.terminals[0][1].error
        titles = [c.kwargs["data"].title for c in sink.switch_db.create_notification.call_args_list]
        assert "No subscription could serve a turn" not in titles

    def test_a_stale_token_falls_through_to_the_cas(self, sink):
        out = _post(sink, token="someone-else")
        assert out.kind == "conflict"
        assert sink.switch_db.record_rate_limit_event.call_count == 0

    def test_the_audit_records_the_retry_without_the_secret(self, sink):
        _post(sink, token="t0")
        details = sink.audit.call_args.kwargs["details"]
        assert details["reason"] == "subscription_auto_switch"
        assert "tok-sub-b" not in json.dumps(details)


# ===========================================================================
# D — the rebuilt result carries the walk summary
# ===========================================================================
def test_the_rebuilt_result_carries_the_walk_summary(monkeypatch):
    from services import task_execution_service as tes
    summary = {"switched": True, "attempts": [{"attempt": 1}]}
    row = SimpleNamespace(
        status="success", error=None, claude_session_id=None, compact_metadata=None,
        response="ok", cost=0.1, context_used=None, context_max=None, execution_log=None,
        backlog_metadata=json.dumps({"subscription_walk": {"summary": summary}}),
    )
    monkeypatch.setattr(tes, "db", SimpleNamespace(get_execution=lambda _id: row))
    assert tes.result_from_execution_row("exec-x").subscription_switch == summary


# ===========================================================================
# E — the agent's pull worker
# ===========================================================================
@pytest.fixture
def worker(monkeypatch):
    from unit.test_1081_pull_worker import _FakeClient, _FakeResp
    from agent_server.services import pull_worker as pw

    staged = []
    import agent_server.utils.credential_sanitizer as cs
    monkeypatch.setattr(cs, "add_credential_value", staged.append)
    monkeypatch.setattr(pw, "_persist_pull_result", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_delete_pull_result", lambda *a, **k: None)

    def install(runtime):
        stub = types.ModuleType("agent_server.services.runtime_adapter")
        stub.get_runtime = lambda: runtime
        monkeypatch.setitem(sys.modules, "agent_server.services.runtime_adapter", stub)

    return SimpleNamespace(pw=pw, FakeClient=_FakeClient, FakeResp=_FakeResp,
                           staged=staged, install=install)


def _md():
    return SimpleNamespace(model_dump=lambda: {"cost_usd": 0.01})


class TestTheWorkerFollowsTheInstruction:
    def _claim(self):
        return {"execution_id": "exec-w", "claim_token": "t0",
                "envelope": {"payload": {"message": "hi", "task_overrides": {"timeout_seconds": 600}}}}

    def test_a_retry_reruns_the_turn_on_the_given_credential(self, worker):
        from fastapi import HTTPException
        runtime = SimpleNamespace(
            supports_auth_override=True,
            execute_headless=AsyncMock(side_effect=[
                HTTPException(status_code=429, detail="usage limit"),
                ("served", [], _md(), "s1"),
            ]),
        )
        worker.install(runtime)
        retry = {"claim_token": "t1", "auth_override": {"oauth_token": "tok-b"},
                 "timeout_seconds": 500, "attempt": 1}
        client = worker.FakeClient(post_responses=[
            worker.FakeResp(200, {"applied": False, "retry": retry}),
            worker.FakeResp(200, {"applied": True}),
        ])
        asyncio.run(worker.pw._run_and_report(self._claim(), client, "http://b", "k", "alpha"))

        first, second = runtime.execute_headless.call_args_list
        assert "auth_override" not in first.kwargs, "the first attempt runs on the baseline"
        assert second.kwargs["auth_override"].kind() == "oauth_token"
        assert second.kwargs["auth_override"].secret() == "tok-b"
        assert second.kwargs["timeout_seconds"] == 500
        assert second.kwargs["execution_id"] == "exec-w"
        (_, b1, _), (_, b2, _) = client.posts
        assert b1["claim_token"] == "t0" and b1["error_code"] == "billing"
        assert b1["metadata"]["auth_override_capable"] is True
        assert "auth_override" not in b1["metadata"]
        assert b2["claim_token"] == "t1" and b2["status"] == "success"
        assert b2["metadata"]["auth_override"] == "oauth_token"
        assert worker.staged == ["tok-b"], "the secret is staged for redaction before the spawn"
        assert "tok-b" not in json.dumps([b1, b2])

    def test_a_runtime_that_cannot_take_a_credential_never_follows(self, worker):
        from fastapi import HTTPException
        runtime = SimpleNamespace(
            execute_headless=AsyncMock(side_effect=HTTPException(status_code=429, detail="limit")),
        )
        worker.install(runtime)
        client = worker.FakeClient(post_responses=[
            worker.FakeResp(200, {"retry": {"claim_token": "t1", "auth_override": {"oauth_token": "x"}}}),
        ])
        asyncio.run(worker.pw._run_and_report(self._claim(), client, "http://b", "k", "alpha"))
        assert runtime.execute_headless.await_count == 1
        assert client.posts[0][1]["metadata"]["auth_override_capable"] is False

    def test_a_terminated_turn_says_so_and_is_never_rerun(self, worker, monkeypatch):
        """A user's cancel must not come back on another credential: the worker
        reports `terminated` (the sink then never walks it) and ignores a retry."""
        from fastapi import HTTPException
        runtime = SimpleNamespace(
            supports_auth_override=True,
            execute_headless=AsyncMock(side_effect=HTTPException(status_code=429, detail="limit")),
        )
        worker.install(runtime)
        registry = MagicMock()
        registry.was_terminated.side_effect = lambda eid: True
        import agent_server.services.process_registry as pr
        monkeypatch.setattr(pr, "get_process_registry", lambda: registry)
        client = worker.FakeClient(post_responses=[
            worker.FakeResp(200, {"retry": {"claim_token": "t1", "auth_override": {"oauth_token": "x"}}}),
        ])
        asyncio.run(worker.pw._run_and_report(self._claim(), client, "http://b", "k", "alpha"))
        assert runtime.execute_headless.await_count == 0, "terminated before spawn"
        assert len(client.posts) == 1, "no re-run"
        assert client.posts[0][1]["metadata"]["terminated"] is True

    def test_a_restarted_worker_resending_a_result_does_not_ask_to_walk(self, worker, monkeypatch, tmp_path):
        pw = worker.pw
        pending = tmp_path / "pending"
        pending.mkdir()
        (pending / "exec-r.json").write_text(json.dumps({"execution_id": "exec-r", "body": {
            "claim_token": "t0", "status": "failed", "error_code": "billing",
            "metadata": {"auth_override_capable": True}}}))
        monkeypatch.setattr(pw, "_PENDING_PULL_DIR", pending)
        monkeypatch.setattr(pw, "_worker_creds_present", lambda: True)
        monkeypatch.setattr(pw, "_SWEEP_INITIAL_JITTER_SECONDS", 0)
        monkeypatch.setattr(pw, "_SWEEP_PER_ENVELOPE_JITTER_SECONDS", 0)
        sent = []

        async def _deliver(client, execution_id, body, *a, **k):
            sent.append(body)
            return True
        monkeypatch.setattr(pw, "_deliver_result", _deliver)
        asyncio.run(pw.resend_pending_pull_results())
        assert sent and sent[0]["metadata"]["auth_override_capable"] is False
