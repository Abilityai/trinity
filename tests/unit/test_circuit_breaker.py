"""
Unit tests for non-state primitives in services/agent_client.py.

The circuit breaker state machine moved to Redis in #631; its tests live in
tests/integration/test_circuit_breaker.py because they need a real Redis to
exercise the atomic Lua transitions. What remains here is the in-process
machinery that has nothing to do with circuit state:

  * the per-base-URL httpx.AsyncClient pool
  * the exception hierarchy

If you want to test allow_request / record_failure / record_success / dormant
transitions / cross-worker probe-lock semantics, run the integration suite.
"""

import asyncio
import importlib
import os
import sys
from unittest.mock import MagicMock

import httpx
import pytest

# Add backend to path and import only agent_client (avoid triggering
# the full services __init__.py which pulls in Docker, models, etc.)
_backend = os.path.join(os.path.dirname(__file__), '..', '..', 'src', 'backend')
sys.path.insert(0, _backend)

# Load module directly to avoid services/__init__.py import chain.
_spec = importlib.util.spec_from_file_location(
    "agent_client",
    os.path.join(_backend, "services", "agent_client.py"),
)
agent_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(agent_client)

AgentClient = agent_client.AgentClient
AgentCircuitOpenError = agent_client.AgentCircuitOpenError
AgentNotReachableError = agent_client.AgentNotReachableError
AgentConnectionDroppedError = agent_client.AgentConnectionDroppedError
AgentClientError = agent_client.AgentClientError
_client_pool = agent_client._client_pool
_get_http_client = agent_client._get_http_client
close_all_clients = agent_client.close_all_clients
is_circuit_failure = agent_client.is_circuit_failure


pytestmark = pytest.mark.unit


# ============================================================================
# Connection Pool Tests
# ============================================================================

class TestConnectionPool:
    """Test the HTTP client connection pool."""

    def setup_method(self):
        # Close any leftover clients from previous tests.
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

    # ─── #474: is_circuit_failure() classification contract ─────────────────

    def test_classifier_connect_error_counts(self):
        """ConnectError = TCP refused / DNS unresolvable → agent unreachable."""
        assert is_circuit_failure(httpx.ConnectError("refused")) is True

    def test_classifier_connect_timeout_counts(self):
        """ConnectTimeout = TCP handshake didn't ack → agent unreachable.

        Must be matched as a CIRCUIT_FAILURE_EXCEPTION even though it's a
        TimeoutException subclass.
        """
        assert is_circuit_failure(httpx.ConnectTimeout("timed out")) is True

    def test_classifier_read_timeout_does_not_count(self):
        """ReadTimeout = agent stopped responding mid-request → usually busy."""
        assert is_circuit_failure(httpx.ReadTimeout("slow")) is False

    def test_classifier_write_error_does_not_count(self):
        """WriteError covers wrapped BrokenPipeError / ConnectionResetError."""
        assert is_circuit_failure(httpx.WriteError("epipe")) is False

    def test_classifier_pool_timeout_does_not_count(self):
        """PoolTimeout = client-side pool exhaustion, not agent unhealth."""
        assert is_circuit_failure(httpx.PoolTimeout("pool")) is False

    def test_classifier_read_error_does_not_count(self):
        """ReadError = mid-read socket loss → transient."""
        assert is_circuit_failure(httpx.ReadError("dropped")) is False

    def test_classifier_remote_protocol_error_does_not_count(self):
        """RemoteProtocolError = garbled response framing → transient."""
        assert is_circuit_failure(httpx.RemoteProtocolError("bad frame")) is False

    def test_classifier_raw_broken_pipe_does_not_count(self):
        """Raw BrokenPipeError (some transports surface it un-wrapped).

        Note: post-#474 follow-up, raw BrokenPipeError IS caught at the
        AgentClient._request() layer (drop-grace stamping + pool eviction +
        AgentConnectionDroppedError raise). The classifier still returns
        False — i.e. the *circuit* still treats it as non-failure — which
        is what this test pins. Per-request side effects are pinned in
        the drop-classification tests below.
        """
        assert is_circuit_failure(BrokenPipeError("epipe")) is False

    def test_classifier_raw_connection_reset_does_not_count(self):
        """Raw ConnectionResetError (sibling of BrokenPipeError)."""
        assert is_circuit_failure(ConnectionResetError("reset")) is False

    def test_classifier_raw_os_error_does_not_count(self):
        """OSError(EPIPE) directly — must NOT trip the circuit."""
        assert is_circuit_failure(OSError(32, "Broken pipe")) is False

    def test_classifier_runtime_error_does_not_count(self):
        """Unrelated RuntimeError → never a circuit signal."""
        assert is_circuit_failure(RuntimeError("oops")) is False

    def test_classifier_tuples_are_disjoint(self):
        """No exception type should be in both CIRCUIT_FAILURE and TRANSIENT
        tuples — that would make classification ambiguous."""
        cf = set(agent_client.CIRCUIT_FAILURE_EXCEPTIONS)
        tt = set(agent_client.TRANSIENT_TRANSPORT_EXCEPTIONS)
        assert cf.isdisjoint(tt), f"overlap: {cf & tt}"


