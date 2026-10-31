"""Who sees which executions of an agent: the viewer tier (trinity-enterprise#875).

`can_user_access_agent` means "may chat with this agent"; the execution and
control routes read it as "may read and steer everyone's turns". The viewer
helpers name the three callers those routes must tell apart: OWNER (a person who
owns the agent, or an admin), SHARE (any other person with access) and KEY (an
agent, system, ops or connector key, whose behaviour does not change).

The point of these tests is the direction of the failure. `mcp_api_keys.scope`
is free text, so the tier is an ALLOWlist both ways: a scope this module has
not heard of (`portal_delegate`, a sixth one a future PR invents) and a
principal with no `mcp_scope` at all resolve to SHARE and are filtered. A
denylist would hand them the unfiltered view (the ent#2323 class).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import dependencies as dep

pytestmark = pytest.mark.unit

AGENT = "shared-agent"


def _p(username="teammate", uid=7, scope=None, role="user", **extra):
    """A principal with every field `is_person_principal` reads set explicitly."""
    return SimpleNamespace(
        id=uid, username=username, role=role, mcp_scope=scope,
        agent_name=extra.pop("agent_name", None),
        connector_agent=extra.pop("connector_agent", None),
        portal_delegate=extra.pop("portal_delegate", False),
        is_event_loopback=extra.pop("is_event_loopback", False),
        vouched_source_agent=extra.pop("vouched_source_agent", None),
        **extra,
    )


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    # The owner and admins share; the teammate does not. Reach is unbounded
    # unless a test narrows it.
    monkeypatch.setattr(dep.db, "can_user_share_agent", lambda u, a: u in ("owner", "admin"))
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: True)


# ---- principal kind -------------------------------------------------------------

@pytest.mark.parametrize("principal,kind", [
    (_p("teammate"), "person"),                                  # a JWT
    (_p("teammate", scope="user"), "person"),                    # their own user-scope key
    (_p("owner"), "person"),
    (_p("admin", role="admin"), "person"),
    (_p("owner", scope="agent", agent_name="sibling"), "key"),
    (_p("trinity-system", scope="system"), "key"),
    (_p("owner", scope="ops"), "key"),
    (_p("owner", scope="connector", connector_agent="shared-agent"), "key"),
    # Fail closed: nothing below is listed as a key, so none inherits the key view.
    (_p("owner", scope="portal_delegate", portal_delegate=True), "other"),
    (_p("owner", scope="robot"), "other"),                       # a scope invented tomorrow
    (_p("owner", scope="event_loopback", is_event_loopback=True), "other"),
    (_p("owner", vouched_source_agent="some-agent"), "other"),   # the loopback JWT
])
def test_principal_kind(principal, kind):
    assert dep.viewer_principal_kind(principal) == kind


def test_a_principal_with_no_mcp_scope_attribute_fails_closed():
    """The getattr-discriminator trap: an absent attribute must not read as the
    JWT value (None). It is "other", so it is filtered."""
    bare = SimpleNamespace(id=7, username="teammate", role="user")
    assert dep.viewer_principal_kind(bare) == "other"
    assert dep.agent_viewer_tier(bare, AGENT) == dep.VIEWER_SHARE


# ---- tier on an agent --------------------------------------------------------------

@pytest.mark.parametrize("principal,tier", [
    (_p("owner"), dep.VIEWER_OWNER),
    (_p("owner", scope="user"), dep.VIEWER_OWNER),
    (_p("admin", role="admin"), dep.VIEWER_OWNER),
    (_p("teammate"), dep.VIEWER_SHARE),
    (_p("teammate", scope="user"), dep.VIEWER_SHARE),
    (_p("owner", scope="agent", agent_name="sibling"), dep.VIEWER_KEY),
    (_p("owner", scope="ops"), dep.VIEWER_KEY),
    (_p("owner", scope="connector", connector_agent="shared-agent"), dep.VIEWER_KEY),
    (_p("owner", scope="portal_delegate", portal_delegate=True), dep.VIEWER_SHARE),
    (_p("owner", scope="robot"), dep.VIEWER_SHARE),
    (_p("owner", vouched_source_agent="x"), dep.VIEWER_SHARE),
])
def test_agent_viewer_tier(principal, tier):
    assert dep.agent_viewer_tier(principal, AGENT) == tier


def test_an_owner_outside_their_reach_is_not_the_owner(monkeypatch):
    """The ent#629 reach bound is part of the owner predicate, as in
    `assert_agent_owner`."""
    monkeypatch.setattr(dep, "agent_may_reach", lambda *a, **k: False)
    assert dep.agent_viewer_tier(_p("owner"), AGENT) == dep.VIEWER_SHARE


def test_the_owner_predicate_is_the_one_assert_agent_owner_uses(monkeypatch):
    """No second spelling of "owner": for every person, the tier is OWNER
    exactly when `assert_agent_owner` does not raise."""
    from fastapi import HTTPException
    for who in (_p("owner"), _p("admin", role="admin"), _p("teammate")):
        try:
            dep.assert_agent_owner(who, AGENT)
            owner = True
        except HTTPException:
            owner = False
        assert (dep.agent_viewer_tier(who, AGENT) == dep.VIEWER_OWNER) is owner


# ---- the row scope ---------------------------------------------------------------------

def test_a_share_holder_is_scoped_to_their_own_id():
    assert dep.viewer_row_scope(_p("teammate", uid=7), AGENT) == 7
    assert dep.viewer_row_scope(_p("teammate", uid=7, scope="user"), AGENT) == 7


@pytest.mark.parametrize("principal", [
    _p("owner"), _p("admin", role="admin"),
    _p("owner", scope="agent", agent_name="sibling"), _p("owner", scope="ops"),
    _p("owner", scope="connector", connector_agent="shared-agent"),
])
def test_owner_admin_and_key_tiers_see_every_row(principal):
    assert dep.viewer_row_scope(principal, AGENT) is None


@pytest.mark.parametrize("uid", [None, 0, -3, True, "7", 7.0])
def test_a_missing_or_malformed_id_matches_no_row_never_all(uid):
    """A share holder whose principal carries no usable id must read as "no
    rows", not as "no filter"."""
    scope = dep.viewer_row_scope(_p("teammate", uid=uid), AGENT)
    assert scope == dep.NO_ROWS_USER_ID
    assert scope is not None


