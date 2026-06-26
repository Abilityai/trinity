"""Unit tests for docker_service.list_all_agents_fast() error observability.

Issue #1131: when the backend can't reach /var/run/docker.sock (the macOS
Docker Desktop ``group_add`` regression from #874), ``containers.list(...)``
raises ``PermissionError`` and ``list_all_agents_fast()`` swallows it and
returns ``[]``. Before this change the swallow site ``print()``-ed, so the
failure never reached the structured (Vector-captured) log stream and the
Agents page silently showed "No agents" with nothing diagnosable in the logs.

The fix converts the swallow site to ``logger.warning(...)``. These tests lock
both halves of that contract: the call still returns ``[]`` (no crash) AND it
now emits a WARNING so the socket-access failure is observable.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import services.docker_service as ds  # noqa: E402


@pytest.fixture
def restore_docker_client():
    """Save/restore docker_client and reset the #1131 warn throttle per test.

    ``_last_socket_warn_monotonic`` is module-level state; without the reset a
    prior test's warning would throttle this test's first warning and the
    assertion would flake under pytest-randomly ordering.
    """
    original = ds.docker_client
    original_throttle = ds._last_socket_warn_monotonic
    ds._last_socket_warn_monotonic = None
    try:
        yield
    finally:
        ds.docker_client = original
        ds._last_socket_warn_monotonic = original_throttle


def test_list_fast_warns_and_returns_empty_on_socket_error(restore_docker_client, caplog):
    """A PermissionError from containers.list (the #1131 symptom) → [] + WARNING."""
    fake_client = MagicMock()
    fake_client.containers.list.side_effect = PermissionError(
        "[Errno 13] Permission denied: '/var/run/docker.sock'"
    )
    ds.docker_client = fake_client

    with caplog.at_level(logging.WARNING, logger="services.docker_service"):
        result = ds.list_all_agents_fast()

    assert result == [], "socket failure must degrade to an empty list, not raise"

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "expected a WARNING to be emitted when the Docker socket is unreachable"
    # The exception message must be carried into the log so the failure is diagnosable.
    assert "Permission denied" in caplog.text


def test_list_fast_returns_empty_without_client(restore_docker_client, caplog):
    """No Docker client at all → [] early-return, no crash, no spurious warning."""
    ds.docker_client = None

    with caplog.at_level(logging.WARNING, logger="services.docker_service"):
        result = ds.list_all_agents_fast()

    assert result == []
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def test_list_fast_warn_is_throttled_under_persistent_denial(restore_docker_client, caplog):
    """#1131 F3: a persistent socket denial warns ONCE per window, not per call.

    list_all_agents_fast() is hit by the 5s heartbeat/operator loops; an
    unthrottled warn would flood Vector while the GID stays wrong. The first
    failing call warns; immediate repeats within the throttle window are silent.
    """
    fake_client = MagicMock()
    fake_client.containers.list.side_effect = PermissionError(
        "[Errno 13] Permission denied: '/var/run/docker.sock'"
    )
    ds.docker_client = fake_client

    with caplog.at_level(logging.WARNING, logger="services.docker_service"):
        for _ in range(5):
            assert ds.list_all_agents_fast() == []

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one throttled warning, got {len(warnings)}"

    # Once the window has elapsed, the next failure warns again (still observable).
    ds._last_socket_warn_monotonic -= ds._SOCKET_WARN_THROTTLE_S + 1
    with caplog.at_level(logging.WARNING, logger="services.docker_service"):
        assert ds.list_all_agents_fast() == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, "warning must resume after the throttle window elapses"
