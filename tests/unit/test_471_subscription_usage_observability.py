"""
#471 — Subscription usage observability: data-layer + service tests.

Covers the five load-bearing changes:
1. `failure_kind` persisted on subscription failure events (24h/kind counters,
   NULL rows bucket as "unknown", 24h boundary honored).
2. `get_subscription_usage` carries the new counters + the DB half of
   `rate_limited_now` (2h predicate), and the extended model's defaults keep
   pre-#471 constructors valid.
3. Per-agent breakdown: chat + execution rows merged per agent, ranked by
   cost desc (the model-mix answer — cost is model-weighted by construction).
4. Record-before-gate: `handle_subscription_failure` records the event even
   when auto-switch is DISABLED (the pre-#471 producer returned before
   recording, blanking observability for opted-out operators).
5. Headroom header parsing (pure functions) against the exact header set the
   2026-08-19 spike captured, + the one-gate `rate_limited_now` freshness rule.

Plus two static pins: `/subscription-pressure` registered before `/{agent_name}`
(Invariant #4), and the new subscription endpoints keep `assert_admin`.

Test hygiene per learnings 2026-08-12/13 (#2114 stub-leak class): no bare
`sys.modules` stubs; the async producer test monkeypatches ATTRIBUTES on the
real imported module and restores via pytest's monkeypatch.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make src/backend importable (mirrors test_subscription_auto_switch_pingpong.py).
_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Fresh SQLite DB with the tables the #471 readers touch."""
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
    cur.execute(
        "INSERT INTO subscription_credentials "
        "(id, name, encrypted_credentials, owner_id, created_at, updated_at) "
        "VALUES ('sub-a', 'sub-A', 'enc-a', 1, ?, ?)",
        (now, now),
    )
    cur.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, subscription_id, use_platform_api_key) "
        "VALUES ('agent-x', 1, 'sub-a', 0)"
    )
    cur.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, subscription_id, use_platform_api_key) "
        "VALUES ('agent-api', 1, NULL, 1)"
    )
    cur.execute(
        "INSERT INTO agent_ownership (agent_name, owner_id, subscription_id, deleted_at) "
        "VALUES ('agent-dead', 1, 'sub-a', ?)",
        (now,),
    )
    conn.commit()
    conn.close()

    # Force re-import so the module-level DB path picks up our env var
    # (same intent as test_subscription_auto_switch_pingpong.py, but via
    # monkeypatch so the original modules are restored at teardown).
    for mod in ("db.connection", "db.subscriptions"):
        monkeypatch.delitem(sys.modules, mod, raising=False)

    yield db_path


@pytest.fixture
def sub_ops(tmp_db):
    from db.subscriptions import SubscriptionOperations

    return SubscriptionOperations(encryption_service=MagicMock())


def _insert_event(db_path, subscription_id, occurred_at, failure_kind="rate_limit", agent="agent-x"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO subscription_rate_limit_events "
        "(id, agent_name, subscription_id, error_message, failure_kind, occurred_at) "
        "VALUES (?, ?, ?, '', ?, ?)",
        (uuid.uuid4().hex, agent, subscription_id, failure_kind, occurred_at),
    )
    conn.commit()
    conn.close()


# =============================================================================
# 1. failure_kind persistence + counters
# =============================================================================

class TestFailureKindCounters:
    def test_writer_persists_kind_and_default(self, sub_ops):
        sub_ops.record_rate_limit_event("agent-x", "sub-a", "boom")  # default kind
        sub_ops.record_rate_limit_event("agent-x", "sub-a", "denied", failure_kind="auth")
        counts = sub_ops.get_failure_event_counts("sub-a", hours=24)
        assert counts["total"] == 2
        assert counts["by_kind"] == {"rate_limit": 1, "auth": 1}

    def test_null_kind_buckets_as_unknown(self, sub_ops, tmp_db):
        now = datetime.now(timezone.utc)
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=1)), failure_kind=None)
        counts = sub_ops.get_failure_event_counts("sub-a", hours=24)
        assert counts["by_kind"] == {"unknown": 1}

    def test_24h_boundary(self, sub_ops, tmp_db):
        now = datetime.now(timezone.utc)
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=23)))
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=25)))
        counts = sub_ops.get_failure_event_counts("sub-a", hours=24)
        assert counts["total"] == 1

    def test_grouped_by_subscription(self, sub_ops, tmp_db):
        now = datetime.now(timezone.utc)
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=1)))
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=2)))
        _insert_event(tmp_db, "sub-other", _iso(now - timedelta(hours=1)))
        by_sub = sub_ops.get_failure_event_counts_by_subscription(hours=24)
        assert by_sub == {"sub-a": 2, "sub-other": 1}


