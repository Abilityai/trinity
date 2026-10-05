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
