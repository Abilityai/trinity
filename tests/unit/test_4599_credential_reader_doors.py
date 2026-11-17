"""The doors a `credential` ask could still be recorded through for a reader
who cannot fill it, and the refusals that named the wrong cause
(abilityai/trinity#4599, the follow-up to #4579 and #4501).

A credential ask ends answered at ONE door, the Workspace secret field, so the
reader recorded for it must be someone that field opens for. #4579 made the
native raise pick that reader with a chain (the primary, else the owner, else a
named refusal). Three things were left:

* the queue-file channel (`~/.trinity/operator-queue.json`) never read the
  chain: the agent picked the reader, the owner was never accepted, and an
  entry nobody could be recorded for became a row nobody could fill;
* neither reach rule read the client block, and `can_open_agent` did not read
  a deactivated account either, so a person who cannot sign in was recorded;
* a failed lookup, an agent with no primary person and an admin's re-address
  each told their reader to share the agent, whatever the real cause was.

Harness: the one of `test_4579_credential_primary_reach.py` (the unit island's
per-process SQLite, the REAL sink and the REAL secret-field door), plus the
REAL file poller over a fake agent client for the queue file.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Bound at collection time — see test_ent747_748_ask_discuss_dismiss.py.
import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.role_addressing  # noqa: E402,F401
import services.skill_gate_service  # noqa: E402,F401
import client_portal.db  # noqa: E402,F401
import client_portal.service  # noqa: E402,F401

CODE = "role_cannot_open_agent"
NAME = "ACME_API_TOKEN"
COULD_NOT_CHECK = "could not check right now"

_REAL_ON_ROSTER = client_portal.service.agent_on_roster
_REAL_OWNER_EMAIL = services.ask_service._owner_email


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-4599-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"alice": _uniq("alice"), "bob": _uniq("bob"), "carol": _uniq("carol")}


@pytest.fixture(autouse=True)
def shared(monkeypatch):
    """The emails the agent is on the roster of. Empty unless a test adds one."""
    import client_portal.service as portal_service
    emails = set()
    monkeypatch.setattr(
        portal_service, "agent_on_roster",
        lambda agent_name, email, include_owned=True: (email or "").lower() in emails)
    return emails


class _Provider:
    """A fake assignments provider: `kinds` maps a kind to the seated emails,
    `role_ids` a canon role id to them. A missing key is "no answer"."""

    def __init__(self, kinds, role_ids=None):
        self.kinds = kinds
        self.role_ids = role_ids or {}

    def assignment_for(self, agent_name, triggered_by):
        return None

    def people_for(self, agent_name, role):
        value = self.kinds.get(role)
        return None if value is None else {"emails": value}

    def people_for_role_id(self, agent_name, role_id):
        value = self.role_ids.get(role_id)
        return None if value is None else {"emails": value}


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; announcements run synchronously."""
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services.rate_limiter import RateLimitResult

    audit, sent = [], []

    class _Audit:
        async def log(self, **kw):
            audit.append(kw)
            return "evt"

    class _WS:
        async def broadcast(self, message):
            sent.append(json.loads(message))

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: asyncio.run(factory()))
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(oqs, "_workspace_attachment",
                        lambda agent, email, *a, **k: (f"thread-{email}", False))
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    yield SimpleNamespace(audit=audit, sent=sent)
    assignment_provider.clear_provider()


def _seat(role_ids=None, **kinds):
    from services import assignment_provider
    assignment_provider.register_provider(_Provider(kinds, role_ids))


def _body(*, kind="credential", to="primary", request_id=None):
    body = {"request_id": request_id or f"r-{uuid.uuid4().hex[:10]}", "type": kind,
            "title": f"Needs {NAME}", "question": "Please provide the token.", "to": to}
    if kind == "credential":
        body["context"] = {"credential_name": NAME}
    if kind == "approval":
        body["options"] = ["approve", "reject"]
    return body


def _raise(agent, body=None, **kw):
    from services import ask_service
    return ask_service.raise_ask(agent, body or _body(**kw), raised_by="agent", channel="mcp")


def _rows(agent):
    from database import db
    return db.list_operator_queue_items(agent_name=agent, limit=50)


def _pending_credential_rows(agent):
    return [r for r in _rows(agent)
            if r.get("type") == "credential" and r.get("status") == "pending"]


def _resolved(row):
    stored = row["resolved_to"]
    return sorted(json.loads(stored) if isinstance(stored, str) else (stored or []))


def _door_opens(row, email=None, *, is_platform=False):
    """The secret field's own door (`_owned_ask`), asked as `email` (default:
    the row's reader)."""
    from client_portal.asks import service as asks
    try:
        asks._owned_ask(row["id"], email or row["addressed_to_email"], is_platform)
    except asks.AskError:
        return False
    return True


def _refused(agent, **kw):
    from services import ask_service
    with pytest.raises(ask_service.AskRejected) as exc:
        _raise(agent, **kw)
    return exc.value


