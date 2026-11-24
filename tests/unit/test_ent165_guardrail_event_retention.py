"""The guardrail event retention sweep (trinity-enterprise#165, S1; ruling D10).

Real rows, real setting resolution, a stubbed blast-radius guard. What is pinned:
the literal setting key the sweep guards on (`test_1771a_retention_edges` pairs
every `RETENTION_OPS_KEYS` entry with exactly one `_guard_allows` site), the
90-day default when no row was ever written, that a refusal deletes nothing,
and that `0` disables the sweep.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("sqlalchemy", reason="backend venv required")
fakeredis = pytest.importorskip("fakeredis")

KEY = "guardrail_event_retention_days"


def test_the_window_is_a_registered_retention_setting_defaulting_to_90():
    import config

    assert config.OPS_SETTINGS_DEFAULTS[KEY] == "90"
    assert KEY in config.RETENTION_OPS_KEYS
    assert config.OPS_SETTINGS_VALIDATION[KEY] == ("int", 0, 3650)
    # Not a community-floor key: a 5-day floor would cut the window the
    # trinity#4281 join to an execution row is sized for.
    assert KEY not in config.COMMUNITY_FRESH_INSTALL_SEED
    from services.settings_service import OPS_SETTINGS_DESCRIPTIONS
    assert KEY in OPS_SETTINGS_DESCRIPTIONS


@pytest.fixture
def world(db_backend, monkeypatch):
    from services import guardrail_event_service as svc
    import services.cleanup_service as cleanup

    client = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(svc, "_get_redis", lambda: client)
    guard = {"calls": [], "verdict": True}

    def _guard_allows(key, table, days, count_fn, floor=None):
        guard["calls"].append({"key": key, "table": table, "days": days,
                               "candidates": count_fn(1000)})
        return guard["verdict"]

    acks = []
    monkeypatch.setattr(cleanup, "_guard_allows", _guard_allows)
    monkeypatch.setattr(cleanup, "_after_guarded_prune", acks.append)
    return svc, cleanup, guard, acks


def _seed(svc, ages_days):
    from database import db
    from db.engine import get_engine
    from db.tables import guardrail_events

    svc.ingest("sweep-agent", [
        {"event_id": f"e-{i}", "ts": "2026-11-08T10:00:00Z", "kind": "deny",
         "hook": "bash_guardrail", "code": "destructive_rm"}
        for i in range(len(ages_days))
    ], runtime="claude-code", dropped=0)
    with get_engine().begin() as conn:
        for i, age in enumerate(ages_days):
            stamp = (datetime.now(timezone.utc) - timedelta(days=age)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(guardrail_events.update()
                         .where(guardrail_events.c.event_id == f"e-{i}")
                         .values(received_at=stamp))
    return db


def _ids():
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import guardrail_events

    with get_engine().connect() as conn:
        return sorted(r[0] for r in conn.execute(select(guardrail_events.c.event_id)))


def test_with_no_setting_row_the_sweep_prunes_past_90_days(world):
    svc, cleanup, guard, acks = world
    _seed(svc, [1, 89, 91, 200])
    report = cleanup.CleanupReport()
    cleanup.CleanupService()._sweep_guardrail_events_retention(report)
    assert guard["calls"] == [{"key": KEY, "table": "guardrail_events", "days": 90,
                               "candidates": 2}]
    assert report.guardrail_events_pruned == 2
    assert report.to_dict()["guardrail_events_pruned"] == 2
    assert _ids() == ["e-0", "e-1"]
    assert acks == [KEY]


def test_a_refused_guard_deletes_nothing(world):
    svc, cleanup, guard, acks = world
    _seed(svc, [200, 300])
    guard["verdict"] = False
    report = cleanup.CleanupReport()
    cleanup.CleanupService()._sweep_guardrail_events_retention(report)
    assert _ids() == ["e-0", "e-1"]
    assert report.guardrail_events_pruned == 0
    assert acks == []


def test_zero_disables_the_sweep(world):
    from database import db

    svc, cleanup, guard, acks = world
    _seed(svc, [400])
    db.set_setting(KEY, "0")
    cleanup.CleanupService()._sweep_guardrail_events_retention(cleanup.CleanupReport())
    assert guard["calls"] == []
    assert _ids() == ["e-0"]


def test_the_sweep_runs_in_the_cleanup_cycle():
    """Wired into the cycle, not only defined (a sweep nothing calls is dead)."""
    import ast
    import inspect
    import services.cleanup_service as cleanup

    tree = ast.parse(inspect.getsource(cleanup.CleanupService))
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_sweep_guardrail_events_retention" in called
