"""Integration tests for the Redis-backed circuit breaker (#631).

State and transitions live in Redis (atomic Lua scripts), so the only
faithful tests run against a real Redis. Connect to the live `trinity-redis`
container via the `backend` ACL credentials in .env. Each test uses a
unique agent name and cleans its keys on the way out so the suite can
re-run against a stack that's seeing real traffic.

Covered:
- closed → open → dormant state machine with correct backoff growth
- one-worker-probes-at-a-time semantics via the SET-NX-EX probe lock
- record_success full reset
- single transition log per cluster (atomic Lua), not per worker
- get_all_circuit_states scans only state hashes (skips probe-locks)
- force_circuit_dormant / reset_circuit operator hooks
- fail-open when Redis is unreachable
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import redis as _redis

# Add backend to sys.path BEFORE importing — agent_client imports `config` which
# fails fast if REDIS_URL lacks credentials. We override REDIS_URL with the
# backend ACL credentials sourced from .env so the backend's config.py accepts.
_REPO = Path(__file__).resolve().parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _load_env_password() -> str:
    """Pull REDIS_BACKEND_PASSWORD out of the repo .env."""
    env_path = _REPO / ".env"
    if not env_path.exists():
        pytest.skip(".env missing — cannot derive Redis credentials", allow_module_level=True)
    for line in env_path.read_text().splitlines():
        if line.startswith("REDIS_BACKEND_PASSWORD="):
            return line.split("=", 1)[1].strip()
    pytest.skip("REDIS_BACKEND_PASSWORD not found in .env", allow_module_level=True)


# Point config.py at the local stack BEFORE importing agent_client.
# Honor a pre-set REDIS_URL (sibling-stack workflows / CI on alternate
# ports). Default: derive from .env + localhost:6379 for the standard
# `./scripts/deploy/start.sh` dev stack.
if "REDIS_URL" not in os.environ:
    _PASSWORD = _load_env_password()
    os.environ["REDIS_URL"] = f"redis://backend:{_PASSWORD}@localhost:6379"
# REDIS_PASSWORD / REDIS_BACKEND_PASSWORD aren't read by config.py (which
# only consumes REDIS_URL), but a few test paths still reach for them.
# Setdefault keeps the contract backward-compatible without overwriting
# values supplied by the caller's environment.
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")


# Import via importlib to avoid pulling in the full services/__init__.py
# (which drags in Docker, models, FastAPI, etc.).
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "agent_client_under_test",
    str(_BACKEND / "services" / "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_client)


pytestmark = pytest.mark.integration


# ── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def redis_client():
    """Direct Redis client used to inspect state and clean up after tests."""
    client = _redis.from_url(
        os.environ["REDIS_URL"],
        decode_responses=True,
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        client.ping()
    except Exception as e:
        pytest.skip(f"Redis unavailable: {e}")
    yield client
    client.close()


@pytest.fixture
def agent_name(redis_client):
    """Unique per-test agent name; auto-cleans both the state hash and
    probe-lock from Redis after the test."""
    name = f"cb-test-{uuid.uuid4().hex[:10]}"
    yield name
    redis_client.delete(
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}",
        f"{agent_client._CIRCUIT_HASH_PREFIX}{name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}",
    )


@pytest.fixture(autouse=True)
def _ensure_redis_client_cached():
    """Force agent_client to re-resolve its Redis client between tests
    (each test may have skewed env / monkeypatched reset)."""
    agent_client._reset_circuit_redis_client()
    yield
    agent_client._reset_circuit_redis_client()


@pytest.fixture(autouse=True)
def cleanup_after_test():
    """Override the parent conftest's `cleanup_after_test` autouse fixture.

    The parent fixture pulls in `api_client`, which authenticates against
    `http://localhost:8000/token` — a dependency these tests don't actually
    need (they exercise the Redis-backed circuit primitives in-process).
    Without this override, running `pytest tests/integration/test_circuit_breaker.py`
    fails with 401 when the dev backend isn't reachable on 8000, even
    though the tests would otherwise pass cleanly against just Redis.
    """
    yield


# ── State machine ────────────────────────────────────────────────────────────

class TestStateMachine:

    def test_closed_state_allows(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        assert cs.allow_request() is True
        assert cs.state == "closed"
        assert cs.failure_count == 0

    def test_below_threshold_stays_closed(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD - 1):
            assert cs.record_failure() == "closed"
        assert cs.state == "closed"
        assert cs.failure_count == agent_client.CIRCUIT_FAILURE_THRESHOLD - 1

    def test_threshold_reached_opens(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        last_state = None
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            last_state = cs.record_failure()
        assert last_state == "open"
        assert cs.state == "open"

    def test_open_in_cooldown_denies(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        # Immediately after open, we're well before next_probe_at.
        assert cs.allow_request() is False

    def test_record_success_resets_to_closed(self, agent_name):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        assert cs.state == "open"
        cs.record_success()
        assert cs.state == "closed"
        assert cs.failure_count == 0
        assert cs.allow_request() is True


# ── Backoff curve ────────────────────────────────────────────────────────────

class TestBackoffSchedule:

    def _read_next_probe(self, redis_client, agent_name) -> float:
        return float(
            redis_client.hget(
                f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}", "next_probe_at"
            )
            or "0"
        )

    def test_backoff_grows_then_caps(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)

        # Push past the failure threshold.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()

        # First open transition: cooldown ≈ base * 2^0 = base.
        cooldown_first = self._read_next_probe(redis_client, agent_name) - time.time()
        assert (
            agent_client.CIRCUIT_BASE_COOLDOWN_SECONDS - 2
            <= cooldown_first
            <= agent_client.CIRCUIT_BASE_COOLDOWN_SECONDS + 2
        ), f"first open cooldown {cooldown_first} not near base"

        # Subsequent open-state failures should grow the cooldown until cap.
        # After enough failures we should plateau at max.
        prev_cd = cooldown_first
        capped = False
        for _ in range(8):
            cs.record_failure()
            cd = self._read_next_probe(redis_client, agent_name) - time.time()
            if cd >= agent_client.CIRCUIT_MAX_COOLDOWN_SECONDS - 5:
                capped = True
                break
            assert cd >= prev_cd - 1, (
                f"cooldown shrank: prev={prev_cd} cur={cd}"
            )
            prev_cd = cd
        assert capped, "cooldown never reached the cap within 8 extra failures"


# ── Dormant state ────────────────────────────────────────────────────────────

class TestDormantState:

    def test_enters_dormant_after_threshold_open_probes(self, agent_name, monkeypatch):
        # Tighten the dormant threshold so we don't slow the test down.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_AFTER_OPEN_PROBES", 4)

        cs = agent_client.CircuitState(agent_name)
        last = None
        # First call: failures 1..threshold-1 stay closed; threshold opens; then
        # each additional failure increments probe_count_since_open.
        # Total iterations = failure_threshold + dormant_threshold to cross the line.
        for _ in range(
            agent_client.CIRCUIT_FAILURE_THRESHOLD
            + agent_client.CIRCUIT_DORMANT_AFTER_OPEN_PROBES
            + 2
        ):
            last = cs.record_failure()
            if last == "dormant":
                break
        assert last == "dormant", f"never transitioned to dormant; last={last}"
        assert cs.state == "dormant"

    def test_dormant_denies_within_cooldown(self, agent_name):
        """While inside the dormant cooldown window, all requests are denied.

        #921: dormant no longer means "never probe again" — it means probe
        on a long cooldown. But within that window, no requests slip through.
        """
        agent_client.force_circuit_dormant(agent_name, reason="test")
        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "dormant"
        assert cs.allow_request() is False
        for _ in range(5):
            assert cs.allow_request() is False

    def test_dormant_transition_emits_operator_queue_alert(self, agent_name, monkeypatch):
        """#921: closed/open → dormant transition fires a
        circuit_breaker_dormant entry in the Operating Room queue so
        operators see the silently-failing agent without grepping logs.

        Stubs the lazy `database` import inside `_emit_dormant_alert` with
        an in-memory fake so we can drive the transition against the real
        Redis CB without needing the live backend SQLite.
        """
        import sys
        import types
        from unittest.mock import MagicMock

        # Faster transition into dormant.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_AFTER_OPEN_PROBES", 4)
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.01)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.01)

        fake_db = MagicMock()
        fake_module = types.ModuleType("database")
        fake_module.db = fake_db
        monkeypatch.setitem(sys.modules, "database", fake_module)
        # utils.helpers is normally available; we don't need to stub it,
        # but if running on a stripped sys.path we provide a shim.
        if "utils.helpers" not in sys.modules:
            shim = types.ModuleType("utils.helpers")
            shim.utc_now_iso = lambda: "2026-05-23T00:00:00Z"
            monkeypatch.setitem(sys.modules, "utils.helpers", shim)

        cs = agent_client.CircuitState(agent_name)
        last = None
        for _ in range(
            agent_client.CIRCUIT_FAILURE_THRESHOLD
            + agent_client.CIRCUIT_DORMANT_AFTER_OPEN_PROBES
            + 2
        ):
            last = cs.record_failure()
            if last == "dormant":
                break
        assert last == "dormant"

        # Alert fired exactly once on the transition.
        assert fake_db.create_operator_queue_item.call_count == 1
        called_agent, item = fake_db.create_operator_queue_item.call_args.args
        assert called_agent == agent_name
        # Type is the generic 'alert' so the existing Operating Room UI
        # renders an Acknowledge control. The narrower CB-specific marker
        # is in context.alert_type for callers that need to filter.
        assert item["type"] == "alert"
        assert item["context"]["alert_type"] == "circuit_breaker_dormant"
        assert item["priority"] == "high"
        assert item["status"] == "pending"
        assert item["agent_name"] == agent_name
        assert "DORMANT" in item["title"]
        assert item["context"]["transition"] == "dormant"
        assert (
            item["context"]["dormant_cooldown_seconds"]
            == agent_client.CIRCUIT_DORMANT_COOLDOWN_SECONDS
        )

        # Subsequent failures stay dormant (prior==new) — no transition,
        # no second alert. Verifies the once-per-entry guarantee.
        for _ in range(3):
            cs.record_failure()
        assert fake_db.create_operator_queue_item.call_count == 1

    def test_dormant_probes_after_cooldown(self, agent_name, monkeypatch):
        """#921: once the dormant cooldown elapses, exactly one probe is
        admitted per worker race. Restores baseline recovery behaviour
        without requiring manual intervention."""
        # Tiny cooldown so the test elapses it without sleeping for an hour.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_COOLDOWN_SECONDS", 0.05)
        # Drive the breaker into dormant via failures (not the force-helper)
        # so next_probe_at is set by the failure Lua path with the new
        # CIRCUIT_DORMANT_COOLDOWN_SECONDS.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_AFTER_OPEN_PROBES", 4)
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.01)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.01)

        cs = agent_client.CircuitState(agent_name)
        last = None
        for _ in range(
            agent_client.CIRCUIT_FAILURE_THRESHOLD
            + agent_client.CIRCUIT_DORMANT_AFTER_OPEN_PROBES
            + 2
        ):
            last = cs.record_failure()
            if last == "dormant":
                break
        assert last == "dormant"
        # Immediately after entering dormant, the cooldown has not elapsed.
        assert cs.allow_request() is False
        time.sleep(0.1)
        # Cooldown elapsed — exactly one probe goes through.
        assert cs.allow_request() is True
        # The probe-lock is held, so a second request right after is denied.
        assert cs.allow_request() is False

    def test_dormant_probe_failure_rearms_full_dormant_cooldown(
        self, agent_name, redis_client, monkeypatch
    ):
        """#921: when a dormant probe fails, next_probe_at must be rearmed to
        the full DORMANT_COOLDOWN — NOT the open-state exponential backoff.

        Locks in the cadence the fix promises: one probe per ~1h while
        dormant, regardless of how many times the agent stays unreachable.
        Without this guarantee a dormant CB could churn through fast
        retries via the open-state backoff curve, defeating the purpose."""
        # Wide gap between the two cooldown families so the assertion can
        # distinguish them: dormant=0.5s, open exp cap=0.001s.
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_COOLDOWN_SECONDS", 0.5)
        monkeypatch.setattr(agent_client, "CIRCUIT_DORMANT_AFTER_OPEN_PROBES", 4)
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.001)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.001)

        cs = agent_client.CircuitState(agent_name)
        # Drive into dormant.
        for _ in range(
            agent_client.CIRCUIT_FAILURE_THRESHOLD
            + agent_client.CIRCUIT_DORMANT_AFTER_OPEN_PROBES
            + 2
        ):
            if cs.record_failure() == "dormant":
                break
        assert cs.state == "dormant"

        # Wait past the cooldown, take the probe, then simulate it failing.
        time.sleep(0.6)
        assert cs.allow_request() is True  # probe admitted
        cs.record_failure()                # probe failed → record_failure on dormant

        # next_probe_at should be ~0.5s in the future (DORMANT_COOLDOWN),
        # not ~0.001s (open-state max). Use the redis_client fixture to
        # read the raw value; the hash field is a unix timestamp.
        key = f"agent:circuit:{agent_name}"
        next_probe_at = float(redis_client.hget(key, "next_probe_at"))
        gap = next_probe_at - time.time()
        assert agent_client.CIRCUIT_DORMANT_COOLDOWN_SECONDS - 0.1 <= gap <= agent_client.CIRCUIT_DORMANT_COOLDOWN_SECONDS + 0.1, (
            f"expected gap ~{agent_client.CIRCUIT_DORMANT_COOLDOWN_SECONDS}s "
            f"(dormant cooldown), got {gap:.3f}s — open-state backoff would "
            f"have yielded ~{agent_client.CIRCUIT_MAX_COOLDOWN_SECONDS}s"
        )
        # Still dormant, no state slide back to open.
        assert cs.state == "dormant"


# ── Probe-lock cross-worker semantics ────────────────────────────────────────

class TestProbeLock:
    """Two CircuitState instances sharing one Redis simulate two uvicorn workers.

    With the cooldown elapsed, only one of them can claim the probe-lock and
    issue the half-open request.
    """

    def test_only_one_worker_probes(self, agent_name, redis_client, monkeypatch):
        # Tiny cooldown so we can elapse it without sleeping for 30 s.
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.05)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.05)

        worker_a = agent_client.CircuitState(agent_name)
        worker_b = agent_client.CircuitState(agent_name)

        # Drive to open via worker_a.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            worker_a.record_failure()
        assert worker_a.state == "open"
        # Worker B observes the same state — Redis is the single source of truth.
        assert worker_b.state == "open"

        # Wait past the cooldown so allow_request is eligible to probe.
        time.sleep(0.1)

        # Race: both workers ask permission. Exactly one should be admitted
        # (probe lock acquired); the other denied.
        verdicts = [worker_a.allow_request(), worker_b.allow_request()]
        assert verdicts.count(True) == 1, (
            f"expected exactly one worker to win probe-lock, got {verdicts}"
        )
        assert verdicts.count(False) == 1


# ── Logging on cluster transitions ───────────────────────────────────────────

class TestTransitionLogging:

    def test_open_transition_logs_once(self, agent_name, caplog):
        # Simulate two workers calling record_failure concurrently. The atomic
        # Lua means only one observes the closed→open transition.
        cs_a = agent_client.CircuitState(agent_name)
        cs_b = agent_client.CircuitState(agent_name)

        with caplog.at_level(logging.WARNING, logger=agent_client.logger.name):
            # First two failures from A keep us closed (assuming threshold=3).
            for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD - 1):
                cs_a.record_failure()
            # The transition fires when failures hit the threshold. Whichever
            # worker tips it logs; the other (if it tips later) sees prior=open.
            cs_a.record_failure()
            cs_b.record_failure()

        opened_logs = [
            r for r in caplog.records
            if "Circuit OPENED" in r.getMessage() and agent_name in r.getMessage()
        ]
        assert len(opened_logs) == 1, (
            f"expected 1 OPENED log, got {len(opened_logs)}: {[r.getMessage() for r in opened_logs]}"
        )

    def test_recovery_logs_closed(self, agent_name, caplog):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()

        with caplog.at_level(logging.INFO, logger=agent_client.logger.name):
            cs.record_success()

        closed_logs = [
            r for r in caplog.records
            if "Circuit CLOSED" in r.getMessage() and agent_name in r.getMessage()
        ]
        assert len(closed_logs) == 1


# ── Operator hooks ───────────────────────────────────────────────────────────

class TestOperatorHooks:

    def test_force_dormant_idempotent(self, agent_name, redis_client):
        agent_client.force_circuit_dormant(agent_name, reason="test")
        agent_client.force_circuit_dormant(agent_name, reason="test")
        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "dormant"

    def test_reset_clears_redis_state(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        assert redis_client.exists(f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}")

        agent_client.reset_circuit(agent_name)
        assert not redis_client.exists(f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}")
        # Fresh facade reads as closed.
        assert agent_client.CircuitState(agent_name).state == "closed"


# ── get_all_circuit_states ───────────────────────────────────────────────────

class TestGetAllStates:

    def test_scan_returns_only_state_hashes(self, agent_name, redis_client):
        cs = agent_client.CircuitState(agent_name)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            cs.record_failure()
        # Manually plant a probe-lock so the scan would pick it up if the
        # filter was wrong.
        redis_client.set(
            f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}",
            "1",
            ex=10,
        )
        try:
            states = agent_client.get_all_circuit_states()
            assert agent_name in states, "state hash missing from scan"
            # No `probe-lock`-suffixed entry should appear as an agent name.
            assert not any(
                name.endswith(agent_client._CIRCUIT_PROBE_LOCK_SUFFIX)
                for name in states.keys()
            )
            entry = states[agent_name]
            assert entry["state"] == "open"
            assert entry["failure_count"] >= agent_client.CIRCUIT_FAILURE_THRESHOLD
        finally:
            redis_client.delete(
                f"{agent_client._CIRCUIT_HASH_PREFIX}{agent_name}{agent_client._CIRCUIT_PROBE_LOCK_SUFFIX}"
            )


# ── Fail-open when Redis is unreachable ──────────────────────────────────────

class TestFailOpen:

    def test_unreachable_redis_returns_allow(self, agent_name, monkeypatch):
        """If Redis is down we fall through to allowing requests — graceful
        degradation, since the underlying HTTP failure will surface anyway."""

        def _fail(*_a, **_kw):
            raise _redis.exceptions.ConnectionError("simulated outage")

        # Force every redis op to error.
        monkeypatch.setattr(agent_client, "_get_circuit_redis", lambda: None)

        cs = agent_client.CircuitState(agent_name)
        assert cs.allow_request() is True
        assert cs.record_failure() == "closed"
        assert cs.state == "closed"
        # record_success is a no-op in fail-open mode but must not raise.
        cs.record_success()


# ── Failure classification (#474) ────────────────────────────────────────────

class TestFailureClassification:
    """#474 — only TCP-level unreachability (ConnectError / ConnectTimeout)
    counts toward the circuit threshold. Read timeouts, broken pipes,
    pool exhaustion, and any HTTP response (incl. 5xx) must NOT trip it.

    Each test injects a MockTransport-wrapped AsyncClient into
    agent_client._client_pool for the test's synthetic agent, so
    AgentClient._request() drives the handler we specify. Cleans up the
    pool entry on teardown to avoid cross-test pollution.
    """

    def _drive(self, agent_name: str, handler, *, timeout: float = 1.0):
        """Drive AgentClient._request() through a MockTransport handler.

        Returns whatever _request returns (or raises). Closes and pops the
        mock client from _client_pool on the way out.
        """
        base_url = f"http://agent-{agent_name}:8000"

        async def runner():
            mock_client = httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                base_url=base_url,
            )
            agent_client._client_pool[base_url] = mock_client
            try:
                client = agent_client.AgentClient(agent_name)
                return await client._request("GET", "/health", timeout=timeout)
            finally:
                await mock_client.aclose()
                agent_client._client_pool.pop(base_url, None)

        return asyncio.run(runner())

    # ─── Hard failures: must increment the circuit ──────────────────────

    def test_connect_error_records_failure(self, agent_name):
        """Case 1: ConnectError → +1 failure, stays closed below threshold."""
        def handler(_req):
            raise httpx.ConnectError("refused")

        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 1
        assert cs.state == "closed"

    def test_connect_timeout_records_failure(self, agent_name):
        """Case 2: ConnectTimeout (a TimeoutException subclass) → +1 failure."""
        def handler(_req):
            raise httpx.ConnectTimeout("handshake timed out")

        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 1
        assert cs.state == "closed"

    # ─── Soft failures: must NOT increment the circuit ──────────────────

    def test_read_timeout_does_not_record(self, agent_name):
        """Case 3: 5× ReadTimeout in a row — the core #474 regression guard.

        On a busy agent, background pollers regularly hit ReadTimeout. They
        must not trip the circuit; we feed 5 in a row (≥ threshold) and
        assert failures stays at 0.
        """
        def handler(_req):
            raise httpx.ReadTimeout("slow")

        for _ in range(5):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    def test_write_error_does_not_record(self, agent_name):
        """Case 4: httpx.WriteError (wraps BrokenPipeError) — literal #474."""
        def handler(_req):
            raise httpx.WriteError("[Errno 32] Broken pipe")

        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    def test_raw_broken_pipe_does_not_record(self, agent_name):
        """Case 5: raw BrokenPipeError surfaced un-wrapped.

        Some transports (and MockTransport) can surface OSError subclasses
        directly. The #474 follow-up DID add a raw OSError catch — for
        drop-grace coordination (stamp + pool eviction) — but it raises
        AgentConnectionDroppedError, a subclass of AgentNotReachableError,
        rather than letting the raw exception propagate. The primary
        assertion of this test — no record_failure() — is unchanged: the
        AgentConnectionDroppedError path explicitly skips the circuit
        counter. Asserting the subclass also pins the typed-error contract
        so `except AgentNotReachableError` blocks still pick it up.
        """
        def handler(_req):
            raise BrokenPipeError("epipe")

        with pytest.raises(agent_client.AgentConnectionDroppedError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    def test_raw_connection_reset_does_not_record(self, agent_name):
        """Case 6: raw ConnectionResetError — sibling of #5.

        Same reclassification as case 5: caught at the drop handler,
        raised as AgentConnectionDroppedError, no record_failure().
        """
        def handler(_req):
            raise ConnectionResetError("reset by peer")

        with pytest.raises(agent_client.AgentConnectionDroppedError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    def test_remote_protocol_error_does_not_record(self, agent_name):
        """Case 7: RemoteProtocolError (HTTP/2 GOAWAY / framing issues)."""
        def handler(_req):
            raise httpx.RemoteProtocolError("bad framing")

        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    def test_pool_timeout_does_not_record(self, agent_name):
        """Case 8: PoolTimeout = client-side pool exhaustion, not agent unhealth.

        PoolTimeout is raised by httpx's connection pool, not the transport,
        so MockTransport can't naturally produce it. We raise it from the
        handler — the exception type is what _request() classifies on.
        """
        def handler(_req):
            raise httpx.PoolTimeout("pool exhausted")

        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == 0
        assert cs.state == "closed"

    # ─── HTTP responses: 200..599 record success ────────────────────────

    def test_5xx_response_records_success(self, agent_name):
        """Case 9: 500 response → record_success() (agent is reachable).

        Pre-seeds the circuit with 2 failures, then asserts a 500 response
        clears them.
        """
        cs = agent_client.CircuitState(agent_name)
        cs.record_failure()
        cs.record_failure()
        assert cs.failure_count == 2

        def handler(_req):
            return httpx.Response(500, json={"detail": "task error"})

        response = self._drive(agent_name, handler)
        assert response.status_code == 500

        # 500 hit record_success → counter cleared.
        cs2 = agent_client.CircuitState(agent_name)
        assert cs2.failure_count == 0
        assert cs2.state == "closed"

    # ─── Mixed-signal interleave ────────────────────────────────────────

    def test_mixed_signals_only_hard_failures_count(self, agent_name):
        """Case 10: 2× ReadTimeout + 3× ConnectError → exactly 3 failures, opens.

        Soft failures must not contaminate the hard counter. Three
        ConnectErrors (the threshold) trip the circuit; the interleaved
        ReadTimeouts are invisible to it.
        """
        def soft_handler(_req):
            raise httpx.ReadTimeout("busy")

        def hard_handler(_req):
            raise httpx.ConnectError("refused")

        # 2 soft failures
        for _ in range(2):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, soft_handler)

        # 3 hard failures → trip threshold (default 3)
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, hard_handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.failure_count == agent_client.CIRCUIT_FAILURE_THRESHOLD
        assert cs.state == "open"

    # ─── Pile-on guard ──────────────────────────────────────────────────

    def test_open_circuit_fast_fails_before_transport(self, agent_name):
        """Case 11: once circuit is open, _request raises AgentCircuitOpenError
        *before* the transport is hit. record_failure is NOT called again.
        """
        def hard_handler(_req):
            raise httpx.ConnectError("refused")

        # Drive to open via 3 ConnectErrors.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, hard_handler)

        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "open"
        baseline_failures = cs.failure_count

        # Sentinel that records whether handler was invoked.
        invoked = []

        def post_open_handler(_req):
            invoked.append(True)
            raise httpx.ConnectError("would record another failure")

        # Next call must short-circuit with AgentCircuitOpenError.
        with pytest.raises(agent_client.AgentCircuitOpenError):
            self._drive(agent_name, post_open_handler)

        # Transport was never hit.
        assert invoked == [], "transport should not be invoked when circuit is open"

        # Failure counter unchanged.
        cs2 = agent_client.CircuitState(agent_name)
        assert cs2.failure_count == baseline_failures

    # ─── Recovery on success ────────────────────────────────────────────

    def test_recovery_on_success_after_open(self, agent_name, monkeypatch):
        """Case 12: after open, a 200 response inside the probe window
        resets failures to 0 and closes the circuit.
        """
        # Shrink cooldown so the probe window opens almost immediately.
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.05)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.05)

        def hard_handler(_req):
            raise httpx.ConnectError("refused")

        def ok_handler(_req):
            return httpx.Response(200, json={"ok": True})

        # Drive to open.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, hard_handler)
        assert agent_client.CircuitState(agent_name).state == "open"

        # Sleep past the cooldown so the half-open probe is admitted.
        time.sleep(0.15)

        response = self._drive(agent_name, ok_handler)
        assert response.status_code == 200

        cs = agent_client.CircuitState(agent_name)
        assert cs.state == "closed"
        assert cs.failure_count == 0

    # ─── Half-open + soft failure interaction (accepted behaviour) ──────

    def test_half_open_soft_failure_holds_probe_lock(self, agent_name, monkeypatch):
        """Case 13: when the half-open probe gets a ReadTimeout (soft), we
        do NOT call record_failure(), so the probe-lock is released only
        via its 10s TTL. Locking in the accepted behaviour — the proper
        fix (a separate soft-failure counter that releases the probe lock
        without tripping the hard-failure threshold) is intentionally
        out of scope for #474.
        """
        monkeypatch.setattr(agent_client, "CIRCUIT_BASE_COOLDOWN_SECONDS", 0.05)
        monkeypatch.setattr(agent_client, "CIRCUIT_MAX_COOLDOWN_SECONDS", 0.05)

        def hard_handler(_req):
            raise httpx.ConnectError("refused")

        def soft_handler(_req):
            raise httpx.ReadTimeout("still busy")

        # Drive to open.
        for _ in range(agent_client.CIRCUIT_FAILURE_THRESHOLD):
            with pytest.raises(agent_client.AgentNotReachableError):
                self._drive(agent_name, hard_handler)

        # Wait past cooldown so probe is eligible.
        time.sleep(0.15)

        # First call wins the probe lock, hits ReadTimeout — soft, no record_failure.
        with pytest.raises(agent_client.AgentNotReachableError):
            self._drive(agent_name, soft_handler)

        cs = agent_client.CircuitState(agent_name)
        # Failures unchanged from when we drove to open.
        assert cs.failure_count == agent_client.CIRCUIT_FAILURE_THRESHOLD
        # Still open (no advance, no recovery).
        assert cs.state == "open"

        # Probe-lock still held → next allow_request denied without invoking
        # transport. Accepted behaviour for #474 — see this test's docstring
        # for the deferred soft-failure-counter fix.
        invoked = []

        def handler(_req):
            invoked.append(True)
            return httpx.Response(200)

        with pytest.raises(agent_client.AgentCircuitOpenError):
            self._drive(agent_name, handler)

        assert invoked == [], "probe-lock should still be held; transport not hit"


