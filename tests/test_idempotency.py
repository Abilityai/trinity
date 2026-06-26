"""
Idempotency-key unit tests (RELIABILITY-006 / Issue #525).

Covers:
- IdempotencyOperations (db/idempotency.py): atomic claim, in-flight detection,
  completed replay, release, 24h expiry re-claim, purge, scope isolation.
- idempotency_service: key derivation (webhook body-hash, schedule), and the
  begin/complete/fail decision flow over a fake db delegating to the real ops.

Runs against an isolated temporary SQLite DB wired into db.connection via
monkeypatch — no live backend required. Mirrors test_audit_log_unit.py.
"""

import importlib.util
import os
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

# Add backend to path for direct imports.
_backend_path = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "src", "backend")
)
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


# Override the backend-requiring autouse fixtures from the package conftest so
# these pure-unit tests run without a live backend (mirrors test_audit_log_unit).
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def idem_ops(monkeypatch, tmp_path):
    """Fresh IdempotencyOperations bound to an isolated temp DB.

    The connection context commits on clean exit / rolls back on exception,
    exactly like the real db.connection.get_db_connection — required so a
    claim() committed by one call is visible to the next (cross-process
    atomicity is the whole point of the table).
    """
    db_path = str(tmp_path / "idem_test.db")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE idempotency_keys (
            scope TEXT NOT NULL,
            idempotency_key TEXT NOT NULL,
            execution_id TEXT,
            status TEXT NOT NULL,
            response_snapshot TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (scope, idempotency_key)
        )
        """
    )
    conn.commit()
    conn.close()

    class _ConnContext:
        def __enter__(self):
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            return self._conn

        def __exit__(self, exc_type, exc_val, exc_tb):
            if exc_type is None:
                self._conn.commit()
            else:
                self._conn.rollback()
            self._conn.close()
            return False

    fake_conn_module = types.ModuleType("db.connection")
    fake_conn_module.get_db_connection = lambda: _ConnContext()
    fake_conn_module.DB_PATH = db_path
    monkeypatch.setitem(sys.modules, "db.connection", fake_conn_module)

    # Route the SQLAlchemy engine seam (#300) at the SAME temp file. The
    # idempotency ops are fully converted to get_engine() (which reads
    # DATABASE_URL); its engine cache is keyed by URL, so dispose after setting
    # DATABASE_URL so the temp file's engine is the one created — and again at
    # teardown to drop the cached engine.
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    import db.engine as engine_mod
    engine_mod.dispose_engines()

    # Force reimport so the stub db.connection is picked up.
    monkeypatch.delitem(sys.modules, "db.idempotency", raising=False)
    from db.idempotency import IdempotencyOperations

    ops = IdempotencyOperations()
    # Pin the temp path so test helpers can open the SAME DB directly without
    # re-importing db.connection (which the autouse #762 baseline-restore may
    # reset to the real module mid-test).
    ops._test_db_path = db_path
    yield ops
    engine_mod.dispose_engines()


@pytest.fixture
def idem_service(idem_ops, monkeypatch):
    """Load services/idempotency_service.py standalone (bypassing the heavy
    services/__init__) with a fake `database.db` delegating to idem_ops."""
    fake_db = types.SimpleNamespace(
        idempotency_claim=idem_ops.claim,
        idempotency_attach_execution=idem_ops.attach_execution,
        idempotency_complete=idem_ops.complete,
        idempotency_release=idem_ops.release,
        idempotency_purge_expired=idem_ops.purge_expired,
    )
    fake_database = types.ModuleType("database")
    fake_database.db = fake_db
    monkeypatch.setitem(sys.modules, "database", fake_database)

    path = os.path.join(_backend_path, "services", "idempotency_service.py")
    spec = importlib.util.spec_from_file_location("_idem_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# IdempotencyOperations — claim lifecycle
# ---------------------------------------------------------------------------

class TestIdempotencyOps:
    def test_first_claim_is_new(self, idem_ops):
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "new"
        assert res["execution_id"] is None
        assert res["snapshot"] is None

    def test_duplicate_claim_is_in_flight(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "in_flight"

    def test_completed_claim_replays_snapshot(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.complete("agent:a", "k1", "exec-123", {"response": "hi", "n": 1})
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "completed"
        assert res["execution_id"] == "exec-123"
        assert res["snapshot"] == {"response": "hi", "n": 1}

    def test_attach_execution_sets_id(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.attach_execution("agent:a", "k1", "exec-xyz")
        res = idem_ops.claim("agent:a", "k1")
        # still in_flight (not completed) but execution_id now populated
        assert res["state"] == "in_flight"
        assert res["execution_id"] == "exec-xyz"

    def test_release_allows_reclaim(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.release("agent:a", "k1")
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "new"

    def test_release_does_not_remove_completed(self, idem_ops):
        idem_ops.claim("agent:a", "k1")
        idem_ops.complete("agent:a", "k1", "exec-1", {"ok": True})
        idem_ops.release("agent:a", "k1")  # must be a no-op on completed rows
        res = idem_ops.claim("agent:a", "k1")
        assert res["state"] == "completed"

    def test_scope_isolation(self, idem_ops):
        idem_ops.claim("agent:a", "shared-key")
        # same key, different scope → independent claim
        res = idem_ops.claim("agent:b", "shared-key")
        assert res["state"] == "new"

    def test_expired_row_is_reclaimed_as_new(self, idem_ops):
        # Seed a row older than the TTL directly.
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=25))
        with _direct(idem_ops) as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (scope, idempotency_key, execution_id, "
                "status, response_snapshot, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("agent:a", "k1", "old-exec", "completed", '{"x":1}', old, old),
            )
            conn.commit()
        res = idem_ops.claim("agent:a", "k1", ttl_hours=24)
        assert res["state"] == "new"  # expired → re-claimed

    def test_purge_expired(self, idem_ops):
        old = _iso(datetime.now(timezone.utc) - timedelta(hours=30))
        with _direct(idem_ops) as conn:
            conn.execute(
                "INSERT INTO idempotency_keys (scope, idempotency_key, execution_id, "
                "status, response_snapshot, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                ("agent:a", "old", None, "completed", None, old, old),
            )
            conn.commit()
        idem_ops.claim("agent:a", "fresh")  # current
        removed = idem_ops.purge_expired(ttl_hours=24)
        assert removed == 1
        # fresh survives
        assert idem_ops.claim("agent:a", "fresh")["state"] == "in_flight"


import contextlib


@contextlib.contextmanager
def _direct(idem_ops):
    """Open a raw connection to the SAME temp DB the ops use, by path — avoids
    re-importing db.connection (which the autouse #762 restore can reset)."""
    conn = sqlite3.connect(idem_ops._test_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# idempotency_service — derivation + decisions
# ---------------------------------------------------------------------------

class TestDerivation:
    def test_webhook_key_deterministic(self, idem_service):
        a = idem_service.derive_webhook_key("tok", b'{"x":1}')
        b = idem_service.derive_webhook_key("tok", b'{"x":1}')
        assert a == b
        assert a.startswith("auto:")

    def test_webhook_key_body_sensitive(self, idem_service):
        a = idem_service.derive_webhook_key("tok", b'{"x":1}')
        b = idem_service.derive_webhook_key("tok", b'{"x":2}')
        assert a != b

    def test_webhook_key_token_sensitive(self, idem_service):
        a = idem_service.derive_webhook_key("tok1", b"body")
        b = idem_service.derive_webhook_key("tok2", b"body")
        assert a != b

    def test_schedule_key(self, idem_service):
        assert idem_service.derive_schedule_key("exec-9") == "sched:exec-9"

    def test_scope_helpers(self, idem_service):
        assert idem_service.make_agent_scope("bob") == "agent:bob"
        assert idem_service.make_webhook_scope("zzz") == "webhook:zzz"


class TestServiceDecisions:
    def test_no_key_is_disabled_noop(self, idem_service):
        d = idem_service.begin("agent:a", None)
        assert d.enabled is False
        assert d.replay is False
        # complete/fail are safe no-ops on a disabled decision
        idem_service.complete(d, "exec", {"a": 1})
        idem_service.fail(d)

    def test_first_begin_then_replay_completed(self, idem_service):
        d1 = idem_service.begin("agent:a", "k1")
        assert d1.enabled and not d1.replay
        idem_service.complete(d1, "exec-1", {"response": "done"})

        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is True
        assert d2.in_flight is False
        assert d2.execution_id == "exec-1"
        assert d2.snapshot == {"response": "done"}

    def test_begin_in_flight_replay(self, idem_service):
        idem_service.begin("agent:a", "k1")  # leaves in_flight
        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is True
        assert d2.in_flight is True

    def test_fail_releases_claim_for_retry(self, idem_service):
        d1 = idem_service.begin("agent:a", "k1")
        idem_service.fail(d1)
        d2 = idem_service.begin("agent:a", "k1")
        assert d2.replay is False  # reclaimed as new


# ---------------------------------------------------------------------------
# Effect-scoped idempotency (#1084) — per-sink exactly-once-style guard
# ---------------------------------------------------------------------------

@pytest.fixture
def effect_service(idem_ops, monkeypatch):
    """idempotency_service loaded with a fake `database.db` that supports BOTH
    the real idempotency ops (temp DB) AND a controllable get_execution(), so
    the effect-scoped guard (#1084) can resolve+validate executions in-unit.

    Tests register executions via module._test_executions[execution_id] = ns.
    """
    executions: dict = {}

    fake_db = types.SimpleNamespace(
        idempotency_claim=idem_ops.claim,
        idempotency_attach_execution=idem_ops.attach_execution,
        idempotency_complete=idem_ops.complete,
        idempotency_release=idem_ops.release,
        idempotency_purge_expired=idem_ops.purge_expired,
        get_execution=lambda eid: executions.get(eid),
    )
    fake_database = types.ModuleType("database")
    fake_database.db = fake_db
    monkeypatch.setitem(sys.modules, "database", fake_database)

    path = os.path.join(_backend_path, "services", "idempotency_service.py")
    spec = importlib.util.spec_from_file_location("_idem_service_effect", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._test_executions = executions
    return module


def _register_execution(svc, execution_id, agent_name, **extra):
    svc._test_executions[execution_id] = types.SimpleNamespace(
        agent_name=agent_name,
        triggered_by=extra.get("triggered_by", "telegram"),
        source_user_email=extra.get("source_user_email", "u@example.com"),
    )


async def _run_effect(
    svc,
    *,
    execution_id,
    agent_name,
    identifying_args,
    effect_type="message",
    dedup_label="",
    body_result=None,
    body_exc=None,
    sends=None,
):
    """Simulate a sink: enter effect_guard, run the 'send' unless replay."""
    try:
        async with svc.effect_guard(
            effect_type,
            identifying_args,
            execution_id=execution_id,
            agent_name=agent_name,
            dedup_label=dedup_label,
        ) as g:
            if g.replay:
                return ("replay", g.snapshot)
            if body_exc is not None:
                raise body_exc
            if sends is not None:
                sends.append(identifying_args)
            g.snapshot = body_result if body_result is not None else {"ok": True}
            return ("sent", g.snapshot)
    except svc.EffectInProgressError as e:
        return ("in_flight", str(e))


class TestEffectScopeAndKey:
    def test_effect_scope_helper(self, effect_service):
        assert effect_service.make_effect_scope("exec-1") == "effect:exec-1"

    def test_payment_scope_helper(self, effect_service):
        assert effect_service.make_payment_scope("areq-9") == "payment:areq-9"

    def test_derive_effect_key_deterministic(self, effect_service):
        a = effect_service.derive_effect_key(
            "exec-1", "message", {"recipient": "u@e.com", "channel": "telegram"}
        )
        b = effect_service.derive_effect_key(
            "exec-1", "message", {"recipient": "u@e.com", "channel": "telegram"}
        )
        assert a == b
        assert a.startswith("message:")

    def test_derive_effect_key_arg_order_insensitive(self, effect_service):
        """Dict identity is canonicalized — key order must not change the key."""
        a = effect_service.derive_effect_key(
            "exec-1", "message", {"recipient": "u@e.com", "channel": "telegram"}
        )
        b = effect_service.derive_effect_key(
            "exec-1", "message", {"channel": "telegram", "recipient": "u@e.com"}
        )
        assert a == b

    def test_derive_effect_key_dedup_label_distinct(self, effect_service):
        a = effect_service.derive_effect_key("exec-1", "message", {"r": "x"}, "")
        b = effect_service.derive_effect_key("exec-1", "message", {"r": "x"}, "second")
        assert a != b

    def test_derive_effect_key_execution_scoped(self, effect_service):
        a = effect_service.derive_effect_key("exec-1", "message", {"r": "x"})
        b = effect_service.derive_effect_key("exec-2", "message", {"r": "x"})
        assert a != b

    def test_derive_effect_key_recipient_distinct(self, effect_service):
        a = effect_service.derive_effect_key("exec-1", "message", {"recipient": "a@e.com"})
        b = effect_service.derive_effect_key("exec-1", "message", {"recipient": "b@e.com"})
        assert a != b


class TestResolveAndValidateExecution:
    def test_valid_match_returns_execution(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        ex = effect_service.resolve_and_validate_execution("exec-1", "agentA")
        assert ex is not None
        assert ex.agent_name == "agentA"

    def test_agent_mismatch_fail_open(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        # The execution belongs to agentA — agentB must NOT be able to claim it.
        ex = effect_service.resolve_and_validate_execution("exec-1", "agentB")
        assert ex is None

    def test_missing_execution_fail_open(self, effect_service):
        ex = effect_service.resolve_and_validate_execution("nope", "agentA")
        assert ex is None

    def test_none_execution_id_fail_open(self, effect_service):
        ex = effect_service.resolve_and_validate_execution(None, "agentA")
        assert ex is None


class TestEffectGuard:
    async def test_success_then_replay_no_second_send(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        outcome1, snap1 = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, body_result={"message_id": "m1"}, sends=sends,
        )
        assert outcome1 == "sent"
        assert snap1 == {"message_id": "m1"}

        # Re-enter the SAME identity → replay, NO second send.
        outcome2, snap2 = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, body_result={"message_id": "SHOULD_NOT_RUN"}, sends=sends,
        )
        assert outcome2 == "replay"
        assert snap2 == {"message_id": "m1"}
        assert len(sends) == 1  # exactly one real send

    async def test_different_body_same_identity_one_send(self, effect_service):
        """CRITICAL (Issue 1): the LLM body is NOT part of the key — a second
        call with a *different generated body* but the same resolved identity
        must dedupe to exactly one send."""
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, body_result={"body": "Hello!"}, sends=sends,
        )
        # Different body content, same (execution, recipient, channel).
        outcome2, _ = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, body_result={"body": "Completely different text"}, sends=sends,
        )
        assert outcome2 == "replay"
        assert len(sends) == 1

    async def test_dedup_label_allows_second_intentional_send(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        outcome2, _ = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, dedup_label="reminder", sends=sends,
        )
        assert outcome2 == "sent"
        assert len(sends) == 2  # distinct label → two sends allowed

    async def test_fresh_execution_id_not_deduped(self, effect_service):
        """A #271-style retry creates a fresh execution_id → distinct scope →
        the send is allowed again."""
        _register_execution(effect_service, "exec-1", "agentA")
        _register_execution(effect_service, "exec-2", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        outcome2, _ = await _run_effect(
            effect_service, execution_id="exec-2", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        assert outcome2 == "sent"
        assert len(sends) == 2

    async def test_exception_releases_for_retry(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}

        with pytest.raises(ValueError):
            await _run_effect(
                effect_service, execution_id="exec-1", agent_name="agentA",
                identifying_args=args, body_exc=ValueError("boom"),
            )

        # The failed first attempt released the claim → retry runs the body.
        sends = []
        outcome2, _ = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        assert outcome2 == "sent"
        assert len(sends) == 1

    async def test_in_flight_replay_raises_not_silent(self, effect_service):
        """codex #6: an in_flight replay must raise a retryable error, NOT
        silently return None-as-success (skip the send AND report success)."""
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}

        # Manually leave an in_flight claim for this exact effect key.
        scope = effect_service.make_effect_scope("exec-1")
        key = effect_service.derive_effect_key("exec-1", "message", args, "")
        effect_service.begin(scope, key)  # claims in_flight, never completed

        outcome, _ = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args,
        )
        assert outcome == "in_flight"

    async def test_fail_open_when_execution_absent(self, effect_service):
        """No execution registered (old image / absent execution_id) → the send
        proceeds with dedup disabled (fail-open), no exception."""
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        outcome, _ = await _run_effect(
            effect_service, execution_id="ghost", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        assert outcome == "sent"
        assert len(sends) == 1
        # A second call also proceeds — dedup is disabled, not silently swallowed.
        outcome2, _ = await _run_effect(
            effect_service, execution_id="ghost", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        assert outcome2 == "sent"
        assert len(sends) == 2

    async def test_agent_mismatch_fail_open_send_proceeds(self, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []
        # agentB borrows agentA's execution id → fail-open (no dedup), send runs.
        outcome, _ = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentB",
            identifying_args=args, sends=sends,
        )
        assert outcome == "sent"
        assert len(sends) == 1

    async def test_completed_effect_replays_after_aging(self, effect_service, idem_ops):
        """Issue 2 (long-TTL): a completed effect row outlives the lease window.
        Age the row 6h (≪ 24h default) — it must still replay, not re-send."""
        _register_execution(effect_service, "exec-1", "agentA")
        args = {"recipient": "u@e.com", "channel": "telegram"}
        sends = []

        await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, body_result={"message_id": "m1"}, sends=sends,
        )

        # Age the persisted row by 6 hours — still inside the 24h TTL.
        scope = effect_service.make_effect_scope("exec-1")
        key = effect_service.derive_effect_key("exec-1", "message", args, "")
        aged = _iso(datetime.now(timezone.utc) - timedelta(hours=6))
        with _direct(idem_ops) as conn:
            conn.execute(
                "UPDATE idempotency_keys SET created_at=?, updated_at=? "
                "WHERE scope=? AND idempotency_key=?",
                (aged, aged, scope, key),
            )
            conn.commit()

        outcome2, snap2 = await _run_effect(
            effect_service, execution_id="exec-1", agent_name="agentA",
            identifying_args=args, sends=sends,
        )
        assert outcome2 == "replay"
        assert snap2 == {"message_id": "m1"}
        assert len(sends) == 1


class TestPaymentScopeGuard:
    async def test_payment_replay_one_settle(self, effect_service):
        """Nevermined: a retried paid chat with the SAME agent_request_id must
        settle exactly once; the second resolves to a replay."""
        settles = []

        async def _settle(label):
            async with effect_service.effect_guard(
                "nevermined_settle",
                {"phase": "settle"},
                payment_request_id="areq-9",
                execution_id=f"exec-{label}",
            ) as g:
                if g.replay:
                    return ("replay", g.snapshot)
                settles.append(label)
                g.snapshot = {"settled": True, "tx": "0xabc"}
                return ("settled", g.snapshot)

        out1, snap1 = await _settle("1")
        out2, snap2 = await _settle("2")  # same agent_request_id, fresh exec id
        assert out1 == "settled"
        assert out2 == "replay"
        assert snap2 == {"settled": True, "tx": "0xabc"}
        assert len(settles) == 1

    async def test_distinct_payment_requests_both_settle(self, effect_service):
        settles = []

        async def _settle(areq):
            async with effect_service.effect_guard(
                "nevermined_settle", {"phase": "settle"},
                payment_request_id=areq, execution_id="exec-x",
            ) as g:
                if g.replay:
                    return "replay"
                settles.append(areq)
                g.snapshot = {"settled": True}
                return "settled"

        assert await _settle("areq-1") == "settled"
        assert await _settle("areq-2") == "settled"
        assert len(settles) == 2


# ---------------------------------------------------------------------------
# Nevermined settle wiring (#1084) — NeverminedPaymentService.settle_payment_once
# ---------------------------------------------------------------------------

@pytest.fixture
def nvm_service(effect_service, monkeypatch):
    """Load nevermined_payment_service standalone with the effect-scoped
    idempotency module (temp DB) injected as `services.idempotency_service`,
    avoiding the docker-heavy real services/__init__."""
    services_stub = types.ModuleType("services")
    services_stub.idempotency_service = effect_service
    monkeypatch.setitem(sys.modules, "services", services_stub)
    monkeypatch.setitem(sys.modules, "services.idempotency_service", effect_service)

    path = os.path.join(_backend_path, "services", "nevermined_payment_service.py")
    spec = importlib.util.spec_from_file_location("_nvm_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestNeverminedSettleOnce:
    def _config(self):
        return types.SimpleNamespace(nvm_plan_id="plan-1", nvm_environment="sandbox")

    async def test_retried_settle_runs_once(self, nvm_service):
        svc = nvm_service.NeverminedPaymentService()
        calls = []

        async def fake_settle(**kwargs):
            calls.append(kwargs.get("agent_request_id"))
            return nvm_service.NeverminedPaymentResult(
                success=True, tx_hash="0xabc", credits_redeemed="1", remaining_balance="9"
            )

        svc.settle_payment = fake_settle
        cfg = self._config()
        kw = dict(config=cfg, nvm_api_key="k", nvm_environment="sandbox",
                  access_token="t", agent_request_id="areq-1", base_url="")

        r1 = await svc.settle_payment_once(execution_id="exec-1", **kw)
        # Same agent_request_id, fresh execution id (retried paid chat).
        r2 = await svc.settle_payment_once(execution_id="exec-2", **kw)

        assert r1.success and r2.success
        assert r2.tx_hash == "0xabc"           # replayed snapshot
        assert r2.remaining_balance == "9"
        assert len(calls) == 1                  # exactly ONE on-chain settle

    async def test_failed_settle_releases_for_retry(self, nvm_service):
        svc = nvm_service.NeverminedPaymentService()
        attempts = []

        async def flaky_settle(**kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                return nvm_service.NeverminedPaymentResult(success=False, error="timeout")
            return nvm_service.NeverminedPaymentResult(success=True, tx_hash="0xdef")

        svc.settle_payment = flaky_settle
        cfg = self._config()
        kw = dict(config=cfg, nvm_api_key="k", nvm_environment="sandbox",
                  access_token="t", agent_request_id="areq-2", base_url="")

        r1 = await svc.settle_payment_once(execution_id="e1", **kw)
        assert not r1.success                   # first settle failed
        r2 = await svc.settle_payment_once(execution_id="e2", **kw)
        assert r2.success                       # claim released → retry re-settled
        assert len(attempts) == 2

    async def test_distinct_payments_settle_separately(self, nvm_service):
        svc = nvm_service.NeverminedPaymentService()
        calls = []

        async def fake_settle(**kwargs):
            calls.append(kwargs.get("agent_request_id"))
            return nvm_service.NeverminedPaymentResult(success=True, tx_hash="0x1")

        svc.settle_payment = fake_settle
        cfg = self._config()
        base = dict(config=cfg, nvm_api_key="k", nvm_environment="sandbox",
                    access_token="t", base_url="", execution_id="e")

        await svc.settle_payment_once(agent_request_id="areq-A", **base)
        await svc.settle_payment_once(agent_request_id="areq-B", **base)
        assert len(calls) == 2                  # distinct tokens → both settle

    async def test_missing_agent_request_id_fail_open(self, nvm_service):
        """No native token → fail-open (settle proceeds, no dedup persisted)."""
        svc = nvm_service.NeverminedPaymentService()
        calls = []

        async def fake_settle(**kwargs):
            calls.append(1)
            return nvm_service.NeverminedPaymentResult(success=True, tx_hash="0x2")

        svc.settle_payment = fake_settle
        cfg = self._config()
        kw = dict(config=cfg, nvm_api_key="k", nvm_environment="sandbox",
                  access_token="t", agent_request_id=None, execution_id="e", base_url="")

        await svc.settle_payment_once(**kw)
        await svc.settle_payment_once(**kw)
        assert len(calls) == 2                  # no token → both attempts run


# ---------------------------------------------------------------------------
# Proactive send_message wiring (#1084) — entry guard on resolved recipient+channel
# ---------------------------------------------------------------------------

@pytest.fixture
def proactive_service(effect_service, monkeypatch):
    """Load proactive_message_service standalone with the effect-scoped
    idempotency module + a stubbed audit service injected, avoiding the
    docker-heavy real services/__init__."""
    eff_db = sys.modules["database"].db
    eff_db.can_agent_message_email = lambda agent, email: True

    services_stub = types.ModuleType("services")
    services_stub.idempotency_service = effect_service

    audit_stub = types.ModuleType("services.platform_audit_service")

    class _AuditEventType:
        PROACTIVE_MESSAGE = "proactive_message"

    class _Audit:
        async def log(self, **kw):
            return None

    audit_stub.platform_audit_service = _Audit()
    audit_stub.AuditEventType = _AuditEventType
    services_stub.platform_audit_service = audit_stub

    monkeypatch.setitem(sys.modules, "services", services_stub)
    monkeypatch.setitem(sys.modules, "services.idempotency_service", effect_service)
    monkeypatch.setitem(sys.modules, "services.platform_audit_service", audit_stub)

    path = os.path.join(_backend_path, "services", "proactive_message_service.py")
    spec = importlib.util.spec_from_file_location("_proactive_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestProactiveSendMessageGuard:
    def _make_service(self, mod, delivers):
        svc = mod.ProactiveMessageService()
        svc._check_rate_limit = lambda *a, **k: True
        svc._increment_rate_limit = lambda *a, **k: None

        async def fake_deliver(agent_name, recipient_email, text, channel, reply_to_thread):
            delivers.append((recipient_email, channel, text))
            return mod.DeliveryResult(success=True, channel=channel, message_id="m1")

        svc._deliver_via_channel = fake_deliver
        return svc

    async def test_redelivery_same_execution_one_send(self, proactive_service, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        delivers = []
        svc = self._make_service(proactive_service, delivers)

        r1 = await svc.send_message(
            "agentA", "U@Example.com", "Hello", channel="telegram", execution_id="exec-1",
        )
        # Re-delivery of the same turn — different generated body, same identity.
        r2 = await svc.send_message(
            "agentA", "u@example.com", "Hello AGAIN reworded", channel="telegram", execution_id="exec-1",
        )
        assert r1.success and r2.success
        assert r2.message_id == "m1"          # replayed snapshot
        assert len(delivers) == 1             # exactly one real delivery

    async def test_dedup_label_allows_two_sends(self, proactive_service, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        delivers = []
        svc = self._make_service(proactive_service, delivers)

        await svc.send_message("agentA", "u@e.com", "first", channel="telegram", execution_id="exec-1")
        await svc.send_message(
            "agentA", "u@e.com", "second", channel="telegram",
            execution_id="exec-1", dedup_label="reminder",
        )
        assert len(delivers) == 2

    async def test_no_execution_id_fail_open(self, proactive_service, effect_service):
        delivers = []
        svc = self._make_service(proactive_service, delivers)

        await svc.send_message("agentA", "u@e.com", "x", channel="telegram")
        await svc.send_message("agentA", "u@e.com", "x", channel="telegram")
        assert len(delivers) == 2             # no execution_id → no dedup

    async def test_distinct_recipient_not_deduped(self, proactive_service, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        delivers = []
        svc = self._make_service(proactive_service, delivers)

        await svc.send_message("agentA", "a@e.com", "x", channel="telegram", execution_id="exec-1")
        await svc.send_message("agentA", "b@e.com", "x", channel="telegram", execution_id="exec-1")
        assert len(delivers) == 2             # different recipient → distinct key


# ---------------------------------------------------------------------------
# VoIP call_user wiring (#1084) — entry guard on resolved dial target
# ---------------------------------------------------------------------------

@pytest.fixture
def voip_service_mod(effect_service, monkeypatch):
    """Load voip_service standalone with stubbed config/services + the effect
    idempotency module injected, avoiding the docker-heavy real services/__init__."""
    eff_db = sys.modules["database"].db
    eff_db.get_voip_binding = lambda agent: {"enabled": True, "account_sid": "AC123"}

    config_stub = types.ModuleType("config")
    config_stub.GEMINI_API_KEY = "test-key"
    config_stub.REDIS_URL = "redis://x"
    config_stub.VOIP_CALL_RATE_LIMIT = 5
    config_stub.VOIP_CALL_RATE_WINDOW = 60
    config_stub.VOIP_DEFAULT_DAILY_CALL_CAP = 50
    config_stub.VOIP_ENABLED = True
    config_stub.VOIP_INTENT_TTL_SECONDS = 300
    config_stub.VOIP_TICKET_TTL_SECONDS = 180
    monkeypatch.setitem(sys.modules, "config", config_stub)

    services_stub = types.ModuleType("services")
    services_stub.idempotency_service = effect_service
    rate_limiter_stub = types.ModuleType("services.rate_limiter")
    rate_limiter_stub.enforce = lambda **kw: None
    services_stub.rate_limiter = rate_limiter_stub
    ws_ticket_stub = types.ModuleType("services.ws_ticket_service")
    ws_ticket_stub.mint_ticket = lambda **kw: "ticket"
    services_stub.ws_ticket_service = ws_ticket_stub

    monkeypatch.setitem(sys.modules, "services", services_stub)
    monkeypatch.setitem(sys.modules, "services.idempotency_service", effect_service)
    monkeypatch.setitem(sys.modules, "services.rate_limiter", rate_limiter_stub)
    monkeypatch.setitem(sys.modules, "services.ws_ticket_service", ws_ticket_stub)

    path = os.path.join(_backend_path, "services", "voip_service.py")
    spec = importlib.util.spec_from_file_location("_voip_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestVoipCallGuard:
    def _make_service(self, mod, dials):
        svc = mod.VoipService()

        async def fake_inner(agent_name, dest, binding, initiator_user_id,
                             initiator_email, public_url, context, process_transcript):
            dials.append(dest)
            return {
                "call_id": f"voip_{len(dials)}",
                "status": "ringing",
                "to_number": dest,
                "twilio_call_sid": "CA1",
                "chat_session_id": "cs1",
            }

        svc._place_call_inner = fake_inner
        return svc

    async def test_redelivery_same_execution_one_dial(self, voip_service_mod, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        dials = []
        svc = self._make_service(voip_service_mod, dials)
        kw = dict(agent_name="agentA", to_number="+14155551234", initiator_user_id=1,
                  initiator_email="u@e.com", public_url="https://x", execution_id="exec-1")

        r1 = await svc.place_outbound_call(**kw)
        r2 = await svc.place_outbound_call(**kw)
        assert r1["call_id"] == "voip_1"
        assert r2["call_id"] == "voip_1"      # replayed snapshot, no second dial
        assert len(dials) == 1

    async def test_dedup_label_allows_two_dials(self, voip_service_mod, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        dials = []
        svc = self._make_service(voip_service_mod, dials)
        base = dict(agent_name="agentA", to_number="+14155551234", initiator_user_id=1,
                    initiator_email="u@e.com", public_url="https://x", execution_id="exec-1")

        await svc.place_outbound_call(**base)
        await svc.place_outbound_call(dedup_label="callback", **base)
        assert len(dials) == 2

    async def test_no_execution_id_fail_open(self, voip_service_mod, effect_service):
        dials = []
        svc = self._make_service(voip_service_mod, dials)
        kw = dict(agent_name="agentA", to_number="+14155551234", initiator_user_id=1,
                  initiator_email="u@e.com", public_url="https://x")

        await svc.place_outbound_call(**kw)
        await svc.place_outbound_call(**kw)
        assert len(dials) == 2                 # no execution_id → no dedup

    async def test_distinct_numbers_not_deduped(self, voip_service_mod, effect_service):
        _register_execution(effect_service, "exec-1", "agentA")
        dials = []
        svc = self._make_service(voip_service_mod, dials)
        base = dict(agent_name="agentA", initiator_user_id=1, initiator_email="u@e.com",
                    public_url="https://x", execution_id="exec-1")

        await svc.place_outbound_call(to_number="+14155551234", **base)
        await svc.place_outbound_call(to_number="+14155559999", **base)
        assert len(dials) == 2                 # different dial target → distinct key


# ---------------------------------------------------------------------------
# share_file wiring (#1084) — guard on filename + content version
# ---------------------------------------------------------------------------

@pytest.fixture
def shared_files_mod(effect_service, monkeypatch):
    """Load agent_shared_files_service standalone with stubbed docker/settings
    deps + the effect idempotency module injected."""
    eff_db = sys.modules["database"].db
    eff_db.get_file_sharing_enabled = lambda agent: True

    services_stub = types.ModuleType("services")
    services_stub.idempotency_service = effect_service

    docker_service_stub = types.ModuleType("services.docker_service")
    docker_service_stub.get_agent_container = lambda agent: object()
    docker_utils_stub = types.ModuleType("services.docker_utils")

    async def _fake_archive(container, path):
        return (iter([]), {})

    docker_utils_stub.container_get_archive = _fake_archive
    settings_stub = types.ModuleType("services.settings_service")
    settings_stub.get_public_chat_url = lambda: "https://x"

    services_stub.docker_service = docker_service_stub
    services_stub.docker_utils = docker_utils_stub
    services_stub.settings_service = settings_stub

    monkeypatch.setitem(sys.modules, "services", services_stub)
    monkeypatch.setitem(sys.modules, "services.idempotency_service", effect_service)
    monkeypatch.setitem(sys.modules, "services.docker_service", docker_service_stub)
    monkeypatch.setitem(sys.modules, "services.docker_utils", docker_utils_stub)
    monkeypatch.setitem(sys.modules, "services.settings_service", settings_stub)

    path = os.path.join(_backend_path, "services", "agent_shared_files_service.py")
    spec = importlib.util.spec_from_file_location("_shared_files_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestShareFileGuard:
    def _wire(self, mod, monkeypatch, content_by_call=None):
        finals = []

        async def fake_extract(agent, container_path):
            data = content_by_call.pop(0) if content_by_call else b"file-bytes-v1"
            return (data, os.path.basename(container_path))

        def fake_persist(agent_name, data, *, basename, display_name,
                         expires_in, created_by):
            finals.append(basename)
            tok = f"tok{len(finals)}"
            return {
                "file_id": f"id-{len(finals)}",
                "url": f"https://x/api/files/id-{len(finals)}?sig={tok}",
                "expires_at": "2099-01-01T00:00:00Z",
                "size_bytes": len(data),
                "mime_type": "text/csv",
            }

        monkeypatch.setattr(mod, "extract_from_agent", fake_extract)
        monkeypatch.setattr(mod, "_persist_and_register", fake_persist)
        return finals

    async def test_rerun_same_file_replays_url(self, shared_files_mod, effect_service, monkeypatch):
        _register_execution(effect_service, "exec-1", "agentA")
        finals = self._wire(shared_files_mod, monkeypatch)

        r1 = await shared_files_mod.create_share("agentA", "report.csv", execution_id="exec-1")
        r2 = await shared_files_mod.create_share("agentA", "report.csv", execution_id="exec-1")
        assert r1["url"] == r2["url"]         # same signed URL replayed, not a new token
        assert len(finals) == 1               # token minted once

    async def test_changed_content_new_share(self, shared_files_mod, effect_service, monkeypatch):
        _register_execution(effect_service, "exec-1", "agentA")
        # Same filename, DIFFERENT bytes on the 2nd call → distinct content version.
        finals = self._wire(
            shared_files_mod, monkeypatch, content_by_call=[b"v1-bytes", b"v2-bytes"]
        )

        r1 = await shared_files_mod.create_share("agentA", "report.csv", execution_id="exec-1")
        r2 = await shared_files_mod.create_share("agentA", "report.csv", execution_id="exec-1")
        assert r1["url"] != r2["url"]         # changed content → new share
        assert len(finals) == 2

    async def test_no_execution_id_fail_open(self, shared_files_mod, effect_service, monkeypatch):
        finals = self._wire(shared_files_mod, monkeypatch)
        await shared_files_mod.create_share("agentA", "report.csv")
        await shared_files_mod.create_share("agentA", "report.csv")
        assert len(finals) == 2               # no execution_id → no dedup