@pytest.fixture
def owner(monkeypatch):
    """An owner with an email, in the stubbed mode; on the roster only when a
    test adds the address to `shared`."""
    from services import ask_service as svc
    email = _uniq("owner")
    monkeypatch.setattr(svc, "_owner_email", lambda agent_name: email)
    return email


@pytest.fixture
def real_roster(monkeypatch, real_db, agent):
    """The real `agent_on_roster` and owner lookup, over a real owner account
    that owns `agent`. Helpers add accounts, shares, blocks and suspensions."""
    import client_portal.db as portal_db
    import client_portal.service as portal_service
    from db_models import UserCreate
    from services import ask_service as svc

    monkeypatch.setattr(portal_service, "agent_on_roster", _REAL_ON_ROSTER)
    monkeypatch.setattr(svc, "_owner_email", _REAL_OWNER_EMAIL)

    def account(role="user"):
        email = _uniq(role)
        real_db.create_user(UserCreate(username=email.split("@")[0], role=role, email=email))
        return email

    def suspend(email):
        from sqlalchemy import update
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(update(users).where(users.c.email == email)
                         .values(suspended_at="2026-11-14T00:00:00Z"))

    def block(email):
        portal_db.block_client(email, "2026-11-17T00:00:00Z", None, "admin@example.com", "test")

    owner_email = account("creator")
    owner_username = owner_email.split("@")[0]
    real_db.register_agent_owner(agent, owner_username)

    def share(email):
        assert real_db.share_agent(agent, owner_username, email) is not None

    return SimpleNamespace(owner=owner_email, account=account, share=share,
                           suspend=suspend, block=block)


# ---------------------------------------------------------------------------
# Item 2: a blocked client is never the reader of a credential ask
# ---------------------------------------------------------------------------

class TestBlockedReader:
    def test_a_blocked_primary_with_a_share_is_skipped_and_the_owner_is_asked(
            self, agent, real_roster):
        """The block leaves the share row in place (`block_client` writes the
        block only), and the Workspace door refuses a blocked email at sign-in
        and on every request: recorded as the reader, they could never fill
        the field while nobody else may."""
        from services import role_addressing
        guest = _uniq("guest")
        real_roster.share(guest)
        assert role_addressing.on_workspace_roster(agent, guest) is True, "the control"
        real_roster.block(guest)
        assert role_addressing.on_workspace_roster(agent, guest) is False
        _seat(primary=[guest])
        _raise(agent)
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == real_roster.owner
        assert _door_opens(row, is_platform=True)

    def test_a_blocked_owner_does_not_stand_in(self, agent, people, real_roster):
        """The Workspace door refuses a blocked platform account too (the block
        is on the identity), so the owner is checked like anyone else."""
        real_roster.block(real_roster.owner)
        _seat(primary=[people["alice"]])
        assert _refused(agent).code == CODE
        assert _rows(agent) == []

    def test_a_block_read_that_fails_refuses_and_says_it_could_not_check(
            self, agent, real_roster, monkeypatch):
        import client_portal.db as portal_db

        def boom(email):
            raise RuntimeError("blocks unreadable")

        monkeypatch.setattr(portal_db, "is_client_blocked", boom)
        refusal = _refused(agent)
        assert refusal.code == CODE and COULD_NOT_CHECK in refusal.message
        assert _rows(agent) == []


class TestCanOpenAgent:
    """`can_open_agent` (#4501) is the rule for a decision that can be made in
    the Workspace OR in Operations. A block closes the Workspace only, so it
    applies to an email with no account and to an account that can only use
    the Workspace; a deactivated account signs in nowhere."""

    def test_a_blocked_email_with_no_account_is_not_reachable(self, agent, real_roster):
        from services import role_addressing
        guest = _uniq("guest")
        real_roster.share(guest)
        assert role_addressing.can_open_agent(agent, guest) is True, "the control"
        real_roster.block(guest)
        assert role_addressing.can_open_agent(agent, guest) is False

    def test_a_blocked_workspace_only_account_is_not_reachable(self, agent, real_roster):
        from services import role_addressing
        member = real_roster.account("user")
        real_roster.share(member)
        assert role_addressing.can_open_agent(agent, member) is True, "the control"
        real_roster.block(member)
        assert role_addressing.can_open_agent(agent, member) is False

    @pytest.mark.parametrize("role", ["operator", "admin"])
    def test_a_blocked_operator_or_admin_stays_reachable(self, agent, real_roster, role):
        """They can still decide in Operations: `get_current_user` does not
        read the client block."""
        from services import role_addressing
        person = real_roster.account(role)
        real_roster.share(person)
        real_roster.block(person)
        assert role_addressing.can_open_agent(agent, person) is True

    def test_a_deactivated_account_is_not_reachable(self, agent, real_roster):
        from services import role_addressing
        gone = real_roster.account("operator")
        real_roster.share(gone)
        assert role_addressing.can_open_agent(agent, gone) is True, "the control"
        real_roster.suspend(gone)
        assert role_addressing.can_open_agent(agent, gone) is False

    def test_a_blocked_approver_is_not_recorded(self, agent, real_roster):
        guest, other = _uniq("guest"), _uniq("other")
        real_roster.share(guest)
        real_roster.share(other)
        real_roster.block(guest)
        _seat(approver=[guest, other])
        _raise(agent, kind="approval", to="approver")
        [row] = _rows(agent)
        assert _resolved(row) == [other]

    def test_a_deactivated_approver_refuses_the_raise(self, agent, real_roster):
        gone = real_roster.account("user")
        real_roster.share(gone)
        real_roster.suspend(gone)
        _seat(approver=[gone])
        refusal = _refused(agent, kind="approval", to="approver")
        assert refusal.code == CODE and COULD_NOT_CHECK not in refusal.message
        assert _rows(agent) == []

    def test_an_unreadable_lookup_is_unknown_and_still_not_reachable(self, agent, people, monkeypatch):
        from services import role_addressing

        def boom(email):
            raise RuntimeError("users unreadable")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        assert role_addressing.agent_reach(agent, people["alice"]) is None
        assert role_addressing.can_open_agent(agent, people["alice"]) is False


