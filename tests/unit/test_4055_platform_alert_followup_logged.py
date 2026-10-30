"""#4055 — a follow-up the platform-alert seam spawns, and that raises, is
logged once at ERROR with the kind and agent.

`observe` hands two follow-ups to `operator_resume_service.spawn_on_loop`: the
alert-budget episode on a `refused_at_budget` outcome, and the thin
`operator_queue_sync` broadcast after a changed row. `spawn_on_loop`'s
done-callback only discards the task, so an exception raised inside one was
never logged. Driven at the seam against the unit island's real SQLite; the
scheduled factory is run here the way the loop would run it.
"""
import asyncio
import logging
import uuid

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def scheduled(monkeypatch):
    from services import operator_resume_service
    factories = []
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", factories.append)
    return factories


@pytest.fixture
def agent():
    return f"agent-4055-{uuid.uuid4().hex[:6]}"


def _run(factory):
    asyncio.run(factory())  # never raises: the seam reports instead


def _errors(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.ERROR and "a follow-up for" in r.getMessage()]


def test_a_raising_alert_budget_episode_is_logged(monkeypatch, scheduled, agent, caplog):
    from services import platform_alerts as pa, operator_queue_service as oqs

    monkeypatch.setattr(oqs, "OPERATOR_ALERT_MAX_PENDING_PER_TYPE", 0)

    async def _boom(*a, **k):
        raise RuntimeError("episode write failed")

    monkeypatch.setattr(oqs, "_maybe_emit_alert_budget_episode", _boom)

    outcome = pa.observe(agent, "skill_not_found", "missing-skill", title="t", question="q")
    assert outcome == pa.OBSERVED_REFUSED_AT_BUDGET, "control: the reading was refused at budget"
    [factory] = scheduled

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        _run(factory)

    [record] = _errors(caplog)
    assert f"{agent}/skill_not_found" in record.getMessage()
    assert "episode write failed" in record.exc_text


def test_a_raising_broadcast_is_logged(monkeypatch, scheduled, agent, caplog):
    from services import platform_alerts as pa, operator_queue_service as oqs

    class _Manager:
        async def broadcast(self, _message):
            raise ConnectionError("socket gone")

    monkeypatch.setattr(oqs, "_websocket_manager", _Manager())

    assert pa.observe(agent, "circuit_dormant", agent, title="t", question="q") == pa.OBSERVED_CREATED
    [factory] = scheduled

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        _run(factory)

    [record] = _errors(caplog)
    assert f"{agent}/circuit_dormant" in record.getMessage()


def test_a_follow_up_that_succeeds_logs_nothing(monkeypatch, scheduled, agent, caplog):
    from services import platform_alerts as pa, operator_queue_service as oqs

    sent = []

    class _Manager:
        async def broadcast(self, message):
            sent.append(message)

    monkeypatch.setattr(oqs, "_websocket_manager", _Manager())
    pa.observe(agent, "circuit_dormant", agent, title="t", question="q")
    [factory] = scheduled

    with caplog.at_level(logging.ERROR, logger="services.platform_alerts"):
        _run(factory)

    assert len(sent) == 1
    assert _errors(caplog) == []