# ── Concurrent transport drops keep circuit closed (#474) ────────────────────

class TestConcurrentTransportDrops:
    """Real-Redis regression for #474.

    When N concurrent requests against the same agent all see a transport
    drop (BrokenPipeError / httpx.ReadError / httpx.RemoteProtocolError),
    none of them must trip the circuit. Verifies via CircuitState.to_dict()
    (handles missing-hash case gracefully — the dict shows `state=closed`
    even when no Redis hash exists for the agent yet) that:
      - state stays 'closed'
      - failure_count is zero
      - no `Circuit OPENED` log line was emitted
      - the pooled httpx client was evicted (no broken keepalive socket left)
    """

    @pytest.mark.parametrize(
        "exc_factory",
        [
            lambda: BrokenPipeError(32, "Broken pipe"),
            lambda: __import__("httpx").ReadError("read"),
            lambda: __import__("httpx").RemoteProtocolError(
                "Server disconnected without sending a response."
            ),
        ],
        ids=["BrokenPipeError", "httpx.ReadError", "httpx.RemoteProtocolError"],
    )
    def test_concurrent_broken_pipe_events_keep_circuit_closed(
        self, agent_name, exc_factory, caplog, monkeypatch
    ):
        import asyncio
        import logging

        # AgentClient builds a CircuitState in __init__ — we let the real
        # Redis-backed CircuitState be constructed (so the cleanup fixture
        # wipes its keys) and just observe state after the burst.
        client = agent_client.AgentClient(agent_name)
        base_url = client.base_url

        # Pre-warm the pool so we can install a raising .request method on
        # the pooled client object.
        pooled = agent_client._get_http_client(base_url)

        async def _raise(*_a, **_kw):
            raise exc_factory()

        monkeypatch.setattr(pooled, "request", _raise)

        async def _burst():
            # 10 concurrent calls all hitting the patched pooled client.
            results = await asyncio.gather(
                *[client._request("GET", "/health") for _ in range(10)],
                return_exceptions=True,
            )
            return results

        with caplog.at_level(logging.WARNING, logger=agent_client.logger.name):
            results = asyncio.run(_burst())

        # Every call should have raised AgentConnectionDroppedError (not
        # AgentNotReachableError → ConnectError → record_failure).
        assert all(
            isinstance(r, agent_client.AgentConnectionDroppedError)
            for r in results
        ), f"unexpected exception types: {[type(r).__name__ for r in results]}"

        # Circuit must remain closed via to_dict() — the API that handles
        # missing-hash gracefully (Phase 3 Eng finding #8).
        state = agent_client.CircuitState(agent_name).to_dict()
        assert state["state"] == "closed", f"state was {state}"
        assert state.get("failure_count", 0) == 0, f"failure_count was {state}"

        # No transition log fired.
        opened_logs = [
            r for r in caplog.records
            if "Circuit OPENED" in r.getMessage() and agent_name in r.getMessage()
        ]
        assert opened_logs == [], (
            f"unexpected OPENED log: {[r.getMessage() for r in opened_logs]}"
        )

        # Pool must be evicted — concurrency guard ensures the first worker
        # to land in the except block wins the pop; siblings see empty pool.
        assert base_url not in agent_client._client_pool, (
            "pooled client should be evicted after a transport drop"
        )
