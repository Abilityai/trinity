"""Pull / work-stealing coordination — Phase 1 DARK endpoints (#1081).

Covers the two internal seams (MESSAGE_ENVELOPE_SCHEMA.md §3):
  * GET  /api/internal/next-task         — atomic claim (§3.1) / empty (§3.2)
  * POST /api/internal/tasks/{id}/result — CAS terminal apply (§3.3 → §3.4)

Three layers, no live agent/model turn:
  * DB layer (db/schedules.py) against a real harness DB — atomic claim stamps
    claim_token/lease/worker + flips status; FIFO; no double-claim; the
    claim_token-gated CAS terminal write.
  * Service layer (services/pull_coordination_service) against the real db
    singleton — §3.1 envelope shape + applied/replayed/conflict/not_found.
  * HTTP layer (routers/internal via TestClient) — DUAL-auth gate (internal
    secret OR the agent's own agent-scoped MCP key; 403 otherwise) + response
    mapping.

Phase 2 hardening: the two pull seams accept the calling agent's OWN scoped MCP
key as a least-privilege alternate to the master internal secret (#307/#1159),
so no master secret is ever injected into an agent container.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable (mirror tests/unit/test_backlog.py).
# ---------------------------------------------------------------------------
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402

pytestmark = pytest.mark.unit

# ---------------------------------------------------------------------------
# sys.modules hygiene (Issue #762): this file evicts shadowing/cached backend
# modules (at import time above, and in the db fixtures below) so production
# code re-resolves against the db_harness engine. Snapshot + restore those
# names around every test so the eviction never leaks to other test files.
# The _STUBBED_MODULE_NAMES + _restore_sys_modules pair is the lint-recognised
# precedent (tests/unit/test_telegram_webhook_backfill.py, tests/lint_sys_modules.py).
# ---------------------------------------------------------------------------
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    "db.connection",
    "db.schedules",
    "db.operator_queue",
    "db.agent_settings.resources",
    "database",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the churned backend modules before each test and restore them
    after, so this file's import-time + fixture sys.modules eviction cannot
    pollute unrelated tests in the same session (Issue #762)."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value



def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def tmp_db(db_backend):
    """Fresh full production schema (db_harness). Pops cached db modules so
    production code re-resolves against the harness engine."""
    def _evict():
        for mod in ("db.connection", "db.schedules",
                    "db.agent_settings.resources", "database"):
            sys.modules.pop(mod, None)

    _evict()
    try:
        yield db_backend
    finally:
        _evict()


@pytest.fixture
def schedule_ops(tmp_db):
    from db.schedules import ScheduleOperations

    return ScheduleOperations(user_ops=MagicMock(), agent_ops=MagicMock())


@pytest.fixture
def seed_agent(tmp_db):
    def _seed(name: str, execution_timeout_seconds: int = 900):
        _hrun(
            "INSERT INTO agent_ownership (agent_name, owner_id, "
            " execution_timeout_seconds, created_at) "
            "VALUES (:n, 1, :t, '2026-01-01T00:00:00Z')",
            n=name, t=execution_timeout_seconds,
        )

    return _seed


@pytest.fixture
def enqueue(tmp_db):
    """Insert a QUEUED schedule_executions row directly. Returns its id."""

    def _q(agent_name: str, *, queued_at: datetime | None = None,
           message: str = "do stuff", backlog_metadata: str | None = None,
           execution_id: str | None = None) -> str:
        import secrets as _secrets

        eid = execution_id or _secrets.token_urlsafe(12)
        qa = _iso(queued_at or datetime.now(timezone.utc))
        _hrun(
            "INSERT INTO schedule_executions "
            "(id, schedule_id, agent_name, status, started_at, queued_at, "
            " message, triggered_by, backlog_metadata) "
            "VALUES (:id, '__manual__', :a, 'queued', :qa, :qa, :m, 'manual', :meta)",
            id=eid, a=agent_name, qa=qa, m=message, meta=backlog_metadata,
        )
        return eid

    return _q


# ===========================================================================
# DB layer — atomic claim (Endpoint 1) and CAS terminal write (Endpoint 2)
# ===========================================================================


class TestDbClaim:
    def test_claim_stamps_token_lease_worker_and_flips_status(
        self, schedule_ops, enqueue
    ):
        eid = enqueue("alpha")
        row = schedule_ops.claim_next_queued("alpha", worker_id="alpha#w1", lease_seconds=900)
        assert row is not None
        assert row["id"] == eid
        # Dark pull columns stamped in the SAME atomic UPDATE.
        assert row["claim_token"] and isinstance(row["claim_token"], str)
        assert row["claimed_by_worker"] == "alpha#w1"
        lease = datetime.fromisoformat(row["lease_expires_at"])
        assert lease > datetime.now(timezone.utc)
        # Persisted row reflects the claim.
        persisted = schedule_ops.get_execution(eid)
        assert persisted.status == "running"
        assert schedule_ops.get_queued_count("alpha") == 0

    def test_claim_is_fifo_oldest_first(self, schedule_ops, enqueue):
        now = datetime.now(timezone.utc)
        a = enqueue("alpha", queued_at=now - timedelta(seconds=3))
        b = enqueue("alpha", queued_at=now - timedelta(seconds=2))
        c = enqueue("alpha", queued_at=now - timedelta(seconds=1))
        assert schedule_ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900)["id"] == a
        assert schedule_ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900)["id"] == b
        assert schedule_ops.claim_next_queued("alpha", worker_id="w", lease_seconds=900)["id"] == c

    def test_no_double_claim_and_empty_when_drained(self, schedule_ops, enqueue):
        enqueue("alpha")
        first = schedule_ops.claim_next_queued("alpha", worker_id="w1", lease_seconds=900)
        assert first is not None
        # A second/concurrent worker cannot re-claim the same (now running) row.
        second = schedule_ops.claim_next_queued("alpha", worker_id="w2", lease_seconds=900)
        assert second is None

    def test_legacy_claim_without_worker_leaves_pull_columns_null(
        self, schedule_ops, enqueue
    ):
        """The existing push/backlog-drain caller (no worker_id) is unchanged:
        pull columns stay NULL — proves the path stays dark."""
        eid = enqueue("alpha")
        row = schedule_ops.claim_next_queued("alpha")
        assert row["id"] == eid
        assert row.get("claim_token") is None
        assert row.get("lease_expires_at") is None
        assert row.get("claimed_by_worker") is None


class TestDbCasResult:
    def _claim(self, schedule_ops, enqueue, agent="alpha"):
        enqueue(agent)
        row = schedule_ops.claim_next_queued(agent, worker_id="w1", lease_seconds=900)
        return row["id"], row["claim_token"]

    def test_correct_token_applies_success_terminal(self, schedule_ops, enqueue):
        eid, token = self._claim(schedule_ops, enqueue)
        assert schedule_ops.update_execution_status(
            eid, "success", response="done", claim_token=token
        ) is True
        row = schedule_ops.get_execution(eid)
        assert row.status == "success"
        assert row.response == "done"

    def test_wrong_token_is_rejected_no_write(self, schedule_ops, enqueue):
        eid, token = self._claim(schedule_ops, enqueue)
        assert schedule_ops.update_execution_status(
            eid, "failed", error="boom", claim_token="not-the-token"
        ) is False
        # Row untouched — still running.
        assert schedule_ops.get_execution(eid).status == "running"

    def test_stale_result_does_not_clobber_terminal(self, schedule_ops, enqueue):
        eid, token = self._claim(schedule_ops, enqueue)
        # First terminal wins.
        assert schedule_ops.update_execution_status(
            eid, "failed", error="boom", claim_token=token
        ) is True
        # A late duplicate (even with the right token) cannot overwrite the
        # already-terminal row — the status precondition blocks it.
        assert schedule_ops.update_execution_status(
            eid, "failed", error="AGAIN", claim_token=token
        ) is False
        row = schedule_ops.get_execution(eid)
        assert row.status == "failed"
        assert row.error == "boom"  # not clobbered


# ===========================================================================
# Service layer — §3.1 envelope + §3.4 outcomes (real db singleton)
# ===========================================================================


class TestServiceClaim:
    def test_claim_next_task_builds_envelope(self, seed_agent, enqueue):
        seed_agent("alpha", execution_timeout_seconds=900)
        import json
        eid = enqueue(
            "alpha",
            message="run recon",
            backlog_metadata=json.dumps({
                "kind": "task", "from": "system", "session_id": None,
                "idempotency_key": "sched:exec-7|abc",
            }),
        )
        from services import pull_coordination_service as pcs

        claim = pcs.claim_next_task("alpha", "alpha#w2")
        assert claim is not None
        assert claim["execution_id"] == eid
        assert claim["claimed_by_worker"] == "alpha#w2"
        assert claim["redelivery_count"] == 0
        assert claim["prior_trace"] is None
        # #946 Phase 2: the claim_token the worker echoes on the result POST must
        # be surfaced (§3.3 requires it; §3.1's field table had omitted it).
        assert claim["claim_token"] and isinstance(claim["claim_token"], str)
        env = claim["envelope"]
        assert env["kind"] == "task"
        assert env["from"] == "system"
        assert env["to"] == "alpha"
        assert env["idempotency_key"] == "sched:exec-7|abc"
        assert env["payload"]["message"] == "run recon"
        assert env["deadline"] == claim["lease_expires_at"]

    def test_claim_empty_returns_none(self, seed_agent):
        seed_agent("alpha")
        from services import pull_coordination_service as pcs

        assert pcs.claim_next_task("alpha", "w1") is None


class TestServiceResult:
    def _claim(self, agent="alpha"):
        from database import db
        row = db.claim_next_queued(agent, worker_id="w1", lease_seconds=900)
        return row["id"], row["claim_token"]

    def test_applied_writes_terminal(self, seed_agent, enqueue):
        seed_agent("alpha")
        enqueue("alpha")
        eid, token = self._claim()
        from services import pull_coordination_service as pcs
        from database import db

        outcome = pcs.apply_task_result(
            eid, token, status="success", content="all done",
            cost=0.012, tokens=8421, session_id="sess-1",
            metadata={"context_window": 200000},
        )
        assert outcome.kind == "applied"
        row = db.get_execution(eid)
        assert row.status == "success"
        assert row.response == "all done"
        assert row.cost == pytest.approx(0.012)

    def test_duplicate_is_replayed_not_double_applied(self, seed_agent, enqueue):
        seed_agent("alpha")
        enqueue("alpha")
        eid, token = self._claim()
        from services import pull_coordination_service as pcs
        from database import db

        first = pcs.apply_task_result(eid, token, status="success", content="first")
        assert first.kind == "applied"
        # A late/duplicate result must NOT overwrite the authoritative terminal.
        second = pcs.apply_task_result(eid, token, status="success", content="SECOND")
        assert second.kind == "replayed"
        assert db.get_execution(eid).response == "first"  # unchanged

    def test_wrong_token_is_conflict(self, seed_agent, enqueue):
        seed_agent("alpha")
        enqueue("alpha")
        eid, _token = self._claim()
        from services import pull_coordination_service as pcs
        from database import db

        outcome = pcs.apply_task_result(eid, "wrong-token", status="success", content="x")
        assert outcome.kind == "conflict"
        assert db.get_execution(eid).status == "running"  # untouched

    def test_unknown_execution_is_not_found(self, seed_agent):
        seed_agent("alpha")
        from services import pull_coordination_service as pcs

        assert pcs.apply_task_result("nope", "tok", status="success").kind == "not_found"

    def test_failed_folds_error_code_into_error_text(self, seed_agent, enqueue):
        seed_agent("alpha")
        enqueue("alpha")
        eid, token = self._claim()
        from services import pull_coordination_service as pcs
        from database import db

        outcome = pcs.apply_task_result(
            eid, token, status="failed", content="no subscription", error_code="auth",
        )
        assert outcome.kind == "applied"
        row = db.get_execution(eid)
        assert row.status == "failed"
        assert "auth" in (row.error or "")


# ===========================================================================
# HTTP layer — X-Internal-Secret gate + response mapping (TestClient)
# ===========================================================================


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("INTERNAL_API_SECRET", "unit-secret")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.internal import router, pull_router

    app = FastAPI()
    app.include_router(router)
    app.include_router(pull_router)  # Phase-2 dual-auth pull seams
    return TestClient(app, raise_server_exceptions=True)


_HDR = {"X-Internal-Secret": "unit-secret"}


def _key(scope: str, agent_name):
    """A validate_mcp_api_key() result shape (mcp_keys.validate_mcp_api_key)."""
    return {"scope": scope, "agent_name": agent_name, "key_id": "k1", "user_id": "u"}


class TestHttpAuth:
    """Dual-auth gate on the two Phase-2 pull seams: a valid X-Internal-Secret
    (trusted backend) OR the agent's OWN agent-scoped MCP key. Neither → 403."""

    # --- neither credential -> 403 ---------------------------------------
    def test_next_task_without_credentials_403(self, client):
        r = client.get("/api/internal/next-task", params={"agent_name": "a", "worker_id": "w"})
        assert r.status_code == 403

    def test_next_task_wrong_secret_403(self, client):
        r = client.get(
            "/api/internal/next-task",
            params={"agent_name": "a", "worker_id": "w"},
            headers={"X-Internal-Secret": "nope"},
        )
        assert r.status_code == 403

    def test_result_without_credentials_403(self, client):
        r = client.post(
            "/api/internal/tasks/exec-1/result",
            json={"claim_token": "t", "status": "success"},
        )
        assert r.status_code == 403

    # --- Proof 1: agent-scoped key whose agent_name matches + PILOTED -> allowed
    def test_next_task_agent_key_matching_agent_name_succeeds(self, client, monkeypatch):
        # #1081 B1: the agent-key claim path is now allowlist-gated, so the agent
        # must be a pilot for its own scoped key to claim.
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        fake = {"envelope": {"id": "m1"}, "execution_id": "exec-1", "claim_token": "t"}
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "alpha")), \
             patch("services.pull_coordination_service.claim_next_task", return_value=fake):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 200
        assert r.json()["execution_id"] == "exec-1"

    # --- Proof 1b (#1081 B1): a valid own scoped key but the agent is NOT a
    # pilot (de-piloted / never piloted) -> 403 on the CLAIM seam. The allowlist
    # is a consumer backstop so a de-piloted-but-still-running worker stops
    # claiming new work on backend restart, not only after a container recreate.
    def test_next_task_agent_key_not_piloted_403(self, client, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "beta")  # alpha absent
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "alpha")), \
             patch("services.pull_coordination_service.claim_next_task",
                   return_value={"envelope": {}, "execution_id": "x", "claim_token": "t"}) as claim:
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 403
        claim.assert_not_called()  # refused before any claim work

    # --- Proof 1c (#1081 B1): the trusted-backend (internal-secret) claim path
    # is UNCHANGED by the allowlist gate — it does not require a pilot.
    def test_next_task_internal_secret_not_allowlist_gated(self, client, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")  # no pilots
        fake = {"envelope": {"id": "m1"}, "execution_id": "exec-9", "claim_token": "t"}
        with patch("services.pull_coordination_service.claim_next_task", return_value=fake):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers=_HDR,
            )
        assert r.status_code == 200
        assert r.json()["execution_id"] == "exec-9"

    def test_result_agent_key_owning_execution_succeeds(self, client):
        from services.pull_coordination_service import ResultApplyOutcome

        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "alpha")), \
             patch("routers.internal.db.get_execution",
                   return_value=SimpleNamespace(agent_name="alpha", status="running")), \
             patch("services.pull_coordination_service.apply_task_result",
                   return_value=ResultApplyOutcome("applied", "success")):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success", "content": "ok"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        assert r.status_code == 200 and r.json()["applied"] is True

    # --- Proof 2: agent-scoped key for a DIFFERENT agent -> 403 -----------
    def test_next_task_mismatched_agent_name_403(self, client):
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "beta")):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_beta"},
            )
        assert r.status_code == 403

    def test_result_key_for_other_agents_execution_403(self, client):
        # beta's key trying to report on alpha's execution.
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "beta")), \
             patch("routers.internal.db.get_execution",
                   return_value=SimpleNamespace(agent_name="alpha", status="running")):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success"},
                headers={"Authorization": "Bearer trinity_mcp_beta"},
            )
        assert r.status_code == 403

    # --- Proof 3: non-agent-scoped key (user / system) -> 403 ------------
    def test_next_task_user_scoped_key_403(self, client):
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("user", None)):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "alpha#w1"},
                headers={"Authorization": "Bearer trinity_mcp_user"},
            )
        assert r.status_code == 403

    def test_result_system_scoped_key_403(self, client):
        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("system", None)):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success"},
                headers={"Authorization": "Bearer trinity_mcp_system"},
            )
        assert r.status_code == 403

    # --- Proof 4: X-Internal-Secret still works (backward compat) ---------
    def test_next_task_internal_secret_still_accepted(self, client):
        with patch("services.pull_coordination_service.claim_next_task", return_value=None):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "alpha", "worker_id": "w"}, headers=_HDR,
            )
        assert r.status_code == 200 and r.json() == {"envelope": None}

    def test_result_internal_secret_still_accepted(self, client):
        from services.pull_coordination_service import ResultApplyOutcome

        with patch("services.pull_coordination_service.apply_task_result",
                   return_value=ResultApplyOutcome("applied", "success")):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success"}, headers=_HDR,
            )
        assert r.status_code == 200 and r.json()["applied"] is True

    # --- Proof 5: ownership passes but the claim_token CAS still gates ----
    def test_result_agent_key_wrong_token_still_conflict_409(self, client):
        from services.pull_coordination_service import ResultApplyOutcome

        with patch("routers.internal.db.validate_mcp_api_key", return_value=_key("agent", "alpha")), \
             patch("routers.internal.db.get_execution",
                   return_value=SimpleNamespace(agent_name="alpha", status="running")), \
             patch("services.pull_coordination_service.apply_task_result",
                   return_value=ResultApplyOutcome("conflict", "running")):
            r = client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "WRONG", "status": "success"},
                headers={"Authorization": "Bearer trinity_mcp_alpha"},
            )
        # ownership passed (not 403) but the stale/wrong claim_token → 409.
        assert r.status_code == 409


