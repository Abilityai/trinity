"""
#2352 — the display predicate is narrowed to real 429s; candidate selection
stays kind-blind.

`is_subscription_rate_limited()` used to count EVERY row in
`subscription_rate_limit_events` within its 2h window regardless of
`failure_kind`, so an auth failure (401/403 — a dead, expired, or
`.env`-shadowed token) set `rate_limited_now` and every surface reported a
credential problem as quota exhaustion. That is exactly the conflation #471's
`failure_kind` column exists to end.

The fix is a SPLIT, not a filter, because the predicate had two consumers with
incompatible meanings:

  - display (`decorate_usage` / `pressure_states` / `get_subscription_usage`)
    wants "is this throttled right now" → rate_limit-kind only;
  - candidate selection (`select_best_alternative_subscription`,
    `select_subscription_for_new_agent`) wants "did this fail recently for ANY
    reason" → kind-blind, or auto-switch starts moving agents onto
    subscriptions whose token it just watched get rejected (the #444 class).

The second half is the regression these tests exist to hold: narrowing the
predicate in place would have passed every display assertion below and quietly
broken switch safety.
"""

from __future__ import annotations

import sqlite3
import sys
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
# 1. The display predicate: rate_limit only
# =============================================================================

class TestDisplayPredicate:
    def test_rate_limit_event_reports_limited(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="rate_limit")
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

    def test_auth_event_alone_does_not_report_limited(self, sub_ops, tmp_db):
        """The bug. A rejected token is a credential problem — claiming a limit
        sends the operator to wait out a window that was never full."""
        _event(tmp_db, "sub-a", failure_kind="auth")
        assert sub_ops.is_subscription_rate_limited("sub-a") is False

    def test_mixed_kinds_report_limited(self, sub_ops, tmp_db):
        """One real 429 is enough — auth events beside it neither add to nor
        cancel that."""
        _event(tmp_db, "sub-a", failure_kind="auth")
        _event(tmp_db, "sub-a", failure_kind="rate_limit")
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

    def test_null_kind_alone_does_not_report_limited(self, sub_ops, tmp_db):
        """Pre-#471 rows are genuinely unknown. `IN (...)` never matches NULL,
        and that is the documented direction: unknown is not promoted to 429,
        matching the frontend's refusal to fold `unknown` into `rate_limit`."""
        _event(tmp_db, "sub-a", failure_kind=None)
        assert sub_ops.is_subscription_rate_limited("sub-a") is False

    def test_window_is_still_two_hours(self, sub_ops, tmp_db):
        """Narrowing by kind must not have moved the window."""
        _event(tmp_db, "sub-a", failure_kind="rate_limit", minutes_ago=125)
        assert sub_ops.is_subscription_rate_limited("sub-a") is False
        _event(tmp_db, "sub-a", failure_kind="rate_limit", minutes_ago=115)
        assert sub_ops.is_subscription_rate_limited("sub-a") is True

    def test_scoped_to_the_subscription(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-b", failure_kind="rate_limit")
        assert sub_ops.is_subscription_rate_limited("sub-a") is False
        assert sub_ops.is_subscription_rate_limited("sub-b") is True

    def test_usage_row_reflects_the_narrowed_predicate(self, sub_ops, tmp_db):
        """`get_subscription_usage.rate_limited_now` is a display field and
        rides the same predicate — the tile reads it through `decorate_usage`."""
        _event(tmp_db, "sub-a", failure_kind="auth")
        usage = sub_ops.get_subscription_usage("sub-a")
        assert usage.rate_limited_now is False
        # ...but the failure is still VISIBLE, not silently dropped.
        assert usage.failure_events_24h == 1
        assert usage.failure_events_by_kind == {"auth": 1}


# =============================================================================
# 2. The candidate-skip predicate: kind-blind (the regression guard)
# =============================================================================

class TestCandidateSkipPredicate:
    @pytest.mark.parametrize("kind", ["rate_limit", "auth", None])
    def test_counts_every_kind(self, sub_ops, tmp_db, kind):
        _event(tmp_db, "sub-a", failure_kind=kind)
        assert sub_ops.has_recent_subscription_failures("sub-a") is True

    def test_clean_subscription_is_not_flagged(self, sub_ops, tmp_db):
        assert sub_ops.has_recent_subscription_failures("sub-a") is False

    def test_default_window_matches_the_display_predicate(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="auth", minutes_ago=125)
        assert sub_ops.has_recent_subscription_failures("sub-a") is False

    def test_window_is_overridable(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="auth", minutes_ago=125)
        assert sub_ops.has_recent_subscription_failures("sub-a", hours=24) is True

    def test_auto_switch_skips_an_auth_failing_candidate(self, sub_ops, tmp_db):
        """THE regression this split exists for.

        sub-b just had its token rejected; sub-c is clean. Auto-switch must pick
        sub-c. Had candidate selection inherited the narrowed display predicate,
        sub-b would look viable and every agent would be switched onto a
        subscription that cannot authenticate — an outage dressed as a remedy.
        """
        _event(tmp_db, "sub-b", failure_kind="auth")
        chosen = sub_ops.select_best_alternative_subscription("sub-a")
        assert chosen is not None
        assert chosen.id == "sub-c"

    def test_auto_switch_returns_none_when_every_candidate_failed(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-b", failure_kind="auth")
        _event(tmp_db, "sub-c", failure_kind="rate_limit")
        assert sub_ops.select_best_alternative_subscription("sub-a") is None


# =============================================================================
# 3. The two predicates disagree — on purpose
# =============================================================================

def test_the_split_is_observable(sub_ops, tmp_db):
    """A single assertion that would fail if the two were ever re-merged."""
    _event(tmp_db, "sub-a", failure_kind="auth")
    assert sub_ops.is_subscription_rate_limited("sub-a") is False
    assert sub_ops.has_recent_subscription_failures("sub-a") is True


# =============================================================================
# 4. Fleet batch counts carry the kind split
# =============================================================================

class TestBatchCountsByKind:
    def test_shape_matches_the_single_subscription_sibling(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="rate_limit", minutes_ago=60)
        _event(tmp_db, "sub-a", failure_kind="auth", minutes_ago=90)
        _event(tmp_db, "sub-b", failure_kind="auth", minutes_ago=90)
        by_sub = sub_ops.get_failure_event_counts_by_subscription(hours=24)
        assert by_sub["sub-a"] == {"total": 2, "by_kind": {"rate_limit": 1, "auth": 1}}
        assert by_sub["sub-b"] == {"total": 1, "by_kind": {"auth": 1}}
        # A subscription with no events is absent, not zero-filled — the caller
        # already treats a missing key as zero.
        assert "sub-c" not in by_sub

    def test_null_kind_buckets_as_unknown(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind=None, minutes_ago=60)
        by_sub = sub_ops.get_failure_event_counts_by_subscription(hours=24)
        assert by_sub["sub-a"]["by_kind"] == {"unknown": 1}
        assert by_sub["sub-a"]["total"] == 1

    def test_window_is_honored(self, sub_ops, tmp_db):
        _event(tmp_db, "sub-a", failure_kind="auth", minutes_ago=23 * 60)
        _event(tmp_db, "sub-a", failure_kind="auth", minutes_ago=25 * 60)
        by_sub = sub_ops.get_failure_event_counts_by_subscription(hours=24)
        assert by_sub["sub-a"]["total"] == 1