# =============================================================================
# 2. Usage extension + model defaults
# =============================================================================

class TestUsageExtension:
    def test_usage_carries_counters_and_2h_predicate(self, sub_ops, tmp_db):
        now = datetime.now(timezone.utc)
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(minutes=30)), failure_kind="auth")
        usage = sub_ops.get_subscription_usage("sub-a")
        assert usage.failure_events_24h == 1
        assert usage.failure_events_by_kind == {"auth": 1}
        assert usage.rate_limited_now is True  # event inside the 2h window
        assert usage.source == "observed"
        assert usage.headroom is None

    def test_old_event_does_not_set_rate_limited_now(self, sub_ops, tmp_db):
        now = datetime.now(timezone.utc)
        _insert_event(tmp_db, "sub-a", _iso(now - timedelta(hours=5)))
        usage = sub_ops.get_subscription_usage("sub-a")
        assert usage.failure_events_24h == 1
        assert usage.rate_limited_now is False

    def test_model_defaults_keep_old_constructors_valid(self):
        from db_models import SubscriptionUsage, SubscriptionUsageWindow

        usage = SubscriptionUsage(
            subscription_id="s",
            window_5h=SubscriptionUsageWindow(),
            window_7d=SubscriptionUsageWindow(),
        )
        assert usage.failure_events_24h == 0
        assert usage.failure_events_by_kind == {}
        assert usage.rate_limited_now is False
        assert usage.source == "observed"
        assert usage.headroom is None


# =============================================================================
# 3. Per-agent breakdown
# =============================================================================

class TestBreakdown:
    def _seed_consumption(self, db_path):
        """Realistic seeds: a modern turn writes BOTH an execution row and (for
        /chat + persisted /task) a cost-bearing chat message — verified live on
        2026-08-19 (one sync-chat turn: chat_messages cost 0.0687 AND a
        triggered_by='chat' execution cost 0.0687)."""
        now = datetime.now(timezone.utc)
        recent = _iso(now - timedelta(hours=1))
        conn = sqlite3.connect(str(db_path))
        # agent-x turn 1: chat turn — BOTH rows carry the same cost (the pair)
        conn.execute(
            "INSERT INTO chat_messages (id, agent_name, subscription_id, role, timestamp, context_used, output_tokens, cost) "
            "VALUES ('m1', 'agent-x', 'sub-a', 'assistant', ?, 1000, 200, 0.10)",
            (recent,),
        )
        conn.execute(
            "INSERT INTO schedule_executions (id, agent_name, subscription_id, status, started_at, context_used, cost) "
            "VALUES ('e1', 'agent-x', 'sub-a', 'success', ?, 1000, 0.10)",
            (recent,),
        )
        # agent-x turn 2: schedule run (execution only, no chat row)
        conn.execute(
            "INSERT INTO schedule_executions (id, agent_name, subscription_id, status, started_at, context_used, cost) "
            "VALUES ('e2', 'agent-x', 'sub-a', 'success', ?, 2000, 0.20)",
            (recent,),
        )
        # agent-y: one chat turn (paired rows)
        conn.execute(
            "INSERT INTO chat_messages (id, agent_name, subscription_id, role, timestamp, context_used, output_tokens, cost) "
            "VALUES ('m2', 'agent-y', 'sub-a', 'assistant', ?, 500, 100, 0.50)",
            (recent,),
        )
        conn.execute(
            "INSERT INTO schedule_executions (id, agent_name, subscription_id, status, started_at, context_used, cost) "
            "VALUES ('e3', 'agent-y', 'sub-a', 'success', ?, 500, 0.50)",
            (recent,),
        )
        # user-role row must NOT count
        conn.execute(
            "INSERT INTO chat_messages (id, agent_name, subscription_id, role, timestamp, context_used, output_tokens, cost) "
            "VALUES ('m3', 'agent-x', 'sub-a', 'user', ?, 999, 999, 9.99)",
            (recent,),
        )
        # running row must NOT count
        conn.execute(
            "INSERT INTO schedule_executions (id, agent_name, subscription_id, status, started_at, context_used, cost) "
            "VALUES ('e4', 'agent-x', 'sub-a', 'running', ?, 5000, 5.00)",
            (recent,),
        )
        conn.commit()
        conn.close()

    def test_chat_turn_cost_counted_once(self, sub_ops, tmp_db):
        """THE dedup pin: a chat turn present in both tables contributes its
        cost exactly once (executions are the sole cost source)."""
        self._seed_consumption(tmp_db)
        usage = sub_ops.get_subscription_usage("sub-a")
        # exec side only: 0.10 + 0.20 + 0.50 — NOT + the chat copies (0.60 more)
        assert usage.window_7d.cost_usd == pytest.approx(0.80)
        assert usage.window_7d.message_count == 3          # terminal executions
        assert usage.window_7d.input_tokens == 3500        # exec context sums
        assert usage.window_7d.output_tokens == 300        # chat-side only source

    def test_breakdown_ranked_by_cost_and_deduped(self, sub_ops, tmp_db):
        self._seed_consumption(tmp_db)
        bd = sub_ops.get_subscription_usage_breakdown("sub-a")
        rows = {r.agent_name: r for r in bd.window_7d}
        assert rows["agent-x"].cost_usd == pytest.approx(0.30)   # 0.10 + 0.20, chat copy NOT re-added
        assert rows["agent-x"].input_tokens == 3000              # exec ctx 1000 + 2000
        assert rows["agent-x"].output_tokens == 200
        assert rows["agent-x"].message_count == 2
        assert rows["agent-y"].cost_usd == pytest.approx(0.50)
        assert rows["agent-y"].output_tokens == 100
        # Ranked by cost desc: agent-y (0.50) before agent-x (0.30)
        assert [r.agent_name for r in bd.window_7d] == ["agent-y", "agent-x"]

    def test_empty_subscription_yields_empty_lists(self, sub_ops):
        bd = sub_ops.get_subscription_usage_breakdown("sub-a")
        assert bd.window_5h == []
        assert bd.window_7d == []


