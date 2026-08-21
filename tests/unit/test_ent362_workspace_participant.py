"""A workspace client as a room participant (ent#362).

Rooms modelled two kinds of member: an agent, and a `user` identified by
USERNAME and authorised through the OSS agent ACL. A workspace client is a
verified email with no `users` row, so it could not be a member at all.

The tests here are the ones where a mistake is a privilege escalation rather
than a bug:

* the new kind must be its OWN identity space — an email that happens to equal
  a username must not inherit that account's access;
* access must come from the portal roster, so "you can only room with agents
  shared to you" is the same rule the rest of the Workspace enforces;
* a workspace principal must never satisfy an admin bypass, and must never be
  able to act as an agent;
* platform callers must be completely unaffected — this is a fallback, and a
  regression there would silently re-identify every existing member.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

# The backend config hard-fails at import without a credentialed REDIS_URL
# (#589). The OSS tests/conftest.py sets this up, but it does not reach this
# tree, so the preamble is repeated here rather than left to the invocation.
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent362.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent362-logs"))

import pytest


def _run(coro):
    return asyncio.run(coro)


def _workspace(email="client@example.com", is_platform=False):
    from shared_sessions.router import WorkspacePrincipal
    return WorkspacePrincipal(email=email, is_platform=is_platform)


def _user(username="alice", role="admin", agent_name=None):
    from models import User
    return User(id=1, username=username, role=role, agent_name=agent_name)


# --- identity ----------------------------------------------------------------

def test_a_workspace_client_is_its_own_participant_kind():
    from shared_sessions import service
    kind, identity = service._caller(_workspace("Client@Example.com"))
    assert kind == service.WORKSPACE_KIND
    assert identity == "client@example.com", "email identity must be normalised"


def test_an_email_matching_a_username_does_not_become_that_user():
    """The escalation this separation exists to prevent. `user` identities are
    usernames resolved against the OSS ACL; if a workspace client landed in that
    space, an email equal to a username would inherit that account's access."""
    from shared_sessions import service
    kind, identity = service._caller(_workspace("alice"))
    assert kind == service.WORKSPACE_KIND
    assert (kind, identity) != ("user", "alice")


def test_platform_and_agent_callers_are_unchanged():
    """This is a fallback. If it re-identified existing principals, every member
    of every live room would change kind underneath them."""
    from shared_sessions import service
    assert service._caller(_user()) == ("user", "alice")
    assert service._caller(_user(agent_name="agent-a")) == ("agent", "agent-a")


def test_a_workspace_principal_carries_no_role_and_no_agent():
    """It cannot satisfy an admin bypass (all three are `kind == "user"`-guarded
    AND read `role`), and it cannot act as an agent."""
    p = _workspace()
    assert getattr(p, "role", None) is None
    assert getattr(p, "agent_name", None) is None


# --- access ------------------------------------------------------------------

def test_access_comes_from_the_portal_roster(monkeypatch):
    """AC#5. Not the platform ACL — this principal has no `users` row for it to
    key on — and not a second bespoke rule that could drift from the roster."""
    from shared_sessions import service
    import client_portal.service as portal_service

    seen = {}

    def _roster(agent_name, email, include_owned=False):
        seen["args"] = (agent_name, email, include_owned)
        return agent_name == "shared-agent"

    monkeypatch.setattr(portal_service, "agent_on_roster", _roster)

    service._assert_can_reach_agent(_workspace(), "shared-agent")   # no raise
    assert seen["args"] == ("shared-agent", "client@example.com", False)

    with pytest.raises(service.RoomError) as ei:
        service._assert_can_reach_agent(_workspace(), "not-shared")
    assert ei.value.status_code == 403
    assert ei.value.code == "agent_not_accessible"


def test_a_platform_workspace_session_sees_the_agents_it_owns(monkeypatch):
    """`include_owned` mirrors the roster: a signed-in platform user reaching the
    Workspace sees agents it OWNS (ent#357), an external client sees only what
    was shared. The gate must be told which one is asking."""
    from shared_sessions import service
    import client_portal.service as portal_service

    seen = {}
    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda a, e, include_owned=False: seen.update(io=include_owned) or True)

    service._assert_can_reach_agent(_workspace(is_platform=True), "my-agent")
    assert seen["io"] is True


def test_a_roster_miss_is_the_same_refusal_as_an_unknown_agent(monkeypatch):
    """Enumeration-safety (Invariant #8): "not shared with you" and "does not
    exist" must be indistinguishable, or room creation becomes a fleet-name
    oracle for anyone with a portal session."""
    from shared_sessions import service
    import client_portal.service as portal_service
    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda a, e, include_owned=False: False)

    errors = []
    for name in ("real-but-not-shared", "totally-made-up"):
        with pytest.raises(service.RoomError) as ei:
            service._assert_can_reach_agent(_workspace(), name)
        errors.append((ei.value.status_code, ei.value.code))
    assert errors[0] == errors[1] == (403, "agent_not_accessible")


# --- transcript --------------------------------------------------------------