class TestHttpNextTask:
    def test_claim_returns_envelope(self, client):
        fake = {
            "envelope": {"id": "m1", "kind": "task", "to": "a"},
            "execution_id": "exec-1", "lease_expires_at": "2026-07-02T14:08:00Z",
            "claimed_by_worker": "a#w1", "redelivery_count": 0, "prior_trace": None,
        }
        with patch("services.pull_coordination_service.claim_next_task", return_value=fake):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "a", "worker_id": "a#w1"}, headers=_HDR,
            )
        assert r.status_code == 200
        assert r.json()["execution_id"] == "exec-1"

    def test_empty_queue_returns_null_envelope(self, client):
        with patch("services.pull_coordination_service.claim_next_task", return_value=None):
            r = client.get(
                "/api/internal/next-task",
                params={"agent_name": "a", "worker_id": "w"}, headers=_HDR,
            )
        assert r.status_code == 200
        assert r.json() == {"envelope": None}


class TestHttpResult:
    def _post(self, client, outcome_kind, status="success"):
        from services.pull_coordination_service import ResultApplyOutcome

        with patch(
            "services.pull_coordination_service.apply_task_result",
            return_value=ResultApplyOutcome(outcome_kind, status),
        ):
            return client.post(
                "/api/internal/tasks/exec-1/result",
                json={"claim_token": "tok", "status": "success", "content": "ok"},
                headers=_HDR,
            )

    def test_applied_200(self, client):
        r = self._post(client, "applied")
        assert r.status_code == 200 and r.json()["applied"] is True

    def test_replayed_200(self, client):
        r = self._post(client, "replayed")
        assert r.status_code == 200 and r.json()["replayed"] is True

    def test_conflict_409(self, client):
        r = self._post(client, "conflict")
        assert r.status_code == 409

    def test_not_found_404(self, client):
        r = self._post(client, "not_found")
        assert r.status_code == 404

    def test_bad_status_is_422(self, client):
        # Taxonomy validation at the boundary (MESSAGE_ENVELOPE_SCHEMA §4).
        r = client.post(
            "/api/internal/tasks/exec-1/result",
            json={"claim_token": "tok", "status": "bogus"},
            headers=_HDR,
        )
        assert r.status_code == 422

    def test_bad_error_code_is_422(self, client):
        r = client.post(
            "/api/internal/tasks/exec-1/result",
            json={"claim_token": "tok", "status": "failed", "error_code": "kaboom"},
            headers=_HDR,
        )
        assert r.status_code == 422