# ---------------------------------------------------------------------------
# Items 3 and 5: each refusal of the chain names its real cause
# ---------------------------------------------------------------------------

class TestRaiseWording:
    def test_nobody_seated_and_an_owner_who_cannot_open_names_no_primary_person(
            self, agent, owner):
        """The assignments provider answered: nobody fills primary. The owner
        stood in and cannot open the agent. There is no primary person to
        share it with."""
        _seat(primary=[])
        refusal = _refused(agent)
        assert refusal.code == CODE
        assert f"{agent} has no primary person" in refusal.message
        assert "assign a primary person" in refusal.message
        assert "with its primary person" not in refusal.message
        assert "@" not in refusal.message

    def test_an_owner_who_is_the_primary_is_not_told_to_share_or_to_assign(self, agent, owner):
        """No assignments module answers, so the owner IS the primary person:
        an agent cannot be shared with its own owner, and nobody can be
        assigned."""
        refusal = _refused(agent)
        assert refusal.code == CODE
        assert f"The primary person for {agent} is its owner" in refusal.message
        assert "share" not in refusal.message.lower()
        assert "assign" not in refusal.message.lower()
        assert "@" not in refusal.message

    def test_a_seated_primary_who_cannot_open_keeps_the_share_remedy(self, agent, people):
        _seat(primary=[people["alice"]])
        refusal = _refused(agent)
        assert refusal.code == CODE and "share" in refusal.message.lower()
        assert COULD_NOT_CHECK not in refusal.message

    def test_a_system_agent_is_told_sharing_cannot_help(self, agent, people, monkeypatch):
        """Both rosters exclude system agents, so nobody ever has one."""
        from services import role_addressing
        monkeypatch.setattr(role_addressing.db, "is_system_agent", lambda name: name == agent)
        _seat(primary=[people["alice"]])
        refusal = _refused(agent)
        assert refusal.code == CODE
        assert "is a system agent" in refusal.message
        assert "share" not in refusal.message.lower()

    def test_a_roster_that_cannot_be_read_says_so_and_names_nobody(
            self, agent, people, owner, monkeypatch, caplog):
        import client_portal.service as portal_service

        def boom(agent_name, email, include_owned=True):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_service, "agent_on_roster", boom)
        _seat(primary=[people["alice"]])
        with caplog.at_level(logging.INFO):
            refusal = _refused(agent)
        assert refusal.code == CODE, "the code is kept: one code, another sentence"
        assert refusal.message == (
            f"The platform could not check right now who can open {agent} in their Workspace, "
            "so this ask was not filed. Nothing is wrong with your request. Try again in a "
            "few minutes.")
        assert "share" not in refusal.message.lower()
        assert _rows(agent) == []
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert "could not check" in logged
        assert people["alice"] not in logged and owner not in logged

    def test_an_account_lookup_that_fails_is_not_read_as_deactivated(
            self, agent, people, shared, monkeypatch):
        """`email_account_suspended` answers "deactivated" when its lookup
        raises, which read as "not a reader" and hid the outage."""
        from services import role_addressing
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])

        def boom(email):
            raise RuntimeError("users unreadable")

        monkeypatch.setattr(role_addressing.db, "is_email_account_suspended", boom)
        refusal = _refused(agent)
        assert refusal.code == CODE and COULD_NOT_CHECK in refusal.message

    def test_a_primary_who_could_not_be_checked_is_not_handed_to_the_owner(
            self, agent, people, shared, owner, monkeypatch):
        """Nothing moves a credential ask back once it is filed for the owner,
        so an unchecked primary refuses instead of handing it on."""
        import client_portal.service as portal_service
        shared.add(owner)

        def flaky(agent_name, email, include_owned=True):
            if email == people["alice"]:
                raise RuntimeError("roster down")
            return email in shared

        monkeypatch.setattr(portal_service, "agent_on_roster", flaky)
        _seat(primary=[people["alice"]])
        refusal = _refused(agent)
        assert refusal.code == CODE and COULD_NOT_CHECK in refusal.message
        assert _rows(agent) == []

    @pytest.mark.parametrize("to", ["approver", "role:head-of-sales"])
    def test_an_approver_or_role_lookup_that_fails_says_it_could_not_check(
            self, agent, people, shared, monkeypatch, to):
        from services import role_addressing
        shared.add(people["alice"])
        _seat({"head-of-sales": [people["alice"]]}, approver=[people["alice"]])

        def boom(email):
            raise RuntimeError("users unreadable")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        refusal = _refused(agent, kind="approval", to=to)
        assert refusal.code == CODE
        assert refusal.message == (
            f"The platform could not check right now who can open {agent}, so this ask was "
            "not filed. Nothing is wrong with your request. Try again in a few minutes.")
        assert "cause" not in refusal.extra, "the cause never becomes a field an agent reads"

    def test_a_lookup_that_fails_once_is_not_read_again_as_cannot_open(
            self, agent, people, shared, monkeypatch):
        """Each person's reach is read ONCE. Read a second time to find the
        cause, a lookup that had recovered answered "cannot open" for a
        person who can, with the share remedy."""
        from services import role_addressing
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        real, calls = role_addressing.db.get_user_by_email, []

        def flaky(email):
            calls.append(email)
            if len(calls) == 1:
                raise RuntimeError("users unreadable")
            return real(email)

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", flaky)
        refusal = _refused(agent, kind="approval", to="approver")
        assert COULD_NOT_CHECK in refusal.message and "share" not in refusal.message.lower()
        assert calls == [people["alice"]]

    def test_an_owner_who_could_not_be_checked_says_so(
            self, agent, people, shared, owner, monkeypatch):
        import client_portal.service as portal_service

        def flaky(agent_name, email, include_owned=True):
            if email == owner:
                raise RuntimeError("roster down")
            return email in shared

        monkeypatch.setattr(portal_service, "agent_on_roster", flaky)
        _seat(primary=[people["alice"]])
        refusal = _refused(agent)
        assert refusal.code == CODE and COULD_NOT_CHECK in refusal.message

    def test_control_an_approver_who_cannot_open_keeps_the_share_remedy(self, agent, people):
        _seat(approver=[people["alice"]])
        refusal = _refused(agent, kind="approval", to="approver")
        assert refusal.code == CODE and "share" in refusal.message.lower()