# ============================================================================
# Transport-drop classification tests (#474)
# ============================================================================
#
# A "transport drop" is a mid-flight connection failure (BrokenPipeError,
# ConnectionResetError, httpx.ReadError/WriteError/RemoteProtocolError) — the
# request started but the socket died before the response arrived. These
# must NOT trip the circuit breaker (the agent itself may be fine) and the
# pooled httpx client must be evicted so the next call doesn't reuse a
# half-closed keepalive socket.

class _DropFixture:
    """Bundle of mocks for transport-drop tests.

    Patches the AgentClient's circuit attribute so we can assert
    record_failure is never called, and patches the pooled httpx client's
    `request` method to raise a chosen exception.

    Construct via the async helper `_make_drop_fixture` because pool draining
    needs to happen inside the running test event loop.
    """

    def __init__(self, agent_name: str, base_url: str, fake_circuit, client, pooled):
        self.agent_name = agent_name
        self.base_url = base_url
        self.fake_circuit = fake_circuit
        self.client = client
        self.pooled = pooled


async def _make_drop_fixture(monkeypatch, agent_name: str, raise_exc: Exception):
    """Async builder for _DropFixture — closes the pool, swaps the circuit
    factory, builds AgentClient, then pre-warms and patches the pooled
    httpx client."""
    base_url = f"http://agent-{agent_name}:8000"

    # Drain any pool entries from prior tests so this test starts from a
    # known state.
    await close_all_clients()

    # AgentClient.__init__ calls _get_circuit which would hit Redis — swap
    # it out before construction.
    fake_circuit = MagicMock()
    fake_circuit.allow_request.return_value = True
    fake_circuit.failure_count = 0
    monkeypatch.setattr(
        agent_client, "_get_circuit", lambda _name: fake_circuit
    )

    client = AgentClient(agent_name)

    # Pre-warm the pool entry, then override .request on the pooled client
    # so the next call from inside _request raises our chosen exception.
    pooled = _get_http_client(base_url)

    async def _raise(*_a, **_kw):
        raise raise_exc

    monkeypatch.setattr(pooled, "request", _raise)

    return _DropFixture(agent_name, base_url, fake_circuit, client, pooled)


@pytest.mark.asyncio
async def test_request_raises_dropped_on_broken_pipe_and_evicts_pool(monkeypatch):
    """A raw BrokenPipeError mid-request is reclassified as
    AgentConnectionDroppedError, never records a circuit failure, and the
    pooled client is evicted so the next call creates a fresh one."""
    fx = await _make_drop_fixture(
        monkeypatch, "test-pipe-evict", BrokenPipeError(32, "Broken pipe")
    )

    with pytest.raises(AgentConnectionDroppedError):
        await fx.client._request("GET", "/health")

    fx.fake_circuit.record_failure.assert_not_called()
    fx.fake_circuit.record_success.assert_not_called()
    assert fx.base_url not in _client_pool, "pooled client should be evicted"

    # Next call to _get_http_client constructs a fresh client (different
    # object than the evicted one).
    fresh = _get_http_client(fx.base_url)
    assert fresh is not fx.pooled


@pytest.mark.asyncio
async def test_request_raises_dropped_on_httpx_read_error(monkeypatch):
    """httpx.ReadError → AgentConnectionDroppedError, no circuit hit."""
    fx = await _make_drop_fixture(monkeypatch, "test-read-err", httpx.ReadError("read"))

    with pytest.raises(AgentConnectionDroppedError):
        await fx.client._request("GET", "/health")

    fx.fake_circuit.record_failure.assert_not_called()
    assert fx.base_url not in _client_pool


