"""
SUB-003 ping-pong prevention tests (issue #444).

Before the fix, `_perform_auto_switch()` called `clear_rate_limit_events()`
after every successful switch, deleting the per-(agent, subscription) events
that are the detection signal for `is_subscription_rate_limited()`. Once
deleted, the just-drained subscription looked viable again, causing agents to
ping-pong between two exhausted subscriptions on every subsequent 429.

These tests pin the fix at the db layer: after a simulated switch, the old
subscription must still be reported as rate-limited, and
`select_best_alternative_subscription()` must return None when every candidate
has rate-limit events in the 2h window.
"""

from __future__ import annotations

import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make src/backend importable and evict any shadow `utils` package that the
# parent tests/ directory would otherwise resolve to (mirrors test_backlog.py).
_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Provision a fresh SQLite DB with the tables SUB-003 touches.

    Only columns read/written by SubscriptionOperations are created — this keeps
    the test isolated from schema drift elsewhere.
    """
    db_path = tmp_path / "trinity.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_path))

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
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
            deleted_at TEXT  -- #834: read paths filter `WHERE deleted_at IS NULL`
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
            occurred_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        "CREATE INDEX idx_rate_limit_agent_sub "
        "ON subscription_rate_limit_events(agent_name, subscription_id, occurred_at DESC)"
    )
    cur.execute(
        "CREATE INDEX idx_rate_limit_sub "
        "ON subscription_rate_limit_events(subscription_id, occurred_at DESC)"
    )

    # Seed: 1 user, 2 subscriptions, 1 agent assigned to sub-A
    now = datetime.utcnow().isoformat()
    cur.execute(
        "INSERT INTO users (id, username, email, role, created_at, updated_at) "
        "VALUES (1, 'tester', 'tester@example.com', 'admin', ?, ?)",
        (now, now),
    )
    cur.execute(
        "INSERT INTO subscription_credentials "
        "(id, name, encrypted_credentials, owner_id, created_at, updated_at) "
        "VALUES ('sub-a', 'sub-A', 'enc-a', 1, ?, ?)",
        (now, now),
    )
    cur.execute(
        "INSERT INTO subscription_credentials "
        "(id, name, encrypted_credentials, owner_id, created_at, updated_at) "
        "VALUES ('sub-b', 'sub-B', 'enc-b', 1, ?, ?)",
        (now, now),
    )
    cur.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, subscription_id) "
        "VALUES ('agent-x', 1, 'sub-a')"
    )
    conn.commit()
    conn.close()

    # Force re-import so the module-level DB_PATH picks up our env var.
    for mod in ("db.connection", "db.subscriptions"):
        sys.modules.pop(mod, None)

    yield db_path


@pytest.fixture
def sub_ops(tmp_db):
    """Fresh SubscriptionOperations bound to tmp_db with a stub encryption service."""
    from db.subscriptions import SubscriptionOperations

    # Encryption service is only used by create_subscription / get_subscription_token,
    # which these tests don't exercise. A stub keeps us off the real service.
    return SubscriptionOperations(encryption_service=MagicMock())


def _record_events(sub_ops, agent_name: str, subscription_id: str, count: int) -> int:
    last = 0
    for _ in range(count):
        last = sub_ops.record_rate_limit_event(
            agent_name=agent_name,
            subscription_id=subscription_id,
            error_message="Subscription usage limit: You've hit your limit",
        )
    return last


class TestPingPongPrevention:
    """SUB-003 regression tests for issue #444."""

    def test_old_subscription_stays_rate_limited_after_switch(self, sub_ops):
        """After a switch, the old sub's events must persist so `is_subscription_rate_limited`
        continues to flag it — this is what stops the ping-pong on the next cycle."""
        # Simulate 2 consecutive 429s on sub-A → triggers switch
        count = _record_events(sub_ops, "agent-x", "sub-a", 2)
        assert count == 2
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

        # Simulate _perform_auto_switch doing its work WITHOUT calling
        # clear_rate_limit_events (post-fix behavior).
        sub_ops.assign_subscription_to_agent("agent-x", "sub-b")

        # Signal must survive — this is the fix.
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

    def test_no_alternative_when_both_subs_exhausted(self, sub_ops):
        """Given two subscriptions that have each hit the limit,
        select_best_alternative_subscription must return None — not pick the
        other exhausted sub."""
        _record_events(sub_ops, "agent-x", "sub-a", 2)
        _record_events(sub_ops, "agent-x", "sub-b", 2)

        # Agent currently on sub-A → asking for an alternative to sub-A
        assert sub_ops.select_best_alternative_subscription("sub-a") is None
        # Symmetric: from sub-B's perspective too
        assert sub_ops.select_best_alternative_subscription("sub-b") is None

    def test_pingpong_blocked_across_two_switches(self, sub_ops):
        """Full ping-pong scenario: both subscriptions have 429s recorded. After
        the first switch (A→B), the second check (from B) must refuse to switch
        back to A because A is still flagged as rate-limited."""
        # First cycle: agent-x on sub-A, 2× 429
        _record_events(sub_ops, "agent-x", "sub-a", 2)
        # Auto-switch picks sub-B (the only other sub, not yet flagged)
        alt1 = sub_ops.select_best_alternative_subscription("sub-a")
        assert alt1 is not None
        assert alt1.id == "sub-b"
        # Perform the switch (post-fix: no clear)
        sub_ops.assign_subscription_to_agent("agent-x", "sub-b")

        # Second cycle: 2× 429 on sub-B too
        _record_events(sub_ops, "agent-x", "sub-b", 2)
        # sub-A still rate-limited → no viable alternative → no ping-pong back
        alt2 = sub_ops.select_best_alternative_subscription("sub-b")
        assert alt2 is None

    def test_viable_alternative_found_when_only_one_sub_exhausted(self, sub_ops):
        """Sanity check: if only one subscription is rate-limited, the other is
        still a valid alternative (the fix must not over-correct and refuse all
        switches)."""
        _record_events(sub_ops, "agent-x", "sub-a", 2)
        alt = sub_ops.select_best_alternative_subscription("sub-a")
        assert alt is not None
        assert alt.id == "sub-b"


