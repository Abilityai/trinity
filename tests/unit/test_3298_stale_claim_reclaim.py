"""#3298 — a claim left in_flight by a dead process stops answering 409.

`idempotency_service.begin()` inserts `(scope, key)` as `in_flight`; only the
claiming request's own `complete()`/`fail()` moved it on. A backend that died
between them left the claim `in_flight`, and every identical retry got 409 for
the full 24 h TTL.

Now each claim carries an owner token (attach/complete/release are
compare-and-set on it), and `/chat` + `/task` admission reclaims a stale claim
once its lease (the agent's execution timeout + the slot buffer) has passed AND
the run it names has ended or is gone, or it never named one. A live or
succeeded run keeps the 409.

Store tests run on a real SQLite built by the platform's own `init_schema`;
admission tests drive the real `_reclaim_stale_claim` / `begin_task_idempotency`
on that database with only the clock moved.
"""
import sqlite3
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


@pytest.fixture()
def real_db(tmp_path, monkeypatch):
    db_file = tmp_path / "trinity-3298.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))
    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))
    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    from database import db
    return db


def _key():
    # `db.engine` is cached across tests, so every test uses its own key.
    return f"k-{uuid.uuid4().hex}"


def _agent():
    return f"agent-{uuid.uuid4().hex[:8]}"


def _row(db, scope, key):
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import idempotency_keys as t
    with get_engine().connect() as conn:
        r = conn.execute(select(t).where(t.c.scope == scope, t.c.idempotency_key == key)).mappings().first()
    return dict(r) if r else None


# ---------------------------------------------------------------- the store


class TestOwnerToken:
    def test_a_fresh_claim_returns_its_token_and_a_duplicate_sees_the_holders(self, real_db):
        key = _key()
        first = real_db.idempotency_claim("agent:a", key)
        dup = real_db.idempotency_claim("agent:a", key)
        assert first["state"] == "new" and first["claim_token"]
        assert dup["state"] == "in_flight"
        assert dup["claim_token"] == first["claim_token"]
        assert dup["updated_at"]

    def test_a_late_complete_after_a_reclaim_leaves_the_reclaimers_row(self, real_db):
        key = _key()
        old = real_db.idempotency_claim("agent:a", key)
        assert real_db.idempotency_reclaim_in_flight("agent:a", key, old["claim_token"])
        new = real_db.idempotency_claim("agent:a", key)
        real_db.idempotency_attach_execution("agent:a", key, "exec-new", claim_token=new["claim_token"])

        # The original request was only slow: its writes now find no row of theirs.
        real_db.idempotency_attach_execution("agent:a", key, "exec-old", claim_token=old["claim_token"])
        real_db.idempotency_complete("agent:a", key, "exec-old", {"r": "old"}, claim_token=old["claim_token"])
        row = _row(real_db, "agent:a", key)
        assert row["status"] == "in_flight"
        assert row["execution_id"] == "exec-new"
        assert row["claim_token"] == new["claim_token"]

    def test_a_late_fail_after_a_reclaim_does_not_delete_the_reclaimers_row(self, real_db):
        key = _key()
        old = real_db.idempotency_claim("agent:a", key)
        real_db.idempotency_reclaim_in_flight("agent:a", key, old["claim_token"])
        new = real_db.idempotency_claim("agent:a", key)
        real_db.idempotency_release("agent:a", key, claim_token=old["claim_token"])
        assert _row(real_db, "agent:a", key)["claim_token"] == new["claim_token"]

    def test_the_owner_still_completes_and_releases_its_own_claim(self, real_db):
        key = _key()
        mine = real_db.idempotency_claim("agent:a", key)
        real_db.idempotency_complete("agent:a", key, "e1", {"ok": True}, claim_token=mine["claim_token"])
        assert _row(real_db, "agent:a", key)["status"] == "completed"
        key2 = _key()
        mine2 = real_db.idempotency_claim("agent:a", key2)
        real_db.idempotency_release("agent:a", key2, claim_token=mine2["claim_token"])
        assert _row(real_db, "agent:a", key2) is None

    def test_a_reclaim_with_a_stale_token_matches_nothing(self, real_db):
        key = _key()
        real_db.idempotency_claim("agent:a", key)
        assert real_db.idempotency_reclaim_in_flight("agent:a", key, "not-the-token") is False
        assert _row(real_db, "agent:a", key) is not None

    def test_a_completed_row_is_never_reclaimed(self, real_db):
        key = _key()
        c = real_db.idempotency_claim("agent:a", key)
        real_db.idempotency_complete("agent:a", key, "e1", {"ok": True}, claim_token=c["claim_token"])
        assert real_db.idempotency_reclaim_in_flight("agent:a", key, c["claim_token"]) is False

    def test_a_pre_3298_tokenless_claim_can_still_be_reclaimed(self, real_db):
        key = _key()
        real_db.idempotency_claim("agent:a", key)
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import idempotency_keys as t
        with get_engine().begin() as conn:
            conn.execute(update(t).where(t.c.idempotency_key == key).values(claim_token=None))
        dup = real_db.idempotency_claim("agent:a", key)
        assert dup["claim_token"] is None
        assert real_db.idempotency_reclaim_in_flight("agent:a", key, None) is True