@pytest.mark.asyncio
async def test_request_raises_dropped_on_httpx_write_error(monkeypatch):
    """httpx.WriteError → AgentConnectionDroppedError. Added per Phase 3
    Eng finding: handler claims it but unit test coverage was missing."""
    fx = await _make_drop_fixture(monkeypatch, "test-write-err", httpx.WriteError("write"))

    with pytest.raises(AgentConnectionDroppedError):
        await fx.client._request("POST", "/api/chat")

    fx.fake_circuit.record_failure.assert_not_called()
    assert fx.base_url not in _client_pool


@pytest.mark.asyncio
async def test_request_raises_dropped_on_remote_protocol_error(monkeypatch):
    """httpx.RemoteProtocolError → AgentConnectionDroppedError."""
    fx = await _make_drop_fixture(
        monkeypatch,
        "test-rpe",
        httpx.RemoteProtocolError("Server disconnected without sending a response."),
    )

    with pytest.raises(AgentConnectionDroppedError):
        await fx.client._request("GET", "/api/chat/session")

    fx.fake_circuit.record_failure.assert_not_called()
    assert fx.base_url not in _client_pool


@pytest.mark.asyncio
async def test_request_still_records_failure_on_connect_error(monkeypatch):
    """Regression guard: a real ConnectError still records circuit failure
    (the original semantics, unchanged by #474)."""
    fx = await _make_drop_fixture(
        monkeypatch, "test-connerr", httpx.ConnectError("connection refused")
    )

    with pytest.raises(AgentNotReachableError) as exc_info:
        await fx.client._request("GET", "/health")

    # The dropped subclass exists; make sure a plain ConnectError is NOT
    # being reclassified through that path.
    assert not isinstance(exc_info.value, AgentConnectionDroppedError)
    fx.fake_circuit.record_failure.assert_called_once()
    # ConnectError doesn't evict the pool — the connection never opened.
    assert fx.base_url in _client_pool


def test_dropped_error_is_agent_not_reachable_subclass():
    """Ensures tenacity's retry_if_exception_type(AgentNotReachableError) and
    callers catching AgentNotReachableError pick up the new subclass."""
    err = AgentConnectionDroppedError("dropped")
    assert isinstance(err, AgentNotReachableError)
    assert isinstance(err, AgentClientError)


# ============================================================================
# In-grace-window behavior (#474 follow-up)
# ============================================================================
#
# When `_is_within_drop_grace(base_url)` is True, `_acquire_client` returns a
# fresh, non-pooled httpx.AsyncClient. Two invariants must hold:
#
#   1. A `ConnectError` raised by a fresh-during-grace client is reclassified
#      as `AgentConnectionDroppedError` (no `record_failure`). Same applies to
#      `TimeoutException`. This is the "sibling collapse" path #474 fixes.
#
#   2. The fresh client is closed on every exit path — success and failure.
#      Otherwise, sustained drop bursts leak one AsyncClient per call for the
#      duration of the grace window (each holding up to 10 connections).


class _TrackingAsyncClient:
    """httpx.AsyncClient stand-in that records aclose() and dispatches its
    request method from an injected callable. Tracks all instances created
    at module level so the leak test can assert closure."""

    instances: list = []

    def __init__(self, *_a, **_kw):
        self.aclose_called = False
        self.is_closed = False
        self._on_request = _TrackingAsyncClient._next_on_request()
        _TrackingAsyncClient.instances.append(self)

    @classmethod
    def reset(cls, on_request):
        """Reset the instance ledger and pin the request behavior for every
        instance constructed afterward."""
        cls.instances = []
        cls._on_request = on_request

    @classmethod
    def _next_on_request(cls):
        return getattr(cls, "_on_request", None)

    async def request(self, *_a, **_kw):
        if self._on_request is None:
            raise RuntimeError("test forgot to set _on_request")
        return await self._on_request()

    async def aclose(self):
        self.aclose_called = True
        self.is_closed = True