# =============================================================================
# #476 regression: rate-limit events must age out correctly within the 2h window
# =============================================================================

class TestRateLimitAging:
    """Issue #476 — before the fix, the SQL `datetime('now', '-2 hours')` filter
    compared against `utc_now_iso()`-formatted TEXT lexicographically. Position 10
    of `utc_now_iso()` is `T` (0x54); `datetime('now', ...)` uses space (0x20). So
    every event whose date prefix matched today's date passed the "last 2 hours"
    check regardless of actual clock time — events never aged out within the same
    UTC day.

    Pin the correct post-fix behavior using explicit `iso_cutoff()` seed values.
    """

    @staticmethod
    def _seed_event(tmp_db_path, subscription_id: str, occurred_at: str) -> None:
        """Insert a rate-limit event with a specific occurred_at timestamp."""
        import sqlite3
        import uuid as _uuid

        conn = sqlite3.connect(str(tmp_db_path))
        try:
            conn.execute(
                "INSERT INTO subscription_rate_limit_events "
                "(id, agent_name, subscription_id, error_message, occurred_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (str(_uuid.uuid4()), "agent-x", subscription_id, "429", occurred_at),
            )
            conn.commit()
        finally:
            conn.close()

    def test_event_3h_ago_does_not_rate_limit(self, sub_ops, tmp_db):
        """Event occurred 3h ago → outside the 2h window → not rate-limited.

        Pre-fix this would incorrectly return True (same UTC day → date prefix
        matched → lexicographic compare at position 10 tripped on T > space)."""
        from utils.helpers import iso_cutoff

        self._seed_event(tmp_db, "sub-a", iso_cutoff(3))
        assert sub_ops.is_subscription_rate_limited("sub-a") is False

    def test_event_1h_ago_rate_limits(self, sub_ops, tmp_db):
        """Sanity check: event 1h ago is inside the 2h window → rate-limited."""
        from utils.helpers import iso_cutoff

        self._seed_event(tmp_db, "sub-a", iso_cutoff(1))
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

    def test_consecutive_count_excludes_out_of_window_event(self, sub_ops, tmp_db):
        """Two seeded events (3h ago + 1h ago) plus one live recording: the
        `consecutive_count` returned by `record_rate_limit_event` must count
        only in-window events (the 1h-old + just-now = 2). Pre-fix it would
        have counted all three = 3, because neither seeded event ages out."""
        from utils.helpers import iso_cutoff

        self._seed_event(tmp_db, "sub-a", iso_cutoff(3))  # outside 2h window
        self._seed_event(tmp_db, "sub-a", iso_cutoff(1))  # inside
        # Live record (stores occurred_at = utc_now_iso, clearly inside)
        count = sub_ops.record_rate_limit_event(
            agent_name="agent-x",
            subscription_id="sub-a",
            error_message="429",
        )
        assert count == 2  # 1h-ago + just-now. Pre-fix: 3.

    def test_event_25h_ago_does_not_rate_limit(self, sub_ops, tmp_db):
        """Cross-day boundary sanity: a 25h-old event (guaranteed to span UTC
        midnight from any execution time) must not rate-limit."""
        from utils.helpers import iso_cutoff

        self._seed_event(tmp_db, "sub-a", iso_cutoff(25))
        assert sub_ops.is_subscription_rate_limited("sub-a") is False

    def test_cleanup_removes_old_events(self, sub_ops, tmp_db):
        """`cleanup_old_rate_limit_events` deletes rows with occurred_at >24h
        ago, leaves fresher rows alone."""
        from utils.helpers import iso_cutoff

        self._seed_event(tmp_db, "sub-a", iso_cutoff(25))   # should prune
        self._seed_event(tmp_db, "sub-a", iso_cutoff(30))   # should prune
        self._seed_event(tmp_db, "sub-a", iso_cutoff(1))    # should keep
        pruned = sub_ops.cleanup_old_rate_limit_events()
        assert pruned == 2
        # Fresh event still flags the subscription
        assert sub_ops.is_subscription_rate_limited("sub-a") is True


