"""#1142 — operator_queue retention sweep (real-engine).

`prune_terminal_items` hard-deletes settled operator-queue rows past their
retention window: acknowledged/cancelled/expired past `retention_days`, and
`responded` only past the more generous `responded_retention_days` floor.
`pending` rows and young rows are never deleted; a disabled window prunes
nothing; per-cycle cap is honored. Backend-agnostic via `db_harness`.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import (  # noqa: E402
    db_backend,
    run as _hrun,
    count as _hcount,
)

pytestmark = pytest.mark.unit


def _days_ago_iso(n: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert(item_id: str, status: str, created_at: str, agent: str = "a1") -> None:
    _hrun(
        "INSERT INTO operator_queue "
        "(id, agent_name, type, status, priority, title, question, created_at) "
        "VALUES (:id, :ag, 'question', :st, 'medium', 't', 'q', :ca)",
        id=item_id, ag=agent, st=status, ca=created_at,
    )


def _ops():
    from db.operator_queue import OperatorQueueOperations
    return OperatorQueueOperations()


@pytest.fixture
def seeded(db_backend):
    # Terminal statuses, old + recent; responded old/mid/recent; pending old.
    _insert("ack-old", "acknowledged", _days_ago_iso(120))
    _insert("ack-new", "acknowledged", _days_ago_iso(3))
    _insert("cancel-old", "cancelled", _days_ago_iso(120))
    _insert("expire-old", "expired", _days_ago_iso(120))
    _insert("resp-veryold", "responded", _days_ago_iso(120))
    _insert("resp-mid", "responded", _days_ago_iso(40))   # past terminal(7) but < responded floor(30)? 40>30 → deleted
    _insert("resp-recent", "responded", _days_ago_iso(10))
    _insert("pending-old", "pending", _days_ago_iso(120))
    return db_backend


def _remaining() -> set:
    from db.engine import get_engine
    from sqlalchemy import text
    with get_engine().connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT id FROM operator_queue")).all()}


def test_terminal_purged_responded_protected(seeded):
    # retention_days=7 (terminal), responded floor=30.
    deleted = _ops().prune_terminal_items(retention_days=7, responded_retention_days=30)
    remaining = _remaining()

    # Old terminal rows gone.
    assert "ack-old" not in remaining
    assert "cancel-old" not in remaining
    assert "expire-old" not in remaining
    # Recent terminal kept (younger than 7d).
    assert "ack-new" in remaining
    # responded: past the 30d floor gone, within it kept.
    assert "resp-veryold" not in remaining   # 120d
    assert "resp-mid" not in remaining        # 40d > 30d floor
    assert "resp-recent" in remaining         # 10d < 30d floor
    # pending is NEVER deleted, regardless of age.
    assert "pending-old" in remaining
    assert deleted == 5


def test_disabled_window_prunes_nothing(seeded):
    before = _hcount("operator_queue")
    assert _ops().prune_terminal_items(retention_days=0, responded_retention_days=30) == 0
    assert _hcount("operator_queue") == before


def test_responded_floor_never_shorter_than_terminal(db_backend):
    # Even if responded_retention_days is set smaller, terminal window is the floor.
    _insert("r", "responded", _days_ago_iso(60))
    # terminal=90, responded arg=1 → effective responded window = max(1,90)=90 → 60<90 kept.
    assert _ops().prune_terminal_items(retention_days=90, responded_retention_days=1) == 0
    assert "r" in _remaining()


def test_per_cycle_cap_is_honored(db_backend):
    for i in range(10):
        _insert(f"old-{i}", "acknowledged", _days_ago_iso(120))
    deleted = _ops().prune_terminal_items(retention_days=7, responded_retention_days=30, limit=4)
    assert deleted == 4
    assert _hcount("operator_queue") == 6
