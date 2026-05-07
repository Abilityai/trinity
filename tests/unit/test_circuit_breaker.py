"""
Circuit Breaker & Retry Tests (RELIABILITY-001)

Unit tests for the circuit breaker, connection pooling, and retry logic
in agent_client.py. These test the reliability primitives directly
without requiring running agent containers.
"""

import asyncio
import importlib
import time
import pytest
import sys
import os

# Add backend to path and import only agent_client (avoid triggering
# the full services __init__.py which pulls in Docker, models, etc.)
_backend = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'backend')
sys.path.insert(0, _backend)

# Load module directly to avoid services/__init__.py import chain
_spec = importlib.util.spec_from_file_location(
    "agent_client",
    os.path.join(_backend, "services", "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_client)

CircuitState = agent_client.CircuitState
AgentClient = agent_client.AgentClient
AgentCircuitOpenError = agent_client.AgentCircuitOpenError
AgentNotReachableError = agent_client.AgentNotReachableError
AgentClientError = agent_client.AgentClientError
_circuit_registry = agent_client._circuit_registry
_get_circuit = agent_client._get_circuit
get_all_circuit_states = agent_client.get_all_circuit_states
_client_pool = agent_client._client_pool
_get_http_client = agent_client._get_http_client
close_all_clients = agent_client.close_all_clients


# ============================================================================
# CircuitState Unit Tests
# ============================================================================

class TestCircuitState:
    """Test per-agent circuit breaker state machine."""

    def test_initial_state_is_closed(self):
        circuit = CircuitState(agent_name="test")
        assert circuit.state == "closed"
        assert circuit.failure_count == 0
        assert circuit.allow_request() is True

    def test_failures_below_threshold_stay_closed(self):
        circuit = CircuitState(agent_name="test", failure_threshold=3)
        circuit.record_failure()
        circuit.record_failure()
        assert circuit.state == "closed"
        assert circuit.failure_count == 2
        assert circuit.allow_request() is True

    def test_failures_at_threshold_open_circuit(self):
        circuit = CircuitState(agent_name="test", failure_threshold=3)
        circuit.record_failure()
        circuit.record_failure()
        circuit.record_failure()
        assert circuit.state == "open"
        assert circuit.failure_count == 3
        assert circuit.allow_request() is False

    def test_success_resets_to_closed(self):
        circuit = CircuitState(agent_name="test", failure_threshold=2)
        circuit.record_failure()
        circuit.record_failure()
        assert circuit.state == "open"

        # Simulate cooldown expiry
        circuit.last_failure_time = time.monotonic() - 60
        assert circuit.allow_request() is True
        assert circuit.state == "half-open"

        circuit.record_success()
        assert circuit.state == "closed"
        assert circuit.failure_count == 0

    def test_cooldown_blocks_requests(self):
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=30.0
        )
        circuit.record_failure()
        assert circuit.state == "open"
        assert circuit.allow_request() is False

    def test_cooldown_expiry_allows_half_open(self):
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=0.1
        )
        circuit.record_failure()
        assert circuit.state == "open"

        time.sleep(0.15)
        assert circuit.allow_request() is True
        assert circuit.state == "half-open"

    def test_half_open_failure_reopens(self):
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=0.0
        )
        circuit.record_failure()
        assert circuit.state == "open"

        # Cooldown expired
        circuit.allow_request()
        assert circuit.state == "half-open"

        # Another failure reopens
        circuit.record_failure()
        assert circuit.state == "open"

    def test_success_before_threshold_resets_count(self):
        circuit = CircuitState(agent_name="test", failure_threshold=3)
        circuit.record_failure()
        circuit.record_failure()
        circuit.record_success()
        assert circuit.failure_count == 0
        assert circuit.state == "closed"

    def test_to_dict(self):
        circuit = CircuitState(agent_name="test")
        d = circuit.to_dict()
        assert d["state"] == "closed"
        assert d["failure_count"] == 0
        assert d["cooldown_remaining"] == 0.0

    def test_to_dict_open_has_cooldown(self):
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=30.0
        )
        circuit.record_failure()
        d = circuit.to_dict()
        assert d["state"] == "open"
        assert d["cooldown_remaining"] > 0

    def test_open_circuit_does_not_reset_cooldown_clock_on_subsequent_failures(self):
        """Subsequent failures while open must not advance last_failure_time (#687).

        Before the fix, every record_failure() call reset last_failure_time even
        when the circuit was already open. This made the cooldown timer restart on
        every probe failure, so the half-open state was unreachable under load.
        """
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=30.0
        )
        circuit.record_failure()
        assert circuit.state == "open"
        clock_at_open = circuit.last_failure_time

        time.sleep(0.01)
        circuit.record_failure()
        circuit.record_failure()

        assert circuit.last_failure_time == clock_at_open

    def test_continuous_failures_while_open_still_allow_half_open_recovery(self):
        """Circuit must enter half-open after cooldown even with ongoing probe failures (#687)."""
        circuit = CircuitState(
            agent_name="test", failure_threshold=1, cooldown_seconds=0.05
        )
        circuit.record_failure()
        assert circuit.state == "open"

        # Simulate cleanup / scheduler probes failing while circuit is open
        circuit.record_failure()
        circuit.record_failure()
        circuit.record_failure()

        time.sleep(0.1)
        assert circuit.allow_request() is True
        assert circuit.state == "half-open"


