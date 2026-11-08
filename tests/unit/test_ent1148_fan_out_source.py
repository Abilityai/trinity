"""A fan-out row names the agent that sent it, never its own target
(abilityai/trinity-enterprise#1148, review finding F1).

Rule A rates a row by its `source_agent_name`: "is the CALLING agent's owner
owner/admin of this agent?". `POST /api/agents/{name}/fan-out` used to fill
that field with the TARGET whenever no `X-Source-Agent` header came — the
router for `agent: "self"`, and the service for any empty value. A
self-stamped row asks "does the callee's owner own the callee?", which is
always yes, so the clamp was vacuous:

  (a) a delegated owner-tier turn on B curls C's fan-out with B's key and no
      header: C's rows rated OWNER although B's owner is only shared C;
  (b) P (shared on B) fans out on B signed in: the rows named B; once the owner
      revoked P's share, P's queued and running tasks re-rated OWNER.

The source is now the initiator's key agent (`current_user.agent_name`) or
the header that `resolve_source_agent` vouched for — the resolution reminders
and loops already use — and nothing else. Rule A is unchanged; (c) pins that
a self fan-out inside a delegated turn keeps that turn's tier.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

import routers.fan_out as _FANOUT  # noqa: E402
import services.fan_out_service as _FOS  # noqa: E402
from models import FanOutRequest  # noqa: E402
from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

B, C = "agent-b", "agent-c"
B_OWNER, C_OWNER, MATE = 1, 2, 9
USERS = {
    B_OWNER: {"id": B_OWNER, "username": "b-owner", "role": "user"},
    C_OWNER: {"id": C_OWNER, "username": "c-owner", "role": "user"},
    MATE: {"id": MATE, "username": "mate", "role": "user"},
}
BY_NAME = {u["username"]: u for u in USERS.values()}


class _Db:
    """Who owns and who is shared what — the only facts rule A reads."""

    def __init__(self, *, owners, shares):
        self.owners, self.shares = owners, set(shares)

    def get_user_by_id(self, uid):
        return USERS.get(int(uid))

    def get_agent_owner(self, agent):
        oid = self.owners.get(agent)
        return None if oid is None else {"owner_id": oid}

    def get_schedule(self, sid):
        return None

    def can_user_share_agent(self, username, agent):
        user = BY_NAME.get(username)
        return bool(user) and self.owners.get(agent) == user["id"]

    def can_user_access_agent(self, username, agent):
        return self.can_user_share_agent(username, agent) or (username, agent) in self.shares


def _agent_key(agent, owner):
    return SimpleNamespace(id=owner, username=USERS[owner]["username"], email=None, role="user",
                           mcp_scope="agent", agent_name=agent, connector_agent=None,
                           portal_delegate=False, is_event_loopback=False, vouched_source_agent=None,
                           mcp_key_id="k", mcp_key_name="k")


def _person(uid):
    return SimpleNamespace(id=uid, username=USERS[uid]["username"], email=None, role="user",
                           mcp_scope=None, agent_name=None, connector_agent=None, portal_delegate=False,
                           is_event_loopback=False, vouched_source_agent=None,
                           mcp_key_id=None, mcp_key_name=None)


@pytest.fixture(autouse=True)
def _no_provider():
    tts.clear_person_tier_providers()
    yield
    tts.clear_person_tier_providers()


@pytest.fixture
def router_run(monkeypatch):
    """Drive the real router; capture the row origin it hands the real service,
    and the origin the real service hands its dispatcher."""
    stamped = {}

    async def _dispatch_all(self, **kwargs):
        stamped.update(kwargs)

    monkeypatch.setattr(_FOS.FanOutService, "_dispatch_all", _dispatch_all)
    monkeypatch.setattr(_FANOUT, "get_fan_out_service", lambda: _FOS.FanOutService())
    monkeypatch.setattr(_FANOUT.dispatch_admission_service, "enforce_inter_agent_depth",
                        AsyncMock(return_value=1))
    idem = MagicMock(replay=False)
    monkeypatch.setattr(_FANOUT.idempotency_service, "begin", lambda *a, **k: idem)
    for name in ("complete", "fail", "attach_execution"):
        monkeypatch.setattr(_FANOUT.idempotency_service, name, lambda *a, **k: None)

    def run(*, target, principal, person, header=None):
        # C1 (ent#1148): the child carries the acting person; pinned elsewhere.
        monkeypatch.setattr(_FANOUT.turn_tier_service, "turn_origin_user_id", lambda db, u: person)
        monkeypatch.setattr(_FANOUT.turn_tier_service, "turn_origin_email", lambda db, u, d: None)
        stamped.clear()
        asyncio.run(_FANOUT.fan_out(
            request=FanOutRequest(agent="self", tasks=[{"id": "t1", "message": "m"}], async_mode=True),
            name=target, current_user=principal, x_source_agent=header, x_via_mcp=None,
            idempotency_key=None,
        ))
        return {"agent_name": target, "source_user_id": stamped["source_user_id"],
                "source_agent_name": stamped["source_agent_name"], "triggered_by": "agent",
                "schedule_id": "__manual__", "source_channel": None, "source_user_email": None}

    return run


def test_a_delegated_turn_fanning_out_to_another_agent_without_the_header_stays_share(router_run):
    """(a) B's owner is only shared C; MATE cannot open C. The row must name B."""
    db = _Db(owners={B: B_OWNER, C: C_OWNER}, shares={("b-owner", C)})
    row = router_run(target=C, principal=_agent_key(B, B_OWNER), person=MATE)
    assert row["source_agent_name"] == B
    assert tts.turn_actor(db, C, execution=row) == ("share", "person", MATE)


