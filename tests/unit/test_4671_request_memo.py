"""The request memo's contract (#4671, #4672) — `utils/request_memo.py`.

Three properties, each a way the memo could turn a speed-up into a wrong
answer:

1. it does nothing outside a scope, and nothing survives a scope;
2. one principal's answer is never returned for another;
3. a failure is never remembered.

Plus the batch readers: each must return exactly what its per-item twin
returns, since inside a scope the batch's answer IS the per-item answer.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_TESTS = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_BACKEND = os.path.abspath(os.path.join(_TESTS, "..", "src", "backend"))
for _p in (_BACKEND, _TESTS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

pytestmark = pytest.mark.unit

from utils import request_memo  # noqa: E402

from query_counter import count_queries  # noqa: E402


class _Lookup:
    """A counted stand-in for a per-item read."""

    def __init__(self):
        self.calls = []

    @request_memo.memoized_method
    def role_of(self, username):
        self.calls.append(username)
        return {"alice": "admin", "bob": "user"}.get(username)

    @request_memo.memoized_method
    def flaky(self, key):
        self.calls.append(key)
        if len(self.calls) == 1:
            raise RuntimeError("down")
        return "up"


class TestScope:
    def test_outside_a_scope_nothing_is_cached(self):
        lookup = _Lookup()
        assert [lookup.role_of("alice") for _ in range(3)] == ["admin"] * 3
        assert lookup.calls == ["alice"] * 3
        assert not request_memo.active()

    def test_inside_a_scope_each_argument_is_read_once(self):
        lookup = _Lookup()
        with request_memo.scope():
            assert request_memo.active()
            assert [lookup.role_of("alice") for _ in range(3)] == ["admin"] * 3
        assert lookup.calls == ["alice"]

    def test_the_memo_does_not_outlive_its_scope(self):
        """AC (#4672): the memo cannot outlive the request. The second scope
        re-reads, and so does a call after any scope."""
        lookup = _Lookup()
        with request_memo.scope():
            lookup.role_of("alice")
        with request_memo.scope():
            lookup.role_of("alice")
        lookup.role_of("alice")
        assert lookup.calls == ["alice"] * 3

    def test_a_scope_is_dropped_when_its_body_raises(self):
        lookup = _Lookup()
        with pytest.raises(ValueError):
            with request_memo.scope():
                lookup.role_of("alice")
                raise ValueError("boom")
        assert not request_memo.active()
        lookup.role_of("alice")
        assert lookup.calls == ["alice", "alice"]

    def test_a_nested_scope_joins_the_outer_one(self):
        lookup = _Lookup()
        with request_memo.scope():
            lookup.role_of("alice")
            with request_memo.scope():
                lookup.role_of("alice")
            assert request_memo.active(), "the inner exit must not end the outer scope"
            lookup.role_of("alice")
        assert lookup.calls == ["alice"]

    def test_scoped_wraps_one_call(self):
        lookup = _Lookup()

        @request_memo.scoped
        def read():
            return lookup.role_of("bob"), lookup.role_of("bob")

        assert read() == ("user", "user")
        assert read() == ("user", "user")
        assert lookup.calls == ["bob", "bob"]


class TestNoCrossPrincipalAnswer:
    def test_another_principals_answer_is_never_returned(self):
        """AC (#4672): the memo cannot leak one principal's answer to another.
        Same scope, different argument — a different read, a different answer."""
        lookup = _Lookup()
        with request_memo.scope():
            assert lookup.role_of("alice") == "admin"
            assert lookup.role_of("bob") == "user"
            assert lookup.role_of("mallory") is None
            assert lookup.role_of("alice") == "admin"
        assert lookup.calls == ["alice", "bob", "mallory"]

    def test_two_requests_never_share_a_memo(self):
        """Two call chains at once — two threads, as two sync handlers are —
        each inside its own scope: neither sees what the other read."""
        lookup = _Lookup()
        ready, release = threading.Barrier(2), threading.Event()
        seen = {}

        def request(name):
            with request_memo.scope():
                lookup.role_of("alice")
                ready.wait(timeout=5)
                release.wait(timeout=5)
                seen[name] = request_memo.active()

        threads = [threading.Thread(target=request, args=(n,)) for n in ("a", "b")]
        for t in threads:
            t.start()
        release.set()
        for t in threads:
            t.join(timeout=5)
        assert seen == {"a": True, "b": True}
        assert lookup.calls == ["alice", "alice"], "each request read for itself"
        assert not request_memo.active(), "a thread's scope never reaches the caller"

    def test_distinct_functions_do_not_collide_on_equal_arguments(self):
        calls = []

        @request_memo.memoized
        def first(x):
            calls.append(("first", x))
            return "first"

        @request_memo.memoized
        def second(x):
            calls.append(("second", x))
            return "second"

        with request_memo.scope():
            assert (first("k"), second("k"), first("k")) == ("first", "second", "first")
        assert calls == [("first", "k"), ("second", "k")]


class TestFailuresAreNotRemembered:
    def test_an_exception_is_retried_not_replayed(self):
        lookup = _Lookup()
        with request_memo.scope():
            with pytest.raises(RuntimeError):
                lookup.flaky("k")
            assert lookup.flaky("k") == "up"
            assert lookup.flaky("k") == "up"
        assert lookup.calls == ["k", "k"]

    def test_none_is_an_answer(self):
        lookup = _Lookup()
        with request_memo.scope():
            assert lookup.role_of("nobody") is None
            assert lookup.role_of("nobody") is None
        assert lookup.calls == ["nobody"]

    def test_an_unhashable_argument_calls_through(self):
        calls = []

        @request_memo.memoized
        def read(names):
            calls.append(names)
            return len(names)

        with request_memo.scope():
            assert read(["a"]) == 1
            assert read(["a"]) == 1
        assert len(calls) == 2


class TestPrime:
    def test_prime_answers_the_per_item_read(self):
        lookup = _Lookup()
        with request_memo.scope():
            lookup.role_of.prime("carol", value="operator")
            assert lookup.role_of("carol") == "operator"
        assert lookup.calls == []

    def test_prime_never_overwrites_a_read_answer(self):
        lookup = _Lookup()
        with request_memo.scope():
            assert lookup.role_of("alice") == "admin"
            lookup.role_of.prime("alice", value="user")
            assert lookup.role_of("alice") == "admin"

    def test_prime_outside_a_scope_is_a_no_op(self):
        lookup = _Lookup()
        lookup.role_of.prime("carol", value="operator")
        assert lookup.role_of("carol") is None
        assert lookup.calls == ["carol"]


# ===========================================================================
# The batch readers agree with their per-item twins, on the real database
# ===========================================================================

def _uid() -> str:
    return uuid.uuid4().hex[:8]


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def world(real_db):
    from db_models import UserCreate

    def user(prefix, role="user", email=True):
        name = f"{prefix}-{_uid()}"
        real_db.create_user(UserCreate(username=name, role=role,
                                       email=f"{name}@example.com" if email else None))
        return name

    owner, viewer, admin = user("owner"), user("viewer"), user("admin", role="admin")
    no_email = user("noemail", email=False)
    shared, private, deleted = (f"agent-memo-{_uid()}" for _ in range(3))
    for agent in (shared, private, deleted):
        assert real_db.register_agent_owner(agent, owner)
    assert real_db.share_agent(shared, owner, f"{viewer}@example.com")
    assert real_db.share_agent(deleted, owner, f"{viewer}@example.com")
    from db_harness import run
    run("UPDATE agent_ownership SET deleted_at='2026-01-01T00:00:00Z' WHERE agent_name=:a",
        a=deleted)
    return {"owner": owner, "viewer": viewer, "admin": admin, "no_email": no_email,
            "agents": [shared, private, deleted, f"agent-missing-{_uid()}"]}


class TestBatchReadersMatchThePerItemRead:
    def test_agent_owners(self, real_db, world):
        with count_queries() as log:
            batch = real_db.get_agent_owners(world["agents"])
        assert log.count == 1
        assert batch == {a: real_db.get_agent_owner(a) for a in world["agents"]}
        assert batch[world["agents"][2]] is None, "a soft-deleted agent has no owner (#834)"
        assert batch[world["agents"][3]] is None

    def test_agents_shared_with_user(self, real_db, world):
        for username in (world["viewer"], world["owner"], world["admin"],
                         world["no_email"], f"ghost-{_uid()}"):
            shared = real_db.agents_shared_with_user(world["agents"], username)
            assert shared == {a for a in world["agents"]
                              if real_db.is_agent_shared_with_user(a, username)}, username

    def test_users_by_emails(self, real_db, world):
        viewer = f"{world['viewer']}@example.com"
        asked = [viewer, viewer.upper(), f"  {viewer} ", f"ghost-{_uid()}@example.com", ""]
        with count_queries() as log:
            batch = real_db.get_users_by_emails(asked)
        assert log.count == 1
        assert batch == {e: real_db.get_user_by_email(e) for e in asked}
        assert batch[viewer.upper()]["username"] == world["viewer"]

    def test_empty_batches_run_no_query(self, real_db):
        with count_queries() as log:
            assert real_db.get_agent_owners([]) == {}
            assert real_db.get_users_by_emails([None, ""]) == {"": None}
            assert real_db.get_gate_requests([]) == {}
            assert real_db.get_operator_queue_items_by_ids([None]) == {}
        assert log.count == 0

    def test_access_over_many_agents_costs_no_query_each(self, real_db, world):
        """The point of the batch: `can_user_access_agent` — unchanged, still
        the rule — answers from what the scope already read, and answers the
        same as it does cold."""
        agents, viewer = world["agents"], world["viewer"]
        cold = {a: real_db.can_user_access_agent(viewer, a) for a in agents}
        assert cold == {agents[0]: True, agents[1]: False, agents[2]: True, agents[3]: False}

        with request_memo.scope():
            real_db.get_agent_owners(agents)
            real_db.agents_shared_with_user(agents, viewer)
            with count_queries() as log:
                warm = {a: real_db.can_user_access_agent(viewer, a) for a in agents}
        assert warm == cold
        assert log.count == 0, log.report()

    def test_a_scope_sees_no_write_made_before_it(self, real_db, world):
        """A scope starts empty: a share revoked before the read is revoked in it."""
        agent, viewer, owner = world["agents"][0], world["viewer"], world["owner"]
        with request_memo.scope():
            assert real_db.can_user_access_agent(viewer, agent) is True
        assert real_db.unshare_agent(agent, owner, f"{viewer}@example.com")
        with request_memo.scope():
            assert real_db.can_user_access_agent(viewer, agent) is False
