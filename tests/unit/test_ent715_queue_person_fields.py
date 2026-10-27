"""trinity-enterprise#715 — the operator queue stops handing a person's email to
a machine key.

A machine principal (an agent-, system-, ops-, connector- or portal_delegate
key, a user-scoped key carrying an agent identity, and whatever scope ships
next) reads the queue for its own work. It never learns who answered, whom an
ask was for, or who a Workspace client is. The rules:

  1. `GET /api/operator-queue`, `/{id}` and `/agents/{name}` give a machine the
     ALLOWLIST projection. Its key set is pinned below as a literal, and so is
     the withheld set: a column added tomorrow turns this file red until
     someone decides which side of the line it is on (learning 2026-09-23 — a
     "whole row minus the withheld fields" diff cannot catch a new leak).
  2. The platform's heads-ups ABOUT a person (the ent#499 problem report, the
     ent#308 inbox collision) are not returned to a machine at all — by any
     door of the queue's routers, the agent's own readback included
     (trinity-enterprise#780: every door is driven with a real agent key and
     the real system key, over the real emitters' rows).
  3. The file write-back no longer writes `responded_by`, on either path, and
     the collision alert is a platform alarm, never written into the agent's
     own file.
  4. The clear-resolved `/ws` trigger no longer carries the operator's email.
  5. Censuses (trinity-enterprise#780): every handler on the queue's routers is
     placed, every door that returns rows consults the one predicate, and every
     read of a queue row anywhere in the backend is classified, so the next door
     inherits the rule instead of reopening it.

A person — a JWT session or the person's own user-scoped key — still reads
every field.

Related flow: docs/memory/feature-flows/operating-room.md
"""

import ast
import asyncio
import json
import os
import re
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
# `_route_census` (#2996) for §6: at module level, never inside a test
# (learning 2026-10-07: a path change made while a test runs rebinds later imports).
_UNIT = os.path.dirname(os.path.abspath(__file__))
if _UNIT not in sys.path:
    sys.path.insert(0, _UNIT)
import _route_census as rc  # noqa: E402

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
    "replaces", "replaced_by",  # #3247
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
        # trinity-enterprise#755: and `gate`, the gated-skill approval card's
        # view, derived the same way (None on a row the gate did not raise).
        # trinity-enterprise#901: and `agent_ending`, the ending the agent
        # authored (replaced | withdrawn | None) — a derived enum, never a column
        # and never PII; the agent reads it on its own readback, not this view.
        assert set(row) == MACHINE_ROW_KEYS | WITHHELD | {"decided_by_options", "gate", "agent_ending"}
        assert row["gate"] is None
        assert row["agent_ending"] is None
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
            real_db.create_user(UserCreate(username=self.OWNER, role="operator", email="owner-715@example.com"))
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

_HEADS_UP_PREFIXES = ("portal-inbox-collision-", "workspace-problem-")


def _file_heads_ups(real_db, agent, tag):
    """Both heads-ups on `agent`, through their REAL emitters, found by their
    own id or target, never by a scan: `trinity-system` is shared by every
    test in the run. The report's id is bucketed by UTC day, so it is never
    recomputed. `markers` is everything that would say who or what they are."""
    from types import SimpleNamespace
    from client_portal import service as cps

    people = (f"a-{tag}@example.com", f"b-{tag}@example.com")
    rater, target, words = f"client-{tag}@example.com", f"msg-{tag}", f"the words {tag} typed"
    cps._alert_collided_inbox(agent, f"legacy_{tag}", list(people))
    assert asyncio.run(cps.raise_problem_report(
        agent, rater, target_kind="message", target_id=target, comment=words)) is True
    collision = real_db.get_operator_queue_item_for_agent_by_request_id(
        agent, f"portal-inbox-collision-legacy_{tag}")
    (report,) = [i for i in real_db.list_operator_queue_items(
                     agent_name=agent, types=("workspace_problem_report",), limit=500)
                 if (i.get("context") or {}).get("target_id") == target]
    assert people[0] in collision["question"] and people[1] in collision["question"]
    assert rater in report["question"] and words in report["question"]
    return SimpleNamespace(agent=agent, ids={collision["id"], report["id"]},
                           collision=collision, report=report,
                           markers=(*people, rater, target, words, f"legacy_{tag}",
                                    collision["id"], report["id"], report["request_id"]))


