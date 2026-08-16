"""
Agent /health richer-signal unit tests (#1020).

Covers the AgentState health counters that back the richer `/health` response:
`active_task_count`, `last_task_at`, `consecutive_failures` — the named fields
the platform consumes for the dispatch circuit breaker (#526) and fleet-health
scoring (#307).

Pure-logic test: it imports `agent_server.state` from `docker/base-image`,
constructs an AgentState via `__new__` (bypassing the runtime-availability
subprocess probe in `__init__`), and drives the record_* methods directly. No
backend, no running agent. Skips cleanly if the agent-server package can't be
imported in this environment, so it can never become a CI collection error.
"""

import os
import sys
import threading

import pytest

# Add the agent-server package root to the path.
_AGENT_BASE = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "docker", "base-image")
)
if _AGENT_BASE not in sys.path:
    sys.path.insert(0, _AGENT_BASE)

try:
    from agent_server.state import AgentState
except Exception as e:  # pragma: no cover - environment-dependent
    pytest.skip(f"agent_server.state not importable here: {e}", allow_module_level=True)


# Override the backend-requiring autouse fixtures from the package conftest so
# this pure-unit test runs without a live backend.
@pytest.fixture(scope="session")
def api_client():
    yield None


@pytest.fixture(autouse=True)
def cleanup_after_test():
    yield


@pytest.fixture
def health_state():
    """An AgentState with only the health counters initialized (no __init__,
    so the runtime-availability subprocess probe never runs)."""
    inst = AgentState.__new__(AgentState)
    inst._health_lock = threading.Lock()
    inst.active_task_count = 0
    inst.last_task_at = None
    inst.consecutive_failures = 0
    return inst


def test_initial_state(health_state):
    assert health_state.active_task_count == 0
    assert health_state.last_task_at is None
    assert health_state.consecutive_failures == 0


def test_start_increments_active_and_sets_last_task_at(health_state):
    health_state.record_task_start()
    assert health_state.active_task_count == 1
    assert health_state.last_task_at is not None


def test_finish_decrements_active(health_state):
    health_state.record_task_start()
    health_state.record_task_finish(success=True)
    assert health_state.active_task_count == 0


def test_concurrent_tasks_tracked(health_state):
    health_state.record_task_start()
    health_state.record_task_start()
    assert health_state.active_task_count == 2
    health_state.record_task_finish(success=True)
    assert health_state.active_task_count == 1


def test_failure_increments_consecutive(health_state):
    health_state.record_task_start()
    health_state.record_task_finish(success=False)
    health_state.record_task_start()
    health_state.record_task_finish(success=False)
    assert health_state.consecutive_failures == 2


def test_success_resets_consecutive_failures(health_state):
    health_state.record_task_start()
    health_state.record_task_finish(success=False)
    health_state.record_task_start()
    health_state.record_task_finish(success=False)
    assert health_state.consecutive_failures == 2
    health_state.record_task_start()
    health_state.record_task_finish(success=True)
    assert health_state.consecutive_failures == 0


def test_finish_never_goes_negative(health_state):
    # Defensive: a finish without a matching start must not underflow.
    health_state.record_task_finish(success=True)
    assert health_state.active_task_count == 0
