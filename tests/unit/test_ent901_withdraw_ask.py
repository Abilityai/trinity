"""Withdraw a pending ask — the agent ends its OWN ask with a reason and no
successor (trinity-enterprise#901).

An ask that became moot (the question answered itself, the action is no longer
needed) must leave the person's Action list without a replacement that asks
again. `ask_service.withdraw` ends the row `cancelled` / `disposed_by='agent'`
/ `disposition_reason=<the agent's reason>` / `replaced_by` NULL through the
compare-and-set the replace path (#3247) already owns — never over a person's
answer, never a row the agent did not raise, never a gate's approval.

Checkpoint A: the db ending door, the sink, the route. Checkpoint B: the prompt
twins and the Pending asks advice (the MCP tool's own tests live in
src/mcp-server). The Workspace / Operating Room readers are checkpoint C.

Related flow: docs/memory/feature-flows/operating-room.md
Requirement: docs/memory/requirements/security.md §26.9 / §26.10

Harness: the unit island's real per-process SQLite (`init_database()` builds the
full schema), the #3247 fixture shape. Agent names are unique to this file.
"""
from __future__ import annotations

import json
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

OWNER = "owner-901@example.com"
RECEIPT_KEYS = {"status", "id", "request_id", "raised_by", "channel", "type", "to_role",
                "resolved", "ask_status", "disposition", "disposed_at", "expires_at",
                "wakes_on_ending", "supersedes_expired",
                "replaces", "replaced_by", "disposed_by",
                "agent_ending"}  # ent#901 — replaced | withdrawn | None, derived once server-side
# Never on an agent's receipt or readback (the ent#611 redaction).
PERSON_FIELDS = {"disposed_by_email", "resolved_to", "addressed_to_email",
                 "responded_by_email", "responded_by_id"}


@pytest.fixture
def real_db():
    from database import db as real
    return real


def _iso(delta_minutes=0):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _native(real_db, agent, rid, *, replaces=None, max_pending=10, proposal=None, **over):
    item = {"id": rid, "type": "approval", "priority": "high", "title": f"Ask {rid}",
            "question": "Release 500 USDC?", "options": ["approve", "reject"]}
    item.update(over)
    return real_db.create_native_operator_queue_item(
        agent, item, max_pending=max_pending, channel="mcp", raised_by="agent",
        to_role=None, resolved_to=None, proposal=proposal, supersedes_expired=None,
        replaces=replaces, guard_pending_proposal=False,
    )


def _filed(real_db, agent, rid, *, raised_by=None, channel=None, **over):
    """A row the sink did not raise: a NULL-raiser platform/file row, or a gate's."""
    item = {"id": rid, "type": "approval", "status": "pending", "priority": "high",
            "title": "Filed", "question": "?", "options": ["approve", "reject"],
            "context": {}, "created_at": _iso()}
    item.update(over)
    return real_db.create_operator_queue_item(agent, item, channel=channel, raised_by=raised_by)


def _withdraw_db(real_db, agent, row_id, reason="no longer needed"):
    return real_db.withdraw_operator_queue_item(agent, row_id, reason=reason)


# ===========================================================================
# 1. The db compare-and-set — the one writer behind replace AND withdraw
# ===========================================================================