# Every handler on the queue's routers, and what a machine principal gets from it
# for one of those rows (trinity-enterprise#780). Keyed like `_route_census`; §6
# fails when a handler is missing here. A recipe takes the agent the key acts
# as, an item id and a request_id, and builds the request.
#   listed  — a list: the agent's own ask is in it, the heads-up is not
#   read    — one row: the own ask reads 200, the heads-up exactly like an unknown id
#   refused — a named refusal, exactly like the same request for an id naming nothing
#   counts  — numbers only
_Q = "routers/operator_queue.py::"
DOORS = {
    _Q + "list_queue_items": ("listed", None, lambda a, i, r: (
        "GET", f"/api/operator-queue?agent_name={a}&limit=500", None)),
    _Q + "get_agent_queue_items": ("listed", None, lambda a, i, r: (
        "GET", f"/api/operator-queue/agents/{a}?limit=500", None)),
    _Q + "get_queue_item": ("read", (404, "Queue item not found"), lambda a, i, r: (
        "GET", f"/api/operator-queue/{i}", None)),
    _Q + "get_my_ask": ("read", (404, "Ask not found"), lambda a, i, r: (
        "GET", f"/api/agents/{a}/operator-queue/{r}", None)),
    _Q + "get_queue_stats": ("counts", None, lambda a, i, r: (
        "GET", "/api/operator-queue/stats", None)),
    _Q + "respond_to_queue_item": ("refused", (403, "person_required"), lambda a, i, r: (
        "POST", f"/api/operator-queue/{i}/respond", {"response": "approve"})),
    _Q + "cancel_queue_item": ("refused", (403, "person_required"), lambda a, i, r: (
        "POST", f"/api/operator-queue/{i}/cancel", None)),
    _Q + "bulk_cancel_queue_items": ("refused", (403, "person_required"), lambda a, i, r: (
        "POST", "/api/operator-queue/bulk-cancel", {"ids": [i]})),
    # A replay's `differs` would compare the agent's guess with the row's text,
    # so the reserved-id refusal must come first, whatever the row says.
    _Q + "raise_my_ask": ("refused", (422, "reserved_request_id"), lambda a, i, r: (
        "POST", f"/api/agents/{a}/operator-queue",
        {"request_id": r, "title": "Ship it?", "type": "question"})),
    _Q + "withdraw_my_ask": ("refused", (404, "ask_not_found"), lambda a, i, r: (
        "POST", f"/api/agents/{a}/operator-queue/{r}/withdraw", {"reason": "moot"})),
}
EXEMPT = {
    _Q + "clear_resolved_queue_items": (
        "a count, pinned exactly by TestTheClearResolvedTrigger::test_the_trigger_is_the_count_only. "
        "Not driven here: it writes cleared_at across the agent's resolved rows, and "
        "trinity-system is shared by every test in the run"),
}
STATS_KEYS = frozenset({"by_status", "by_type", "by_priority", "by_agent",
                        "pending_count", "avg_response_seconds", "responded_today"})


class TestPlatformRowsAboutAPerson:
    """Both driven through their REAL emitters, so a renamed id or type turns
    this red instead of leaving the registry pointing at nothing."""

    @pytest.fixture
    def heads_ups(self, real_db, monkeypatch):
        from routers import operator_queue as r

        monkeypatch.setattr(r, "_websocket_manager", None)
        tag = uuid.uuid4().hex[:10]
        agent = f"agent-715-hu-{tag}"      # its own agent: the #1677 budget counts per agent
        # The agent key reaches this agent through an edge (trinity-enterprise#629),
        # so what is withheld below is the person rows, not the whole queue.
        real_db.add_agent_permission(AGENT, agent, "op-715")
        return _file_heads_ups(real_db, agent, tag)

    @pytest.fixture(params=["agent-key", "system-key"])
    def own_doors(self, request, real_db, monkeypatch):
        """The two principals `get_self_acting_agent` admits: an agent's key acting
        as its agent, and the system key acting as `trinity-system`. Each is a REAL
        minted key presented to an app with NO overrides, so key resolution, the
        self-acting gate and `get_authorized_agent` run as in production (the
        issue's own test overrode the gate, which made the two indistinguishable).
        Under the agent the key acts as: both heads-ups, and one ask of its own.

        `trinity-system` cannot be a fresh name, so the finalizer, registered
        before anything is filed, removes the rows (every one carries the tag),
        the key and any ownership row this fixture wrote; the two users stay, as
        other files' users do. Nothing else in the run writes a real
        `trinity-system` ownership row, and the #1677 budget counts pending rows
        per agent and type."""
        from types import SimpleNamespace
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from sqlalchemy import delete, or_, update
        from db.agents import SYSTEM_AGENT_NAME
        from db.engine import get_engine
        from db.tables import agent_ownership, mcp_api_keys, operator_queue
        from db_models import UserCreate
        from routers import operator_queue as r
        from utils.helpers import utc_now_iso

        monkeypatch.setattr(r, "_websocket_manager", None)
        tag = uuid.uuid4().hex[:10]
        system = request.param == "system-key"
        agent = SYSTEM_AGENT_NAME if system else f"agent-780-own-{tag}"
        owner, role = ("admin-780-sys", "admin") if system else ("owner-780-own", "operator")
        keys, registered = [], []

        def remove():
            with get_engine().begin() as conn:
                conn.execute(delete(operator_queue).where(
                    operator_queue.c.agent_name == agent,
                    or_(operator_queue.c.request_id.contains(tag),
                        operator_queue.c.question.contains(tag))))
                if keys:
                    conn.execute(delete(mcp_api_keys).where(mcp_api_keys.c.id.in_(keys)))
                if registered:
                    conn.execute(delete(agent_ownership).where(agent_ownership.c.agent_name == agent))

        request.addfinalizer(remove)
        if not real_db.get_user_by_username(owner):
            real_db.create_user(UserCreate(username=owner, role=role, email=f"{owner}@example.com"))
        assert real_db.get_user_by_username(owner)["role"] == role
        if not real_db.get_agent_owner(agent):
            assert real_db.register_agent_owner(agent, owner, is_system=system)
            registered.append(agent)
        key = real_db.create_agent_mcp_api_key(agent, owner)
        keys.append(key.id)
        if system:
            # What `system_agent_service._set_system_scope` runs; that module
            # imports docker, so its one UPDATE is mirrored rather than imported.
            with get_engine().begin() as conn:
                conn.execute(update(mcp_api_keys).where(mcp_api_keys.c.id == key.id)
                             .values(scope="system"))
        own_id = real_db.create_operator_queue_item(agent, {
            "id": f"own-780-{tag}", "type": "question", "status": "pending", "priority": "low",
            "title": "Is the build green?", "question": "Is the build green?", "context": {},
            "created_at": utc_now_iso()}, channel="mcp", raised_by="agent")
        hu = _file_heads_ups(real_db, agent, tag)
        app = FastAPI()
        app.include_router(r.router)
        app.include_router(r.agent_router)
        return SimpleNamespace(**vars(hu), tag=tag, own=real_db.get_operator_queue_item(own_id),
                               client=TestClient(app),
                               headers={"Authorization": f"Bearer {key.api_key}"})

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

    @pytest.mark.parametrize("which", ["collision", "report"])
    @pytest.mark.parametrize("door", sorted(DOORS))
    def test_no_door_hands_a_machine_a_row_about_a_person(self, own_doors, real_db, door, which):
        """trinity-enterprise#780: every door, the agent's own readback included
        (the issue's test is the `get_my_ask` cases), for both principals the
        self-acting gate admits. A refusal must not depend on the row: it answers
        exactly as for an id that names nothing. Nothing any door answers may carry
        the heads-up's people, folder or words, and nothing may end the row."""
        d = own_doors
        row = getattr(d, which)
        kind, expected, recipe = DOORS[door]

        def send(item_id, request_id):
            method, url, body = recipe(d.agent, item_id, request_id)
            res = d.client.request(method, url, json=body, headers=d.headers)
            leaked = [m for m in d.markers if m in res.text]
            assert not leaked, f"{door} handed a machine {leaked}: {res.text[:300]}"
            return res

        res = send(row["id"], row["request_id"])
        if kind == "listed":
            assert res.status_code == 200, res.text
            listed = {i["id"] for i in res.json()["items"]}
            assert d.own["id"] in listed, "the door is not open to this key"
            assert row["id"] not in listed
        elif kind == "counts":
            assert res.status_code == 200, res.text
            stats = res.json()
            assert set(stats) == STATS_KEYS
            for key in ("by_status", "by_type", "by_priority", "by_agent"):
                assert all(isinstance(n, int) for n in stats[key].values()), key
            assert isinstance(stats["pending_count"], int) and isinstance(stats["responded_today"], int)
            assert stats["avg_response_seconds"] is None or isinstance(stats["avg_response_seconds"], (int, float))
            assert stats["by_agent"].get(d.agent, 0) >= 1, "the door is not open to this key"
        else:
            # Tagged, so a raise a regression accepted would be removed with the rest.
            family = next(p for p in _HEADS_UP_PREFIXES if row["request_id"].startswith(p))
            unknown = send(uuid.uuid4().hex, f"{family}none-{d.tag}")
            assert (res.status_code, res.json()) == (unknown.status_code, unknown.json())
            status, detail = expected
            assert res.status_code == status, res.text
            if kind == "read":
                assert res.json() == {"detail": detail}
                own = send(d.own["id"], d.own["request_id"])
                assert own.status_code == 200 and own.json()["id"] == d.own["id"], own.text
            else:
                assert res.json()["detail"]["code"] == detail
        assert real_db.get_operator_queue_item(row["id"])["status"] == "pending"


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
    monkeypatch.setattr(oqs.deps, "db", db)
    monkeypatch.setattr(oqs.deps, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.rate_limiter, "check", lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    monkeypatch.setattr(oqs.deps, "_audit_sync", AsyncMock())
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


# ===========================================================================
# 6. Every door of the queue's routers is placed (trinity-enterprise#780)
#    §2 proves what each door does today; this is what makes the NEXT door
#    inherit the rule. It reads source on purpose: its consumer is CI, layered
#    on §2's execution, never instead of it.
# ===========================================================================

_ROUTER_FILE = "routers/operator_queue.py"
_PREDICATE = ("services.operator_queue_service", "is_about_a_person")
# How each door that lists or reads rows keeps a row about a person from a machine.
READ_GATES = {
    _Q + "list_queue_items": "_for_principal",
    _Q + "get_agent_queue_items": "_for_principal",
    _Q + "get_queue_item": "_for_principal",
    _Q + "get_my_ask": "is_about_a_person",
}


def _router_tree():
    tree = ast.parse((rc.BACKEND / _ROUTER_FILE).read_text(encoding="utf-8"))
    return tree, rc._FileIndex(_ROUTER_FILE, tree)


def _handler(tree, key):
    name = key.split("::", 1)[1]
    (fn,) = [n for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    return fn


def _own_scope(fn):
    """The nodes of `fn` itself, not of a def, class or lambda nested in it."""
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))


