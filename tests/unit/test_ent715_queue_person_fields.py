"""trinity-enterprise#715 — the operator queue stops handing a person's email to
a machine key.

A machine principal (an agent-, system-, ops-, connector- or portal_delegate
key, a user-scoped key carrying an agent identity, and whatever scope ships
next) reads the queue for its own work. It never learns who answered, whom an
ask was for, or who a Workspace client is. Four doors:

  1. `GET /api/operator-queue`, `/{id}` and `/agents/{name}` give a machine the
     ALLOWLIST projection. Its key set is pinned below as a literal, and so is
     the withheld set: a column added tomorrow turns this file red until
     someone decides which side of the line it is on (learning 2026-09-23 — a
     "whole row minus the withheld fields" diff cannot catch a new leak).
  2. The platform's heads-ups ABOUT a person (the ent#499 problem report, the
     ent#308 inbox collision) are not returned to a machine at all.
  3. The file write-back no longer writes `responded_by`, on either path, and
     the collision alert is a platform alarm, never written into the agent's
     own file.
  4. The clear-resolved `/ws` trigger no longer carries the operator's email.

A person — a JWT session or the person's own user-scoped key — still reads
every field.

Related flow: docs/memory/feature-flows/operating-room.md
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Test-local literals, never imported from the router: importing the code's own
# tuple would make the classification check circular.
WITHHELD = frozenset({
    "responded_by_id", "responded_by_email", "addressed_to_email",
    "disposed_by_email", "resolved_to",
})
MACHINE_ROW_KEYS = frozenset({
    "id", "agent_name", "request_id", "type", "status", "priority", "title",
    "question", "options", "context", "execution_id", "created_at", "expires_at",
    "response", "response_text", "responded_at", "acknowledged_at", "cleared_at",
    "sync_state", "sync_detail", "sync_updated_at", "last_confirmed_at",
    "delivery_state", "delivery_detail", "delivery_updated_at",
    "divergence_acknowledged_at",
    "disposition", "disposed_at", "disposed_by", "disposition_reason", "batch_id",
    "raised_by", "channel", "to_role", "proposal", "supersedes_expired",
    "aging", "aged_since",
    "subject", "last_seen_at",
})

AGENT = "agent-715-reads"
OP_EMAIL = "op-715@example.com"
FOR_EMAIL = "client-715@example.com"
RESOLVED_EMAIL = "ceo-715@example.com"

PERSON_PRINCIPALS = [
    pytest.param({}, id="jwt"),
    pytest.param({"mcp_scope": "user"}, id="user-key"),
]
MACHINE_PRINCIPALS = [
    pytest.param({"mcp_scope": "agent", "agent_name": AGENT}, id="agent-key"),
    pytest.param({"mcp_scope": "system"}, id="system-key"),
    pytest.param({"mcp_scope": "ops"}, id="ops-key"),
    pytest.param({"mcp_scope": "connector", "connector_agent": AGENT}, id="connector-key"),
    pytest.param({"mcp_scope": "portal_delegate", "portal_delegate": True}, id="portal-delegate-key"),
    pytest.param({"mcp_scope": "user", "agent_name": AGENT}, id="user-scope-with-agent-identity"),
    pytest.param({"mcp_scope": "a-scope-that-ships-tomorrow"}, id="unknown-scope"),
]
ROUTES = ["list", "item", "agent_list"]


def _rid(prefix="r715"):
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


@pytest.fixture
def real_db():
    from database import db
    return db


# ---------------------------------------------------------------------------
# One app over the real router; `get_current_user` overridden by walking the
# routes' own dependant trees (learning 2026-09-23: override the dependency the
# route captured, never a fresh import).
# ---------------------------------------------------------------------------

_APP = None
_PRINCIPAL = {"user": None}


def _client():
    global _APP
    from fastapi.testclient import TestClient
    if _APP is None:
        from fastapi import FastAPI
        from routers import operator_queue as r
        app = FastAPI()
        app.include_router(r.router)
        found = set()

        def walk(dependant):
            for sub in dependant.dependencies:
                if getattr(sub.call, "__name__", "") == "get_current_user":
                    found.add(sub.call)
                walk(sub)

        for route in r.router.routes:
            if getattr(route, "dependant", None) is not None:
                walk(route.dependant)
        assert found, "no get_current_user dependency on the operator-queue routes"
        for call in found:
            app.dependency_overrides[call] = lambda: _PRINCIPAL["user"]
        _APP = app
    return TestClient(_APP, raise_server_exceptions=True)


def _as(**principal):
    from models import User
    base = {"id": 7, "username": "op-715", "email": OP_EMAIL, "role": "admin"}
    base.update(principal)
    _PRINCIPAL["user"] = User(**base)


def _seed(real_db, *, channel, agent=AGENT):
    """An answered row carrying EVERY person field, each asserted non-null — the
    redaction below is proven on real values, never on NULLs."""
    from sqlalchemy import update
    from db.engine import get_engine
    from db.tables import operator_queue

    uid = real_db.create_operator_queue_item(agent, {
        "id": _rid(), "type": "approval", "status": "pending", "priority": "high",
        "title": "Approve payout", "question": "Release 500 USDC?",
        "options": ["approve", "reject"], "context": {},
        "created_at": "2026-09-01T10:00:00Z", "addressed_to_email": FOR_EMAIL,
    }, channel=channel, raised_by="agent")
    real_db.respond_to_operator_queue_item(uid, "approve", "fine", "7", OP_EMAIL)
    with get_engine().begin() as conn:
        conn.execute(update(operator_queue).where(operator_queue.c.id == uid).values(
            addressed_to_email=FOR_EMAIL, resolved_to=json.dumps([RESOLVED_EMAIL])))
    row = real_db.get_operator_queue_item(uid)
    for key in WITHHELD:
        assert row[key], key
    return uid


def _read(client, route, uid, agent=AGENT, headers=None):
    if route == "item":
        res = client.get(f"/api/operator-queue/{uid}", headers=headers)
        assert res.status_code == 200, res.text
        return res.json()
    url = (f"/api/operator-queue?agent_name={agent}&limit=500" if route == "list"
           else f"/api/operator-queue/agents/{agent}?limit=500")
    res = client.get(url, headers=headers)
    assert res.status_code == 200, res.text
    rows = [i for i in res.json()["items"] if i["id"] == uid]
    assert len(rows) == 1, f"row {uid} not in the {route} response"
    return rows[0]


# ===========================================================================
# 1. The machine view is an allowlist; a person reads everything
# ===========================================================================

class TestTheMachineView:
    @pytest.mark.parametrize("channel", ["file", "mcp"])
    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("principal", MACHINE_PRINCIPALS)
    def test_a_machine_reads_exactly_the_machine_keys(self, real_db, principal, route, channel):
        """File rows included: the agent wrote their addressee, but a sibling's
        rows are readable by the same key, so it is a person's email all the same."""
        uid = _seed(real_db, channel=channel)
        _as(**principal)
        row = _read(_client(), route, uid)
        assert set(row) == MACHINE_ROW_KEYS
        assert "example.com" not in json.dumps(row)

    @pytest.mark.parametrize("route", ROUTES)
    @pytest.mark.parametrize("principal", PERSON_PRINCIPALS)
    def test_a_person_reads_every_person_field(self, real_db, principal, route):
        """Also the classification guard: a person reads the whole row, so a new
        column not yet placed on either side of the line fails here."""
        uid = _seed(real_db, channel="file")
        _as(**principal)
        row = _read(_client(), route, uid)
        # #3242: plus the sink's own "decided by its options" predicate — a bare
        # boolean derived for the person's answer controls, never a column.
        assert set(row) == MACHINE_ROW_KEYS | WITHHELD | {"decided_by_options"}
        assert (row["responded_by_email"], row["responded_by_id"], row["addressed_to_email"],
                row["disposed_by_email"], row["resolved_to"]) == (
            OP_EMAIL, "7", FOR_EMAIL, OP_EMAIL, [RESOLVED_EMAIL])

    def test_the_two_sides_do_not_overlap_and_the_readback_sits_inside_the_machine_view(self):
        from routers import operator_queue as r
        assert not MACHINE_ROW_KEYS & WITHHELD
        assert set(r._READBACK_FIELDS) <= MACHINE_ROW_KEYS


