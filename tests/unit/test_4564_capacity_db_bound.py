"""#4564 — the DB layer must not re-impose the old hardcoded cap of 10.

#506 moved the capacity upper bound to the admin-set fleet ceiling (validated
in ``routers/agent_config.py``), but ``set_max_parallel_tasks`` kept
``max_tasks > 10 -> False``, which the router turned into a 500 for any value
from 11 up to the ceiling.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture
def ops(db_backend):
    from database import db  # noqa: WPS433

    seed_user()
    seed_agent("agent-1")
    return db


def test_value_above_old_cap_is_stored(ops):
    """Ceiling raised to 32: 16 must be accepted and read back (#4564)."""
    assert ops.set_max_parallel_tasks("agent-1", 16) is True
    assert ops.get_max_parallel_tasks("agent-1") == 16


def test_values_1_and_10_still_stored(ops):
    assert ops.set_max_parallel_tasks("agent-1", 1) is True
    assert ops.get_max_parallel_tasks("agent-1") == 1
    assert ops.set_max_parallel_tasks("agent-1", 10) is True
    assert ops.get_max_parallel_tasks("agent-1") == 10


def test_below_one_still_rejected(ops):
    assert ops.set_max_parallel_tasks("agent-1", 0) is False
    assert ops.set_max_parallel_tasks("agent-1", -3) is False


def test_unknown_agent_returns_false(ops):
    assert ops.set_max_parallel_tasks("nope", 5) is False