# =============================================================================
# 4. Batch agent→subscription map (pressure endpoint's data)
# =============================================================================

class TestAgentSubscriptionMap:
    def test_fleet_map_excludes_soft_deleted(self, sub_ops):
        m = sub_ops.get_agent_subscription_map(None)
        assert set(m) == {"agent-x", "agent-api"}
        assert m["agent-x"] == {
            "subscription_id": "sub-a",
            "subscription_name": "sub-A",
            "use_platform_api_key": False,
        }
        assert m["agent-api"]["subscription_id"] is None
        assert m["agent-api"]["use_platform_api_key"] is True

    def test_scoped_names_filter(self, sub_ops):
        m = sub_ops.get_agent_subscription_map(["agent-api"])
        assert set(m) == {"agent-api"}

    def test_empty_scope_returns_empty(self, sub_ops):
        assert sub_ops.get_agent_subscription_map([]) == {}


# =============================================================================
# 5. derive_auth_mode — one vocabulary
# =============================================================================

class TestDeriveAuthMode:
    def test_truth_table(self, tmp_db):
        from services.subscription_service import derive_auth_mode

        assert derive_auth_mode(True, True) == "subscription"
        assert derive_auth_mode(True, False) == "subscription"
        assert derive_auth_mode(False, True) == "api_key"
        assert derive_auth_mode(False, False) == "not_configured"


# =============================================================================
# 6. Record-before-gate (the producer fix)
# =============================================================================