def _raises_404(stmt):
    """`raise HTTPException(404, …)` or `raise HTTPException(status_code=404, …)`."""
    exc = stmt.exc if isinstance(stmt, ast.Raise) else None
    if not (isinstance(exc, ast.Call)
            and getattr(exc.func, "id", getattr(exc.func, "attr", None)) == "HTTPException"):
        return False
    status = exc.args[0] if exc.args else next(
        (k.value for k in exc.keywords if k.arg == "status_code"), None)
    return isinstance(status, ast.Constant) and status.value == 404


def _refuses_a_person_row_first(fn, idx):
    """An `if` directly in the handler, whose test is the predicate on the row the
    handler looked up (the bare call, or a top-level `or` term: never under `not`
    or `and`, which would narrow it; resolved through the module's imports, so a
    local shadow does not count), whose body is one `raise HTTPException(404)`,
    above every `return` of the handler's own scope (learning 2026-09-25: assert
    the check's shape, not its spelling)."""
    looked_up = {t.id for s in fn.body
                 if isinstance(s, ast.Assign) and isinstance(s.value, ast.Call)
                 and isinstance(s.value.func, ast.Attribute)
                 and isinstance(s.value.func.value, ast.Name) and s.value.func.value.id == "db"
                 for t in s.targets if isinstance(t, ast.Name)}
    first_return = min((n.lineno for n in _own_scope(fn) if isinstance(n, ast.Return)), default=10**9)
    for stmt in fn.body:
        if not isinstance(stmt, ast.If) or stmt.lineno > first_return:
            continue
        test = stmt.test
        terms = test.values if isinstance(test, ast.BoolOp) and isinstance(test.op, ast.Or) else [test]
        if (any(isinstance(t, ast.Call) and idx.resolve(t.func) == _PREDICATE and len(t.args) == 1
                and isinstance(t.args[0], ast.Name) and t.args[0].id in looked_up for t in terms)
                and len(stmt.body) == 1 and _raises_404(stmt.body[0])):
            return True
    return False