# =============================================================================
# #441 regression: single failure triggers switch (threshold 1) + auth path
# =============================================================================
#
# These tests exercise `services.subscription_auto_switch` directly. That
# module does `from database import db` at top level, which would normally
# instantiate a real `DatabaseManager` (open SQLite, run migrations, ensure
# admin user). For unit tests we stub `database` and `db_models` in
# sys.modules BEFORE the import, so the service module gets a controllable
# fake `db` and zero side effects on import.


def _install_database_stub() -> object:
    """Pre-populate sys.modules['database'] with a stub exposing a
    `db = StubDB()` so `from database import db` resolves to our fake.

    Returns the stub `db` object so tests can configure it.
    """
    import types
    from unittest.mock import MagicMock

    stub_db = MagicMock(name="stub_db")
    # Default behaviors — tests override per-fixture
    stub_db.get_setting_value.return_value = "true"
    stub_db.get_agent_subscription_id.return_value = "sub-a"
    stub_db.record_rate_limit_event.return_value = 1
    stub_db.get_subscription.return_value = MagicMock(name="current_sub", name_attr="sub-a")
    # `get_subscription` returns an object with `.name`; MagicMock attribute
    # access returns a Mock — we want a real string for clean assertion.
    type(stub_db.get_subscription.return_value).name = "sub-a"
    stub_db.assign_subscription_to_agent.return_value = None
    stub_db.create_notification.return_value = None

    db_module = types.ModuleType("database")
    db_module.db = stub_db
    sys.modules["database"] = db_module

    # Minimal db_models stub — handle_subscription_failure → _perform_auto_switch
    # imports NotificationCreate. Provide a tolerant pass-through.
    if "db_models" not in sys.modules:
        models_module = types.ModuleType("db_models")

        class _NotificationCreate:
            def __init__(self, **kwargs):
                self.__dict__.update(kwargs)

        models_module.NotificationCreate = _NotificationCreate
        sys.modules["db_models"] = models_module

    return stub_db