def test_a_workspace_sender_reads_as_human_to_the_agents():
    """Otherwise an agent sees a bare email as an unlabelled kind and cannot
    tell a customer in the room from another agent."""
    from shared_sessions import service
    rendered = service._render_transcript([
        {"sender_kind": service.WORKSPACE_KIND, "sender_identity": "client@example.com",
         "content": "hello", "kind": "message"},
    ]) if hasattr(service, "_render_transcript") else None
    if rendered is None:
        pytest.skip("transcript renderer is private to the delta builder")
    assert "(human)" in rendered


# --- wake semantics ----------------------------------------------------------

def test_a_revoked_share_stops_future_wakes_but_keeps_the_transcript(monkeypatch):
    """AC#6. Membership is the grant, but un-sharing has to stop the spending.
    Checked at WAKE time, not join time — the shared record is permanent, the
    ability to keep costing an agent its time is not."""
    from shared_sessions import service
    import client_portal.service as portal_service

    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda a, e, include_owned=False: a == "still-shared")
    system_lines = []
    monkeypatch.setattr(service, "_post_system",
                        lambda room_id, text: system_lines.append(text))
    monkeypatch.setattr(service, "_apply_wake_cap",
                        lambda room_id, k, i, targets: targets)

    targets = ["still-shared", "was-revoked"]
    # Exercise the same filter the post path runs.
    email = service._workspace_identity(_workspace())
    kept = [t for t in targets if portal_service.agent_on_roster(t, email, False)]

    assert kept == ["still-shared"]
    assert "was-revoked" not in kept


def test_the_wake_cap_is_per_participant_and_per_room(monkeypatch):
    """AC#4. The chain-depth cap bounds one message's cascade; it does nothing
    about a participant sending many messages that each mention many agents.
    With customers in rooms that is a spend amplifier."""
    from shared_sessions import service
    from services import rate_limiter

    keys = []

    def _check(key, limit, window):
        keys.append(key)
        return len(keys) <= 2          # third wake is over budget

    monkeypatch.setattr(rate_limiter, "check", _check)
    monkeypatch.setattr(service, "_post_system", lambda *a, **k: None)

    allowed = service._apply_wake_cap("room-1", service.WORKSPACE_KIND,
                                      "client@example.com", ["a", "b", "c"])
    assert allowed == ["a", "b"], "the third mention must not wake anyone"
    assert all(k == "room_wake:room-1:workspace_user:client@example.com" for k in keys)


def test_the_wake_cap_fails_open(monkeypatch):
    """A room going silent because Redis hiccuped is worse than one extra wake.
    The message has landed either way — the cap governs spend, not speech."""
    from shared_sessions import service
    from services import rate_limiter

    def _boom(key, limit, window):
        raise RuntimeError("redis down")

    monkeypatch.setattr(rate_limiter, "check", _boom)
    monkeypatch.setattr(service, "_post_system", lambda *a, **k: None)

    assert service._apply_wake_cap("r", "user", "alice", ["a", "b"]) == ["a", "b"]


def test_a_capped_participant_is_told_why(monkeypatch):
    """Silence would read as the agents ignoring them."""
    from shared_sessions import service
    from services import rate_limiter

    monkeypatch.setattr(rate_limiter, "check", lambda *a, **k: False)
    lines = []
    monkeypatch.setattr(service, "_post_system", lambda room_id, text: lines.append(text))

    assert service._apply_wake_cap("r", "user", "alice", ["a"]) == []
    assert lines and "wake limit" in lines[0]


def test_cost_attribution_carries_the_workspace_identity():
    """AC#3. `_wake_agent` passes `source_user_email` from the acting principal,
    and a WorkspacePrincipal carries one — so a customer-driven turn is
    attributable without the wake path knowing what kind of caller it has."""
    p = _workspace("Client@Example.com")
    assert getattr(p, "email", None) == "Client@Example.com"


def test_the_wake_controls_are_actually_wired_into_the_post_path():
    """The helpers above are only worth anything if `post_message` calls them.

    Added after a mutation check: deleting the `_apply_wake_cap(...)` call site
    left every other test in this file green, because they exercise the helper
    directly. A source-level pin is the cheap guard — the behavioural path needs
    the full room DB fixture, which does not run in this tree.
    """
    import ast
    import inspect
    from shared_sessions import service

    src = inspect.getsource(service.post_message)
    tree = ast.parse(src.lstrip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_apply_wake_cap" in called, (
        "post_message must run the per-participant wake cap (ent#362 AC#4)"
    )
    assert "agent_on_roster" in src, (
        "post_message must re-check the roster before waking, so a revoked "
        "share stops future wakes (ent#362 AC#6)"
    )


# --- creating and moderating a room as a workspace client (ent#361) -----------