def _calls(fn, name):
    return any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
               for n in ast.walk(fn))


# What a failing door is told: the rule, so nobody has to read it out of the AST walk.
_GATE_SHAPES = {
    "_for_principal": (
        "the handler must pass the rows it returns through `_for_principal`, the machine "
        "projection that drops a row about a person"),
    "is_about_a_person": (
        "expected, directly in the handler body and above its first `return`: "
        "`if not row or operator_queue_service.is_about_a_person(row): raise HTTPException(404, ...)` "
        "with nothing else in that `if`, where `row` is bound in the handler from a `db.` lookup. "
        "The predicate must be the module's own `is_about_a_person`, as a top-level `or` term "
        "(never under `not` or `and`, never a wrapper or a local shadow), so a prefix added to it "
        "covers this door too. Moving the lookup or the predicate into a helper changes the rule: "
        "update `_refuses_a_person_row_first` in the same PR"),
}


class TestEveryDoorIsPlaced:
    def test_every_handler_is_driven_or_exempt(self):
        """Found twice, by the #2996 walker over the source and from the live
        routers the module defines, and keyed by module + qualname, so a third
        router, a second registration or a reused name cannot hide a door."""
        from fastapi import APIRouter
        from routers import operator_queue as r

        census = rc.walk(files=[rc.BACKEND / _ROUTER_FILE])
        assert census.problems == []
        live = [route.endpoint for router in vars(r).values() if isinstance(router, APIRouter)
                for route in router.routes]
        assert {e.__module__ for e in live} == {"routers.operator_queue"}
        assert sorted(rc.runtime_key(e.__module__, e.__qualname__) for e in live) == sorted(census.routes)
        assert not set(DOORS) & set(EXEMPT)
        placed = set(DOORS) | set(EXEMPT)
        assert set(census.routes) == placed, (
            "Place every handler of routers/operator_queue.py: a recipe in DOORS (§2 then "
            "drives it with a real agent key and the system key over real heads-ups), or "
            f"an EXEMPT entry saying why not. Unplaced: {sorted(set(census.routes) - placed)}; "
            f"stale: {sorted(placed - set(census.routes))}")
        assert hasattr(TestTheClearResolvedTrigger, "test_the_trigger_is_the_count_only")

    def test_every_door_that_returns_rows_consults_the_one_predicate(self):
        """A door that lists or reads rows goes through `_for_principal`, or refuses
        a row the predicate names before it returns anything. A prefix added to the
        predicate then covers every door at once."""
        assert set(READ_GATES) == {k for k, (kind, _e, _r) in DOORS.items() if kind in ("listed", "read")}
        tree, idx = _router_tree()
        helper = _handler(tree, _Q + "_for_principal")
        assert any(isinstance(n, ast.Call) and idx.resolve(n.func) == _PREDICATE for n in ast.walk(helper))
        for key, gate in READ_GATES.items():
            fn = _handler(tree, key)
            if gate == "_for_principal":
                assert _calls(fn, "_for_principal"), f"{key}: {_GATE_SHAPES[gate]}"
            else:
                assert _refuses_a_person_row_first(fn, idx), f"{key}: {_GATE_SHAPES[gate]}"

    def test_a_person_only_door_refuses_a_machine_before_it_reads(self):
        """The doors §2 expects to answer `person_required` are the ones the #2996
        walker classes `person`: the refusal is their FIRST statement, so no
        machine reaches the row they read."""
        census = rc.walk(files=[rc.BACKEND / _ROUTER_FILE])
        person = {k for k, (kind, expected, _r) in DOORS.items()
                  if kind == "refused" and expected[1] == "person_required"}
        assert person and all(census.routes[k].code_class == "person" for k in person), sorted(person)

    def test_no_other_door_reads_a_row_itself(self):
        """A door outside those classes calls no row accessor (§7) and not the
        broad list's engine, so a read filed under the wrong class fails here
        before §2 ever drives it."""
        tree, _idx = _router_tree()
        census = rc.walk(files=[rc.BACKEND / _ROUTER_FILE])
        others = set(census.routes) - set(READ_GATES) - {
            k for k, route in census.routes.items() if route.code_class == "person"}
        assert others
        for key in others:
            reads = sorted({n.func.attr for n in ast.walk(_handler(tree, key))
                            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                            and n.func.attr in _ROW_ACCESSORS | {"list_for_principal"}})
            assert not reads, f"{key} reads rows itself: {reads}"


# ===========================================================================
# 7. Every read of a queue row in the backend is classified (trinity-enterprise#780)
#    §6 covers one file's handlers, but a row can also leave through a service
#    or another router; the readback was the door #715's fix never enumerated.
#    The read-side twin of test_1677's emitter census: every call to an accessor
#    that returns queue rows (person tags are queue rows too) is listed with what
#    happens to the row. AST over source on purpose: its consumer is CI.
#    OSS tree only, like test_1677: the private submodule owns its own twin.
# ===========================================================================

_DATA_LAYER = ("db/", "database.py", "migrations/", "enterprise/")
_OPS_CLASSES = ("OperatorQueueOperations", "QueueMentionOperations")
_OPS_RECEIVERS = frozenset({"_operator_queue_ops", "_queue_mention_ops", *_OPS_CLASSES})