class TestARealAgentKey:
    """The override above stands in for key resolution. This presents a real
    minted agent key to an app with NO overrides, so `get_current_user` resolves
    it to the owner — carrying `mcp_scope='agent'` — as in production."""
    OWNER = "owner-715-keys"
    AGENT = "agent-715-keys"

    @pytest.mark.parametrize("route", ROUTES)
    def test_a_minted_agent_key_reads_the_machine_view(self, real_db, route, monkeypatch):
        from db_models import UserCreate
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import operator_queue as r

        monkeypatch.setattr(r, "_websocket_manager", None)
        if not real_db.get_user_by_username(self.OWNER):
            real_db.create_user(UserCreate(username=self.OWNER, role="user", email="owner-715@example.com"))
        if not real_db.get_agent_owner(self.AGENT):
            real_db.register_agent_owner(self.AGENT, self.OWNER)
        uid = _seed(real_db, channel="file", agent=self.AGENT)
        app = FastAPI()
        app.include_router(r.router)
        key = real_db.create_agent_mcp_api_key(self.AGENT, self.OWNER)
        row = _read(TestClient(app), route, uid, agent=self.AGENT,
                    headers={"Authorization": f"Bearer {key.api_key}"})
        assert set(row) == MACHINE_ROW_KEYS
        assert "example.com" not in json.dumps(row)