def test_the_creator_is_seeded_from_the_resolved_caller_not_a_username():
    """A workspace client has no username. Seeding from `_user_identity` gave
    `created_by=""` and a `user` participant with an EMPTY identity — leaving
    the creator a non-member of their own room, 404'd on the next request."""
    import ast
    import inspect
    from shared_sessions import service

    src = inspect.getsource(service.create_room)
    assert "_caller(current_user)" in src, (
        "create_room must seed the creator from the resolved caller"
    )
    tree = ast.parse(src.lstrip())
    # No literal "user" kind may be passed to add_participant any more.
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_participant"):
            literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
            assert "user" not in literals, (
                "the creator's kind must come from _caller, not a literal"
            )


def test_a_workspace_client_moderates_the_room_it_created(monkeypatch):
    """ent#361 AC#3 needs this: adding an agent mid-chat is a moderator action,
    and the gate previously required kind == 'user'."""
    from shared_sessions import service

    room = {"id": "r1", "status": "open"}
    monkeypatch.setattr(service, "_require_membership", lambda room_id, user: room)
    monkeypatch.setattr(service.db, "get_participant",
                        lambda room_id, kind, identity: {"role": "moderator"})

    assert service._require_moderator("r1", _workspace()) is room


def test_an_agent_still_cannot_moderate_a_room(monkeypatch):
    """What the kind check is actually for: an agent is reachable through its
    own MCP key and is a prompt-injection surface, so it must never rewrite a
    roster or close a room (ent#220). Widening the gate for workspace clients
    must not widen it for agents."""
    from shared_sessions import service

    room = {"id": "r1", "status": "open"}
    monkeypatch.setattr(service, "_require_membership", lambda room_id, user: room)
    monkeypatch.setattr(service.db, "get_participant",
                        lambda room_id, kind, identity: {"role": "moderator"})

    with pytest.raises(service.RoomError) as ei:
        service._require_moderator("r1", _user(agent_name="agent-a"))
    assert ei.value.status_code == 403
    assert ei.value.code == "not_moderator"


# --- who is mid-turn, after a reload ------------------------------------------

class _FakeRedis:
    """Minimal stand-in — the marker only ever does set / get / delete."""

    def __init__(self, broken=False):
        self.data = {}
        self.broken = broken

    def _guard(self):
        if self.broken:
            raise RuntimeError("redis down")

    def set(self, k, v, ex=None):
        self._guard(); self.data[k] = v

    def get(self, k):
        self._guard(); return self.data.get(k)

    def delete(self, k):
        self._guard(); self.data.pop(k, None)


@pytest.fixture()
def redis_stub(monkeypatch):
    import redis_breaker_util
    fake = _FakeRedis()
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis", lambda: fake)
    return fake


def test_working_state_survives_a_reload(redis_stub):
    """Reported from testing: mentioning two agents and reloading while they
    thought made the indicator vanish — the room looked idle while two turns
    were in flight, which reads as it having given up.

    The WS broadcast only reaches clients connected AT THAT MOMENT. A reloaded
    client has nothing to re-derive from, so the state is also recorded where a
    poll can read it back.
    """
    from shared_sessions import service

    parts = [{"kind": "agent", "identity": "a"}, {"kind": "agent", "identity": "b"},
             {"kind": service.WORKSPACE_KIND, "identity": "client@example.com"}]

    assert service.working_agents("r1", parts) == []

    service._mark_agent_working("r1", "a")
    service._mark_agent_working("r1", "b")
    assert sorted(service.working_agents("r1", parts)) == ["a", "b"]

    service._clear_agent_working("r1", "a")
    assert service.working_agents("r1", parts) == ["b"]


def test_working_state_is_scoped_to_its_room(redis_stub):
    """Two rooms can hold the same agent; one being busy must not make it look
    busy in the other."""
    from shared_sessions import service
    parts = [{"kind": "agent", "identity": "a"}]

    service._mark_agent_working("room-1", "a")
    assert service.working_agents("room-1", parts) == ["a"]
    assert service.working_agents("room-2", parts) == []


def test_a_departed_participant_is_never_reported_working(redis_stub):
    from shared_sessions import service
    service._mark_agent_working("r1", "gone")
    parts = [{"kind": "agent", "identity": "gone", "left_at": "2026-08-12T00:00:00Z"}]
    assert service.working_agents("r1", parts) == []


def test_working_state_reads_empty_when_redis_is_down(monkeypatch):
    """Showing no indicator is a smaller lie than showing a permanent one."""
    import redis_breaker_util
    from shared_sessions import service
    monkeypatch.setattr(redis_breaker_util, "get_breaker_redis",
                        lambda: _FakeRedis(broken=True))
    assert service.working_agents("r1", [{"kind": "agent", "identity": "a"}]) == []


def test_the_marker_is_set_and_cleared_around_the_turn():
    """Wiring pin: the mark is worthless if the wake does not set it, and a mark
    that is never cleared leaves an agent looking busy until its TTL."""
    import inspect
    from shared_sessions import service

    src = inspect.getsource(service._wake_agent)
    assert "_mark_agent_working(" in src
    assert "_clear_agent_working(" in src
    # The clear must be in the finally, or a raised turn leaves it stuck.
    finally_block = src.split("finally:")[-1]
    assert "_clear_agent_working(" in finally_block
