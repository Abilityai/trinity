"""#2443 — the headroom service must not run synchronous DB calls on the loop.

`_believed_limited` called `db.is_subscription_rate_limited(...)` straight from
the event loop, once per subscription per 300s recovery sweep — and since the
sweep became chunk-concurrent (ent#434) N of them land together. `_record_history`
in the same module documents at length why that is forbidden: a sync SQLAlchemy
call landing during the 03:30 backup or the 04:30 VACUUM blocks `/health`, the WS
dispatcher and every in-flight request for up to the 30s busy timeout.

Three things are pinned here, in order of durability:

1. **The class, by discovery** — no `db.<x>(...)` call may appear in the body of
   an `async def` anywhere in the two modules. That is the rule; the three
   instances this issue names are just today's members of it, and a fourth added
   next month is caught without anyone remembering to look.
2. **The batch agrees with the per-id predicate**, derived over one fixture
   rather than restated as cases — two spellings of "recently rate-limited" is
   the drift #2352 spent a fix untangling.
3. **The wiring** — the sweep takes one batched read and hands it down, and the
   fallback path still reaches the DB from a worker thread.
"""

from __future__ import annotations

import ast
import asyncio
import sqlite3
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

_MODULES = (
    _BACKEND / "services" / "subscription_headroom_service.py",
    _BACKEND / "services" / "subscription_recovery_service.py",
)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT,
            role TEXT NOT NULL DEFAULT 'user',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE subscription_credentials (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            encrypted_credentials TEXT NOT NULL,
            subscription_type TEXT,
            rate_limit_tier TEXT,
            owner_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE agent_ownership (
            agent_name TEXT PRIMARY KEY,
            owner_id INTEGER,
            subscription_id TEXT,
            use_platform_api_key INTEGER DEFAULT 1,
            deleted_at TEXT
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE subscription_rate_limit_events (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            subscription_id TEXT NOT NULL,
            error_message TEXT,
            failure_kind TEXT,
            occurred_at TEXT NOT NULL
        )
        """
    )
    # `get_subscription_usage` aggregates both consumption tables; empty is fine,
    # absent is a hard error.
    cur.execute(
        """
        CREATE TABLE chat_messages (
            id TEXT PRIMARY KEY,
            agent_name TEXT,
            subscription_id TEXT,
            role TEXT,
            timestamp TEXT,
            context_used INTEGER,
            output_tokens INTEGER,
            cost REAL
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE schedule_executions (
            id TEXT PRIMARY KEY,
            agent_name TEXT,
            subscription_id TEXT,
            status TEXT,
            started_at TEXT,
            context_used INTEGER,
            cost REAL
        )
        """
    )
    now = _iso(datetime.now(timezone.utc))
    cur.execute(
        "INSERT INTO users (id, username, email, role, created_at, updated_at) "
        "VALUES (1, 'tester', 'tester@example.com', 'admin', ?, ?)",
        (now, now),
    )
    for sid, name in (("sub-a", "sub-A"), ("sub-b", "sub-B"), ("sub-c", "sub-C")):
        cur.execute(
            "INSERT INTO subscription_credentials "
            "(id, name, encrypted_credentials, owner_id, created_at, updated_at) "
            "VALUES (?, ?, 'enc', 1, ?, ?)",
            (sid, name, now, now),
        )
    cur.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, subscription_id, use_platform_api_key) "
        "VALUES ('agent-x', 1, 'sub-a', 0)"
    )
    conn.commit()
    conn.close()

    for mod in ("db.connection", "db.subscriptions"):
        monkeypatch.delitem(sys.modules, mod, raising=False)

    yield db_path


@pytest.fixture
def sub_ops(tmp_db):
    from db.subscriptions import SubscriptionOperations

    return SubscriptionOperations(encryption_service=MagicMock())


def _event(db_path, subscription_id, *, minutes_ago=30, failure_kind="rate_limit",
           agent="agent-x"):
    occurred = _iso(datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO subscription_rate_limit_events "
        "(id, agent_name, subscription_id, error_message, failure_kind, occurred_at) "
        "VALUES (?, ?, ?, '', ?, ?)",
        (uuid.uuid4().hex, agent, subscription_id, failure_kind, occurred),
    )
    conn.commit()
    conn.close()



# =============================================================================
# 1. The class: no sync db call inside an async function (discovered)
# =============================================================================

def _sync_db_calls_in_async(path: Path) -> list:
    """(function, db attribute, line) for every ``db.x(...)`` CALL that would
    execute on the event loop.

    A *reference* — ``asyncio.to_thread(db.x, arg)`` — is an Attribute, not a
    Call, so it is correctly invisible here: that is the fixed form, and the
    check is precisely the difference between the two.

    Nested ``def``s are excluded: a sync helper defined inside an async function
    runs wherever its caller puts it, and the ones in these modules are handed
    to ``to_thread``. Async nesting is not excluded — an inner ``async def``
    still runs on the loop.
    """
    tree = ast.parse(path.read_text(), filename=str(path))
    offenders = []

    def scan(node, fn_name):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef):
                continue  # sync helper — see docstring
            if isinstance(child, ast.AsyncFunctionDef):
                scan(child, child.name)
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "db"
            ):
                offenders.append((fn_name, child.func.attr, child.lineno))
            scan(child, fn_name)

    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef):
            scan(node, node.name)
    return offenders


@pytest.mark.parametrize("path", _MODULES, ids=lambda p: p.name)
def test_no_sync_db_call_on_the_event_loop(path):
    offenders = _sync_db_calls_in_async(path)
    assert offenders == [], (
        f"{path.name}: synchronous db call(s) inside an async function — "
        f"{offenders}. Wrap in asyncio.to_thread, or batch the read for the "
        "whole sweep (#2443)."
    )


def test_the_guard_can_actually_see_one():
    """Non-vacuity: the walker must find a planted offender, or the test above
    passes for the wrong reason on any future refactor of these modules."""
    src = (
        "import asyncio\n"
        "async def f():\n"
        "    ok = await asyncio.to_thread(db.threaded, 1)\n"
        "    bad = db.on_the_loop(2)\n"
        "    return ok, bad\n"
    )
    tmp = Path("/tmp/_2443_probe.py")
    tmp.write_text(src)
    found = _sync_db_calls_in_async(tmp)
    assert [f[1] for f in found] == ["on_the_loop"]


# =============================================================================
# 2. The batch is the per-id predicate, derived
# =============================================================================

class TestBatchAgreesWithPerId:
    def test_derived_over_a_mixed_fixture(self, sub_ops, tmp_db):
        """Property, not cases: whatever the per-id predicate answers for each
        subscription, the batch must return exactly that set. Stated this way it
        keeps holding if the window or the kind filter is ever changed — a case
        list would silently stop covering the change."""
        _event(tmp_db, "sub-a", failure_kind="rate_limit")
        _event(tmp_db, "sub-b", failure_kind="auth")
        _event(tmp_db, "sub-b", failure_kind=None)
        _event(tmp_db, "sub-c", failure_kind="rate_limit", minutes_ago=125)
        ids = ["sub-a", "sub-b", "sub-c", "sub-missing"]

        expected = {s for s in ids if sub_ops.is_subscription_rate_limited(s)}
        assert sub_ops.rate_limited_subscription_ids(ids) == expected
        assert expected == {"sub-a"}, "fixture must exercise both answers"

    def test_one_row_answers_for_several_ids(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="rate_limit")
        _event(tmp_db, "sub-c", failure_kind="rate_limit")
        assert sub_ops.rate_limited_subscription_ids(
            ["sub-a", "sub-b", "sub-c"]
        ) == {"sub-a", "sub-c"}

    def test_empty_input_asks_nothing(self, sub_ops, monkeypatch):
        """An ``IN ()`` is a round-trip whose answer is already known. Proven by
        making any engine access fatal."""
        import db.subscriptions as mod

        def _boom():  # pragma: no cover - must not be reached
            raise AssertionError("touched the engine for an empty id list")

        monkeypatch.setattr(mod, "get_engine", _boom)
        assert sub_ops.rate_limited_subscription_ids([]) == set()
        assert sub_ops.rate_limited_subscription_ids([None, ""]) == set()


# =============================================================================
# 3. The wiring
# =============================================================================

class TestWiring:
    def test_precomputed_set_asks_no_question(self, monkeypatch):
        import services.subscription_headroom_service as svc

        def _boom(_sid):  # pragma: no cover - must not be reached
            raise AssertionError("queried the db with a batched set in hand")

        monkeypatch.setattr(svc.db, "is_subscription_rate_limited", _boom)
        assert svc._believed_limited("s1", None, limited_ids={"s1"}) is True
        assert svc._believed_limited("s2", None, limited_ids={"s1"}) is False

    def test_snapshot_arm_still_wins_without_the_db(self, monkeypatch):
        """The union is unchanged: a rate_limited snapshot is believed even when
        the batch says otherwise (the provider is more recent than the window)."""
        import services.subscription_headroom_service as svc

        assert svc._believed_limited(
            "s1", {"status": "rate_limited"}, limited_ids=set()
        ) is True

    def test_batch_helper_fails_open_to_empty(self, monkeypatch):
        import services.subscription_headroom_service as svc

        def _boom(_ids):
            raise RuntimeError("db down")

        monkeypatch.setattr(svc.db, "rate_limited_subscription_ids", _boom)
        assert svc.rate_limited_ids(["a", "b"]) == set()

    def test_fallback_path_reaches_the_db_from_a_worker_thread(self, monkeypatch):
        """No batched set ⇒ the per-id read still happens, but not on the loop.
        Recorded by thread identity, which is the property that matters — a test
        asserting "to_thread was called" would pass on a wrapper that awaits it
        on the loop anyway."""
        import services.subscription_headroom_service as svc

        seen = {}

        def _predicate(sid):
            seen["thread"] = threading.get_ident()
            return True

        monkeypatch.setattr(svc.db, "is_subscription_rate_limited", _predicate)
        monkeypatch.setattr(svc, "_read_snapshot", lambda sid: (True, None))
        monkeypatch.setattr(svc, "_probe_floor_ok", lambda sid, snap: False)

        async def _run():
            loop_thread = threading.get_ident()
            out = await svc.recover_probe("s1")
            return loop_thread, out

        loop_thread, out = asyncio.run(_run())
        assert out == "floored", "must have got past the believed-limited gate"
        assert seen["thread"] != loop_thread, "predicate ran on the event loop"

    def test_pressure_states_reads_the_batch_once(self, monkeypatch):
        """Was one synchronous query per subscription on the 60s dashboard
        poll."""
        import services.subscription_headroom_service as svc

        calls = []
        monkeypatch.setattr(
            svc.db, "get_failure_event_counts_by_subscription",
            lambda hours: {},
        )
        monkeypatch.setattr(
            svc.db, "rate_limited_subscription_ids",
            lambda ids: (calls.append(list(ids)) or set()),
        )

        def _boom(_sid):  # pragma: no cover - must not be reached
            raise AssertionError("per-id predicate still used by pressure_states")

        monkeypatch.setattr(svc.db, "is_subscription_rate_limited", _boom)

        async def _hr(sid, wait=False):
            return None

        monkeypatch.setattr(svc, "get_headroom", _hr)

        ids = [f"s{i}" for i in range(5)]
        out = asyncio.run(svc.pressure_states(ids))
        assert set(out) == set(ids)
        assert calls == [ids], f"expected exactly one batched read, got {calls}"

    def test_sweep_hands_the_batch_down(self, monkeypatch):
        """The batch is useless if the cycle forgets to pass it — and the
        fallback would hide that, since it still produces correct answers."""
        import services.subscription_recovery_service as svc

        class _Sub:
            def __init__(self, sid):
                self.id = sid
                self.name = sid

        monkeypatch.setattr(svc.db, "list_subscriptions", lambda: [_Sub("s1"), _Sub("s2")])
        monkeypatch.setattr(svc, "rate_limited_ids", lambda ids: {"s1"})
        monkeypatch.setattr(
            svc.SubscriptionRecoveryService, "_try_acquire_leadership",
            lambda self, ttl: True,
        )

        seen = []

        async def _sweep_one(self, sub, *, threshold, alerting, limited_ids=None):
            seen.append((sub.id, limited_ids))
            return {"sid": sub.id, "outcome": "not_limited",
                    "classification": None, "reading": None}

        monkeypatch.setattr(svc.SubscriptionRecoveryService, "_sweep_one", _sweep_one)
        asyncio.run(svc.SubscriptionRecoveryService().run_cycle())
        assert seen and all(li == {"s1"} for _sid, li in seen), seen