class TestRecordBeforeGate:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_disabled_auto_switch_still_records(self, monkeypatch, tmp_db):
        import services.subscription_auto_switch as auto_switch

        fake_db = MagicMock()
        fake_db.get_agent_subscription_id.return_value = "sub-1"
        fake_db.get_setting_value.return_value = "false"  # auto-switch OFF
        fake_db.record_rate_limit_event.return_value = 1
        monkeypatch.setattr(auto_switch, "db", fake_db)

        result = self._run(
            auto_switch.handle_subscription_failure("agent-x", "429 boom", "rate_limit")
        )
        assert result is None  # no switch attempted
        fake_db.record_rate_limit_event.assert_called_once_with(
            agent_name="agent-x",
            subscription_id="sub-1",
            error_message="429 boom",
            failure_kind="rate_limit",
        )

    def test_no_subscription_records_nothing(self, monkeypatch, tmp_db):
        import services.subscription_auto_switch as auto_switch

        fake_db = MagicMock()
        fake_db.get_agent_subscription_id.return_value = None
        monkeypatch.setattr(auto_switch, "db", fake_db)

        result = self._run(
            auto_switch.handle_subscription_failure("agent-x", "boom", "auth")
        )
        assert result is None
        fake_db.record_rate_limit_event.assert_not_called()

    def test_auth_kind_threaded_through(self, monkeypatch, tmp_db):
        import services.subscription_auto_switch as auto_switch

        fake_db = MagicMock()
        fake_db.get_agent_subscription_id.return_value = "sub-1"
        fake_db.get_setting_value.return_value = "false"
        fake_db.record_rate_limit_event.return_value = 1
        monkeypatch.setattr(auto_switch, "db", fake_db)

        self._run(auto_switch.handle_subscription_failure("agent-x", "401", "auth"))
        assert fake_db.record_rate_limit_event.call_args.kwargs["failure_kind"] == "auth"


# =============================================================================
# 7. Headroom header parsing (pure) + freshness rule
# =============================================================================

class TestHeadroomParsing:
    # The exact header set the 2026-08-19 spike captured from a real probe.
    SPIKE_HEADERS = {
        "anthropic-ratelimit-unified-5h-reset": "1787155200",
        "anthropic-ratelimit-unified-5h-status": "allowed",
        "anthropic-ratelimit-unified-5h-utilization": "0.15",
        "anthropic-ratelimit-unified-7d-reset": "1787720400",
        "anthropic-ratelimit-unified-7d-status": "allowed",
        "anthropic-ratelimit-unified-7d-utilization": "0.06",
        "anthropic-ratelimit-unified-representative-claim": "five_hour",
        "anthropic-ratelimit-unified-overage-status": "rejected",
        "anthropic-ratelimit-unified-status": "allowed",
    }

    def test_spike_payload_parses(self, tmp_db):
        from services.subscription_headroom_service import parse_unified_headers

        snap = parse_unified_headers(self.SPIKE_HEADERS)
        assert snap["five_hour"]["utilization_pct"] == 15.0
        assert snap["five_hour"]["resets_at"].endswith("Z")
        assert snap["seven_day"]["utilization_pct"] == 6.0
        assert snap["representative_claim"] == "five_hour"
        assert snap["overage_status"] == "rejected"

    def test_snapshot_age_parses_microsecond_timestamps(self, tmp_db):
        """utc_now_iso() emits microseconds; a seconds-only strptime made every
        age None — 'refresh due' on every poll AND 'never fresh' for the
        gauge/limited verdict. Caught live on the first deployed probe."""
        from services.subscription_headroom_service import _snapshot_age_seconds
        from utils.helpers import utc_now_iso

        age = _snapshot_age_seconds({"fetched_at": utc_now_iso()})
        assert age is not None and 0 <= age <= 5
        age2 = _snapshot_age_seconds({"fetched_at": "2026-08-19T13:13:16.356984Z"})
        assert age2 is not None
        assert _snapshot_age_seconds({"fetched_at": "2026-08-19T13:13:16Z"}) is not None
        assert _snapshot_age_seconds({"fetched_at": "garbage"}) is None
        assert _snapshot_age_seconds({}) is None

    def test_absent_family_returns_none(self, tmp_db):
        from services.subscription_headroom_service import parse_unified_headers

        assert parse_unified_headers({"content-type": "application/json"}) is None

    def test_already_percent_value_tolerated(self, tmp_db):
        from services.subscription_headroom_service import _parse_utilization

        assert _parse_utilization("0.15") == 15.0
        assert _parse_utilization("1.0") == 100.0
        assert _parse_utilization("42.5") == 42.5
        assert _parse_utilization("garbage") is None
        assert _parse_utilization(None) is None

    def test_freshness_gates_the_limited_verdict(self, tmp_db):
        from db_models import HeadroomWindow, SubscriptionHeadroom
        from services.subscription_headroom_service import _headroom_indicates_limited

        fresh_limited = SubscriptionHeadroom(
            five_hour=HeadroomWindow(utilization_pct=100.0, status="rejected"),
            snapshot_age_seconds=60,
            status="ok",
        )
        assert _headroom_indicates_limited(fresh_limited) is True

        stale_limited = fresh_limited.model_copy(update={"snapshot_age_seconds": 999999})
        assert _headroom_indicates_limited(stale_limited) is False

        fresh_allowed = SubscriptionHeadroom(
            five_hour=HeadroomWindow(utilization_pct=15.0, status="allowed"),
            snapshot_age_seconds=60,
            status="ok",
        )
        assert _headroom_indicates_limited(fresh_allowed) is False

        rate_limited_status = SubscriptionHeadroom(status="rate_limited", snapshot_age_seconds=60)
        assert _headroom_indicates_limited(rate_limited_status) is True

        assert _headroom_indicates_limited(None) is False