class TestIsAuthFailure:
    """`is_auth_failure` correctly classifies common subscription error
    strings. Pure-function test — no db, no fixtures."""

    @pytest.fixture(autouse=True)
    def _stubs(self):
        _install_database_stub()

    def test_known_indicators_match(self):
        # Force re-import so the database stub is in place
        sys.modules.pop("services.subscription_auto_switch", None)
        from services.subscription_auto_switch import is_auth_failure

        positives = [
            "Your credit balance is too low to make this request",
            "401 Unauthorized",
            "HTTP 403 Forbidden",
            "OAuth token expired",
            "Authentication required",
            "Not authenticated",
            "Invalid credentials",
        ]
        for msg in positives:
            assert is_auth_failure(msg) is True, f"expected match for: {msg!r}"

    def test_unrelated_messages_do_not_match(self):
        sys.modules.pop("services.subscription_auto_switch", None)
        from services.subscription_auto_switch import is_auth_failure

        negatives = [
            "Connection reset by peer",
            "Internal Server Error",
            "Timeout while reading response",
            "Rate limit reached: please retry",
            "",
            None,
        ]
        for msg in negatives:
            assert is_auth_failure(msg) is False, f"unexpected match for: {msg!r}"


class TestSingleEventThreshold:
    """#441 — auto-switch must fire on the FIRST subscription failure (no 2× gate)
    and must trigger on auth-class failures, not just 429s.

    `_perform_auto_switch` is stubbed to avoid Docker / activity-service /
    notifications. The behaviors under test (threshold, classifier dispatch,
    alternative-selection skip-list) all happen before that call.
    """

    @pytest.fixture
    def svc(self, monkeypatch):
        """Yield the auto-switch service module with `database.db` stubbed
        and `_perform_auto_switch` replaced with a recording spy."""
        import importlib
        from unittest.mock import MagicMock

        stub_db = _install_database_stub()

        # Ensure a fresh import so the new database stub is picked up
        sys.modules.pop("services.subscription_auto_switch", None)
        import services.subscription_auto_switch as auto_switch
        importlib.reload(auto_switch)

        # Default alternative subscription returned by select_best_alternative_subscription
        alt = MagicMock()
        alt.id = "sub-b"
        alt.name = "sub-b"
        stub_db.select_best_alternative_subscription.return_value = alt

        # Stub the heavy sub-call. Record args, return a synthetic switch result.
        calls = []

        async def _spy(**kwargs):
            calls.append(kwargs)
            return {
                "switched": True,
                "agent_name": kwargs["agent_name"],
                "old_subscription": kwargs["old_subscription_name"],
                "new_subscription": kwargs["new_subscription"].name,
                "failure_kind": kwargs["failure_kind"],
                "event_count": kwargs["event_count"],
                "restart_result": "stub",
            }

        monkeypatch.setattr(auto_switch, "_perform_auto_switch", _spy)
        auto_switch._spy_calls = calls  # exposed for assertions
        auto_switch._stub_db = stub_db  # exposed for per-test reconfigure
        return auto_switch

    @pytest.mark.asyncio
    async def test_first_429_triggers_switch(self, svc):
        """A single 429 on a subscription-backed agent triggers auto-switch
        when an alternative is viable. Pre-#441 this required 2 events."""
        result = await svc.handle_subscription_failure(
            agent_name="agent-x",
            error_message="429 Too Many Requests",
            failure_kind="rate_limit",
        )
        assert result is not None
        assert result["switched"] is True
        assert result["new_subscription"] == "sub-b"
        assert result["failure_kind"] == "rate_limit"
        assert len(svc._spy_calls) == 1
        assert svc._spy_calls[0]["event_count"] == 1

    @pytest.mark.asyncio
    async def test_first_auth_error_triggers_switch(self, svc):
        """A single auth-class failure also triggers auto-switch — the
        important #441 broadening."""
        result = await svc.handle_subscription_failure(
            agent_name="agent-x",
            error_message="Your credit balance is too low",
            failure_kind="auth",
        )
        assert result is not None
        assert result["switched"] is True
        assert result["failure_kind"] == "auth"
        assert len(svc._spy_calls) == 1

    @pytest.mark.asyncio
    async def test_handle_rate_limit_error_shim_still_works(self, svc):
        """Backward-compat shim: existing 429 callers keep working without
        migration."""
        result = await svc.handle_rate_limit_error(
            agent_name="agent-x",
            error_message="429",
        )
        assert result is not None
        assert result["failure_kind"] == "rate_limit"

    @pytest.mark.asyncio
    async def test_no_switch_when_alternative_recently_rate_limited(self, svc):
        """Regression on the 2h skip-list: when no alternative is viable,
        the service must NOT call _perform_auto_switch even at threshold=1.
        We simulate the skip-list returning None for the alternative."""
        svc._stub_db.select_best_alternative_subscription.return_value = None

        result = await svc.handle_subscription_failure(
            agent_name="agent-x",
            error_message="429",
            failure_kind="rate_limit",
        )
        assert result is None
        assert svc._spy_calls == []

    @pytest.mark.asyncio
    async def test_setting_disabled_blocks_switch(self, svc):
        """Operators who explicitly opted out keep their choice — when the
        setting is "false", short-circuit before recording any event."""
        svc._stub_db.get_setting_value.return_value = "false"

        result = await svc.handle_subscription_failure(
            agent_name="agent-x",
            error_message="429",
            failure_kind="rate_limit",
        )
        assert result is None
        assert svc._spy_calls == []
        # Also verify we short-circuited before recording the event
        svc._stub_db.record_rate_limit_event.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_switch_when_agent_has_no_subscription(self, svc):
        """API-key-backed agents (no subscription assigned) are skipped."""
        svc._stub_db.get_agent_subscription_id.return_value = None

        result = await svc.handle_subscription_failure(
            agent_name="agent-x",
            error_message="429",
            failure_kind="rate_limit",
        )
        assert result is None
        assert svc._spy_calls == []


