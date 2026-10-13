"""
Fail-closed effect guard on pull-mode agents (#2392).

Pull re-delivers the SAME execution after a lease expiry, so an outbound side
effect (message, call, share, A2A call) without a usable execution id cannot be
de-duplicated. On a pull-mode agent `effect_guard` refuses it and raises an
operator alarm; elsewhere the send proceeds and is logged as degraded. A
`manual` id (a person's terminal session) and a lookup error always proceed.

Reuses the temp-DB `effect_service` fixture from test_idempotency.py.
Related flow: docs/memory/feature-flows/effect-idempotency.md
"""

import logging
import sys
import types
from pathlib import Path

import pytest

# Sibling import (the unit dir is not implicitly importable) — reuse the
# temp-DB effect_service harness rather than fork it.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_idempotency import (  # noqa: E402,F401 — fixtures
    _register_execution,
    effect_service,
    idem_ops,
)


PILOT = "pilot-agent"
PUSH = "push-agent"
ARGS = {"recipient": "u@example.com", "channel": "telegram"}


@pytest.fixture
def alarms(monkeypatch):
    """Stub the operator-queue seam the guard imports lazily; record alarms."""
    calls = []
    stub = types.ModuleType("services.operator_queue_service")

    async def create_bounded_alert(agent_name, item):
        calls.append((agent_name, item))
        return True

    stub.create_bounded_alert = create_bounded_alert
    monkeypatch.setitem(sys.modules, "services.operator_queue_service", stub)
    return calls


@pytest.fixture
def pilot_env(monkeypatch):
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", PILOT)


async def _send(svc, *, execution_id, agent_name, sends, effect_type="message", args=ARGS):
    async with svc.effect_guard(
        effect_type, args, execution_id=execution_id, agent_name=agent_name,
    ) as g:
        if g.replay:
            return "replay"
        sends.append(execution_id)
        g.snapshot = {"ok": True}
        return "sent"


pytestmark = pytest.mark.asyncio


class TestPullAgentRefuses:

    @pytest.mark.parametrize("execution_id,reason", [
        (None, "absent"),
        ("", "absent"),
        ("ghost-exec", "unknown"),
    ])
    async def test_unusable_id_refused_with_alarm(
        self, effect_service, alarms, pilot_env, execution_id, reason
    ):
        sends = []
        with pytest.raises(effect_service.EffectUnguardedError) as exc:
            await _send(effect_service, execution_id=execution_id, agent_name=PILOT, sends=sends)
        assert sends == []
        assert exc.value.reason == reason
        assert "Do not retry" in str(exc.value)
        assert len(alarms) == 1
        agent, item = alarms[0]
        assert agent == PILOT
        assert item["type"] == "effect_unguarded"
        assert item["id"].startswith(effect_service.EFFECT_UNGUARDED_ALERT_PREFIX + PILOT)
        assert item["context"] == {"effect_type": "message", "reason": reason}

    async def test_foreign_execution_refused(self, effect_service, alarms, pilot_env):
        _register_execution(effect_service, "exec-other", "someone-else")
        sends = []
        with pytest.raises(effect_service.EffectUnguardedError) as exc:
            await _send(effect_service, execution_id="exec-other", agent_name=PILOT, sends=sends)
        assert exc.value.reason == "foreign"
        assert sends == []
        assert len(alarms) == 1

    async def test_refused_even_when_alarm_budget_exhausted(
        self, effect_service, alarms, pilot_env, monkeypatch
    ):
        async def at_cap(agent_name, item):
            return False  # budget full — no queue item, but the send still must not go

        monkeypatch.setattr(sys.modules["services.operator_queue_service"], "create_bounded_alert", at_cap)
        sends = []
        with pytest.raises(effect_service.EffectUnguardedError):
            await _send(effect_service, execution_id=None, agent_name=PILOT, sends=sends)
        assert sends == []

    async def test_manual_session_proceeds_without_alarm(self, effect_service, alarms, pilot_env, caplog):
        sends = []
        with caplog.at_level(logging.WARNING):
            assert await _send(effect_service, execution_id="manual", agent_name=PILOT, sends=sends) == "sent"
        assert sends == ["manual"]
        assert alarms == []
        assert "effect_guard.degraded" in caplog.text and "reason=manual" in caplog.text

    async def test_lookup_error_proceeds(self, effect_service, alarms, pilot_env, monkeypatch, caplog):
        def boom(eid):
            raise RuntimeError("db down")

        monkeypatch.setattr(sys.modules["database"].db, "get_execution", boom)
        sends = []
        with caplog.at_level(logging.WARNING):
            assert await _send(effect_service, execution_id="exec-1", agent_name=PILOT, sends=sends) == "sent"
        assert alarms == []
        assert "reason=lookup_error" in caplog.text


class TestRedeliveryEmitsOnce:
    """AC: a re-delivered execution emits each effect exactly once."""

    @pytest.mark.parametrize("effect_type,args", [
        ("message", {"recipient": "u@example.com", "channel": "telegram"}),
        ("voip_call", {"to": "+15550100"}),
        ("share_file", {"filename": "report.pdf"}),
        ("a2a_call", {"endpoint": "peer", "message_sha": "abc"}),
    ])
    async def test_same_execution_twice_one_send(
        self, effect_service, alarms, pilot_env, effect_type, args
    ):
        _register_execution(effect_service, "exec-1", PILOT)
        sends = []
        first = await _send(effect_service, execution_id="exec-1", agent_name=PILOT,
                            sends=sends, effect_type=effect_type, args=args)
        # Lease expired → reaper re-queues the SAME execution id → the turn runs again.
        second = await _send(effect_service, execution_id="exec-1", agent_name=PILOT,
                             sends=sends, effect_type=effect_type, args=args)
        assert (first, second) == ("sent", "replay")
        assert sends == ["exec-1"]
        assert alarms == []


class TestPushAgentUnchanged:

    @pytest.mark.parametrize("execution_id,reason", [
        (None, "absent"), ("ghost-exec", "unknown"), ("manual", "manual"),
    ])
    async def test_send_proceeds_and_is_logged(
        self, effect_service, alarms, pilot_env, caplog, execution_id, reason
    ):
        sends = []
        with caplog.at_level(logging.WARNING):
            assert await _send(effect_service, execution_id=execution_id, agent_name=PUSH, sends=sends) == "sent"
        assert len(sends) == 1
        assert alarms == []
        assert "effect_guard.degraded" in caplog.text and f"reason={reason}" in caplog.text

    async def test_no_pilots_configured_never_refuses(self, effect_service, alarms, monkeypatch):
        monkeypatch.delenv("PULL_MODE_PILOT_AGENTS", raising=False)
        sends = []
        assert await _send(effect_service, execution_id=None, agent_name=PILOT, sends=sends) == "sent"
        assert alarms == []


def test_alarm_type_registered_and_prefix_reserved():
    """The alarm must pass create_bounded_alert's closed type set, and its id
    prefix must be reserved so an agent cannot pre-create and suppress it."""
    import services.operator_queue_service as oqs
    from services.idempotency_service import EFFECT_UNGUARDED_ALERT_PREFIX

    assert "effect_unguarded" in oqs._BUDGETED_ALERT_TYPES
    assert EFFECT_UNGUARDED_ALERT_PREFIX in oqs._RESERVED_ID_PREFIXES