# ===========================================================================
# Injection — the master internal secret NEVER enters a pilot agent's env
# (Proof 6). services/agent_service/pull_mode.py builds the pilot env vars.
# ===========================================================================


class TestPullModeInjection:
    def test_pilot_env_has_pull_knobs_but_not_the_master_secret(self, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha,beta")
        with patch(
            "services.settings_service.get_effective_max_parallel_tasks",
            return_value=5,
        ):
            from services.agent_service.pull_mode import pull_mode_env_vars

            env = pull_mode_env_vars("alpha")
        assert env["TRINITY_PULL_MODE"] == "true"
        assert env["TRINITY_MAX_PARALLEL_TASKS"] == "5"
        # The security win: the master internal secret is NEVER injected into an
        # agent container via the pull path (least-privilege, #307/#1159).
        assert "TRINITY_INTERNAL_SECRET" not in env

    def test_non_pilot_agent_gets_empty_env(self, monkeypatch):
        monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "alpha")
        from services.agent_service.pull_mode import pull_mode_env_vars

        assert pull_mode_env_vars("gamma") == {}

    def test_pull_mode_no_longer_exposes_internal_secret_helper(self):
        """The dead ``_internal_secret()`` helper (which read the master secret)
        is removed — the module can no longer surface it."""
        import services.agent_service.pull_mode as pm

        assert not hasattr(pm, "_internal_secret")