# =============================================================================
# 7b. Fail-closed ambient probing (review fixes C1/C2)
# =============================================================================

class TestAmbientProbeFailClosed:
    def test_unreachable_redis_never_ambient_probes(self, monkeypatch, tmp_db):
        """A Redis CLIENT existing proves nothing about the SERVER. When the
        cache read raises, the ambient path must not probe (fail-closed) —
        collapsing 'Redis down' into 'no snapshot' re-opens the
        probe-per-poll storm."""
        import services.subscription_headroom_service as hs

        broken = MagicMock()
        broken.get.side_effect = ConnectionError("redis down")
        monkeypatch.setattr(hs, "get_breaker_redis", lambda: broken)
        monkeypatch.setattr(hs, "is_auto_refresh_enabled", lambda: True)

        probe_spy = MagicMock()

        async def _no_probe(sid):
            probe_spy(sid)
            return {"fetched_at": "2026-08-19T00:00:00Z", "status": "ok"}

        monkeypatch.setattr(hs, "_probe", _no_probe)

        result = asyncio.run(hs.get_headroom("sub-a"))
        assert result is None
        probe_spy.assert_not_called()

    def test_wait_false_serves_stale_without_blocking(self, monkeypatch, tmp_db):
        """The dashboard batch path (wait=False) must return the cached
        snapshot immediately; the refresh happens in a background task."""
        import services.subscription_headroom_service as hs

        stale = {
            "fetched_at": "2020-01-01T00:00:00Z",  # ancient — refresh due
            "status": "ok",
            "five_hour": {"utilization_pct": 10.0, "resets_at": None, "status": "allowed"},
        }
        monkeypatch.setattr(hs, "_read_snapshot", lambda sid: (True, dict(stale)))
        monkeypatch.setattr(hs, "is_auto_refresh_enabled", lambda: True)
        monkeypatch.setattr(hs, "_probe_floor_ok", lambda sid, snap: True)

        started = MagicMock()

        async def _slow_probe(sid):
            started(sid)
            await asyncio.sleep(30)  # a hung provider must not block the caller

        monkeypatch.setattr(hs, "_locked_probe", _slow_probe)

        async def scenario():
            return await asyncio.wait_for(
                hs.get_headroom("sub-a", wait=False), timeout=2.0
            )

        result = asyncio.run(scenario())
        assert result is not None
        assert result.five_hour.utilization_pct == 10.0  # the STALE reading, served now
        started.assert_called_once()  # refresh was spawned, not awaited


# =============================================================================
# 8. Static pins: route order (Invariant #4) + admin gates
# =============================================================================

class TestStaticPins:
    AGENTS_SRC = (_BACKEND / "routers" / "agents.py").read_text()
    SUBS_SRC = (_BACKEND / "routers" / "subscriptions.py").read_text()

    def test_pressure_route_registered_before_catchall(self):
        pressure = self.AGENTS_SRC.index('@router.get("/subscription-pressure"')
        catchall = self.AGENTS_SRC.index('"/{agent_name}"')
        assert pressure < catchall, (
            "Invariant #4: /subscription-pressure must be declared before the "
            "/{agent_name} catch-all or it resolves as an agent named "
            "'subscription-pressure'"
        )

    def test_new_subscription_endpoints_keep_admin_gate(self):
        import ast

        tree = ast.parse(self.SUBS_SRC)
        gated = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in (
                "get_subscription_usage_breakdown",
                "refresh_subscription_headroom",
                "get_headroom_auto_refresh_setting",
                "set_headroom_auto_refresh_setting",
            ):
                calls = {
                    n.func.id
                    for n in ast.walk(node)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                }
                gated[node.name] = "assert_admin" in calls
        assert len(gated) == 4, f"missing handler(s): {gated}"
        assert all(gated.values()), f"handler missing assert_admin: {gated}"