def test_no_rows_user_id_is_not_a_real_user_id():
    assert dep.NO_ROWS_USER_ID <= 0


# ---- the row predicate ---------------------------------------------------------------------

@pytest.mark.parametrize("scope,owner_of_row,visible", [
    (None, 7, True), (None, None, True), (None, "abc", True),   # no scope: every row
    (7, 7, True),
    (7, "7", True),                  # the in-memory queue carries the id as a string
    (7, 8, False), (7, "8", False),
    (7, None, False),                # NULL never belongs to a share holder
    (7, "", False), (7, "abc", False), (7, [], False),
    (dep.NO_ROWS_USER_ID, 7, False), (dep.NO_ROWS_USER_ID, None, False),
])
def test_row_is_viewers(scope, owner_of_row, visible):
    assert dep.row_is_viewers(scope, owner_of_row) is visible


# ---- the shared-state refusal -----------------------------------------------------------------

def test_a_share_holder_is_refused_the_containers_shared_state():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        dep.assert_not_share_viewer(_p("teammate"), AGENT)
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "owner_tier_path"
    # Fixed text: it names no agent, user or id (Invariant #8).
    assert AGENT not in exc.value.detail["message"]


@pytest.mark.parametrize("principal", [
    _p("owner"), _p("admin", role="admin"),
    _p("owner", scope="agent", agent_name="sibling"),
])
def test_everyone_else_still_reads_the_shared_state(principal):
    dep.assert_not_share_viewer(principal, AGENT)    # does not raise