class TestWithdrawCas:
    AGENT = "agent-901-cas"

    def _own_pending(self, real_db, rid, **over):
        out = _native(real_db, self.AGENT, rid, **over)
        assert out["outcome"] == "created", out
        return out["row"]

    def test_a_withdraw_ends_the_agents_own_pending_row_with_its_reason(self, real_db):
        row = self._own_pending(real_db, "w-1")
        before = real_db.get_operator_queue_item(row["id"])
        out = _withdraw_db(real_db, self.AGENT, row["id"], reason="the vendor cancelled the order")
        assert out["outcome"] == "withdrawn"
        ended = real_db.get_operator_queue_item(row["id"])
        assert (ended["status"], ended["disposition"], ended["disposed_by"]) == (
            "cancelled", "cancelled", "agent")
        assert ended["disposed_by_email"] is None
        assert ended["disposition_reason"] == "the vendor cancelled the order"
        assert ended["replaced_by"] is None and ended["replaces"] is None
        assert ended["disposed_at"]
        # the call hands back the row AS THE CAS LEFT IT
        assert out["row"]["id"] == row["id"] and out["row"]["disposed_by"] == "agent"
        assert out["row"]["disposition_reason"] == "the vendor cancelled the order"
        # ended, never rewritten: only the ending moved
        ending = {"status", "disposition", "disposed_at", "disposed_by", "disposition_reason"}
        changed = {k for k in before if ended.get(k) != before[k]}
        assert changed == ending, changed ^ ending

    def test_a_person_who_answered_first_wins_and_nothing_is_written(self, real_db):
        row = self._own_pending(real_db, "w-2")
        answered = real_db.respond_to_operator_queue_item(
            row["id"], "approve", "go", None, "op-901@example.com")
        assert not answered.get("_status_conflict")
        out = _withdraw_db(real_db, self.AGENT, row["id"])
        assert out["outcome"] == "ended"
        assert out["row"]["disposition"] == "answered" and out["row"]["response"] == "approve"
        after = real_db.get_operator_queue_item(row["id"])
        assert after["response"] == "approve" and after["disposed_by"] == "person"
        assert after["disposition_reason"] is None

    def test_an_operators_cancel_stands_with_its_own_reason(self, real_db):
        row = self._own_pending(real_db, "w-3")
        real_db.cancel_operator_queue_item(row["id"], disposed_by_email="op-901@example.com",
                                           reason="the operator's words")
        out = _withdraw_db(real_db, self.AGENT, row["id"], reason="mine")
        assert out["outcome"] == "ended" and out["row"]["disposed_by"] == "person"
        after = real_db.get_operator_queue_item(row["id"])
        assert after["disposition_reason"] == "the operator's words"

    def test_an_already_replaced_row_is_refused_as_it_stands(self, real_db):
        old = self._own_pending(real_db, "w-4-old")
        new = _native(real_db, self.AGENT, "w-4-new", replaces=old["id"])["row"]
        out = _withdraw_db(real_db, self.AGENT, old["id"])
        assert out["outcome"] == "ended"
        assert out["row"]["disposed_by"] == "agent" and out["row"]["replaced_by"] == new["id"]
        assert out["row"]["disposition_reason"] == "replaced"

    def test_a_pending_row_past_its_deadline_is_withdrawn_not_expired(self, real_db):
        """H1: a withdraw lands no approval, so "denied by timeout" is not
        sidestepped — the person's cancel has no deadline clause, nor does this."""
        row = self._own_pending(real_db, "w-5", expires_at=_iso(-5))
        assert real_db.get_operator_queue_item(row["id"])["status"] == "pending"
        out = _withdraw_db(real_db, self.AGENT, row["id"], reason="moot")
        assert out["outcome"] == "withdrawn"
        after = real_db.get_operator_queue_item(row["id"])
        assert (after["status"], after["disposed_by"], after["disposition_reason"]) == (
            "cancelled", "agent", "moot")
        # the sweep finds nothing left to end for it
        assert row["id"] not in {r["id"] for r in real_db.mark_operator_queue_expired()}

    def test_when_the_sweep_won_first_the_withdraw_is_refused_as_expired(self, real_db):
        row = self._own_pending(real_db, "w-6", expires_at=_iso(-5))
        assert row["id"] in {r["id"] for r in real_db.mark_operator_queue_expired()}
        out = _withdraw_db(real_db, self.AGENT, row["id"])
        assert out["outcome"] == "ended"
        assert out["row"]["status"] == "expired" and out["row"]["disposed_by"] == "timeout"
        after = real_db.get_operator_queue_item(row["id"])
        assert after["disposed_by"] == "timeout" and after["disposition_reason"] != "no longer needed"

    def test_a_null_raiser_row_under_the_agents_name_is_not_its_own(self, real_db):
        uid = _filed(real_db, self.AGENT, "w-alarm")
        assert real_db.get_operator_queue_item(uid)["raised_by"] is None
        out = _withdraw_db(real_db, self.AGENT, uid)
        assert out["outcome"] == "not_own"
        untouched = real_db.get_operator_queue_item(uid)
        assert untouched["status"] == "pending" and untouched["disposed_by"] is None

    def test_a_gate_row_is_not_the_agents_own(self, real_db):
        uid = _filed(real_db, self.AGENT, "gate-w-1", raised_by="gate", channel="gate")
        out = _withdraw_db(real_db, self.AGENT, uid)
        assert out["outcome"] == "not_own"
        assert real_db.get_operator_queue_item(uid)["status"] == "pending"

    def test_another_agents_row_is_not_found(self, real_db):
        other = _native(real_db, "agent-901-cas-other", "o-1")["row"]
        out = _withdraw_db(real_db, self.AGENT, other["id"])
        assert out["outcome"] == "not_own" and out["row"] is None
        assert real_db.get_operator_queue_item(other["id"])["status"] == "pending"

    def test_a_withdraw_frees_a_depth_slot(self, real_db):
        agent = "agent-901-cap"
        row = _native(real_db, agent, "cap-1", max_pending=1)["row"]
        assert _native(real_db, agent, "cap-2", max_pending=1)["outcome"] == "queue_full"
        assert _withdraw_db(real_db, agent, row["id"])["outcome"] == "withdrawn"
        assert _native(real_db, agent, "cap-3", max_pending=1)["outcome"] == "created"

    def test_the_replace_path_still_ends_the_predecessor_through_the_shared_writer(self, real_db):
        """Regression for the generalised CAS: replace keeps its vocabulary,
        its link, and its deadline clause (T5b)."""
        old = self._own_pending(real_db, "r-old")
        out = _native(real_db, self.AGENT, "r-new", replaces=old["id"])
        assert out["outcome"] == "created"
        ended = real_db.get_operator_queue_item(old["id"])
        assert ended["disposition_reason"] == "replaced" and ended["replaced_by"] == out["row"]["id"]
        late = self._own_pending(real_db, "r-late", expires_at=_iso(-5))
        out = _native(real_db, self.AGENT, "r-late-new", replaces=late["id"])
        assert out["outcome"] == "replaces_ended" and out["expired_now"] is True
        assert real_db.get_operator_queue_item(late["id"])["disposed_by"] == "timeout"

    def test_withdraw_and_replace_of_one_row_have_exactly_one_winner_either_order(self, real_db):
        a = self._own_pending(real_db, "x-a")
        assert _withdraw_db(real_db, self.AGENT, a["id"])["outcome"] == "withdrawn"
        out = _native(real_db, self.AGENT, "x-a-new", replaces=a["id"])
        assert out["outcome"] == "replaces_ended"
        assert out["predecessor"]["disposed_by"] == "agent" and out["predecessor"]["replaced_by"] is None
        assert real_db.get_operator_queue_item_for_agent_by_request_id(self.AGENT, "x-a-new") is None

        b = self._own_pending(real_db, "x-b")
        new = _native(real_db, self.AGENT, "x-b-new", replaces=b["id"])["row"]
        out = _withdraw_db(real_db, self.AGENT, b["id"])
        assert out["outcome"] == "ended" and out["row"]["replaced_by"] == new["id"]
        assert real_db.get_operator_queue_item(b["id"])["disposition_reason"] == "replaced"