# ===========================================================================
# 2. The platform's heads-ups about a person never reach a machine
# ===========================================================================

class TestPlatformRowsAboutAPerson:
    """Both driven through their REAL emitters, so a renamed id or type turns
    this red instead of leaving the registry pointing at nothing."""

    @pytest.fixture
    def heads_ups(self, real_db, monkeypatch):
        from types import SimpleNamespace
        from client_portal import service as cps
        from routers import operator_queue as r

        monkeypatch.setattr(r, "_websocket_manager", None)
        tag = uuid.uuid4().hex[:10]
        agent = f"agent-715-hu-{tag}"      # its own agent: the #1677 budget counts per agent
        cps._alert_collided_inbox(agent, f"legacy_{tag}",
                                  [f"a-{tag}@example.com", f"b-{tag}@example.com"])
        assert asyncio.run(cps.raise_problem_report(
            agent, f"client-{tag}@example.com", target_kind="message",
            target_id=f"m-{tag}", comment="the words they typed")) is True
        rows = real_db.list_operator_queue_items(agent_name=agent, limit=50)
        collision = next(i for i in rows if i["request_id"].startswith("portal-inbox-collision-"))
        report = next(i for i in rows if i["type"] == "workspace_problem_report")
        assert f"a-{tag}@example.com" in collision["question"]
        assert f"client-{tag}@example.com" in report["question"]
        return SimpleNamespace(agent=agent, ids={collision["id"], report["id"]},
                               collision=collision, report=report)

    @pytest.mark.parametrize("principal", MACHINE_PRINCIPALS[:3])
    def test_a_machine_never_reads_them(self, heads_ups, principal):
        _as(**principal)
        client = _client()
        for url in (f"/api/operator-queue?agent_name={heads_ups.agent}",
                    f"/api/operator-queue/agents/{heads_ups.agent}"):
            res = client.get(url)
            assert res.status_code == 200, res.text
            assert not {i["id"] for i in res.json()["items"]} & heads_ups.ids
            assert "example.com" not in res.text
        for item_id in heads_ups.ids:
            res = client.get(f"/api/operator-queue/{item_id}")
            assert res.status_code == 404 and res.json() == {"detail": "Queue item not found"}

    @pytest.mark.parametrize("principal", PERSON_PRINCIPALS)
    def test_a_person_still_reads_them(self, heads_ups, principal):
        _as(**principal)
        res = _client().get(f"/api/operator-queue?agent_name={heads_ups.agent}")
        assert {i["id"] for i in res.json()["items"]} >= heads_ups.ids

    def test_both_are_platform_alarms_never_written_into_the_agents_file(self, heads_ups):
        from services.operator_queue_service import is_platform_minted
        assert is_platform_minted(heads_ups.collision)
        assert is_platform_minted(heads_ups.report)

    def test_every_prefix_about_a_person_is_reserved(self):
        """The machine reads key on `_ABOUT_A_PERSON_ID_PREFIXES`; the file
        write-back, the resume wake and the ended-asks line key on
        `_RESERVED_ID_PREFIXES`. A heads-up prefix outside the reserved set would
        be hidden from a machine's read yet still written into the agent's file,
        and an agent could pre-create its id. The tests above drive only today's
        two emitters; this holds for the next one."""
        import services.operator_queue_service as oqs
        assert set(oqs._ABOUT_A_PERSON_ID_PREFIXES) <= set(oqs._RESERVED_ID_PREFIXES)


