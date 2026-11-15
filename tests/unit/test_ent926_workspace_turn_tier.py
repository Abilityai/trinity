"""A Workspace turn is rated as the person chatting, and the route fence meets
the turns actually running (trinity-enterprise#926, found testing PR #3778 live).

Every Workspace turn is created `triggered_by="public"`, `source_channel="portal"`,
with the signed-in person's email and NO `source_user_id`. `turn_actor` knew a
person only by user id, so every Workspace turn — the owner's own included — was
rated a `visitor`: a shared person got the shell (and the owner's MCP servers)
while `visitor_shell` stayed on, which the ruling says a shared person never
gets. Now a Workspace turn is rated by that email's account: the owner or an
admin is `owner`, anyone else a shared `person`. A public-link turn's email is
what the visitor typed and is never read as an account.

And the fence only checked a call that named its turn in `X-Trinity-Execution-Id`:
a shell `curl` with the agent's key named none and reached every owner-tier
route mid-turn. `restricted_agent_turn` now also meets the ledger
(`running_turn_restricts`), so the header can only narrow.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

import dependencies as dep  # noqa: E402
from services import turn_tier_service as tts  # noqa: E402

pytestmark = pytest.mark.unit

AGENT = "agent-a"
OWNER_ID, ADMIN_ID, MATE_ID = 1, 2, 9
USERS = {
    OWNER_ID: {"id": OWNER_ID, "username": "owner", "role": "user", "email": "owner@example.com"},
    ADMIN_ID: {"id": ADMIN_ID, "username": "admin", "role": "admin", "email": "admin@example.com"},
    MATE_ID: {"id": MATE_ID, "username": "mate", "role": "operator", "email": "mate@example.com"},
}
BY_EMAIL = {u["email"]: u for u in USERS.values()}
SCHEDULES = {"s-mate": SimpleNamespace(agent_name=AGENT, owner_id=MATE_ID)}
RUNNING: list = []


def _row(email, *, channel="portal", trigger="public", schedule_id="__manual__", rid="e1"):
    return {"id": rid, "agent_name": AGENT, "source_user_id": None, "triggered_by": trigger,
            "schedule_id": schedule_id, "source_channel": channel, "source_user_email": email}


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: USERS.get(int(uid)))
    monkeypatch.setattr(dep.db, "get_user_by_email", lambda email: BY_EMAIL.get(email))
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u == "owner")
    monkeypatch.setattr(dep.db, "get_schedule", lambda sid: SCHEDULES.get(sid))
    monkeypatch.setattr(dep.db, "get_guardrails_config", lambda a: {})   # visitor_shell on
    RUNNING.clear()
    monkeypatch.setattr(dep.db, "get_running_turn_attribution", lambda agent: list(RUNNING))


def _actor(row):
    return tts.turn_actor(dep.db, AGENT, execution=row)


class TestAWorkspaceTurnIsThePersonChatting:
    def test_the_owner_in_the_workspace_is_the_owner(self):
        assert _actor(_row("owner@example.com")) == ("owner", None, None)

    def test_an_admin_in_the_workspace_is_the_owner_tier(self):
        assert _actor(_row("admin@example.com")) == ("owner", None, None)

    def test_a_shared_person_in_the_workspace_is_a_person_not_a_visitor(self):
        assert _actor(_row("mate@example.com")) == ("share", "person", MATE_ID)
        assert tts.turn_person(dep.db, AGENT, _row("mate@example.com")) == MATE_ID

    def test_a_shared_person_gets_no_shell_even_with_visitor_shell_on(self):
        tier, kind, _ = _actor(_row("mate@example.com"))
        assert tts.shell_allowed(dep.db, AGENT, tier, kind) is False

    def test_an_email_only_client_is_a_person_with_no_account(self):
        assert _actor(_row("client@example.org")) == ("share", "person", None)

    def test_an_unreadable_account_lookup_is_a_person_never_higher(self, monkeypatch):
        def boom(email):
            raise RuntimeError("db down")
        monkeypatch.setattr(dep.db, "get_user_by_email", boom)
        assert _actor(_row("owner@example.com")) == ("share", "person", None)

    @pytest.mark.parametrize("channel", [None, "", "public_link", "telegram", "slack"])
    def test_a_public_link_or_channel_email_never_lifts_a_visitor(self, channel):
        """A visitor can type the owner's address; only the Workspace stamp counts."""
        assert _actor(_row("owner@example.com", channel=channel)) == ("share", "visitor", None)

    def test_a_schedule_run_delivered_to_the_workspace_keeps_the_schedule_rule(self):
        row = _row("owner@example.com", trigger="schedule", schedule_id="s-mate")
        assert _actor(row) == ("share", "person", MATE_ID)   # the creator, not the audience