# ===========================================================================
# 2. The kind of an agent ending — derived ONCE, from the raw row
# ===========================================================================

class TestAgentEndingKind:
    @pytest.mark.parametrize("row, kind", [
        pytest.param({"disposed_by": "agent", "replaced_by": "u-2", "disposition_reason": "replaced"},
                     "replaced", id="replaced"),
        pytest.param({"disposed_by": "agent", "replaced_by": None, "disposition_reason": "moot"},
                     "withdrawn", id="withdrawn"),
        # M2: a reason that merely SAYS `replaced` is still a withdraw — readers key on the link
        pytest.param({"disposed_by": "agent", "replaced_by": None, "disposition_reason": "replaced"},
                     "withdrawn", id="withdrawn-reason-collides-with-the-token"),
        pytest.param({"disposed_by": "person", "replaced_by": None, "disposition_reason": "a reason"},
                     None, id="person-cancel"),
        pytest.param({"disposed_by": "timeout", "replaced_by": None}, None, id="timeout"),
        pytest.param({"disposed_by": "platform", "replaced_by": None, "disposition_reason": "superseded"},
                     None, id="platform"),
        pytest.param({"disposed_by": None, "replaced_by": None}, None, id="pre-ledger"),
        pytest.param({}, None, id="pending"),
    ])
    def test_the_kind(self, row, kind):
        import services.ask_service as svc
        assert svc.agent_ending_kind(row) == kind

    def test_the_constants(self):
        import services.ask_service as svc
        assert svc.WITHDRAWN == "withdrawn" and svc.REPLACED == "replaced"
        assert set(svc.AGENT_ENDING_KINDS) == {"replaced", "withdrawn"}


# ===========================================================================
# 3. The sink — ask_service.withdraw
# ===========================================================================

class _Rejected:
    def __init__(self, svc, status, code):
        self.svc, self.status, self.code = svc, status, code

    def __enter__(self):
        self._cm = pytest.raises(self.svc.AskRejected)
        self._info = self._cm.__enter__()
        return self._info

    def __exit__(self, *exc):
        ok = self._cm.__exit__(*exc)
        assert (self._info.value.status_code, self._info.value.code) == (self.status, self.code)
        return ok


@pytest.fixture
def ask(real_db, monkeypatch):
    """The real sink over the real SQLite; the world around it stubbed (the
    ent#611 fixture): audit + broadcast recorded, owner, thread, opt-in, rate."""
    from types import SimpleNamespace
    import services.ask_service as svc
    import services.operator_queue_service as oqs
    import services.operator_resume_service as ors
    from services import assignment_provider
    from services.rate_limiter import RateLimitResult

    audit, sent, events, wakes = [], [], [], []
    state = {"owner": OWNER, "rate_ok": True, "opted_in": False}

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: state["owner"])
    monkeypatch.setattr(oqs, "_workspace_attachment", lambda agent, email, **_: (f"thread-{email}", False))
    monkeypatch.setattr(real_db, "get_operator_resume_enabled", lambda agent: state["opted_in"], raising=False)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(state["rate_ok"], 10, 0, 60))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: wakes.append(("ending", rows, kw)))
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: wakes.append(("resume", item, kw)))
    svc.register_ending_observer(events.append)
    assignment_provider.clear_provider()
    yield SimpleNamespace(svc=svc, audit=audit, sent=sent, events=events, wakes=wakes,
                          state=state, db=real_db)
    svc._observers.remove(events.append)
    assignment_provider.clear_provider()


async def _drain():
    import asyncio
    import services.operator_resume_service as ors
    for _ in range(5):
        await asyncio.sleep(0)
        pending = list(ors._inflight)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _body(request_id, **over):
    b = {"request_id": request_id, "type": "approval", "title": "Pay invoice",
         "question": "Release 500 USDC to the vendor?", "options": ["approve", "reject"],
         "proposal": {"pay": 500, "to": f"vendor-{request_id}"}}
    b.update(over)
    return b


def _raise(ask, agent, body):
    return ask.svc.raise_ask(agent, body, raised_by="agent", channel="mcp")


def _withdraw(ask, agent, request_id, reason="the order was cancelled upstream"):
    return ask.svc.withdraw(agent, request_id, reason)