class TestGateWording:
    """A gated skill's approver. `approver_cannot_open_agent` is a STANDING
    refusal: a room records the post as refused for good. A failed lookup
    clears by itself, so it answers with the gate's own retry code."""

    def test_a_lookup_that_fails_is_the_gates_retry_refusal(self, agent, people, shared, monkeypatch):
        from services import role_addressing, skill_gate_service as sgs
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        assert sgs._approvers(agent, "approver") == [people["alice"]], "the control"

        def boom(email):
            raise RuntimeError("users unreadable")

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", boom)
        with pytest.raises(sgs.SkillGateRefused) as exc:
            sgs._approvers(agent, "approver")
        assert (exc.value.status_code, exc.value.code) == (503, "gate_unavailable")
        assert exc.value.code not in sgs.STANDING_REFUSALS
        assert "Could not check who can open" in str(exc.value)
        assert sgs.approver_reason(agent, "approver") == "not_shared", (
            "the Skills tab keeps the label it showed before")

    def test_a_lookup_that_fails_once_is_not_the_standing_refusal(
            self, agent, people, shared, monkeypatch):
        """One read per person: a lookup that failed and then recovered must
        not leave the refusal a room records for good."""
        from services import role_addressing, skill_gate_service as sgs
        shared.add(people["alice"])
        _seat(approver=[people["alice"]])
        real, calls = role_addressing.db.get_user_by_email, []

        def flaky(email):
            calls.append(email)
            if len(calls) == 1:
                raise RuntimeError("users unreadable")
            return real(email)

        monkeypatch.setattr(role_addressing.db, "get_user_by_email", flaky)
        with pytest.raises(sgs.SkillGateRefused) as exc:
            sgs._approvers(agent, "approver")
        assert exc.value.code == "gate_unavailable" and calls == [people["alice"]]

    def test_control_an_approver_who_cannot_open_is_still_the_standing_refusal(self, agent, people):
        from services import skill_gate_service as sgs
        _seat(approver=[people["alice"]])
        with pytest.raises(sgs.SkillGateRefused) as exc:
            sgs._approvers(agent, "approver")
        assert (exc.value.status_code, exc.value.code) == (422, "approver_cannot_open_agent")