#: Every `DatabaseManager` delegation to the two ops classes, by whether what it
#: returns carries a row (its title, question, answer or context). A new
#: accessor fails `test_every_queue_accessor_is_placed` until it is one or the other.
_ROW_ACCESSORS = frozenset({
    "get_operator_queue_item", "get_operator_queue_item_for_agent_by_request_id",
    "list_operator_queue_items", "list_operator_queue_items_walk",
    "list_pending_operator_queue_asks", "list_recent_operator_queue_endings",
    "list_expired_operator_queue_proposals", "list_pending_operator_queue_proposals",
    "get_operator_queue_responded_for_agent", "get_operator_queue_terminal_for_agent",
    "get_operator_queue_sync_index_for_agent", "mark_operator_queue_undelivered_for_stopped_agents",
    "find_pending_operator_queue_by_subject", "find_person_ended_operator_queue_by_subject",
    "create_native_operator_queue_item", "create_platform_operator_queue_item",
    "respond_to_operator_queue_item", "cancel_operator_queue_item", "withdraw_operator_queue_item",
    "bulk_cancel_operator_queue_items", "end_operator_queue_items_by_platform",
    "mark_operator_queue_expired", "set_operator_queue_discussion_link",
    "create_person_mention", "list_person_mentions_for", "get_person_mention",
    "mark_person_mention_read", "list_person_mentions_by_prefix",
})
_NON_ROW_ACCESSORS = frozenset({
    "create_operator_queue_item", "create_operator_queue_item_with_outcome",   # ids (test_1677)
    "count_operator_queue_items", "count_operator_queue_pending_for_agent",
    "count_operator_queue_terminal_candidates", "count_operator_queue_flags",
    "get_operator_queue_stats", "list_operator_queue_agent_names",
    "operator_queue_walk_alert_priorities", "operator_queue_item_exists",
    "prune_operator_queue_terminal_items", "clear_resolved_operator_queue_items",
    "mark_operator_queue_acknowledged",   # #3254: update_pending_problem_report retired
    "mark_operator_queue_unconfirmed", "refresh_operator_queue_last_confirmed",
    "set_operator_queue_sync_state", "set_operator_queue_delivery_state",
})

#: What happens to the rows each reader gets.
#:   door      — a handler of routers/operator_queue.py, placed and driven in §6/§2
#:   filtered  — returned to any principal, a machine's exclusion done in SQL
#:   person    — returned only to the person who ended, answered or wrote it
#:   addressee — returned only to the person it is addressed to
#:   agent     — reaches the agent itself; the reason names what keeps person rows out
#:   ruled     — a person's words from a row about a person reach a machine by an
#:               explicit product ruling; the reason names the field and the ruling
#:   internal  — read to decide or act; only ids, enums, counts or booleans leave
_READ_CLASSES = frozenset({"door", "filtered", "person", "addressee", "agent", "ruled", "internal"})
_PORTAL_ASKS = "client_portal/asks/service.py"
_ASKS = "services/ask_service.py"
_OQS = "services/operator_queue_service/__init__.py"
_TAGS = "services/person_mention_service.py"
_ALERTS = "services/platform_alerts.py"
_PROMPT = "services/platform_prompt_service.py"
_GATE = "services/skill_gate_service.py"
_ROW_READERS = {
    (_PORTAL_ASKS, "_discussed_in"): (
        "addressee", "the ask a chat discusses: addressed to the viewer in SQL, then the roster"),
    (_PORTAL_ASKS, "_linked_request_id"): (
        "addressee", "the linked row's request_id only, when this ask's addressee could already see it"),
    (_PORTAL_ASKS, "_owned_ask"): (
        "addressee", "the viewer's own ask (`_is_addressed_to`, roster, visible kinds), else the uniform 404"),
    (_PORTAL_ASKS, "_recent_answers"): (
        "addressee", "the viewer's own recent answers: addressed to the viewer in SQL"),
    (_PORTAL_ASKS, "discuss_ask"): (
        "addressee", "the discussion link on the viewer's own ask, after `_owned_ask`"),
    (_PORTAL_ASKS, "discussion_context_line"): (
        "agent", "the line a discussion chat carries to the agent: the viewer's own ask's request_id, "
                 "kind, status, title and options, never its question or answer (a gate card's title "
                 "names no person)"),
    (_PORTAL_ASKS, "list_asks_page"): (
        "addressee", "the Workspace inbox: addressed to the viewer in SQL, before the limit"),
    (_ROUTER_FILE, "bulk_cancel_queue_items"): ("door", "§6: person only"),
    (_ROUTER_FILE, "cancel_queue_item"): ("door", "§6: person only"),
    (_ROUTER_FILE, "get_agent_queue_items"): ("door", "§6: `_for_principal`, the exclusion in SQL"),
    (_ROUTER_FILE, "get_my_ask"): ("door", "§6: the predicate's 404"),
    (_ROUTER_FILE, "get_queue_item"): ("door", "§6: `_for_principal`"),
    (_ROUTER_FILE, "respond_to_queue_item"): ("door", "§6: person only"),
    (_ASKS, "_predecessor"): (
        "internal", "a re-ask's link: the agent's own expired ask, or one uniform refusal"),
    (_ASKS, "_refuse_unlinked_reask"): (
        "internal", "the agent's own expired proposals, compared to refuse an unlinked re-ask"),
    (_ASKS, "_replace_target"): (
        "internal", "a replace's target: the agent's own pending row (`raised_by='agent'`), or one 422"),
    (_ASKS, "answer"): (
        "person", "a person's answer; the ended row returns to their route and the listed ending observers"),
    (_ASKS, "bulk_cancel"): (
        "person", "a person's sweep; its route returns counts and a batch id, the observers get the rows"),
    (_ASKS, "cancel"): (
        "person", "a person's cancel; the ended row returns to their route and the listed ending observers"),
    (_ASKS, "clear_platform"): (
        "internal", "the platform ends its own alerts (#3246); nothing returns to a principal"),
    (_ASKS, "dismiss"): (
        "person", "the addressee's dismiss; the ended row returns to them and the listed ending observers"),
    (_ASKS, "expire"): ("internal", "the clock's ending; the poll cycle sends one thin trigger"),
    (_ASKS, "raise_ask"): (
        "agent", "the receipt of the agent's own ask, ids and enums; a reserved id is refused "
                 "before the replay lookup (§2 drives it)"),
    (_ASKS, "request_id_of"): ("internal", "a uuid's request_id, nothing else"),
    (_ASKS, "withdraw"): (
        "agent", "the receipt of the agent's own ask; a row it did not raise, or one about a "
                 "person, is the uniform 404 (§2 drives it)"),
    (_OQS, "OperatorQueueSyncService._poll_cycle"): (
        "internal", "marks answers owed to stopped agents, reserved prefixes excluded; audits ids"),
    (_OQS, "OperatorQueueSyncService._sync_agent"): (
        "agent", "the file write-back: `is_platform_minted` keeps every reserved-prefix row, so "
                 "every row about a person, out of the agent's file (§3)"),
    (_OQS, "list_for_principal"): (
        "filtered", "the broad list's engine: a machine's about-a-person exclusion runs in SQL "
                    "before the limit (§2 drives its door)"),
    (_TAGS, "_mine"): ("addressee", "one tag, only for the person it tags"),
    (_TAGS, "deliver"): (
        "person", "writes the tags; returns the tagger's own view of each (label, state, read time)"),
    (_TAGS, "list_for_reader"): ("addressee", "the tagged person's Inbox: their own tags"),
    (_TAGS, "mark_read"): ("addressee", "the tagged person marks their own tag read"),
    (_TAGS, "tags_by_message"): (
        "person", "the tagger's marks on their own messages; the room door refuses agents"),
    (_ALERTS, "clear"): ("internal", "ends a condition's pending row (#3246); returns a count"),
    (_ALERTS, "observe"): ("internal", "records a reading on a condition's row (#3246); returns an outcome"),
    (_ALERTS, "reconcile"): ("internal", "ends the rows whose condition cleared (#3246); returns a count"),
    (_PROMPT, "_resolve_ended_asks"): (
        "agent", "the agent's own ended asks for its prompt: reserved prefixes excluded in SQL"),
    (_PROMPT, "_resolve_pending_asks"): (
        "agent", "the agent's own pending asks for its prompt: reserved prefixes excluded in SQL"),
    ("services/seat_decision_service.py", "_request_belongs_to_agent"): (
        "internal", "a boolean: the request names a row of this agent"),
    (_GATE, "_decider_of"): (
        "internal", "who ended a gate ask, for the gate's own record; an agent's notice never names them"),
    (_GATE, "_pending_from_record"): ("internal", "a gate ask's deadline only"),
    (_GATE, "request_status"): (
        "ruled", "a gate request's outcome for the readers of its conversation: the state, the "
                 "platform's sentence and a denial's `response_text` reason (trinity-enterprise#755); "
                 "never who decided"),
    (_GATE, "resolve"): (
        "ruled", "consumes a gate ask's ending; a denial's `response_text` reason is quoted to the "
                 "requester (trinity-enterprise#755); never who decided (#715)"),
    (_GATE, "sweep"): ("internal", "the gate's reconcile pass"),
    ("services/task_execution_service.py", "_alert_skill_not_found"): (
        "internal", "dedupe: is a skill_not_found alert already pending"),
}


