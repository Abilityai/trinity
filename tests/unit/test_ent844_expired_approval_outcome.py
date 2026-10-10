"""An approval the clock ended says whether the platform held its action
(abilityai/trinity-enterprise#844).

The expiry sweep (`mark_expired`, driven every poll cycle) ends any pending ask
past its deadline. For a gated-skill approval (`raised_by='gate'`) the platform
holds the action: expiry runs nothing. For every other approval — an agent's
own, file or native — the platform holds nothing: the agent may act whether or
not anyone answered. Before this fix both endings were written identically
(`expired` / `timeout`, no reason), so "the approval went stale and nothing
happened" and "the approval was skipped and the action went ahead anyway" left
the same trace. Now the second kind is stamped `disposition_reason =
'outcome_unknown'` in the same compare-and-set that ends it, and the audit row
says so. Existing rows are never rewritten.

Harness: the real per-process SQLite the unit conftest pins; rows are seeded
under agent names unique to this file and every assertion filters to them.
"""
from __future__ import annotations

import os
import sys

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _iso(delta_minutes=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _seed(real_db, agent, rid, *, type_="approval", raised_by=None, channel=None, **over):
    item = {"id": rid, "type": type_, "status": "pending", "priority": "high",
            "title": "Merge the canon PR", "question": "Merge it?",
            "options": ["approve", "reject"], "context": {},
            "created_at": _iso(-60), "expires_at": _iso(-1)}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item, raised_by=raised_by, channel=channel)


def _expire_mine(real_db, agent):
    return {r["id"]: r for r in real_db.mark_operator_queue_expired() if r["agent_name"] == agent}


class TestTheSweepRecordsWhetherTheActionWasHeld:
    @pytest.mark.parametrize("raised_by,channel", [
        ("agent", "mcp"),    # a native ask the agent raised
        ("agent", "file"),   # the file poller's row
        (None, None),        # a legacy row from before the ledger
    ])
    def test_an_approval_the_platform_did_not_hold_expires_as_outcome_unknown(self, real_db, raised_by, channel):
        agent = f"agent-844-unheld-{channel or 'legacy'}"
        uid = _seed(real_db, agent, "a-1", raised_by=raised_by, channel=channel)
        ended = _expire_mine(real_db, agent)
        assert list(ended) == [uid]
        r = ended[uid]
        assert (r["status"], r["disposition"], r["disposed_by"]) == ("expired", "expired", "timeout")
        assert r["disposition_reason"] == "outcome_unknown"
        # and it is what every later read sees, not only the sweep's return
        assert real_db.get_operator_queue_item(uid)["disposition_reason"] == "outcome_unknown"

    def test_a_gate_approval_is_held_by_the_platform_and_carries_no_marker(self, real_db):
        agent = "agent-844-gate"
        uid = _seed(real_db, agent, "g-1", raised_by="gate", channel="gate")
        r = _expire_mine(real_db, agent)[uid]
        assert r["disposition"] == "expired" and r["disposition_reason"] is None

    @pytest.mark.parametrize("type_", ["question", "alert"])
    def test_an_ask_that_names_no_action_carries_no_marker(self, real_db, type_):
        agent = f"agent-844-{type_}"
        uid = _seed(real_db, agent, "q-1", type_=type_, raised_by="agent", channel="mcp")
        r = _expire_mine(real_db, agent)[uid]
        assert r["disposition"] == "expired" and r["disposition_reason"] is None

    def test_an_approval_answered_in_time_is_never_marked(self, real_db):
        agent = "agent-844-answered"
        uid = _seed(real_db, agent, "ans-1", raised_by="agent", channel="mcp", expires_at=_iso(5))
        real_db.respond_to_operator_queue_item(uid, "approve", None, "7", "op@example.com")
        assert _expire_mine(real_db, agent) == {}
        row = real_db.get_operator_queue_item(uid)
        assert row["disposition"] == "answered" and row["disposition_reason"] is None

    def test_the_replace_path_expires_the_same_way_as_the_sweep(self, real_db):
        """#3247 T5b: replacing a pending ask already past its deadline ends it
        as the clock would, on the create's own connection — the same marker."""
        agent = "agent-844-replace"
        old = _seed(real_db, agent, "r-old", raised_by="agent", channel="mcp")
        out = real_db.create_native_operator_queue_item(
            agent, {"id": "r-new", "type": "approval", "priority": "high", "title": "again",
                    "question": "Merge it?", "options": ["approve", "reject"]},
            max_pending=10, channel="mcp", raised_by="agent", to_role=None,
            resolved_to=None, proposal=None, supersedes_expired=None,
            replaces=old, guard_pending_proposal=False,
        )
        assert out["outcome"] == "replaces_ended" and out.get("expired_now") is True
        row = real_db.get_operator_queue_item(old)
        assert row["disposition"] == "expired" and row["disposition_reason"] == "outcome_unknown"


async def _drain():
    import asyncio
    import services.operator_resume_service as ors
    for _ in range(5):
        await asyncio.sleep(0)
        pending = list(ors._inflight)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


@pytest.fixture
def sink(monkeypatch):
    from types import SimpleNamespace
    import services.ask_service as svc

    audit = []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_observers", [])
    return SimpleNamespace(svc=svc, audit=audit)


class TestTheAuditTrailSaysSo:
    @pytest.mark.asyncio
    async def test_the_expiry_audit_row_names_an_unknown_outcome_and_only_that(self, real_db, sink):
        agent = "agent-844-audit"
        unheld = _seed(real_db, agent, "au-1", raised_by="agent", channel="mcp")
        held = _seed(real_db, agent, "au-2", raised_by="gate", channel="gate")
        sink.svc.expire()
        await _drain()
        by_id = {a["target_id"]: a for a in sink.audit if a.get("target_id") in (unheld, held)}
        assert by_id[unheld]["details"]["outcome_unknown"] is True
        assert "outcome_unknown" not in by_id[held]["details"]


# The #3247 sink harness (real SQLite, audit/broadcast/wakes recorded) — reused
# so the replace path is driven through `ask_service.raise_ask`, the way an
# agent reaches it, not only through the db writer.
from unit.test_3247_replace_ask import _Rejected, _body, _raise, ask  # noqa: E402,F401


class TestTheReplacePathAuditSaysSoToo:
    AGENT = "agent-844-replace-audit"

    @pytest.mark.asyncio
    async def test_the_replace_path_expiry_audit_row_names_an_unknown_outcome(self, ask):
        """Review finding (trinity-enterprise#844): the sweep's `expired` audit
        row carried the marker but #3247's in-transaction expiry — the second
        timeout writer — wrote its own `expired` audit row without it, so the
        same bypass read differently depending on which writer the clock used."""
        old = _raise(ask, self.AGENT, _body("ra-old", expires_at=_iso(20)))
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == old["id"])
                         .values(expires_at=_iso(-5)))
        await _drain()
        ask.audit.clear()
        with _Rejected(ask.svc, 409, "replaces_ended"):
            _raise(ask, self.AGENT, _body("ra-new", replaces="ra-old"))
        await _drain()
        [row] = [a for a in ask.audit if a["event_action"] == "expired"]
        assert row["target_id"] == old["id"]
        assert row["details"] == {"agent_name": self.AGENT, "outcome_unknown": True}
        assert ask.db.get_operator_queue_item(old["id"])["disposition_reason"] == "outcome_unknown"