# ===========================================================================
# C1 regression — concurrent claim MUST be exactly-once (Postgres, multi-thread)
# ===========================================================================


class TestClaimConcurrencyC1:
    """C1 (#1081): ``claim_next_queued`` hands each queued row to EXACTLY ONE
    worker under real concurrency.

    **Postgres-only, multi-thread.** SQLite serialises writers and lacks
    ``FOR UPDATE SKIP LOCKED``, so the race cannot be constructed there — the
    test skips on SQLite (and whenever ``TEST_POSTGRES_URL`` is unset). This is
    the committed replacement for the scratch multi-process harness that first
    surfaced C1 (``docs/testing/PULL_MIGRATION_TESTING.md`` §5); the previous
    suite proved only sequential no-double-claim, which passes with OR without
    the fix (the inner subquery's ``status='queued'`` filter already no-ops a
    *sequential* second claim).

    Regression guard: with BOTH C1 guards removed — the ``FOR UPDATE SKIP
    LOCKED`` on the claim subquery AND the outer ``status='queued'`` re-check —
    the uncorrelated scalar subquery compiles to an InitPlan evaluated once, so
    under READ COMMITTED every one of the N concurrent updaters resolves the SAME
    head id and (its EvalPlanQual re-check passing on the bare outer ``id=X``)
    re-applies to it. All N workers then claim the same head row (double-RUN),
    the bijection assertion below sees one id N times, and the test FAILS. See
    ``docs/memory/learnings.md`` (2026-07-09).
    """

    def test_concurrent_claim_is_exactly_once(self, schedule_ops, enqueue, db_backend):
        if db_backend != "postgres":
            pytest.skip(
                "C1 race is Postgres-only (SQLite serialises writers; no FOR "
                "UPDATE SKIP LOCKED). Set TEST_POSTGRES_URL to run."
            )

        import threading

        N = 8            # concurrent workers == queued rows ⇒ a correct claim is a bijection
        ITERATIONS = 5   # repeat so a lucky single round can't green a regression

        for it in range(ITERATIONS):
            # Fresh queue of exactly N rows; the PG pool (size 10 + overflow 20)
            # serves all N claims concurrently, so they genuinely contend.
            _hrun("DELETE FROM schedule_executions WHERE agent_name = :a", a="alpha")
            seeded = {enqueue("alpha", message=f"it{it}-r{r}") for r in range(N)}
            assert len(seeded) == N
            assert schedule_ops.get_queued_count("alpha") == N

            barrier = threading.Barrier(N)
            results: list = [None] * N
            errors: list = [None] * N

            def _worker(i: int) -> None:
                try:
                    barrier.wait(timeout=30)  # release all N claims together
                    row = schedule_ops.claim_next_queued(
                        "alpha", worker_id=f"alpha#w{i}", lease_seconds=900
                    )
                    results[i] = row["id"] if row else None
                except Exception as e:  # noqa: BLE001 — re-asserted on the main thread
                    errors[i] = e

            threads = [threading.Thread(target=_worker, args=(i,)) for i in range(N)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert not any(errors), f"iteration {it}: claim raised {errors}"
            claimed = [r for r in results if r is not None]

            # Core C1 assertion: no queued row was handed to two workers.
            assert len(claimed) == len(set(claimed)), (
                f"iteration {it}: a queued row was DOUBLE-CLAIMED — {len(claimed)} "
                f"claims, {len(set(claimed))} distinct (results={results})"
            )
            # N workers, N rows ⇒ a correct claim is a perfect N-way partition.
            assert set(claimed) == seeded, (
                f"iteration {it}: expected an exact partition of the queue; got "
                f"{sorted(claimed)} vs seeded {sorted(seeded)}"
            )
            assert schedule_ops.get_queued_count("alpha") == 0
