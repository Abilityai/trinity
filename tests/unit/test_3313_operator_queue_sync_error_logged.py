"""
#3313 — an agent's sync failure is logged, not silently dropped.

`_poll_cycle` runs every running agent's `_sync_agent` under
`asyncio.gather(..., return_exceptions=True)`. That keeps one agent's failure
from cancelling the others, but it also discarded the exception with no log
line, so a sync that raised every 5 s (an unhashable id in the agent's file)
stopped that agent's answer delivery with nothing in the logs.

Driven through the real `OperatorQueueSyncService._poll_cycle`; only its
collaborators (leadership, expiry, skill-gate sweep, Docker, db) are stubbed.
The per-entry fixes are pinned in `test_ec_operator_queue_edges.py`
(`TestUnhashableEntryValues`).
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import types
from unittest.mock import MagicMock

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

import services  # noqa: E402
import services.docker_service as docker_service  # noqa: E402
import services.operator_queue_service as oqs  # noqa: E402


def _wire(monkeypatch, synced):
    svc = oqs.OperatorQueueSyncService()
    monkeypatch.setattr(svc, "_try_acquire_leadership", lambda: True)
    monkeypatch.setattr(oqs.ask_service, "expire", lambda: MagicMock(rows=[]))

    async def _sweep():
        return None
    gate = types.SimpleNamespace(sweep=_sweep)
    monkeypatch.setitem(sys.modules, "services.skill_gate_service", gate)
    monkeypatch.setattr(services, "skill_gate_service", gate, raising=False)

    monkeypatch.setattr(docker_service, "agent_container_states",
                        lambda: {"bad": "running", "good": "running"})
    db = MagicMock()
    db.mark_operator_queue_unconfirmed.return_value = 0
    db.mark_operator_queue_undeliverable_not_running.return_value = []
    monkeypatch.setattr(oqs, "db", db)

    async def _sync_agent(name):
        synced.append(name)
        if name == "bad":
            raise TypeError("unhashable type: 'list'")
    monkeypatch.setattr(svc, "_sync_agent", _sync_agent)
    return svc


def test_a_failing_agent_sync_is_logged_with_the_agent_name(monkeypatch, caplog):
    synced = []
    svc = _wire(monkeypatch, synced)
    with caplog.at_level(logging.ERROR, logger=oqs.logger.name):
        asyncio.run(svc._poll_cycle())

    assert sorted(synced) == ["bad", "good"]  # one failure cancels no other agent
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("'bad'" in m and "TypeError" in m for m in errors), errors
    assert not any("'good'" in m for m in errors), errors