@pytest.mark.asyncio
async def test_connect_error_during_grace_reclassified_as_dropped(monkeypatch):
    """ConnectError raised by the fresh-during-grace client must be
    reclassified as AgentConnectionDroppedError — the pool eviction just
    fired, and the next sibling's reconnect attempt failing isn't a
    circuit-health signal. (#474.)"""
    await close_all_clients()

    fake_circuit = MagicMock()
    fake_circuit.allow_request.return_value = True
    fake_circuit.failure_count = 0
    monkeypatch.setattr(agent_client, "_get_circuit", lambda _name: fake_circuit)

    client = AgentClient("test-grace-connect")
    base_url = client.base_url

    # Pre-stamp the grace window so _acquire_client returns a fresh client.
    agent_client._stamp_drop(base_url)

    async def _raise_connect():
        raise httpx.ConnectError("connection refused")

    _TrackingAsyncClient.reset(on_request=_raise_connect)
    monkeypatch.setattr(agent_client.httpx, "AsyncClient", _TrackingAsyncClient)

    with pytest.raises(AgentConnectionDroppedError):
        await client._request("GET", "/health")

    fake_circuit.record_failure.assert_not_called()
    fake_circuit.record_success.assert_not_called()
    # And the fresh client was aclose'd (no leak).
    assert len(_TrackingAsyncClient.instances) == 1
    assert _TrackingAsyncClient.instances[0].aclose_called is True
    # Cleanup: clear the drop stamp so other tests don't observe grace.
    agent_client._recent_drops.pop(base_url, None)


@pytest.mark.asyncio
async def test_timeout_during_grace_reclassified_as_dropped(monkeypatch):
    """Parallel coverage for TimeoutException — same reclassification rule."""
    await close_all_clients()

    fake_circuit = MagicMock()
    fake_circuit.allow_request.return_value = True
    monkeypatch.setattr(agent_client, "_get_circuit", lambda _name: fake_circuit)

    client = AgentClient("test-grace-timeout")
    base_url = client.base_url
    agent_client._stamp_drop(base_url)

    async def _raise_timeout():
        raise httpx.TimeoutException("timed out")

    _TrackingAsyncClient.reset(on_request=_raise_timeout)
    monkeypatch.setattr(agent_client.httpx, "AsyncClient", _TrackingAsyncClient)

    with pytest.raises(AgentConnectionDroppedError):
        await client._request("GET", "/health")

    fake_circuit.record_failure.assert_not_called()
    assert _TrackingAsyncClient.instances[0].aclose_called is True
    agent_client._recent_drops.pop(base_url, None)


@pytest.mark.asyncio
async def test_fresh_clients_closed_during_drop_burst(monkeypatch):
    """Sustained drop burst inside the grace window: every fresh AsyncClient
    constructed by `_acquire_client` must be aclose'd, regardless of whether
    its single request succeeded or raised. Otherwise the burst leaks one
    client (each holding up to 10 connections) per call for the duration of
    the grace window. (#474 follow-up.)"""
    await close_all_clients()

    fake_circuit = MagicMock()
    fake_circuit.allow_request.return_value = True
    monkeypatch.setattr(agent_client, "_get_circuit", lambda _name: fake_circuit)

    client = AgentClient("test-grace-leak")
    base_url = client.base_url
    agent_client._stamp_drop(base_url)

    async def _raise_read_err():
        raise httpx.ReadError("read")

    _TrackingAsyncClient.reset(on_request=_raise_read_err)
    monkeypatch.setattr(agent_client.httpx, "AsyncClient", _TrackingAsyncClient)

    # Fire a burst of calls inside the grace window. All raise; none must leak.
    for _ in range(8):
        with pytest.raises(AgentConnectionDroppedError):
            await client._request("GET", "/health")

    # Every fresh client (one per call) was constructed and aclose'd.
    assert len(_TrackingAsyncClient.instances) == 8
    for inst in _TrackingAsyncClient.instances:
        assert inst.aclose_called is True, "fresh client was not closed"
    # Cleanup.
    agent_client._recent_drops.pop(base_url, None)


@pytest.mark.asyncio
async def test_pooled_client_not_closed_on_success(monkeypatch):
    """Regression guard: pooled clients (no grace active) must NOT be closed
    after a successful request — they're long-lived and shared. Only the
    fresh-during-grace branch closes its client in `finally`."""
    await close_all_clients()

    fake_circuit = MagicMock()
    fake_circuit.allow_request.return_value = True
    monkeypatch.setattr(agent_client, "_get_circuit", lambda _name: fake_circuit)

    client = AgentClient("test-pool-keepalive")
    base_url = client.base_url

    # Make sure no grace window is active for this base_url.
    agent_client._recent_drops.pop(base_url, None)

    class _OkResponse:
        status_code = 200

    pooled = _get_http_client(base_url)

    async def _ok(*_a, **_kw):
        return _OkResponse()

    monkeypatch.setattr(pooled, "request", _ok)

    response = await client._request("GET", "/health")
    assert response.status_code == 200
    fake_circuit.record_success.assert_called_once()
    assert pooled.is_closed is False, "pooled client must survive a success"
    assert base_url in _client_pool, "pooled client must remain in the pool"