#: Reads of the `operator_queue` table that do not go through an accessor.
_TABLE_READERS = {
    ("canary/snapshot.py", "_collect_orphan_refs"):
        "the canary's L-03 orphan scan: each row's id and agent name, never its text",
}
#: The data-layer modules that build queries over the table object themselves.
_TABLE_MODULES = {
    "db/operator_queue.py": "OperatorQueueOperations, behind the accessors above",
    "db/queue_mentions.py": "QueueMentionOperations: person tags are queue rows, behind the accessors above",
    "db/skill_gate_requests.py": "exists() subqueries joining the gate's own ask rows; returns none of their columns",
}
#: Every observer the ask sink hands the ended rows to (`ask_service._ended`).
_ENDING_OBSERVERS = {
    ("services/ask_service.py", "_wake_filer"):
        "wakes the agent that raised the ask; a platform-minted row (every row about a person is "
        "reserved) never dispatches: on an answer here, on an ending in "
        "operator_resume_service._wakes_on_ending",
    ("services/skill_gate_service.py", "on_ending"):
        "the gate consumes its own asks' endings (`resolve`, ruled above)",
}
_TABLE_HOMES = frozenset({"db.tables", "db.operator_queue", "db.queue_mentions"})
_RAW_SQL = re.compile(r"\b(FROM|JOIN|UPDATE|INTO)\s+operator_queue\b", re.IGNORECASE)


def _qualified(tree):
    """(enclosing qualname, node) for every node of `tree`."""
    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            yield ".".join(stack) or "<module>", child
            scoped = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            yield from visit(child, stack + [child.name] if scoped else stack)
    yield from visit(tree, [])


def _facade_delegations():
    """{facade method: {ops method}} for every `DatabaseManager` method that calls
    into the operator-queue or person-tag ops."""
    tree = ast.parse((rc.BACKEND / "database.py").read_text(encoding="utf-8"))
    manager = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "DatabaseManager")
    out = {}
    for fn in manager.body:
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for n in ast.walk(fn):
                if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Attribute)
                        and n.value.attr in ("_operator_queue_ops", "_queue_mention_ops")):
                    out.setdefault(fn.name, set()).add(n.attr)
    return out


def _receiver_names(node):
    """Identifiers along a receiver chain (`a.b.c` → [c, b, a])."""
    names = []
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    return names