# =============================================================================
# #1089: hot-reload subscription token (rotate without recreating the container)
# =============================================================================
#
# `_hot_reload_subscription_token` pushes the agent's current DB subscription
# token to the running container via POST /api/credentials/reload-token, so the
# NEXT claude subprocess uses the new token while in-flight turns keep their
# already-inherited old token and finish. It falls back to the full
# `_restart_agent` path (today's behavior) on:
#   - transport failure (AgentClientError / AgentNotReachableError),
#   - HTTP >= 400 (a 404 means an old base image without the endpoint), or
#   - no resolvable token.
# Early-returns `no_container` / `not_running` exactly like `_restart_agent`.

import asyncio  # noqa: E402  (used by the hot-reload + key-rollover tests below)
import types as _types  # noqa: E402  (module-level helpers for the tests below)
from unittest.mock import AsyncMock  # noqa: E402


def _docker_stub(*, container: object = object(), status: str = "running"):
    """Fake `services.docker_service` exposing the two helper lookups."""
    mod = _types.ModuleType("services.docker_service")
    mod.get_agent_container = lambda name: container
    _status = _types.SimpleNamespace(status=status)
    mod.get_agent_status_from_container = lambda c: _status
    return mod


class _StubAgentClientError(Exception):
    pass


class _StubAgentNotReachableError(_StubAgentClientError):
    pass


