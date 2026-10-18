"""The ent#940 db accessors exist on the REAL facade and forward (checkpoint A).

The facade-delegation class (learnings 2026-07-06 / 2026-09-01 / ent#525): a
method added to an `*Operations` class but not to `DatabaseManager` passes
every test that calls the ops class directly and raises AttributeError in
production. So this drives `database.db` — the object the evaluator imports.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest  # noqa: E402

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("name, ops_attr, args, kw", [
    ("sibling_executions", "_schedule_ops", ("p-1", 7), {}),
    ("pair_rework_series", "_schedule_ops", ("a", "b", "review", 3), {}),
    ("collaboration_rows_for_executions", "_activity_ops", (["x"],), {}),
    ("unevaluated_pair_execution_ids", "_evaluation_ops", (), {"limit": 4}),
    ("latest_tier0_for_execution", "_evaluation_ops", ("ex-1",),
     {"evaluator": "platform:tier0:pair"}),
])
def test_the_facade_forwards_every_new_accessor(monkeypatch, name, ops_attr, args, kw):
    from database import db
    seen = {}
    ops = getattr(db, ops_attr)
    monkeypatch.setattr(ops, name, lambda *a, **k: seen.update(a=a, k=k) or "ok")
    assert getattr(db, name)(*args, **kw) == "ok"
    assert seen["a"] == args and seen["k"] == kw


def test_the_facade_reads_land_on_the_real_schema(db_backend):
    """Not only forwarded: each one runs its SQL on both backends."""
    from database import db
    assert db.sibling_executions("nope") == []
    assert db.pair_rework_series("a", "b", "message") == []
    assert db.collaboration_rows_for_executions(["nope"]) == []
    assert db.collaboration_rows_for_executions([]) == []
    assert db.unevaluated_pair_execution_ids() == []
    assert db.latest_tier0_for_execution("nope", evaluator="platform:tier0:pair") is None