class TestTheFenceMeetsTheTurnsRunning:
    def _key(self, run=None, scope="agent"):
        return SimpleNamespace(id=OWNER_ID, username="owner", role="user", mcp_scope=scope,
                               agent_name=AGENT, connector_agent=None, portal_delegate=False,
                               is_event_loopback=False, vouched_source_agent=None,
                               _acting_execution_id=run)

    def test_a_key_with_no_header_is_fenced_while_a_shared_persons_turn_runs(self):
        RUNNING.append(_row("mate@example.com", rid="e-mate"))
        assert dep.restricted_agent_turn(self._key(run=None)) is True

    def test_naming_an_owner_turn_does_not_lift_the_fence(self, monkeypatch):
        RUNNING.append(_row("mate@example.com", rid="e-mate"))
        monkeypatch.setattr(dep.db, "get_execution",
                            lambda run: SimpleNamespace(**_row("owner@example.com", rid=run)))
        assert dep.restricted_agent_turn(self._key(run="e-owner")) is True

    def test_only_owner_turns_running_leaves_the_key_unfenced(self):
        RUNNING.append(_row("owner@example.com", rid="e-owner"))
        assert dep.restricted_agent_turn(self._key(run=None)) is False

    def test_a_visitor_turn_with_visitor_shell_on_is_not_fenced(self):
        RUNNING.append(_row("someone@example.org", channel="public_link", rid="e-pl"))
        assert dep.restricted_agent_turn(self._key(run=None)) is False

    def test_an_unreadable_ledger_fences(self, monkeypatch):
        def boom(agent):
            raise RuntimeError("db down")
        monkeypatch.setattr(dep.db, "get_running_turn_attribution", boom)
        assert dep.restricted_agent_turn(self._key(run=None)) is True

    @pytest.mark.parametrize("scope", [None, "user", "system"])
    def test_a_principal_that_is_not_an_agent_key_is_never_fenced(self, scope):
        RUNNING.append(_row("mate@example.com", rid="e-mate"))
        assert dep.restricted_agent_turn(self._key(run=None, scope=scope)) is False


def test_the_ledger_read_carries_the_workspace_columns(monkeypatch):
    """The real read on the unit island's SQLite returns what the rule reads."""
    pytest.importorskip("sqlalchemy")
    monkeypatch.undo()   # the autouse fakes sit on this same `db` object
    from database import db as real
    from db.write_params import TaskExecutionFields

    agent = f"ws-{uuid.uuid4().hex[:6]}"
    real.create_task_execution(agent_name=agent, message="hi", triggered_by="public",
                               fields=TaskExecutionFields(source_user_email="mate@example.com",
                                                          source_channel="portal"))
    rows = real.get_running_turn_attribution(agent)
    assert len(rows) == 1
    assert (rows[0]["source_channel"], rows[0]["source_user_email"]) == ("portal", "mate@example.com")


def test_the_workspace_channel_is_the_one_the_portal_stamps():
    from client_portal import service as portal
    assert tts.PORTAL_SOURCE_CHANNEL == portal.PORTAL_SOURCE_CHANNEL == "portal"