# ---------------------------------------------------------------------------
# Item 4 and its twins: an admin re-addressing by rule reads the real cause
# ---------------------------------------------------------------------------

class TestAdminReaddressWording:
    def _admin(self):
        from models import User
        from services import ask_service
        return ask_service.Actor(email="root@example.com",
                                 user=User(id=3, username="root", email="root@example.com", role="admin"))

    def _refusal(self, row):
        from services import ask_service as svc
        with pytest.raises(svc.ReaddressRefused) as exc:
            svc.admin_readdress(row["id"], to="rule", reason="check", actor=self._admin())
        return exc.value

    def _filed_for_alice(self, agent, people, shared, **kw):
        shared.add(people["alice"])
        _seat(primary=[people["alice"]], approver=[people["alice"]])
        _raise(agent, **kw)
        [row] = _rows(agent)
        return row

    def test_a_seated_primary_who_cannot_open_is_told_to_share(self, agent, people, shared):
        row = self._filed_for_alice(agent, people, shared)
        _seat(primary=[people["bob"]])
        refusal = self._refusal(row)
        assert (refusal.status, refusal.code) == (409, "rule_refused")
        assert refusal.message == (
            "The primary person for this agent cannot open it in their Workspace, and neither "
            "can its owner, so the card stays as it is. Share the agent with its primary "
            "person and re-address the card again, or end it with a reason.")

    def test_nobody_seated_is_told_to_assign_a_primary_person(self, agent, people, shared, owner):
        row = self._filed_for_alice(agent, people, shared)
        _seat(primary=[])
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused"
        assert "has no primary person" in refusal.message
        assert "Assign a primary person" in refusal.message

    def test_an_owner_who_is_the_primary_is_told_to_check_the_owners_access(
            self, agent, people, shared, owner):
        from services import assignment_provider
        row = self._filed_for_alice(agent, people, shared)
        assignment_provider.clear_provider()
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused"
        assert "The primary person for this agent is its owner" in refusal.message
        assert "Check the owner's access" in refusal.message
        assert "Share the agent" not in refusal.message

    def test_a_system_agent_is_told_only_to_end_the_card(self, agent, people, shared, monkeypatch):
        from services import role_addressing
        row = self._filed_for_alice(agent, people, shared)
        _seat(primary=[people["bob"]])
        monkeypatch.setattr(role_addressing.db, "is_system_agent", lambda name: name == agent)
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused"
        assert "This is a system agent" in refusal.message
        assert "Share" not in refusal.message and "re-address" not in refusal.message

    def test_a_lookup_that_fails_is_told_to_try_again(self, agent, people, shared, monkeypatch):
        import client_portal.service as portal_service
        row = self._filed_for_alice(agent, people, shared)

        def boom(agent_name, email, include_owned=True):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_service, "agent_on_roster", boom)
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused"
        assert COULD_NOT_CHECK in refusal.message and "Try again" in refusal.message
        assert "end it with a reason" not in refusal.message

    def test_an_approver_who_cannot_open_is_told_to_share_not_that_nobody_is_found(
            self, agent, people, shared):
        row = self._filed_for_alice(agent, people, shared, kind="approval", to="approver")
        _seat(approver=[people["bob"]])
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused"
        assert "Nobody can be found" not in refusal.message
        assert "approver role for this agent cannot open it" in refusal.message
        assert "Share the agent with them" in refusal.message

    def test_control_an_unfilled_role_keeps_nobody_can_be_found(self, agent, people, shared):
        row = self._filed_for_alice(agent, people, shared, kind="approval", to="approver")
        _seat(primary=[people["alice"]])
        refusal = self._refusal(row)
        assert refusal.code == "rule_refused" and "Nobody can be found" in refusal.message

    def test_an_unchanged_role_card_is_not_offered_the_admin_seat(self, agent, people, shared):
        """`to: admin` is always refused `admin_seat_unsupported` for a card
        addressed by role, so the refusal must not offer it."""
        from services import ask_service as svc
        row = self._filed_for_alice(agent, people, shared)
        refusal = self._refusal(row)
        assert refusal.code == "audience_unchanged"
        assert "to: admin" not in refusal.message
        assert "End it with a reason instead." in refusal.message
        with pytest.raises(svc.ReaddressRefused) as exc:
            svc.admin_readdress(row["id"], to="admin", reason="check", actor=self._admin())
        assert exc.value.code == "admin_seat_unsupported", "the move the old text offered"


class TestReaddressRole:
    def test_a_refused_credential_row_stays_while_a_question_row_moves(
            self, agent, people, shared, sink):
        """A refusal still means "unchanged" for the seat write that calls
        this, and it does not stop the rows the rule can move."""
        from services import ask_service as svc
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        _raise(agent)
        _raise(agent, kind="question")
        _seat(primary=[people["bob"]])
        out = svc.readdress_role(agent, "primary", reason=svc.ASSIGNMENT_CHANGED)
        assert out == {"readdressed": 1, "unchanged": 1}
        by_type = {r["type"]: _resolved(r) for r in _rows(agent)}
        assert by_type == {"credential": [people["alice"]], "question": [people["bob"]]}