# ============================================================================
# Circuit Registry Tests
# ============================================================================

class TestCircuitRegistry:
    """Test the module-level circuit breaker registry."""

    def setup_method(self):
        _circuit_registry.clear()

    def test_get_circuit_creates_new(self):
        circuit = _get_circuit("agent-a")
        assert isinstance(circuit, CircuitState)
        assert circuit.agent_name == "agent-a"

    def test_get_circuit_returns_same_instance(self):
        c1 = _get_circuit("agent-b")
        c2 = _get_circuit("agent-b")
        assert c1 is c2

    def test_get_all_circuit_states(self):
        c1 = _get_circuit("agent-x")
        c1.record_failure()
        c2 = _get_circuit("agent-y")

        states = get_all_circuit_states()
        assert "agent-x" in states
        assert "agent-y" in states
        assert states["agent-x"]["failure_count"] == 1
        assert states["agent-y"]["failure_count"] == 0


# ============================================================================
# Connection Pool Tests
# ============================================================================

class TestConnectionPool:
    """Test the HTTP client connection pool."""

    def setup_method(self):
        # Close any leftover clients
        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_all_clients())
        loop.close()

    def test_get_http_client_creates_client(self):
        client = _get_http_client("http://agent-test:8000")
        assert client is not None
        assert not client.is_closed

    def test_get_http_client_reuses_client(self):
        c1 = _get_http_client("http://agent-test:8000")
        c2 = _get_http_client("http://agent-test:8000")
        assert c1 is c2

    def test_different_urls_get_different_clients(self):
        c1 = _get_http_client("http://agent-a:8000")
        c2 = _get_http_client("http://agent-b:8000")
        assert c1 is not c2

    def test_close_all_clients(self):
        _get_http_client("http://agent-test:8000")
        assert len(_client_pool) > 0

        loop = asyncio.new_event_loop()
        loop.run_until_complete(close_all_clients())
        loop.close()
        assert len(_client_pool) == 0


# ============================================================================
# AgentClient Integration Tests
# ============================================================================

class TestAgentClientCircuitBreaker:
    """Test AgentClient circuit breaker integration."""

    def setup_method(self):
        _circuit_registry.clear()

    def test_client_shares_circuit_per_agent(self):
        c1 = AgentClient("test-agent")
        c2 = AgentClient("test-agent")
        assert c1._circuit is c2._circuit

    def test_different_agents_have_different_circuits(self):
        c1 = AgentClient("agent-a")
        c2 = AgentClient("agent-b")
        assert c1._circuit is not c2._circuit

    @pytest.mark.asyncio
    async def test_circuit_open_raises_immediately(self):
        client = AgentClient("broken-agent")
        # Force circuit open
        client._circuit.failure_threshold = 1
        client._circuit.record_failure()
        assert client._circuit.state == "open"

        with pytest.raises(AgentCircuitOpenError):
            await client.get("/api/health")

    @pytest.mark.asyncio
    async def test_circuit_open_health_check_returns_false(self):
        client = AgentClient("broken-agent")
        client._circuit.failure_threshold = 1
        client._circuit.record_failure()

        result = await client.health_check()
        assert result is False

    @pytest.mark.asyncio
    async def test_unreachable_agent_records_failure(self):
        client = AgentClient("nonexistent-agent-xyz")
        assert client._circuit.failure_count == 0

        # This will fail to connect (agent doesn't exist)
        try:
            await client.get("/api/health", timeout=1.0)
        except AgentClientError:
            pass

        assert client._circuit.failure_count > 0


# ============================================================================
# Exception Hierarchy Tests
# ============================================================================

class TestExceptionHierarchy:
    """Verify exception inheritance for backward compatibility."""

    def test_circuit_open_is_agent_client_error(self):
        err = AgentCircuitOpenError("test")
        assert isinstance(err, AgentClientError)

    def test_not_reachable_is_agent_client_error(self):
        err = AgentNotReachableError("test")
        assert isinstance(err, AgentClientError)

    def test_existing_catch_blocks_catch_circuit_open(self):
        """Callers using 'except AgentClientError' will catch circuit open errors."""
        try:
            raise AgentCircuitOpenError("circuit open")
        except AgentClientError:
            pass  # This should catch it