def _row_reads(rel, text, ops_names):
    """(path, enclosing qualname, accessor) for every call that returns queue rows:
    a row accessor on any receiver (`db.`, `core_db.`, a bare name), or its ops
    method on an ops receiver chain (the facade-bypass spelling). A file that
    names the tables' accessors but cannot be parsed fails closed."""
    if not any(token in text for token in ("operator_queue", "person_mention", "queue_mention")):
        return []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return [(rel, "<unparseable>", "")]
    sites = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, stack + [child.name])
                continue
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in _ROW_ACCESSORS or (
                        isinstance(func, ast.Attribute) and name in ops_names
                        and _OPS_RECEIVERS & set(_receiver_names(func.value))):
                    sites.append((rel, ".".join(stack) or "<module>", name))
            visit(child, stack)

    visit(tree, [])
    return sites


def _backend_sources(data_layer=False):
    for path in sorted(rc.BACKEND.rglob("*.py")):
        rel = path.relative_to(rc.BACKEND).as_posix()
        if rel.startswith("enterprise/") or rel.startswith(_DATA_LAYER) != data_layer:
            continue
        yield rel, path.read_text(encoding="utf-8")


def _table_reads(rel, text):
    """Enclosing qualnames in `text` that reach the ops classes or the
    `operator_queue` table past the accessors: an import of either, an attribute
    of a data module naming them (`tables.operator_queue`, `_queue_db.operator_queue`,
    `db.tables.operator_queue`, through a module-level or a function-local alias),
    the table by name (`metadata.tables['operator_queue']`, `sa.table('operator_queue')`),
    or SQL text naming it. Docstrings are prose."""
    if not any(token in text for token in (*_OPS_CLASSES, "operator_queue")):
        return set()
    tree = ast.parse(text)
    idx = rc._FileIndex(rel, tree)
    aliases = {}  # every import in the file, at any depth: local name -> dotted module
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and not n.level:
            aliases.update({a.asname or a.name: f"{n.module}.{a.name}" for a in n.names})
        elif isinstance(n, ast.Import):
            aliases.update({a.asname or a.name.split(".")[0]: a.name if a.asname else a.name.split(".")[0]
                            for a in n.names})
    prose = {id(n.body[0].value) for n in ast.walk(tree)
             if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
             and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    found = set()
    for qual, n in _qualified(tree):
        if isinstance(n, ast.ImportFrom):
            hit = any(a.name in _OPS_CLASSES or (n.module in _TABLE_HOMES and a.name in ("operator_queue", "*"))
                      for a in n.names)
        elif isinstance(n, ast.Name):
            hit = n.id in _OPS_CLASSES
        elif isinstance(n, ast.Attribute):
            owner = (idx.resolve(n) or ("", ""))[0]
            parts = list(reversed(_receiver_names(n.value)))
            chain = ".".join(parts)
            via_alias = ".".join([aliases[parts[0]], *parts[1:]]) if parts and parts[0] in aliases else ""
            hit = n.attr in _OPS_CLASSES or (
                n.attr == "operator_queue" and bool(_TABLE_HOMES & {owner, chain, via_alias}))
        elif isinstance(n, ast.Subscript):
            hit = (isinstance(n.slice, ast.Constant) and n.slice.value == "operator_queue"
                   and "tables" in (getattr(n.value, "attr", None), getattr(n.value, "id", None)))
        elif isinstance(n, ast.Call):
            hit = (getattr(n.func, "id", getattr(n.func, "attr", None)) in ("table", "Table")
                   and bool(n.args) and isinstance(n.args[0], ast.Constant)
                   and n.args[0].value == "operator_queue")
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and id(n) not in prose:
            hit = bool(_RAW_SQL.search(n.value))
        else:
            hit = False
        if hit:
            found.add(qual)
    return found


def _ending_observers(rel, text):
    """(path, observer) for every `register_ending_observer(fn)` call and every
    function it decorates."""
    if "register_ending_observer" not in text:
        return set()
    named = lambda e: getattr(e, "id", getattr(e, "attr", None))  # noqa: E731
    found = set()
    for _qual, n in _qualified(ast.parse(text)):
        if isinstance(n, ast.Call) and named(n.func) == "register_ending_observer" and n.args:
            found.add((rel, named(n.args[0]) or "<expression>"))
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
                named(d if not isinstance(d, ast.Call) else d.func) == "register_ending_observer"
                for d in n.decorator_list):
            found.add((rel, n.name))
    return found


def _scan_row_reads():
    delegations = _facade_delegations()
    ops = {o for m in _ROW_ACCESSORS for o in delegations.get(m, ())}
    return {(p, q) for rel, text in _backend_sources() for p, q, _a in _row_reads(rel, text, ops)}