class TestWithdrawSink:
    AGENT = "agent-901-sink"

    @pytest.mark.asyncio
    async def test_a_withdraw_ends_the_ask_records_it_and_announces_it(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-1"))
        await _drain()                       # the `raised` announcement lands first
        ask.audit.clear(); ask.sent.clear()
        r = _withdraw(ask, self.AGENT, "ws-1", reason="the order was cancelled upstream")
        await _drain()
        assert set(r) == RECEIPT_KEYS, set(r) ^ RECEIPT_KEYS
        assert r["status"] == "withdrawn" and r["id"] == raised["id"]
        assert (r["ask_status"], r["disposition"], r["disposed_by"]) == ("cancelled", "cancelled", "agent")
        assert r["agent_ending"] == "withdrawn" and r["replaced_by"] is None
        assert not PERSON_FIELDS & set(r)
        ended = ask.db.get_operator_queue_item(raised["id"])
        assert ended["disposition_reason"] == "the order was cancelled upstream"
        assert ended["disposed_by_email"] is None and ended["replaced_by"] is None
        # the ending reached the observers with the row AS THE CAS LEFT IT
        [ev] = [e for e in ask.events if e.disposition == "cancelled"]
        assert ev.reason == "the order was cancelled upstream" and ev.actor_email is None
        assert ev.rows[0]["id"] == raised["id"] and ev.rows[0]["disposed_by"] == "agent"
        assert len(ev.rows) == 1
        # one audit row, agent-keyed, ids and enums only — never the reason's text
        [row] = ask.audit
        assert row["event_action"] == "withdrawn" and row["source"] == "api"
        assert row["actor_agent_name"] == self.AGENT and row["target_id"] == raised["id"]
        assert row["details"] == {"agent_name": self.AGENT, "request_id": "ws-1", "has_reason": True}
        assert "actor_user" not in row
        assert "cancelled upstream" not in json.dumps(ask.audit)
        # one thin trigger: ids only, scoped to the agent
        assert ask.sent == [{"type": "operator_queue_cancelled",
                             "data": {"id": raised["id"], "agent_name": self.AGENT}}]

    def test_a_withdraw_wakes_nobody_even_when_the_owner_opted_in(self, ask):
        ask.state["opted_in"] = True
        _raise(ask, self.AGENT, _body("ws-wake"))
        r = _withdraw(ask, self.AGENT, "ws-wake")
        assert r["status"] == "withdrawn"
        assert ask.wakes == []
        # the receipt still says whether a PERSON's ending would have woken it
        assert r["wakes_on_ending"] is True

    def test_the_reason_is_whitespace_normalised_at_ingest(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-norm"))
        _withdraw(ask, self.AGENT, "ws-norm", reason="  the\tvendor \n  cancelled   ")
        assert ask.db.get_operator_queue_item(raised["id"])["disposition_reason"] == "the vendor cancelled"

    @pytest.mark.parametrize("reason", ["", "   ", "\n\t", "x" * 501, None, 42],
                             ids=["empty", "blank", "whitespace", "too-long", "none", "not-text"])
    def test_a_missing_blank_or_oversized_reason_is_refused_and_nothing_is_written(self, ask, reason):
        raised = _raise(ask, self.AGENT, _body(f"ws-bad-{abs(hash(str(reason))) % 10**6}"))
        with _Rejected(ask.svc, 422, "invalid_reason") as info:
            ask.svc.withdraw(self.AGENT, raised["request_id"], reason)
        assert info.value.extra == {"limit": 500}
        assert ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_a_500_char_reason_is_accepted(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-max"))
        r = _withdraw(ask, self.AGENT, "ws-max", reason="y" * 500)
        assert r["status"] == "withdrawn"
        assert ask.db.get_operator_queue_item(raised["id"])["disposition_reason"] == "y" * 500

    def test_an_unknown_request_id_is_a_uniform_404(self, ask):
        with _Rejected(ask.svc, 404, "ask_not_found") as info:
            _withdraw(ask, self.AGENT, "ws-nothing")
        assert info.value.extra == {}
        assert "ws-nothing" not in info.value.message

    def test_another_agents_ask_is_the_same_404(self, ask):
        other = "agent-901-sink-other"
        raised = _raise(ask, other, _body("ws-theirs"))
        with _Rejected(ask.svc, 404, "ask_not_found"):
            _withdraw(ask, self.AGENT, "ws-theirs")
        assert ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_a_gate_approval_is_the_same_404_and_stays_pending(self, ask):
        uid = _filed(ask.db, self.AGENT, "gate-ws-1", raised_by="gate", channel="gate")
        with _Rejected(ask.svc, 404, "ask_not_found"):
            _withdraw(ask, self.AGENT, "gate-ws-1")
        assert ask.db.get_operator_queue_item(uid)["status"] == "pending"
        assert ask.events == []

    def test_a_platform_row_about_the_agent_is_the_same_404(self, ask):
        uid = _filed(ask.db, self.AGENT, "retention-guard-ws")
        assert ask.db.get_operator_queue_item(uid)["raised_by"] is None
        with _Rejected(ask.svc, 404, "ask_not_found"):
            _withdraw(ask, self.AGENT, "retention-guard-ws")
        assert ask.db.get_operator_queue_item(uid)["status"] == "pending"

    def test_a_row_about_a_person_is_the_same_404(self, ask, monkeypatch):
        import services.operator_queue_service as oqs
        raised = _raise(ask, self.AGENT, _body("ws-person"))
        monkeypatch.setattr(oqs, "is_about_a_person", lambda item: item["id"] == raised["id"])
        with _Rejected(ask.svc, 404, "ask_not_found"):
            _withdraw(ask, self.AGENT, "ws-person")
        assert ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_a_person_who_answered_first_wins_with_ids_and_enums_only(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-answered"))
        ask.db.respond_to_operator_queue_item(raised["id"], "approve", "the secret words", None,
                                              "op-901@example.com")
        with _Rejected(ask.svc, 409, "ask_ended") as info:
            _withdraw(ask, self.AGENT, "ws-answered")
        extra = info.value.extra
        assert extra == {"request_id": "ws-answered", "ask_status": "responded",
                         "disposition": "answered", "disposed_by": "person",
                         "disposed_at": extra["disposed_at"], "replaced_by": None}
        assert "secret" not in str(extra) and "secret" not in info.value.message
        assert "get_my_ask" in info.value.message
        after = ask.db.get_operator_queue_item(raised["id"])
        assert after["response"] == "approve" and after["disposition_reason"] is None

    def test_an_operators_cancel_is_named_as_such(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-cancelled"))
        ask.db.cancel_operator_queue_item(raised["id"], disposed_by_email="op-901@example.com",
                                          reason="the operator's words")
        with _Rejected(ask.svc, 409, "ask_ended") as info:
            _withdraw(ask, self.AGENT, "ws-cancelled")
        assert info.value.extra["disposed_by"] == "person" and "operator" in info.value.message
        assert "operator's words" not in info.value.message

    def test_an_already_replaced_ask_names_its_successor(self, ask):
        _raise(ask, self.AGENT, _body("ws-rep-old"))
        _raise(ask, self.AGENT, _body("ws-rep-new", replaces="ws-rep-old"))
        with _Rejected(ask.svc, 409, "ask_ended") as info:
            _withdraw(ask, self.AGENT, "ws-rep-old")
        assert info.value.extra["replaced_by"] == "ws-rep-new" and "ws-rep-new" in info.value.message
        assert info.value.extra["disposed_by"] == "agent"

    def test_an_expired_ask_is_named_as_denied_by_timeout(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-exp", expires_at=_iso(20)))
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == raised["id"])
                         .values(expires_at=_iso(-5)))
        assert raised["id"] in {r["id"] for r in ask.db.mark_operator_queue_expired()}
        with _Rejected(ask.svc, 409, "ask_ended") as info:
            _withdraw(ask, self.AGENT, "ws-exp")
        assert info.value.extra["disposition"] == "expired" and "supersedes_expired" in info.value.message

    def test_a_pending_ask_past_its_deadline_is_withdrawn_like_a_persons_cancel(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-late", expires_at=_iso(20)))
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import operator_queue
        with get_engine().begin() as conn:
            conn.execute(update(operator_queue).where(operator_queue.c.id == raised["id"])
                         .values(expires_at=_iso(-5)))
        r = _withdraw(ask, self.AGENT, "ws-late", reason="moot")
        assert r["status"] == "withdrawn" and r["disposition"] == "cancelled"
        assert [e.disposition for e in ask.events] == ["cancelled"]
        # and a later raise of the same proposal is not called a re-ask of a denial (C6)
        again = _raise(ask, self.AGENT, _body("ws-late-again", proposal={"pay": 500, "to": "vendor-ws-late"}))
        assert again["status"] == "created"

    @pytest.mark.asyncio
    async def test_a_repeated_withdraw_replays_without_a_second_ending(self, ask):
        raised = _raise(ask, self.AGENT, _body("ws-twice"))
        first = _withdraw(ask, self.AGENT, "ws-twice", reason="moot")
        await _drain()
        ask.audit.clear(); ask.sent.clear(); ask.events.clear()
        again = _withdraw(ask, self.AGENT, "ws-twice", reason="moot")
        await _drain()
        assert again["status"] == "replayed" and again["id"] == first["id"]
        assert again["differs"] == [] and again["agent_ending"] == "withdrawn"
        assert set(again) == RECEIPT_KEYS | {"differs"}
        assert ask.audit == [] and ask.sent == [] and ask.events == []
        # a different reason on the retry is reported, never applied
        other = _withdraw(ask, self.AGENT, "ws-twice", reason="a new story")
        assert other["status"] == "replayed" and other["differs"] == ["reason"]
        assert ask.db.get_operator_queue_item(raised["id"])["disposition_reason"] == "moot"

    def test_withdrawing_a_successor_leaves_the_predecessors_link_as_history(self, ask):
        old = _raise(ask, self.AGENT, _body("ws-chain-old"))
        new = _raise(ask, self.AGENT, _body("ws-chain-new", replaces="ws-chain-old"))
        r = _withdraw(ask, self.AGENT, "ws-chain-new")
        assert r["status"] == "withdrawn" and r["replaces"] == "ws-chain-old"
        assert ask.svc.agent_ending_kind(ask.db.get_operator_queue_item(new["id"])) == "withdrawn"
        pred = ask.db.get_operator_queue_item(old["id"])
        assert pred["replaced_by"] == new["id"]
        assert ask.svc.agent_ending_kind(pred) == "replaced"

    def test_a_replace_refuses_a_withdrawn_predecessor_naming_the_withdraw(self, ask):
        _raise(ask, self.AGENT, _body("ws-wd-old"))
        _withdraw(ask, self.AGENT, "ws-wd-old")
        with _Rejected(ask.svc, 409, "replaces_ended") as info:
            _raise(ask, self.AGENT, _body("ws-wd-new", replaces="ws-wd-old"))
        assert info.value.extra["disposed_by"] == "agent" and info.value.extra["replaced_by"] is None
        assert "withdr" in info.value.message and "replaced by" not in info.value.message

    def test_the_gate_observer_ignores_a_withdraw(self, ask, monkeypatch):
        """S2: the gate observers filter to gate-raised rows, which this door
        cannot reach — a withdraw event spawns nothing for them."""
        import services.operator_resume_service as ors
        from services import skill_gate_service
        spawned = []
        monkeypatch.setattr(ors, "spawn_on_loop", lambda fn: spawned.append(fn))
        _raise(ask, self.AGENT, _body("ws-gate-obs"))
        _withdraw(ask, self.AGENT, "ws-gate-obs")
        [ev] = [e for e in ask.events if e.disposition == "cancelled"]
        spawned.clear()
        skill_gate_service.on_ending(ev)
        assert spawned == []

    def test_a_replay_of_a_withdrawn_ask_through_raise_says_who_ended_it(self, ask):
        _raise(ask, self.AGENT, _body("ws-re-raise"))
        _withdraw(ask, self.AGENT, "ws-re-raise")
        r = _raise(ask, self.AGENT, _body("ws-re-raise"))
        assert r["status"] == "replayed"
        assert (r["disposition"], r["disposed_by"], r["agent_ending"]) == ("cancelled", "agent", "withdrawn")


# ===========================================================================
# 4. The route — POST /api/agents/{name}/operator-queue/{request_id}/withdraw
# ===========================================================================

_ROUTE_APP = None
_PRINCIPAL = {"user": None}


def _principal(**kw):
    from models import User
    base = {"id": 7, "username": "op", "email": "op@example.com", "role": "admin"}
    base.update(kw)
    return User(**base)


def _route_client():
    """ONE app over both operator-queue routers; `get_current_user` overridden by
    walking the routes' own dependant trees (never a fresh import)."""
    global _ROUTE_APP
    from fastapi.testclient import TestClient
    if _ROUTE_APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        app.include_router(r.agent_router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in list(r.router.routes) + list(r.agent_router.routes):
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the operator-queue routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _ROUTE_APP = app
    return TestClient(_ROUTE_APP, raise_server_exceptions=True)


class TestWithdrawRoute:
    AGENT = "agent-901-route"

    @pytest.fixture
    def route(self, ask, monkeypatch):
        from types import SimpleNamespace
        from routers import operator_queue as r
        monkeypatch.setattr(r, "_websocket_manager", None)
        mine = {self.AGENT, "trinity-system"}
        real_owner, real_access = ask.db.get_agent_owner, ask.db.can_user_access_agent
        monkeypatch.setattr(ask.db, "get_agent_owner",
                            lambda name: {"owner_username": "op"} if name in mine else real_owner(name))
        monkeypatch.setattr(ask.db, "can_user_access_agent",
                            lambda user, name: True if name in mine else real_access(user, name))

        def as_(**principal):
            _PRINCIPAL["user"] = _principal(**principal)

        as_(mcp_scope="agent", agent_name=self.AGENT)
        return SimpleNamespace(client=_route_client(), as_=as_, ask=ask)

    def _withdraw(self, route, rid, body=None, agent=None):
        return route.client.post(
            f"/api/agents/{agent or self.AGENT}/operator-queue/{rid}/withdraw",
            json={"reason": "no longer needed"} if body is None else body)

    def test_the_agent_withdraws_its_own_ask_and_gets_a_200_receipt(self, route):
        _raise(route.ask, self.AGENT, _body("rt-1"))
        res = self._withdraw(route, "rt-1")
        assert res.status_code == 200, res.text
        body = res.json()
        assert set(body) == RECEIPT_KEYS and body["status"] == "withdrawn"
        assert body["agent_ending"] == "withdrawn" and body["disposed_by"] == "agent"
        assert not PERSON_FIELDS & set(body)

    def test_a_retry_is_a_200_replay(self, route):
        _raise(route.ask, self.AGENT, _body("rt-2"))
        first = self._withdraw(route, "rt-2").json()
        res = self._withdraw(route, "rt-2", {"reason": "another story"})
        assert res.status_code == 200 and res.json()["status"] == "replayed"
        assert res.json()["id"] == first["id"] and res.json()["differs"] == ["reason"]

    @pytest.mark.parametrize("principal", [
        pytest.param({"mcp_scope": "agent", "agent_name": "agent-901-sibling"}, id="another-agents-key"),
        pytest.param({}, id="a-person"),
        pytest.param({"mcp_scope": "user"}, id="a-user-key"),
        pytest.param({"mcp_scope": "system"}, id="system-key-for-another-agent"),
        pytest.param({"mcp_scope": "connector"}, id="a-connector"),
    ])
    def test_only_the_agent_itself_may_withdraw(self, route, principal):
        raised = _raise(route.ask, self.AGENT, _body("rt-who"))
        route.as_(**principal)
        res = self._withdraw(route, "rt-who")
        assert res.status_code == 403 and res.json()["detail"]["code"] == "agent_identity_required"
        assert route.ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_an_unknown_ask_is_a_404_with_the_named_code(self, route):
        res = self._withdraw(route, "rt-nothing")
        assert res.status_code == 404
        assert res.json()["detail"]["code"] == "ask_not_found"

    def test_an_answered_ask_is_a_409_naming_the_row(self, route):
        raised = _raise(route.ask, self.AGENT, _body("rt-answered"))
        route.ask.db.respond_to_operator_queue_item(raised["id"], "approve", "go", None,
                                                    "op-901@example.com")
        res = self._withdraw(route, "rt-answered")
        assert res.status_code == 409, res.text
        detail = res.json()["detail"]
        assert detail["code"] == "ask_ended" and detail["request_id"] == "rt-answered"
        assert detail["disposition"] == "answered" and "message" in detail
        assert not PERSON_FIELDS & set(detail)

    def test_a_blank_reason_is_a_422_with_the_named_code(self, route):
        raised = _raise(route.ask, self.AGENT, _body("rt-blank"))
        res = self._withdraw(route, "rt-blank", {"reason": "   "})
        assert res.status_code == 422 and res.json()["detail"]["code"] == "invalid_reason"
        assert route.ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_an_unknown_body_field_is_refused(self, route):
        raised = _raise(route.ask, self.AGENT, _body("rt-extra"))
        res = self._withdraw(route, "rt-extra", {"reason": "x", "replaced_by": "rt-1"})
        assert res.status_code == 422
        assert route.ask.db.get_operator_queue_item(raised["id"])["status"] == "pending"

    def test_the_readback_shows_the_withdraw_like_a_replace(self, route):
        _raise(route.ask, self.AGENT, _body("rt-read"))
        self._withdraw(route, "rt-read", {"reason": "the order was cancelled"})
        res = route.client.get(f"/api/agents/{self.AGENT}/operator-queue/rt-read")
        assert res.status_code == 200
        body = res.json()
        assert (body["disposition"], body["disposed_by"], body["agent_ending"]) == (
            "cancelled", "agent", "withdrawn")
        assert body["disposition_reason"] == "the order was cancelled" and body["replaced_by"] is None
        assert not PERSON_FIELDS & set(body)

    def test_the_route_is_in_the_census_as_agent_callable(self):
        """The #2996 census is the wiring pin: the runtime half asserts every
        listed route is on the live app."""
        sys.path.insert(0, os.path.dirname(__file__))
        import _route_census as rc
        key = "routers/operator_queue.py::withdraw_my_ask"
        assert key in rc.AGENT_CALLABLE
        assert rc.AGENT_CALLABLE[key][0] == "POST /api/agents/{name}/operator-queue/{request_id}/withdraw"


# ===========================================================================
# 5. The prompt contract (checkpoint B) — both authored twins carry the same
#    "Withdraw what is moot." paragraph right after "Do not re-ask", and the
#    Pending asks line offers the withdraw beside the replace.
# ===========================================================================

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
WITHDRAW_LEAD = "**Withdraw what is moot.**"
REASK_LEAD = "**Do not re-ask what is still pending.**"


def _paragraphs(text):
    return [p for p in text.split("\n\n") if p.strip()]


class TestPromptContract:
    def _meta(self):
        with open(os.path.join(_REPO, "config/trinity-meta-prompt/prompt.md"), encoding="utf-8") as f:
            return f.read()

    def test_both_twins_carry_the_paragraph_byte_identically(self):
        from services.platform_prompt_service import PLATFORM_INSTRUCTIONS
        ours = [p for p in _paragraphs(PLATFORM_INSTRUCTIONS) if p.startswith(WITHDRAW_LEAD)]
        theirs = [p for p in _paragraphs(self._meta()) if p.startswith(WITHDRAW_LEAD)]
        assert len(ours) == 1 and ours == theirs

    @pytest.mark.parametrize("which", ["platform", "meta"])
    def test_the_paragraph_follows_do_not_reask(self, which):
        from services.platform_prompt_service import PLATFORM_INSTRUCTIONS
        paras = _paragraphs(PLATFORM_INSTRUCTIONS if which == "platform" else self._meta())
        i = next(n for n, p in enumerate(paras) if p.startswith(REASK_LEAD))
        assert paras[i + 1].startswith(WITHDRAW_LEAD)

    def test_the_paragraph_names_the_tool_the_refusal_and_the_rule(self):
        from services.platform_prompt_service import PLATFORM_INSTRUCTIONS
        para = next(p for p in _paragraphs(PLATFORM_INSTRUCTIONS) if p.startswith(WITHDRAW_LEAD))
        for token in ("`withdraw_ask`", "`request_id`", "`reason`", "`replaces`",
                      "`ask_ended`", "`get_my_ask`", "queue-file entry cannot be withdrawn"):
            assert token in para, token
        assert "mcp__trinity__" not in para


class TestPendingLineOffersWithdraw:
    AGENT = "agent-901-line"

    def _line(self, prompt):
        return next((l for l in prompt.splitlines() if "Pending asks" in l), None)

    def test_an_own_pending_ask_is_offered_the_withdraw(self, real_db):
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        _native(real_db, self.AGENT, "line-own-1")
        line = self._line(compose_system_prompt(
            ExecutionContext(agent_name=self.AGENT, triggered_by="schedule")))
        assert line is not None
        assert "replace it (ask_operator, replaces)" in line
        assert "if it is moot, withdraw it (withdraw_ask)" in line

    def test_a_line_with_nothing_of_its_own_offers_no_withdraw(self, real_db):
        """A NULL-raiser row is not the agent's to withdraw (the sink 404s it),
        so a line of only such rows offers neither the replace nor the withdraw."""
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        agent = "agent-901-line-legacy"
        _filed(real_db, agent, "line-legacy-1")
        line = self._line(compose_system_prompt(
            ExecutionContext(agent_name=agent, triggered_by="schedule")))
        assert line is not None and "line-legacy-1" in line
        assert "withdraw" not in line


# ===========================================================================
# 6. The readers (checkpoint C) — every surface that maps an agent ending says
#    `withdrawn` for a withdraw, keyed on the derived kind, never on the reason
#    token; the reason reaches the person asked, never the Execution Context.
# ===========================================================================

def _withdrawn_row(real_db, agent, rid, reason="The build fixed itself.", **over):
    row = _native(real_db, agent, rid, **over)["row"]
    out = _withdraw_db(real_db, agent, row["id"], reason=reason)
    assert out["outcome"] == "withdrawn", out
    return real_db.get_operator_queue_item(row["id"])


class TestWorkspaceProjection:
    AGENT = "agent-901-ws"

    @pytest.fixture(autouse=True)
    def _roster(self, monkeypatch):
        import client_portal.service as portal_service
        monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a, **k: True)

    def test_a_withdraw_projects_its_kind_and_the_agents_reason(self, real_db):
        from client_portal.asks.service import _project
        to = "ws901-a@example.com"
        row = _withdrawn_row(real_db, self.AGENT, "ws-w-a", addressed_to_email=to)
        p = _project(row, viewer_email=to)
        assert (p.status, p.ended_by, p.agent_ending) == ("cancelled", "agent", "withdrawn")
        assert p.ending_reason == "The build fixed itself."
        assert p.ended_at == row["disposed_at"]
        assert p.replaced_by is None

    def test_a_reason_that_reads_replaced_is_still_a_withdraw(self, real_db):
        from client_portal.asks.service import _project
        to = "ws901-b@example.com"
        row = _withdrawn_row(real_db, self.AGENT, "ws-w-b", reason="replaced", addressed_to_email=to)
        p = _project(row, viewer_email=to)
        assert (p.agent_ending, p.ending_reason) == ("withdrawn", "replaced")

    def test_a_replace_carries_no_reason(self, real_db):
        from client_portal.asks.service import _project
        to = "ws901-c@example.com"
        old = _native(real_db, self.AGENT, "ws-r-old", addressed_to_email=to)["row"]
        assert _native(real_db, self.AGENT, "ws-r-new", replaces=old["id"],
                       addressed_to_email=to)["outcome"] == "created"
        p = _project(real_db.get_operator_queue_item(old["id"]), viewer_email=to)
        assert (p.ended_by, p.agent_ending, p.ending_reason) == ("agent", "replaced", None)

    def test_no_other_ending_forwards_a_reason(self, real_db):
        """The operator's cancel reason is theirs (§26.9): never on the projection."""
        from client_portal.asks.service import _project
        to = "ws901-d@example.com"
        row = _native(real_db, self.AGENT, "ws-p-d", addressed_to_email=to)["row"]
        real_db.cancel_operator_queue_item(row["id"], disposed_by_email="op@example.com",
                                           reason="Not needed, I did it myself")
        p = _project(real_db.get_operator_queue_item(row["id"]), viewer_email=to)
        assert (p.ended_by, p.agent_ending, p.ending_reason) == ("operator", None, None)
        for ended in ({"status": "expired", "disposition": "expired", "disposed_by": "timeout",
                       "disposition_reason": "x"},
                      {"status": "cancelled", "disposition": "cancelled", "disposed_by": "platform",
                       "disposition_reason": "condition_cleared"}):
            p = _project({**row, **ended}, viewer_email=to)
            assert (p.agent_ending, p.ending_reason) == (None, None), ended

    def test_a_withdrawn_ask_leaves_the_open_list_and_shows_as_history(self, real_db):
        """AC2/AC3: no longer Waiting for the person asked; still on their
        ended history with the reason. (Delivery is at parity with a person's
        cancel — the Workspace refetches; nothing here claims a push.)"""
        from client_portal.asks.service import list_asks
        to = "ws901-e@example.com"
        _withdrawn_row(real_db, self.AGENT, "ws-w-e", addressed_to_email=to)
        assert all(a.id != "ws-w-e" and a.status != "pending" or a.title != "Ask ws-w-e"
                   for a in list_asks(to, False, self.AGENT))
        ended = [a for a in list_asks(to, False, self.AGENT, include_ended=True)
                 if a.title == "Ask ws-w-e"]
        assert len(ended) == 1
        assert (ended[0].status, ended[0].agent_ending) == ("cancelled", "withdrawn")
        assert ended[0].ending_reason == "The build fixed itself."

    def test_answering_an_ask_the_agent_just_withdrew_says_so(self, real_db, monkeypatch):
        import client_portal.asks.service as asks
        to = "ws901-race@example.com"
        row = _native(real_db, self.AGENT, "ws-w-race", addressed_to_email=to)["row"]
        stale = dict(real_db.get_operator_queue_item(row["id"]))   # what the page read
        assert _withdraw_db(real_db, self.AGENT, row["id"])["outcome"] == "withdrawn"
        monkeypatch.setattr(asks, "_owned_ask", lambda *a, **k: stale)
        with pytest.raises(asks.AskError) as ei:
            asks.answer_ask(row["id"], to, False, "approve", None)
        assert (ei.value.status_code, ei.value.code) == (409, "already_resolved")
        assert "withdrew" in ei.value.detail
        assert "replaced" not in ei.value.detail and "answered" not in ei.value.detail
        fresh = real_db.get_operator_queue_item(row["id"])
        assert fresh["disposed_by"] == "agent" and fresh["response"] is None


class TestEndedAsksLine:
    AGENT = "agent-901-ended-line"

    def _line(self, prompt):
        return next((l for l in prompt.splitlines() if "Ended asks" in l), None)

    def test_a_withdraw_reads_withdrawn_and_never_carries_the_reason(self, real_db):
        from services.platform_prompt_service import ExecutionContext, compose_system_prompt
        _withdrawn_row(real_db, self.AGENT, "el-w-1", reason="replaced because SECRET-901")
        old = _native(real_db, self.AGENT, "el-r-old")["row"]
        assert _native(real_db, self.AGENT, "el-r-new", replaces=old["id"])["outcome"] == "created"
        line = self._line(compose_system_prompt(
            ExecutionContext(agent_name=self.AGENT, triggered_by="schedule")))
        assert line is not None
        assert "el-w-1 withdrawn" in line
        assert "el-r-old replaced" in line
        assert "SECRET-901" not in line and "because" not in line

    def test_the_read_selects_the_link_and_still_no_text(self, real_db):
        from utils.helpers import iso_cutoff
        agent = "agent-901-ended-read"
        _withdrawn_row(real_db, agent, "er-w-1")
        rows = real_db.list_recent_operator_queue_endings(agent, iso_cutoff(hours=24), 10)
        assert rows and all(
            set(r) == {"request_id", "disposition", "disposed_at", "disposed_by", "replaced_by"}
            for r in rows)


class TestOperatorView:
    def test_the_person_view_carries_the_derived_kind(self, real_db):
        from types import SimpleNamespace
        import routers.operator_queue as r
        agent = "agent-901-op-view"
        row = _withdrawn_row(real_db, agent, "op-w-1")
        person = SimpleNamespace(email="op@example.com", mcp_scope=None, agent_name=None,
                                 role="admin", id=1, username="op")
        out = r._for_principal([row], person) if r.is_person_principal(person) else None
        assert out is not None, "fixture is not a person principal"
        assert out[0]["agent_ending"] == "withdrawn"