# ===========================================================================
# 3. The file write-back no longer writes who answered
#    (the #2915 harness: explicit returns, never MagicMock defaults)
# ===========================================================================

def _recent():
    return (datetime.now(timezone.utc) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _row(rid="req-1", **over):
    r = {
        "id": f"uuid-{rid}", "request_id": rid, "agent_name": "a", "type": "approval",
        "status": "responded", "priority": "high", "title": "Approve payout",
        "question": "Release 500 USDC?", "options": ["approve", "reject"], "context": {},
        "execution_id": None, "created_at": _recent(), "expires_at": None,
        "response": "approve", "response_text": "fine",
        "responded_by_id": "7", "responded_by_email": OP_EMAIL,
        "responded_at": "2026-09-02T10:00:00Z", "acknowledged_at": None, "cleared_at": None,
        "addressed_to_email": None, "sync_state": "confirmed", "sync_detail": None,
        "sync_updated_at": None, "last_confirmed_at": None, "delivery_state": None,
        "delivery_detail": None, "delivery_updated_at": None,
    }
    r.update(over)
    return r


def _entry(rid="req-1"):
    return {"id": rid, "type": "approval", "status": "pending", "priority": "high",
            "title": "Approve payout", "question": "Release 500 USDC?",
            "options": ["approve", "reject"], "created_at": _recent()}


def _fake_db(responded):
    db = MagicMock()
    db.count_operator_queue_pending_for_agent.return_value = 0
    db.get_operator_queue_sync_index_for_agent.return_value = {
        "open": [dict(r) for r in responded], "terminal": {}, "foreign": []}
    db.set_operator_queue_sync_state.return_value = True
    db.set_operator_queue_delivery_state.return_value = True
    db.mark_operator_queue_unconfirmed.return_value = 1
    db.refresh_operator_queue_last_confirmed.return_value = 0
    db.mark_operator_queue_acknowledged.return_value = None
    db.get_operator_queue_responded_for_agent.return_value = [dict(r) for r in responded]
    db.get_operator_queue_terminal_for_agent.return_value = []
    db.get_setting_value.return_value = "24"
    db.create_operator_queue_item_with_outcome.side_effect = AssertionError("no create expected")
    db.mark_operator_queue_expired.return_value = []
    db.mark_operator_queue_undelivered_for_stopped_agents.return_value = []
    return db


def _sync(monkeypatch, responded, *entries):
    import services.operator_queue_service as oqs
    from services.rate_limiter import RateLimitResult

    db = _fake_db(responded)
    content = json.dumps({"$schema": "operator-queue-v1", "requests": list(entries)})
    client = MagicMock()
    client.read_file = AsyncMock(return_value={"success": True, "content": content})
    client.write_file = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(oqs, "db", db)
    monkeypatch.setattr(oqs, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(oqs, "_audit_sync", AsyncMock())
    asyncio.run(oqs.OperatorQueueSyncService()._sync_agent("a"))
    return db, client


def _delivery_calls(db):
    return [(c.args[0], c.args[1]) for c in db.set_operator_queue_delivery_state.call_args_list]


class TestTheFileWriteBack:
    def test_an_answer_delivered_into_the_pending_entry_names_no_person(self, monkeypatch):
        db, client = _sync(monkeypatch, [_row()], _entry())
        client.write_file.assert_awaited_once()
        (written,) = json.loads(client.write_file.call_args.args[1])["requests"]
        assert (written["status"], written["response"], written["response_text"],
                written["responded_at"]) == ("responded", "approve", "fine", "2026-09-02T10:00:00Z")
        assert "responded_by" not in written
        assert "example.com" not in client.write_file.call_args.args[1]

    def test_a_reconstructed_entry_names_no_person(self, monkeypatch):
        db, client = _sync(monkeypatch, [_row()])            # the file lost the entry
        client.write_file.assert_awaited_once()
        (written,) = json.loads(client.write_file.call_args.args[1])["requests"]
        assert (written["id"], written["status"], written["response"]) == ("req-1", "responded", "approve")
        assert "responded_by" not in written
        assert "example.com" not in client.write_file.call_args.args[1]

    def test_an_answered_inbox_collision_alert_is_never_written_into_the_agents_file(self, monkeypatch):
        """Its text lists client addresses. A platform alarm, like ent#499's:
        not reconstructed, not delivered, `not_applicable`."""
        alarm = _row("portal-inbox-collision-alice_example_com",
                     type="alert", question="written to by 2 client addresses: a@example.com, b@example.com")
        db, client = _sync(monkeypatch, [alarm])
        for call in client.write_file.call_args_list:
            assert "portal-inbox-collision-" not in call.args[1]
        assert ("uuid-portal-inbox-collision-alice_example_com", "not_applicable") in _delivery_calls(db)


# ===========================================================================
# 4. The clear-resolved trigger carries no one's email
# ===========================================================================

class TestTheClearResolvedTrigger:
    def test_the_trigger_is_the_count_only(self, real_db, monkeypatch):
        """`/ws` hands a fleet-level trigger to every connection, and any key can
        mint a ticket. The frontend only refetches on it; the email was dead
        weight that leaked."""
        from routers import operator_queue as r

        sent = []

        class _WS:
            async def broadcast(self, message):
                sent.append(json.loads(message))

        class _Audit:
            async def log(self, **kw):
                return "evt"

        monkeypatch.setattr(r, "_websocket_manager", _WS())
        monkeypatch.setattr(r, "platform_audit_service", _Audit())
        agent = f"agent-715-clear-{uuid.uuid4().hex[:10]}"
        uid = real_db.create_operator_queue_item(agent, {
            "id": _rid(), "type": "question", "status": "pending", "priority": "low",
            "title": "t", "question": "q", "context": {}, "created_at": "2026-09-01T10:00:00Z"})
        real_db.cancel_operator_queue_item(uid, disposed_by_email=OP_EMAIL)
        _as()
        res = _client().post("/api/operator-queue/clear-resolved", json={"agent_name": agent})
        assert res.status_code == 200 and res.json() == {"cleared": 1}
        assert sent == [{"type": "operator_queue_cleared", "data": {"scope": "resolved", "count": 1}}]


# ===========================================================================
# 5. The agent contract says what the platform writes
# ===========================================================================

def test_the_composed_prompt_no_longer_promises_who_answered():
    from services.platform_prompt_service import ExecutionContext, compose_system_prompt
    composed = compose_system_prompt(
        ExecutionContext(agent_name="a1", mode="task", triggered_by="schedule",
                         collaborators=[], platform_url=""),
        runtime="claude-code",
    )
    assert "with `response`, `response_text` and `responded_at`" in composed
    assert "responded_by" not in composed
