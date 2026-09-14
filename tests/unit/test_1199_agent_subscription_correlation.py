"""Regression test for #1199 — SQLAlchemy auto-correlation in the agent-count
subquery (introduced by the #1093 SQLAlchemy-Core db rewrite, v0.6.1).

`SubscriptionOperations._agent_count_subquery()` builds a scalar subquery whose
only FROM is `agent_ownership`, correlated to the outer query on
`agent_ownership.subscription_id == subscription_credentials.id`. That is safe
in callers whose outer FROM is just `subscription_credentials JOIN users`
(`get_subscription`, `list_subscriptions`, `list_assignable_subscriptions`,
`list_viable_alternative_subscriptions`). But `get_agent_subscription` *also*
joins `agent_ownership` in its outer query (to filter by `agent_name`), so
SQLAlchemy auto-correlates `agent_ownership` *out* of the subquery — leaving it
with no FROM clause and raising `InvalidRequestError` at statement-compile time.
Because it fails at compile time, `GET /api/ops/auth-report` 500s for every call
regardless of data, on **both** SQLite and PostgreSQL.

The fix aliases `agent_ownership` inside the subquery (`ao_count`) so its FROM
is always distinct from any outer `agent_ownership`; auto-correlation then only
removes `subscription_credentials`, the intended correlation.

Backend-agnostic via ``db_harness`` (#300): runs on SQLite and, when
``TEST_POSTGRES_URL`` is set, PostgreSQL too — mirroring the issue's
"reproduces identically on both backends".
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_BACKEND = _THIS.parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
# #2080: the shadow-eviction loop that used to sit here is GONE. It popped
# `utils` (and the test-helper submodules) from sys.modules to defeat
# `tests/utils` shadowing `src/backend/utils`. That package is now
# `tests/testkit`, so `utils` IS the backend package — and popping it
# evicted the canonical module mid-session, leaving anything that had
# already imported it holding a stale reference (observed as
# `ImportError: module services.subscription_auto_switch not in sys.modules`
# from an importlib.reload several hundred tests later).
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, run as _hrun  # noqa: E402


pytestmark = pytest.mark.unit


# Modules this test mutates in sys.modules — the import-time shadow cleanup
# above and the per-test ``tmp_db`` fixture both pop cached ``database`` /
# ``db.*`` (and shadow ``utils*``) modules so production code re-resolves
# against the test engine. They are snapshotted and restored after each test so
# the mutation can't leak into unrelated test files in the same pytest session
# (#762 lint pattern; precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "database",
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
]


def _is_managed_module(name: str) -> bool:
    return name in _STUBBED_MODULE_NAMES or name == "db" or name.startswith("db.")


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the db/utils sys.modules entries before each test and restore
    them after, so popped modules don't leak across the session."""
    saved = {name: mod for name, mod in sys.modules.items() if _is_managed_module(name)}
    try:
        yield
    finally:
        for name in [n for n in sys.modules if _is_managed_module(n)]:
            if name not in saved:
                sys.modules.pop(name, None)
        for name, mod in saved.items():
            sys.modules[name] = mod


@pytest.fixture
def tmp_db(db_backend):
    """Active backend with a fresh full schema (db_harness, #300). Pops cached
    db modules so production code re-resolves against the test engine, while
    keeping the harness-loaded engine/tables/schema modules importable."""
    for modname in list(sys.modules):
        if modname == "database" or modname.startswith("db."):
            if modname in ("db.engine", "db.tables", "db.schema"):
                continue
            sys.modules.pop(modname, None)
    return db_backend


_NOW = "2026-01-01T00:00:00Z"


def _seed_owner_and_subscription(owner_id: int = 1, sub_id: str = "sub-1") -> None:
    _hrun(
        "INSERT INTO users (id, username, role, email, created_at, updated_at) "
        "VALUES (:id, :u, 'user', :e, :n, :n)",
        id=owner_id, u=f"owner-{owner_id}", e=f"owner-{owner_id}@example.com", n=_NOW,
    )
    _hrun(
        "INSERT INTO subscription_credentials "
        "(id, name, encrypted_credentials, subscription_type, rate_limit_tier, "
        " owner_id, created_at, updated_at) "
        "VALUES (:id, :name, :enc, 'max', 'default', :o, :n, :n)",
        id=sub_id, name=f"max-{sub_id}", enc="{}", o=owner_id, n=_NOW,
    )


def _seed_agent(agent_name: str, owner_id: int = 1,
                sub_id: str | None = None, deleted_at: str | None = None) -> None:
    _hrun(
        "INSERT INTO agent_ownership "
        "(agent_name, owner_id, subscription_id, created_at, deleted_at) "
        "VALUES (:a, :o, :s, :n, :d)",
        a=agent_name, o=owner_id, s=sub_id, n=_NOW, d=deleted_at,
    )


def _ops():
    from db.subscriptions import SubscriptionOperations

    return SubscriptionOperations()


def test_get_agent_subscription_compiles_and_returns_row(tmp_db):
    """The crux of #1199: get_agent_subscription must not raise at compile time
    and must return the agent's subscription with a correct agent_count."""
    _seed_owner_and_subscription(sub_id="sub-1")
    _seed_agent("agent-1", sub_id="sub-1")

    # Pre-fix this raised sqlalchemy.exc.InvalidRequestError
    # ("returned no FROM clauses due to auto-correlation").
    sub = _ops().get_agent_subscription("agent-1")

    assert sub is not None
    assert sub.id == "sub-1"
    assert sub.owner_email is not None  # users join still resolves
    assert sub.agent_count == 1


def test_agent_count_excludes_soft_deleted_agents(tmp_db):
    """The aliased subquery must preserve the deleted_at IS NULL filter — a
    soft-deleted agent on the same subscription must not inflate the count."""
    _seed_owner_and_subscription(sub_id="sub-1")
    _seed_agent("agent-live", sub_id="sub-1")
    _seed_agent("agent-gone", sub_id="sub-1", deleted_at=_NOW)

    sub = _ops().get_agent_subscription("agent-live")

    assert sub is not None
    # Only the live agent counts (the deleted sibling is filtered out).
    assert sub.agent_count == 1


def test_get_agent_subscription_none_when_unassigned(tmp_db):
    """An agent with no subscription returns None (the join yields no row),
    not an error — confirms the fix doesn't change the no-match contract."""
    _seed_owner_and_subscription(sub_id="sub-1")
    _seed_agent("agent-unassigned", sub_id=None)

    assert _ops().get_agent_subscription("agent-unassigned") is None


def test_sibling_callers_still_compile(tmp_db):
    """The shared helper is used by callers without an outer agent_ownership
    join too — make sure the alias didn't regress them. ``list_subscriptions``
    and ``get_subscription`` embed ``_agent_count_subquery()`` with a
    non-agent_ownership outer FROM and don't decrypt the token, so they isolate
    the compile/correlation path cleanly."""
    _seed_owner_and_subscription(sub_id="sub-1")
    _seed_agent("agent-1", sub_id="sub-1")

    ops = _ops()
    subs = ops.list_subscriptions()
    assert len(subs) == 1
    assert subs[0].agent_count == 1

    one = ops.get_subscription("sub-1")
    assert one is not None and one.agent_count == 1