# ---------------------------------------------------------------------------
# Item 1: the queue-file channel reads the same chain
# ---------------------------------------------------------------------------

def _entry(rid, **over):
    e = {"id": rid, "type": "credential", "status": "pending", "title": f"Needs {NAME}",
         "question": "Please provide the token.", "context": {"credential_name": NAME}}
    e.update(over)
    return e


@pytest.fixture
def poller(monkeypatch, agent):
    """The REAL file poller over the real database and a fake agent client
    whose file is `state["content"]`."""
    import services.operator_queue_service as oqs

    state = {"content": json.dumps({"requests": []}), "writes": []}
    client = MagicMock()

    async def _read(path, timeout=5.0):
        return {"success": True, "content": state["content"]}

    async def _write(path, content, **kw):
        state["writes"].append(json.loads(content))
        state["content"] = content
        return {"success": True}

    client.read_file = AsyncMock(side_effect=_read)
    client.write_file = AsyncMock(side_effect=_write)
    monkeypatch.setattr(oqs.deps, "AgentClient", lambda name: client)
    monkeypatch.setattr(oqs.deps, "_audit_sync", AsyncMock())
    monkeypatch.setattr(oqs, "_websocket_manager", None)
    oqs.reset_alert_budget_state()
    svc = oqs.OperatorQueueSyncService()

    def put(*entries):
        state["content"] = json.dumps({"requests": list(entries)})

    def run(cycles=1):
        for _ in range(cycles):
            asyncio.run(svc._sync_agent(agent))

    def marker():
        return json.loads(state["content"]).get("platform", {}).get("ingestion")

    return SimpleNamespace(put=put, run=run, marker=marker, state=state, svc=svc)