# ------------------------------------------------------------ the admission


# Default leases: 3600 s timeout + 300 s; a claim that names no run waits out
# the in-memory queue too, (3 + 1) x 3600 s + 300 s.
_LEASE = 3900
_UNNAMED_LEASE = 4 * 3600 + 300


def _stale(seconds_past_lease=60, *, lease=_LEASE):
    """Move the admission clock past a lease (default: a claim naming a run)."""
    from services import idempotency_service
    from utils.helpers import utc_now
    return patch.object(idempotency_service, "_now",
                        return_value=utc_now() + timedelta(seconds=lease + seconds_past_lease))


def _held_claim(db, agent, *, execution_status=None):
    """A claim some (now dead) request took, optionally with its run attached."""
    from services import idempotency_service as svc
    key = _key()
    scope = svc.make_agent_scope(agent)
    first = svc.begin(scope, key)
    assert first.enabled and not first.replay
    if execution_status is not None:
        row = db.create_task_execution(agent_name=agent, message="m", triggered_by="mcp")
        svc.attach_execution(first, row.id)
        if execution_status != "running":
            from db.write_params import ExecutionResult
            db.update_execution_status(row.id, execution_status,
                                       ExecutionResult(response="r", error="boom"))
    return key, first


def _retry(agent, key):
    from services import dispatch_admission_service as das
    return das.begin_task_idempotency(name=agent, idempotency_key=key)


class TestStaleClaimReclaim:
    def test_inside_the_lease_a_retry_still_gets_in_progress(self, real_db):
        agent = _agent()
        key, _ = _held_claim(real_db, agent)
        idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_past_the_lease_a_claim_that_never_named_a_run_is_reclaimed(self, real_db):
        agent = _agent()
        key, first = _held_claim(real_db, agent)
        with _stale(lease=_UNNAMED_LEASE):
            idem, replay = _retry(agent, key)
        assert replay is None and not idem.replay  # the retry dispatches
        assert idem.claim_token and idem.claim_token != first.claim_token

    def test_a_claim_that_never_named_a_run_waits_out_the_in_memory_queue(self, real_db):
        """A /chat turn with no execution row can sit behind IN_MEMORY_DEPTH
        turns; past the plain lease it may still be queued, so it holds."""
        agent = _agent()
        key, _ = _held_claim(real_db, agent)
        with _stale():
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight
        with _stale(-60, lease=_UNNAMED_LEASE):
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_past_the_lease_a_claim_whose_run_ended_is_reclaimed(self, real_db):
        agent = _agent()
        key, _ = _held_claim(real_db, agent, execution_status="failed")
        with _stale():
            idem, replay = _retry(agent, key)
        assert replay is None and not idem.replay

    @pytest.mark.parametrize("status", ["running", "success"])
    def test_a_live_or_succeeded_run_keeps_its_claim(self, real_db, status):
        agent = _agent()
        key, _ = _held_claim(real_db, agent, execution_status=status)
        with _stale():
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_the_lease_follows_the_agents_timeout(self, real_db):
        agent = _agent()
        key, _ = _held_claim(real_db, agent, execution_status="failed")
        from services import dispatch_admission_service as das
        # 60 s past the DEFAULT lease is still inside a 2 h timeout's lease.
        with _stale(), patch.object(das.db, "get_execution_timeout", return_value=7200):
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_an_unreadable_timeout_holds(self, real_db):
        agent = _agent()
        key, _ = _held_claim(real_db, agent, execution_status="failed")
        from services import dispatch_admission_service as das
        with _stale(), patch.object(das.db, "get_execution_timeout", side_effect=RuntimeError("db down")):
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_a_scheduler_key_is_never_reclaimed(self, real_db):
        from services import idempotency_service as svc
        agent = _agent()
        key = f"sched:{uuid.uuid4().hex}"
        svc.begin(svc.make_agent_scope(agent), key)
        with _stale():
            idem, replay = _retry(agent, key)
        assert replay is not None and replay.in_flight

    def test_two_racing_retries_start_one_run(self, real_db):
        from services import idempotency_service as svc
        from services import dispatch_admission_service as das
        agent = _agent()
        key, _ = _held_claim(real_db, agent)
        seen = svc.begin(svc.make_agent_scope(agent), key)  # both retries read the stale claim
        with _stale(lease=_UNNAMED_LEASE):
            a = das._reclaim_stale_claim(seen, name=agent)
            b = das._reclaim_stale_claim(seen, name=agent)
        assert [a.replay, b.replay].count(False) == 1  # exactly one dispatches

    def test_the_slow_original_cannot_complete_over_the_reclaimer(self, real_db):
        from services import idempotency_service as svc
        agent = _agent()
        key, original = _held_claim(real_db, agent)
        with _stale(lease=_UNNAMED_LEASE):
            fresh, _ = _retry(agent, key)
        svc.complete(original, "exec-late", {"late": True})   # the original finally returns
        svc.fail(original)
        row = _row(real_db, svc.make_agent_scope(agent), key)
        assert row["status"] == "in_flight" and row["claim_token"] == fresh.claim_token
        svc.complete(fresh, "exec-fresh", {"fresh": True})
        assert _row(real_db, svc.make_agent_scope(agent), key)["execution_id"] == "exec-fresh"