def test_the_header_and_its_absence_now_agree(router_run):
    db = _Db(owners={B: B_OWNER, C: C_OWNER}, shares={("b-owner", C)})
    with_header = router_run(target=C, principal=_agent_key(B, B_OWNER), person=MATE, header=B)
    without = router_run(target=C, principal=_agent_key(B, B_OWNER), person=MATE)
    assert with_header == without
    assert tts.turn_actor(db, C, execution=without)[0] == "share"


def test_a_persons_own_fan_out_names_no_agent_and_does_not_rise_after_revocation(router_run):
    """(b) MATE, shared B, fans out on B signed in. After the owner revokes the
    share, MATE's rows on B must not re-rate as the owner's."""
    row = router_run(target=B, principal=_person(MATE), person=MATE)
    assert row["source_agent_name"] is None
    shared = _Db(owners={B: B_OWNER}, shares={("mate", B)})
    revoked = _Db(owners={B: B_OWNER}, shares=set())
    assert tts.turn_actor(shared, B, execution=row) == ("share", "person", MATE)
    assert tts.turn_actor(revoked, B, execution=row) == ("share", "person", MATE)


def test_a_self_fan_out_in_a_delegated_turn_keeps_that_turns_tier(router_run):
    """(c) The delegated owner-tier turn on B (MATE cannot open B) fans out on B
    itself: the rows name B and rate exactly what the parent turn rated."""
    db = _Db(owners={B: B_OWNER, "agent-a": B_OWNER}, shares=set())
    parent = tts.turn_actor(db, B, execution={"agent_name": B, "source_user_id": MATE, "source_agent_name": "agent-a",
                                              "triggered_by": "agent", "schedule_id": "__manual__"})
    assert parent == ("owner", "delegated", MATE)
    row = router_run(target=B, principal=_agent_key(B, B_OWNER), person=MATE)
    assert row["source_agent_name"] == B
    assert tts.turn_actor(db, B, execution=row) == parent


def test_a_self_fan_out_in_a_share_turn_is_not_elevated(router_run):
    """(c) The same self fan-out in MATE's share turn on B stays share."""
    db = _Db(owners={B: B_OWNER}, shares={("mate", B)})
    row = router_run(target=B, principal=_agent_key(B, B_OWNER), person=MATE)
    assert tts.turn_actor(db, B, execution=row) == ("share", "person", MATE)


def test_the_service_never_fills_the_source_with_its_target():
    """Belt to the router's braces: an empty source stays empty."""
    stamped = {}

    async def _dispatch_all(**kwargs):
        stamped.update(kwargs)

    svc = _FOS.FanOutService()
    svc._dispatch_all = _dispatch_all
    asyncio.run(svc.execute(agent_name=C, tasks=[_FOS.FanOutTaskInput(id="t", message="m")],
                            async_mode=True, source_agent_name=None))
    assert stamped["source_agent_name"] is None