class TestQueueFile:
    def test_an_entry_that_names_nobody_is_filed_for_the_primary(
            self, agent, people, shared, poller):
        """The platform picks the reader; the entry carries none."""
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == people["alice"]
        assert row["channel"] == "file"
        assert (row.get("context") or {}).get("workspace_session_id") == f"thread-{people['alice']}"
        assert _door_opens(row)
        assert poller.marker() is None

    def test_a_primary_off_the_roster_hands_the_entry_to_the_owner(
            self, agent, people, shared, owner, poller):
        """The owner is never on the SHARED roster, so the old veto refused
        every owner: on an install without an assignments module no file
        credential entry was ever addressed."""
        shared.add(owner)
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == owner and _door_opens(row)

    def test_the_owner_is_the_reader_over_real_ownership(self, agent, real_roster, poller):
        poller.put(_entry("cred-1"))
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == real_roster.owner
        assert _door_opens(row, is_platform=True)
        assert not _door_opens(row, is_platform=False), (
            "an owner signed in as an outside client does not see owned agents")

    def test_the_addressee_the_agent_wrote_is_ignored(self, agent, people, shared, poller):
        """Before, the agent picked the reader and the platform only vetoed."""
        shared.update({people["alice"], people["bob"]})
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1", addressed_to_email=people["bob"]))
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == people["alice"]

    def test_an_entry_nobody_can_fill_is_held_by_name_and_files_no_row(
            self, agent, people, poller, sink):
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run(2)
        assert _rows(agent) == [], "a credential row with no reader can only be cancelled"
        marker = poller.marker()
        assert marker["reason"] == CODE and marker[CODE] == ["cred-1"]
        [entry] = json.loads(poller.state["content"])["requests"]
        assert entry["status"] == "pending"
        assert len(poller.state["writes"]) == 1, "an unchanged hold is not rewritten"

    def test_a_held_entry_is_ingested_once_a_reader_can_open_the_agent(
            self, agent, people, shared, poller):
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run()
        assert poller.marker()["reason"] == CODE
        shared.add(people["alice"])
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert row["addressed_to_email"] == people["alice"]
        assert poller.marker() is None

    def test_two_primaries_are_held_as_no_person(self, agent, people, shared, poller):
        shared.update({people["alice"], people["bob"]})
        _seat(primary=[people["alice"], people["bob"]])
        poller.put(_entry("cred-1"))
        poller.run()
        assert _rows(agent) == []
        assert poller.marker()["no_person"] == ["cred-1"]

    def test_a_lookup_that_fails_holds_the_entry_without_a_marker(
            self, agent, people, shared, poller, monkeypatch):
        """Not the agent's to fix, and a marker has no sentence: the entry is
        read again on the next cycle."""
        import client_portal.service as portal_service
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        real = portal_service.agent_on_roster

        def boom(agent_name, email, include_owned=True):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_service, "agent_on_roster", boom)
        poller.put(_entry("cred-1"))
        poller.run()
        assert _rows(agent) == [] and poller.marker() is None
        monkeypatch.setattr(portal_service, "agent_on_roster", real)
        poller.run()
        assert len(_pending_credential_rows(agent)) == 1

    @pytest.mark.parametrize("bad", [
        {"context": {"credential_name": "not a name"}},
        {"context": {"credential_name": "PATH"}},
        {"context": {}},
        {"context": None},
        {"context": "ACME_API_TOKEN"},
        {"options": ["approve", "reject"]},
        {"context": {"credential_name": NAME, "credential_destination": "elsewhere"}},
        {"context": {"credential_name": NAME, "blob": "x" * 9000}},
    ], ids=["malformed-name", "reserved-name", "no-name", "no-context", "context-not-an-object",
            "options", "bad-destination", "context-too-large"])
    def test_an_entry_that_fails_the_kinds_own_checks_is_held(
            self, agent, people, shared, poller, bad):
        """The checks `raise_ask` runs for the kind. Before, the file skipped
        them all and the row reached a field that could not work."""
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-bad", **bad), _entry("cred-good"))
        poller.run()
        assert [r["request_id"] for r in _rows(agent)] == ["cred-good"]
        marker = poller.marker()
        assert marker["reason"] == "invalid_credential"
        assert marker["invalid_credential"] == ["cred-bad"]

    def test_an_outage_leaves_a_standing_hold_marker_as_it_is(
            self, agent, people, poller, monkeypatch):
        """A cycle that could not check decides nothing about a hold: clearing
        the marker would tell the agent its entry was no longer held."""
        import client_portal.service as portal_service
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run()
        before = poller.marker()
        assert before["reason"] == CODE

        def boom(agent_name, email, include_owned=True):
            raise RuntimeError("roster down")

        monkeypatch.setattr(portal_service, "agent_on_roster", boom)
        poller.run(2)
        assert poller.marker() == before
        assert len(poller.state["writes"]) == 1

    def test_destination_checks_are_bounded_per_cycle_and_are_no_flood(
            self, agent, people, shared, poller, monkeypatch):
        """The destination check reads the endpoint registry, so an
        agent-authored file may cost ten a cycle. An entry past the bound is
        not judged, not named and not an alert: it waits for the named ones."""
        from services import credential_destination
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        checked = []
        real = credential_destination.validate
        monkeypatch.setattr(credential_destination, "validate",
                            lambda destination, **kw: checked.append(1) or real(destination, **kw))
        flood = AsyncMock()
        monkeypatch.setattr(poller.svc, "_maybe_emit_flood_alert", flood)
        bad = {"credential_name": NAME, "credential_destination": "elsewhere"}
        poller.put(*[_entry(f"cred-{i:02d}", context=bad) for i in range(13)])
        poller.run()
        assert len(checked) == 10
        assert poller.marker()["invalid_credential"] == [f"cred-{i:02d}" for i in range(10)]
        assert _rows(agent) == [] and flood.await_count == 0
        poller.put(*[_entry(f"cred-{i:02d}", context=bad) for i in range(10, 13)])
        poller.run()
        assert poller.marker()["invalid_credential"] == ["cred-10", "cred-11", "cred-12"]

    def test_a_destination_is_stored_validated_and_is_not_a_rewrite_next_cycle(
            self, agent, people, shared, poller, monkeypatch):
        """The row stores the destination as the validator normalises it, as
        the native raise does; the entry keeps what the agent wrote."""
        from services import credential_destination
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        stored = {"instance": "Partner", "endpoint_id": "ep-1",
                  "instance_url": "https://partner.example.com", "agent_name": "billing"}
        monkeypatch.setattr(credential_destination, "validate",
                            lambda destination, agent_name: dict(stored))
        poller.put(_entry("cred-1", context={
            "credential_name": NAME,
            "credential_destination": {"instance": "partner", "agent_name": "billing"}}))
        poller.run(3)
        [row] = _pending_credential_rows(agent)
        assert row["context"]["credential_destination"] == stored
        assert credential_destination.destination_of(row["context"]) == stored
        assert row["sync_state"] == "confirmed" and not row.get("sync_detail")

    def test_the_chain_is_read_once_per_cycle_whatever_the_file_holds(
            self, agent, people, poller, monkeypatch):
        from services import role_addressing
        calls = []
        real = role_addressing.resolve_credential_reader

        def counting(agent_name, **kw):
            calls.append(agent_name)
            return real(agent_name, **kw)

        monkeypatch.setattr(role_addressing, "resolve_credential_reader", counting)
        _seat(primary=[people["alice"]])
        poller.put(*[_entry(f"cred-{i}") for i in range(25)])
        poller.run()
        assert calls == [agent]
        assert len(poller.marker()[CODE]) == 10, "the marker names at most ten ids"

    def test_the_next_cycle_keeps_the_row_confirmed_and_the_field_open(
            self, agent, people, shared, poller, real_db):
        """The platform stored an addressee the entry does not carry. Compared
        as a rewrite, the row turned `changed` five seconds later and the
        secret field refused it for good (`item_diverged`)."""
        from client_portal.asks import service as asks
        shared.update({people["alice"], people["bob"]})
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1", addressed_to_email=people["bob"]))
        poller.run(3)
        [row] = _pending_credential_rows(agent)
        assert row["sync_state"] == "confirmed" and not row.get("sync_detail")
        assert asks._owned_ask(row["id"], people["alice"], False)["sync_state"] == "confirmed"

    def test_control_a_rewritten_title_still_reads_as_changed(
            self, agent, people, shared, poller):
        shared.add(people["alice"])
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"))
        poller.run()
        poller.put(_entry("cred-1", title="Needs something else"))
        poller.run()
        [row] = _pending_credential_rows(agent)
        assert (row["sync_state"], row["sync_detail"]) == ("changed", "title")

    def test_a_question_entry_keeps_the_roster_rule_it_had(self, agent, people, shared, poller):
        shared.add(people["bob"])
        _seat(primary=[people["alice"]])
        poller.put(_entry("q-1", type="question", context={}, addressed_to_email=people["bob"]),
                   _entry("q-2", type="question", context={}, addressed_to_email=people["carol"]))
        poller.run()
        by_id = {r["request_id"]: r["addressed_to_email"] for r in _rows(agent)}
        assert by_id == {"q-1": people["bob"], "q-2": None}
        assert poller.marker() is None

    def test_no_credential_row_from_the_file_waits_on_a_reader_the_field_refuses(
            self, agent, people, shared, poller):
        """The state that must not exist, whatever the remedy."""
        shared.add(people["bob"])
        _seat(primary=[people["alice"]])
        poller.put(_entry("cred-1"), _entry("cred-2", addressed_to_email=people["bob"]),
                   _entry("cred-3", addressed_to_email=people["alice"]))
        poller.run(2)
        stuck = [r["request_id"] for r in _pending_credential_rows(agent)
                 if not r.get("addressed_to_email") or not _door_opens(r)]
        assert stuck == []


