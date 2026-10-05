"""
#3246 C5′ — the four named emitter families report through the platform
alert seam, against the unit island's real SQLite: a repeated reading is one
pending row updated in place, and the place the code knows the condition
cleared ends that row as the platform.
"""
import os
import sys
import uuid

import pytest

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def db(monkeypatch):
    from database import db
    from services import operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    return db


def _pending(db, agent):
    return db.list_operator_queue_items(agent_name=agent, status="pending")


def _ended(db, row):
    return db.get_operator_queue_item(row["id"])


class TestCircuitDormant:
    @pytest.fixture
    def agent(self):
        return f"agent-3246cb-{uuid.uuid4().hex[:6]}"

    def test_repeat_dormant_entries_are_one_row(self, db, agent):
        from services.agent_client import circuit
        circuit._emit_dormant_alert(agent)
        circuit._emit_dormant_alert(agent)
        rows = _pending(db, agent)
        assert len(rows) == 1
        row = rows[0]
        assert row["request_id"].startswith(f"cb-dormant-{agent}-")
        assert row["type"] == "alert" and row["priority"] == "high"
        assert row["context"]["alert_type"] == "circuit_breaker_dormant"
        assert row["context"]["seen_count"] == 2
        assert row["expires_at"] is not None          # the 30-day net

    def test_recovering_from_dormant_ends_the_row_as_the_platform(self, db, agent, monkeypatch):
        from services.agent_client import circuit
        circuit._emit_dormant_alert(agent)
        row = _pending(db, agent)[0]
        monkeypatch.setattr(circuit, "_ensure_scripts",
                            lambda client: (None, None, lambda **kw: "dormant"))
        circuit.CircuitState(agent, redis_client=object()).record_success()
        assert _pending(db, agent) == []
        assert _ended(db, row)["disposed_by"] == "platform"
        assert _ended(db, row)["disposition_reason"] == "condition_cleared"

    def test_recovering_from_open_leaves_nothing_to_clear(self, db, agent, monkeypatch):
        from services.agent_client import circuit
        calls = []
        monkeypatch.setattr(circuit, "_clear_dormant_alert", calls.append)
        monkeypatch.setattr(circuit, "_ensure_scripts",
                            lambda client: (None, None, lambda **kw: "open"))
        circuit.CircuitState(agent, redis_client=object()).record_success()
        assert calls == []

    def test_admin_reset_ends_the_row(self, db, agent, monkeypatch):
        from services.agent_client import circuit

        class _Redis:
            def delete(self, *keys):
                return len(keys)

        circuit._emit_dormant_alert(agent)
        monkeypatch.setattr(circuit, "_get_circuit_redis", lambda: _Redis())
        circuit.reset_circuit(agent)
        assert _pending(db, agent) == []


class TestSystemAgent:
    AGENT = "trinity-system"

    def _rows(self, db, kind):
        return [r for r in _pending(db, self.AGENT) if r["subject"] == f"{kind}:{self.AGENT}"]

    def test_stale_image_is_one_row_ended_when_the_image_is_current(self, db, monkeypatch):
        from services import system_agent_service as sas
        monkeypatch.setattr(sas.SystemAgentService, "_last_base_image_alert_at", None)
        sas.SystemAgentService._clear_alert("base_image_stale")
        sas.SystemAgentService()._emit_base_image_stale_alert()
        rows = self._rows(db, "base_image_stale")
        assert len(rows) == 1 and rows[0]["request_id"].startswith("base-image-stale-trinity-system-")
        sas.SystemAgentService._clear_alert("base_image_stale")
        assert self._rows(db, "base_image_stale") == []
        assert _ended(db, rows[0])["disposed_by"] == "platform"

    def test_start_failure_is_one_row_ended_when_a_start_succeeds(self, db):
        from services import system_agent_service as sas
        sas.SystemAgentService._clear_alert("system_agent_start_failed")
        sas.SystemAgentService()._emit_start_failed_alert("network missing")
        sas.SystemAgentService()._emit_start_failed_alert("still missing")
        rows = self._rows(db, "system_agent_start_failed")
        assert len(rows) == 1 and "still missing" in rows[0]["question"]
        assert rows[0]["priority"] == "critical"
        sas.SystemAgentService._clear_alert("system_agent_start_failed")
        assert self._rows(db, "system_agent_start_failed") == []


class TestSubscriptionHeadroom:
    AGENT = "_sub-headroom"

    @pytest.fixture
    def sid(self):
        return f"sub/3246-{uuid.uuid4().hex[:6]}"

    def _rows(self, db, key):
        return [r for r in _pending(db, self.AGENT)
                if r["subject"] == f"subscription_headroom:{key}"]

    def _alert(self, sid, tier, util):
        from services import subscription_headroom_alerts as a
        return a.emit_subscription_alert(
            subscription_id=sid, subscription_name="one", tier=tier,
            utilization_pct=util, projected_end=None,
            resets_at="2026-10-09T00:00:00Z", threshold_pct=75, agents=[])

    def test_a_critical_reading_replaces_the_warning_on_one_row(self, db, sid):
        from services import subscription_headroom_alerts as a
        key = a.subject_key(sid)
        assert self._alert(sid, "warn", 78.0) is True
        assert self._alert(sid, "warn", 84.0) is True
        assert self._alert(sid, "crit", 93.0) is True
        rows = self._rows(db, key)
        assert len(rows) == 1
        assert rows[0]["request_id"].startswith(f"sub-headroom-{key}-")
        assert "93%" in rows[0]["title"] and rows[0]["context"]["tier"] == "crit"

    def test_the_evaluation_pass_ends_a_row_it_no_longer_backs(self, db, sid):
        from services import subscription_headroom_alerts as a
        other = f"{sid}-x"
        self._alert(sid, "warn", 80.0)
        self._alert(other, "warn", 80.0)
        a.clear_recovered([a.subject_key(other), a.FLEET_KEY])
        assert self._rows(db, a.subject_key(sid)) == []
        assert len(self._rows(db, a.subject_key(other))) == 1
        ended = [r for r in db.list_operator_queue_items(agent_name=self.AGENT, limit=1000)
                 if r["subject"] == f"subscription_headroom:{a.subject_key(sid)}"]
        assert ended and ended[0]["disposed_by"] == "platform"

    def test_the_sweep_keeps_an_unassessable_row_and_clears_a_measured_recovery(
            self, db, sid, monkeypatch):
        """Only a MEASURED `HAS_HEADROOM` clears — no evidence is not recovery."""
        import asyncio
        from services import subscription_headroom_alerts as a
        from services import subscription_recovery_service as svc
        recovered, unknown = sid, f"{sid}-u"
        self._alert(recovered, "warn", 80.0)
        self._alert(unknown, "warn", 80.0)
        subs = [type("S", (), {"id": recovered, "name": "r"})(),
                type("S", (), {"id": unknown, "name": "u"})()]
        results = [{"sid": recovered, "classification": a.HAS_HEADROOM, "reading": None},
                   {"sid": unknown, "classification": None, "reading": None}]
        asyncio.run(svc.SubscriptionRecoveryService()._evaluate_alerts(subs, results, 75))
        assert self._rows(db, a.subject_key(recovered)) == []
        assert len(self._rows(db, a.subject_key(unknown))) == 1