def _agent_client_stub(*, post):
    """Fake `services.agent_client`. `post` is bound to `client.post`
    (an AsyncMock or coroutine fn). `AgentClientError` is the base the helper
    catches; `AgentNotReachableError` subclasses it (transport-failure case)."""
    mod = _types.ModuleType("services.agent_client")
    mod.AgentClientError = _StubAgentClientError
    mod.AgentNotReachableError = _StubAgentNotReachableError
    client = _types.SimpleNamespace(post=post)
    mod.get_agent_client = lambda name: client
    return mod


class TestHotReloadSwitch:
    """#1089 — the auto-switch path hot-reloads the token instead of recreating
    the container; it falls back to restart on 404 / transport error / no token,
    and short-circuits when the agent isn't a running container."""

    @pytest.fixture
    def auto_switch(self, monkeypatch):
        import importlib

        stub_db = _install_database_stub()
        # Token resolution defaults: agent on sub-a, sub-a token present.
        stub_db.get_agent_subscription_id.return_value = "sub-a"
        stub_db.get_subscription_token.return_value = "sk-ant-oat01-new-token"

        import services.subscription_auto_switch as mod
        importlib.reload(mod)

        # Spy the fallback so we can assert when it IS / IS NOT taken.
        restart_calls: list[str] = []

        async def _restart_spy(agent_name):
            restart_calls.append(agent_name)
            return "restarted_fallback"

        monkeypatch.setattr(mod, "_restart_agent", _restart_spy)
        mod._restart_calls = restart_calls  # type: ignore[attr-defined]
        mod._stub_db = stub_db  # type: ignore[attr-defined]
        return mod

    @pytest.mark.asyncio
    async def test_happy_path_posts_reload_token_no_recreate(self, auto_switch, monkeypatch):
        post = AsyncMock(return_value=_types.SimpleNamespace(status_code=200))
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub())
        monkeypatch.setitem(sys.modules, "services.agent_client", _agent_client_stub(post=post))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "hot_reloaded"
        assert auto_switch._restart_calls == []  # NO container recreate — in-flight turns survive
        post.assert_awaited_once()
        args, kwargs = post.call_args
        assert args[0] == "/api/credentials/reload-token"
        assert kwargs["json"] == {"token": "sk-ant-oat01-new-token", "remove_api_key": False}

    @pytest.mark.asyncio
    async def test_falls_back_to_restart_on_404(self, auto_switch, monkeypatch):
        """404 = old base image without the endpoint → restart fallback."""
        post = AsyncMock(return_value=_types.SimpleNamespace(status_code=404))
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub())
        monkeypatch.setitem(sys.modules, "services.agent_client", _agent_client_stub(post=post))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "restarted_fallback"
        assert auto_switch._restart_calls == ["agent-x"]

    @pytest.mark.asyncio
    async def test_falls_back_to_restart_on_transport_error(self, auto_switch, monkeypatch):
        post = AsyncMock(side_effect=_StubAgentNotReachableError("connection refused"))
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub())
        monkeypatch.setitem(sys.modules, "services.agent_client", _agent_client_stub(post=post))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "restarted_fallback"
        assert auto_switch._restart_calls == ["agent-x"]

    @pytest.mark.asyncio
    async def test_falls_back_to_restart_when_no_token(self, auto_switch, monkeypatch):
        auto_switch._stub_db.get_subscription_token.return_value = None
        post = AsyncMock()
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub())
        monkeypatch.setitem(sys.modules, "services.agent_client", _agent_client_stub(post=post))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "restarted_fallback"
        assert auto_switch._restart_calls == ["agent-x"]
        post.assert_not_awaited()  # never reached the POST

    @pytest.mark.asyncio
    async def test_no_container_short_circuits(self, auto_switch, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub(container=None))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "no_container"
        assert auto_switch._restart_calls == []

    @pytest.mark.asyncio
    async def test_not_running_short_circuits(self, auto_switch, monkeypatch):
        monkeypatch.setitem(sys.modules, "services.docker_service", _docker_stub(status="stopped"))

        result = await auto_switch._hot_reload_subscription_token("agent-x")

        assert result == "not_running"
        assert auto_switch._restart_calls == []

    @pytest.mark.asyncio
    async def test_perform_auto_switch_hot_reloads_not_restarts(self, auto_switch, monkeypatch):
        """The auto-switch wire-in: `_perform_auto_switch` routes through the
        hot-reload helper, so `restart_result == "hot_reloaded"` and the
        recreate path (`_restart_agent`) is never taken."""
        # Stub the heavy local-import targets in `_perform_auto_switch`.
        act_mod = _types.ModuleType("services.activity_service")
        act_svc = MagicMock()
        act_svc.track_activity = AsyncMock(return_value="act-1")
        act_svc.complete_activity = AsyncMock(return_value=None)
        act_mod.activity_service = act_svc
        monkeypatch.setitem(sys.modules, "services.activity_service", act_mod)

        models_mod = _types.ModuleType("models")
        models_mod.ActivityType = _types.SimpleNamespace(SCHEDULE_END="schedule_end")
        models_mod.ActivityState = _types.SimpleNamespace(COMPLETED="completed", FAILED="failed")
        monkeypatch.setitem(sys.modules, "models", models_mod)

        hot_calls: list[str] = []

        async def _hot_spy(agent_name):
            hot_calls.append(agent_name)
            return "hot_reloaded"

        monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token", _hot_spy)

        new_sub = MagicMock()
        new_sub.id = "sub-b"
        new_sub.name = "sub-B"

        result = await auto_switch._perform_auto_switch(
            agent_name="agent-x",
            old_subscription_id="sub-a",
            old_subscription_name="sub-A",
            new_subscription=new_sub,
            failure_kind="rate_limit",
            event_count=1,
        )

        assert result["switched"] is True
        assert result["restart_result"] == "hot_reloaded"
        assert hot_calls == ["agent-x"]  # hot-reload used
        assert auto_switch._restart_calls == []  # recreate path NOT taken