class TestFingerprint:
    """`changed_fields` for a credential row: the addressee and the stored
    destination are the platform's, written at ingest."""

    def _row(self, **over):
        row = {"type": "credential", "title": f"Needs {NAME}", "question": "q", "options": None,
               "expires_at": None, "priority": "medium",
               "context": {"credential_name": NAME}, "addressed_to_email": "reader@example.com"}
        row.update(over)
        return row

    def _req(self, **over):
        req = {"id": "cred-1", "type": "credential", "title": f"Needs {NAME}", "question": "q",
               "context": {"credential_name": NAME}}
        req.update(over)
        return req

    def test_the_platforms_addressee_is_not_a_rewrite(self):
        from services.operator_queue_service import filechannel
        assert filechannel.changed_fields(self._row(), self._req()) == []
        assert filechannel.changed_fields(
            self._row(), self._req(addressed_to_email="other@example.com")) == []

    def test_the_stored_destination_is_not_a_rewrite(self):
        from services.operator_queue_service import filechannel
        stored = {"credential_name": NAME, "workspace_session_id": "thread-1",
                  "credential_destination": {"instance": "Partner", "endpoint_id": "ep-1",
                                             "instance_url": "https://partner.example.com",
                                             "agent_name": "billing"}}
        written = {"credential_name": NAME,
                   "credential_destination": {"instance": "partner", "agent_name": "billing"}}
        assert filechannel.changed_fields(self._row(context=stored), self._req(context=written)) == []

    def test_control_another_name_is_a_rewrite(self):
        from services.operator_queue_service import filechannel
        assert filechannel.changed_fields(
            self._row(), self._req(context={"credential_name": "OTHER_TOKEN"})) == ["context"]

    def test_control_a_question_row_still_compares_its_addressee(self):
        from services.operator_queue_service import filechannel
        row = self._row(type="question", context={})
        assert filechannel.changed_fields(
            row, self._req(type="question", context={},
                           addressed_to_email="other@example.com")) == ["addressee"]

    def test_every_hold_name_is_ranked(self):
        """`_stronger_hold` indexes the rank: an unranked name raises out of
        the whole sync, and the agent's answers stop being written back."""
        from services.operator_queue_service import filechannel
        for hold in filechannel.CREDENTIAL_HOLDS:
            assert filechannel._stronger_hold(None, hold) == hold
            assert filechannel._stronger_hold(filechannel.HOLD_QUEUE_FULL, hold) == filechannel.HOLD_QUEUE_FULL
        marker = filechannel._ingestion_marker(
            filechannel.HOLD_QUEUE_FULL, credential={CODE: [f"c{i}" for i in range(12)]})
        assert marker["reason"] == "queue_full" and len(marker[CODE]) == 10
