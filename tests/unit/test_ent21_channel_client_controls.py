"""trinity-enterprise#21 — per-client access controls for channel users.

An agent's owner can block, unblock and revoke one external channel client from
the Sharing-tab roster, and approve a pending one. Covered here against a real
database (both backends via the harness):

- the roster carries each client's ``client_id`` and access state —
  blocked / granted / pending (with the request id approve needs) / none;
- block is idempotent, per agent and per channel, and keeps the client's share;
- unblock restores exactly what they had; unblocking an unblocked client is 409;
- revoke removes the share (so a restricted agent asks again), refuses a client
  with nothing to revoke, and works on a blocked client too;
- every control refuses a client that never reached this agent (404).

The gate that enforces a block is in ``test_message_router_access_gate.py``; the
routes' person/owner gate in the route census and below.
"""

import os
import secrets
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

AGENT, OTHER = "agent-a", "agent-b"
OWNER = "owner"
ALICE = "alice@example.com"   # telegram 1001, shared on AGENT
CAROL = "carol@example.com"   # whatsapp, pending on AGENT
WA = "whatsapp:+15559998888"


@pytest.fixture
def world(db_backend, monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    from database import db
    from db_models import UserCreate

    db.create_user(UserCreate(username=OWNER, password="x", role="creator", email="owner@example.com"))
    for name in (AGENT, OTHER):
        db.register_agent_owner(name, OWNER)

    db.create_telegram_binding(agent_name=AGENT, bot_token="tok-a", bot_id="111")
    tg = db.get_telegram_binding(AGENT)["id"]
    db.record_telegram_inbound(tg, "1001", "alice")
    db.set_telegram_verified_email(tg, "1001", ALICE)
    db.record_telegram_inbound(tg, "1002", None)          # never verified

    db.create_whatsapp_binding(agent_name=AGENT, account_sid="AC" + "x" * 32, auth_token="tok",
                               from_number="whatsapp:+15551230000")
    wa = db.get_whatsapp_binding(AGENT)["id"]
    db.record_whatsapp_inbound(wa, WA, "Carol")
    db.set_whatsapp_verified_email(wa, WA, CAROL)

    db.share_agent(AGENT, OWNER, ALICE)
    db.upsert_access_request(AGENT, CAROL, "whatsapp")
    return db


def _roster():
    from services.client_roster_service import get_client_roster
    return {(c["channel"], c["client_id"]): c for c in get_client_roster(AGENT)}


def _block(client=("telegram", "1001"), reason=None, agent=AGENT):
    from services import client_roster_service as svc
    return svc.block_client(agent, *client, actor_id=1, actor_email="owner@example.com", reason=reason)


def _err(fn, *a, **kw):
    from services.client_roster_service import ClientControlError
    with pytest.raises(ClientControlError) as e:
        fn(*a, **kw)
    return e.value.status_code


class TestRosterAccessState:
    def test_each_row_carries_its_client_id_and_state(self, world):
        r = _roster()
        assert set(r) == {("telegram", "1001"), ("telegram", "1002"), ("whatsapp", WA)}
        assert r[("telegram", "1001")]["access"] == "granted"
        assert r[("telegram", "1002")]["access"] == "none"
        carol = r[("whatsapp", WA)]
        assert carol["access"] == "pending"
        pending = world.list_access_requests(AGENT, "pending")
        assert carol["access_request_id"] == pending[0]["id"]

    def test_a_block_wins_and_says_who_and_why(self, world):
        _block(reason="  spam  ")
        alice = _roster()[("telegram", "1001")]
        assert alice["access"] == "blocked"
        assert alice["blocked_by_email"] == "owner@example.com"
        assert alice["block_reason"] == "spam"
        assert alice["blocked_at"]


class TestBlock:
    def test_block_is_what_the_gate_reads_and_is_scoped(self, world):
        _block()
        assert world.is_channel_client_blocked(AGENT, "telegram", "1001")
        # Not the same id on another channel, not another agent, not another client.
        assert not world.is_channel_client_blocked(AGENT, "whatsapp", "1001")
        assert not world.is_channel_client_blocked(OTHER, "telegram", "1001")
        assert not world.is_channel_client_blocked(AGENT, "telegram", "1002")

    def test_block_is_idempotent_and_keeps_the_first_block(self, world):
        first = _block(reason="first")
        again = _block(reason="second")
        assert again["blocked_at"] == first["blocked_at"] and again["reason"] == "first"
        assert len(world.list_channel_client_blocks(AGENT)) == 1

    def test_block_keeps_the_share_and_unblock_restores_it(self, world):
        from services import client_roster_service as svc
        _block()
        assert ALICE in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        svc.unblock_client(AGENT, "telegram", "1001")
        assert not world.is_channel_client_blocked(AGENT, "telegram", "1001")
        assert _roster()[("telegram", "1001")]["access"] == "granted"

    def test_an_unverified_client_is_blockable(self, world):
        _block(("telegram", "1002"))
        assert world.is_channel_client_blocked(AGENT, "telegram", "1002")

    def test_unblocking_a_client_that_is_not_blocked_is_409(self, world):
        from services import client_roster_service as svc
        assert _err(svc.unblock_client, AGENT, "telegram", "1001") == 409


class TestRevoke:
    def test_revoke_removes_the_share_and_a_restricted_agent_asks_again(self, world):
        from services import client_roster_service as svc
        out = svc.revoke_client(AGENT, "telegram", "1001", actor_username=OWNER)
        assert out["email"] == ALICE
        assert ALICE not in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        assert not world.email_has_agent_access(AGENT, ALICE)
        assert _roster()[("telegram", "1001")]["access"] == "none"

    def test_a_blocked_client_can_still_be_revoked(self, world):
        from services import client_roster_service as svc
        _block()
        svc.revoke_client(AGENT, "telegram", "1001", actor_username=OWNER)
        assert ALICE not in {s.shared_with_email for s in world.get_agent_shares(AGENT)}
        assert _roster()[("telegram", "1001")]["access"] == "blocked"

    @pytest.mark.parametrize("client", [("telegram", "1002"), ("whatsapp", WA)])
    def test_nothing_to_revoke_is_409(self, world, client):
        from services import client_roster_service as svc
        assert _err(svc.revoke_client, AGENT, *client, actor_username=OWNER) == 409


class TestOnlyThisAgentsClients:
    @pytest.mark.parametrize("client", [("telegram", "9999"), ("whatsapp", "1001"), ("slack", "U1")])
    def test_every_control_refuses_a_client_that_never_reached_this_agent(self, world, client):
        from services import client_roster_service as svc
        assert _err(_block, client) == 404
        assert _err(svc.unblock_client, AGENT, *client) == 404
        assert _err(svc.revoke_client, AGENT, *client, actor_username=OWNER) == 404
        assert world.list_channel_client_blocks(AGENT) == []

    def test_another_agents_client_is_not_found_here(self, world):
        assert _err(_block, ("telegram", "1001"), agent=OTHER) == 404


class TestRoutes:
    """The three routes take an owner and a person, and audit what they did."""

    def test_routes_are_owner_and_person_gated_and_audited(self):
        import inspect
        from routers import sharing
        for fn in (sharing.block_channel_client_endpoint, sharing.unblock_channel_client_endpoint,
                   sharing.revoke_channel_client_endpoint):
            sig = inspect.signature(fn)
            assert "get_owned_agent_by_name" in str(sig.parameters["agent_name"].annotation), fn.__name__
            src = inspect.getsource(fn)
            assert "assert_person(current_user)" in src and "_audit_client_control(" in src, fn.__name__
        routes = {(tuple(sorted(r.methods)), r.path): r for r in sharing.router.routes}
        for action in ("block", "unblock", "revoke"):
            r = routes[(("POST",), f"/api/agents/{{agent_name}}/clients/{action}")]
            assert any(getattr(d.dependency, "__name__", "") == "require_person" for d in r.dependencies)