class TestKeyRolloverFanOut:
    """#1089 (F1) — re-registering a subscription's token fans a best-effort
    hot-reload out to every running agent on that subscription. One agent's
    failure must not abort the fan-out nor block the others."""

    @pytest.fixture
    def auto_switch(self, monkeypatch):
        import importlib

        stub_db = _install_database_stub()
        import services.subscription_auto_switch as mod
        importlib.reload(mod)
        mod._stub_db = stub_db  # type: ignore[attr-defined]
        return mod

    @pytest.mark.asyncio
    async def test_fan_out_attempts_every_agent_despite_one_failure(self, auto_switch, monkeypatch):
        auto_switch._stub_db.get_agents_by_subscription.return_value = ["a1", "a2", "a3"]

        seen: list[str] = []

        async def _hot(name):
            seen.append(name)
            if name == "a2":
                raise RuntimeError("boom")
            return "hot_reloaded"

        monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token", _hot)

        async def _lock(name):
            return asyncio.Lock()

        monkeypatch.setattr(auto_switch, "agent_switch_lock", _lock)

        results = await auto_switch.reload_subscription_for_all_agents("sub-a")

        assert seen == ["a1", "a2", "a3"]  # all attempted, fan-out not aborted
        assert results["a1"] == "hot_reloaded"
        assert results["a3"] == "hot_reloaded"
        assert results["a2"].startswith("failed:")

    @pytest.mark.asyncio
    async def test_fan_out_no_agents_is_noop(self, auto_switch, monkeypatch):
        auto_switch._stub_db.get_agents_by_subscription.return_value = []

        async def _hot(name):
            raise AssertionError("must not be called when no agents are assigned")

        monkeypatch.setattr(auto_switch, "_hot_reload_subscription_token", _hot)

        results = await auto_switch.reload_subscription_for_all_agents("sub-a")

        assert results == {}