class TestEveryRowReadIsClassified:
    def test_every_queue_accessor_is_placed(self):
        delegations = _facade_delegations()
        assert not _ROW_ACCESSORS & _NON_ROW_ACCESSORS
        placed = _ROW_ACCESSORS | _NON_ROW_ACCESSORS
        assert set(delegations) == placed, (
            "A DatabaseManager accessor over the operator queue (or person tags) must be placed: "
            "_ROW_ACCESSORS when what it returns carries a row, _NON_ROW_ACCESSORS otherwise. "
            f"Unplaced: {sorted(set(delegations) - placed)}; stale: {sorted(placed - set(delegations))}")

    def test_every_row_read_is_classified(self):
        unlisted = _scan_row_reads() - set(_ROW_READERS)
        assert not unlisted, (
            "Unclassified read(s) of operator-queue rows (trinity-enterprise#780). Add a "
            "(path, qualname) entry to _ROW_READERS saying what happens to the rows; a row about "
            "a person (`is_about_a_person`) never reaches a machine principal, except a field a "
            "`ruled` entry names with its ruling:\n  "
            + "\n  ".join(f"{p} :: {q}" for p, q in sorted(unlisted)))

    def test_no_stale_row_reader_entries(self):
        stale = set(_ROW_READERS) - _scan_row_reads()
        assert not stale, "Stale _ROW_READERS entries; update or drop:\n  " + "\n  ".join(
            f"{p} :: {q}" for p, q in sorted(stale))

    def test_every_class_is_known_and_the_router_reads_are_its_placed_doors(self):
        assert {cls for cls, _reason in _ROW_READERS.values()} <= _READ_CLASSES
        doors = {f"{p}::{q}" for (p, q), (cls, _r) in _ROW_READERS.items() if cls == "door"}
        assert doors and doors <= set(DOORS) | set(EXEMPT)
        assert all((p == _ROUTER_FILE) == (cls == "door") for (p, _q), (cls, _r) in _ROW_READERS.items())

    def test_nothing_else_reads_the_table_past_the_accessors(self):
        """Outside the data layer, the ops classes and the `operator_queue` table
        are reached only where listed, so the accessors above are every other way
        to read a queue row."""
        found = {(rel, qual) for rel, text in _backend_sources() for qual in _table_reads(rel, text)}
        assert found == set(_TABLE_READERS), (
            f"unlisted: {sorted(found - set(_TABLE_READERS))}; stale: {sorted(set(_TABLE_READERS) - found)}")

    def test_the_table_is_queried_only_by_the_listed_data_modules(self):
        """Inside the data layer, a module that builds its own query over the table
        could return a queue row through an accessor this census does not follow."""
        modules = {rel for rel, text in _backend_sources(data_layer=True)
                   if not rel.startswith("migrations/") and rel not in ("db/tables.py", "db/schema.py",
                                                                         "db/migrations.py")
                   and any(re.match(r"\.?(db\.)?tables$", n.module or "") and
                           any(a.name in ("operator_queue", "*") for a in n.names)
                           for n in ast.walk(ast.parse(text)) if isinstance(n, ast.ImportFrom))}
        assert modules == set(_TABLE_MODULES), (
            f"unlisted: {sorted(modules - set(_TABLE_MODULES))}; stale: {sorted(set(_TABLE_MODULES) - modules)}")

    def test_every_ending_observer_is_listed(self):
        """The ask sink hands every ended row to its observers, past every read above."""
        found = set().union(*(_ending_observers(rel, text) for rel, text in _backend_sources()))
        assert found == set(_ENDING_OBSERVERS), (
            f"unlisted: {sorted(found - set(_ENDING_OBSERVERS))}; "
            f"stale: {sorted(set(_ENDING_OBSERVERS) - found)}")

    def test_the_table_detector_sees_every_spelling(self):
        spellings = {
            "from db.tables import operator_queue": ("from db.tables import operator_queue\n", "<module>"),
            "the data module's re-export": ("from db import operator_queue as q\nx = q.operator_queue\n",
                                            "<module>"),
            "the full dotted path": ("import db.tables\nx = db.tables.operator_queue\n", "<module>"),
            "a function-local alias": ("def f():\n    from db import tables as t\n"
                                       "    return t.operator_queue\n", "f"),
            "MetaData.tables by name": ("x = metadata.tables['operator_queue']\n", "<module>"),
            "a lightweight table()": ("import sqlalchemy as sa\nx = sa.table('operator_queue')\n", "<module>"),
            "the ops class": ("from db.operator_queue import OperatorQueueOperations\n", "<module>"),
            "SQL text": ("q = 'SELECT question FROM operator_queue WHERE id = :i'\n", "<module>"),
        }
        for what, (text, where) in spellings.items():
            assert _table_reads("s.py", text) == {where}, what
        # Prose, a constant from the data module and an audit target's name are not reads.
        assert _table_reads("t.py", '"""Rows FROM operator_queue, in prose."""\n'
                                    "from db.operator_queue import OUTCOME_UNKNOWN\n"
                                    "log(target_type='operator_queue', details={'operator_queue': 1})\n") == set()

    def test_the_observer_detector_sees_calls_and_decorators(self):
        text = ("register_ending_observer(_a)\n"
                "def f():\n    ask_service.register_ending_observer(_b)\n"
                "@ask_service.register_ending_observer\ndef c(event):\n    pass\n")
        assert _ending_observers("o.py", text) == {("o.py", "_a"), ("o.py", "_b"), ("o.py", "c")}

    # The scanner must actually catch things (the test_1677 meta-tests).
    def test_the_scanner_sees_every_spelling(self):
        text = (
            "# operator_queue\n"
            "def a():\n    db.get_operator_queue_item(i)\n"
            "async def b():\n    core_db.list_operator_queue_items(agent_name=n)\n"
            "def c():\n    get_person_mention(i)\n"
            "class C:\n    def m(self):\n        self._operator_queue_ops.get_item(i)\n"
            "db.list_operator_queue_items_walk()\n"
        )
        assert [(q, a) for _p, q, a in _row_reads("x.py", text, {"get_item"})] == [
            ("a", "get_operator_queue_item"), ("b", "list_operator_queue_items"),
            ("c", "get_person_mention"), ("C.m", "get_item"), ("<module>", "list_operator_queue_items_walk")]

    def test_the_scanner_ignores_prose_counts_and_other_receivers(self):
        text = ('"""db.get_operator_queue_item(i) in prose"""\n'
                "# db.list_operator_queue_items()\n"
                "def m():\n    cache.get_item(k)\n    self.store.get_item(k)\n"
                "    db.count_operator_queue_items()\n")
        assert _row_reads("y.py", text, {"get_item"}) == []

    def test_an_unparseable_file_with_the_token_fails_closed(self):
        broken = "def f(:\n    db.get_operator_queue_item(i)\n"
        assert _row_reads("z.py", broken, set()) == [("z.py", "<unparseable>", "")]
