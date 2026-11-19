"""AC3: the caller's reads in a shared person's turn stay that person's
(abilityai/trinity-enterprise#1148, decision 15).

A share holder P chats with agent A, and A delegates to agent B that P cannot
open. B's turn runs at B's own tier (rule A) and B's row records P. A's key,
still acting in P's turn, later reads B's rows (`get_execution_result`):
`viewer_row_scope` narrowed those reads to P, and B's row carries P, so the
read was ALLOWED whenever A's owner could reach B — P learnt through A what
P may not open. The fence is explicit now: a real acting person who cannot
open the route's agent sees no rows there. P's reads on an agent P can open are
unchanged, and an unreadable lookup fails closed.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

import dependencies as dep  # noqa: E402

pytestmark = pytest.mark.unit

CALLER, CALLEE = "agent-a", "agent-b"
OWNER_ID, MATE_ID = 1, 9
USERS = {OWNER_ID: {"id": OWNER_ID, "username": "owner", "role": "user"},
         MATE_ID: {"id": MATE_ID, "username": "mate", "role": "user"}}
ACCESS = {("owner", CALLER), ("owner", CALLEE), ("mate", CALLER)}
EXECUTIONS = {
    "e-mate": SimpleNamespace(agent_name=CALLER, source_user_id=MATE_ID, triggered_by="user",
                              source_agent_name=None),
}


def _key(run, agent=CALLER):
    return SimpleNamespace(id=OWNER_ID, username="owner", role="user", mcp_scope="agent", agent_name=agent,
                           connector_agent=None, portal_delegate=False, is_event_loopback=False,
                           vouched_source_agent=None, _acting_execution_id=run)


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    monkeypatch.setattr(dep.db, "get_execution", lambda run: EXECUTIONS.get(run))
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: USERS.get(uid))
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u == "owner")
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: (u, a) in ACCESS)
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: True)
    monkeypatch.setattr(dep.db, "get_running_turn_attribution",
                        lambda agent: [dict(vars(EXECUTIONS["e-mate"]), id="e-mate")] if agent == CALLER else [])


def test_the_callers_key_in_the_persons_turn_sees_nothing_of_an_agent_they_cannot_open():
    """The acting turn on CALLER is P's (share); CALLEE is not P's to open."""
    assert dep.acting_turn_user_id(_key("e-mate")) == MATE_ID
    assert dep.viewer_row_scope(_key("e-mate"), CALLEE) == dep.NO_ROWS_USER_ID


def test_the_same_key_still_reads_the_persons_rows_on_the_agent_they_chat_with():
    assert dep.viewer_row_scope(_key("e-mate"), CALLER) == MATE_ID


def test_an_agent_the_person_can_open_keeps_todays_rule(monkeypatch):
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: (u, a) in ACCESS | {("mate", CALLEE)})
    assert dep.viewer_row_scope(_key("e-mate"), CALLEE) == MATE_ID


def test_an_unreadable_access_lookup_fails_closed(monkeypatch):
    def boom(u, a):
        raise RuntimeError("db down")
    monkeypatch.setattr(dep.db, "can_user_access_agent", boom)
    assert dep.viewer_row_scope(_key("e-mate"), CALLEE) == dep.NO_ROWS_USER_ID
    assert dep.viewer_row_scope(_key("e-mate"), CALLER) == dep.NO_ROWS_USER_ID


def test_an_access_answer_that_is_not_true_fails_closed(monkeypatch):
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: None)
    assert dep.viewer_row_scope(_key("e-mate"), CALLER) == dep.NO_ROWS_USER_ID


def test_a_person_with_no_account_row_fails_closed(monkeypatch):
    monkeypatch.setattr(dep.db, "get_user_by_id", lambda uid: None)
    # The turn itself is still P's (the user id is on the row); the fence cannot name P.
    assert dep.viewer_row_scope(_key("e-mate"), CALLER) == dep.NO_ROWS_USER_ID


def test_no_rows_stays_no_rows_without_a_lookup(monkeypatch):
    asked = []
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: asked.append((u, a)) or True)
    assert dep.viewer_row_scope(_key("no-such-run"), CALLEE) == dep.NO_ROWS_USER_ID
    assert asked == []


def test_an_owner_turn_or_no_turn_keeps_the_full_view_without_a_lookup(monkeypatch):
    asked = []
    monkeypatch.setattr(dep.db, "can_user_access_agent", lambda u, a: asked.append((u, a)) or True)
    monkeypatch.setattr(dep.db, "get_running_turn_attribution", lambda agent: [])
    assert dep.viewer_row_scope(_key(None), CALLEE) is None
    assert asked == []


def test_a_person_principal_is_not_the_fence_s_business():
    """A signed-in share holder reading their own rows: today's share-viewer rule."""
    person = SimpleNamespace(id=MATE_ID, username="mate", role="user", mcp_scope=None, agent_name=None,
                             connector_agent=None, portal_delegate=False, is_event_loopback=False,
                             vouched_source_agent=None)
    assert dep.viewer_row_scope(person, CALLER) == MATE_ID
