"""Fleet health carries each agent's git sync health (trinity-enterprise#707).

`GET /api/monitoring/status` (and MCP `get_fleet_health`, which maps it) gains
a per-agent `sync` block read from `agent_sync_state` through the one shared
reader and the one policy (`services/sync_health_view.py`), fleet totals in
`sync_summary`, and an `issues[]` entry for a red agent. Sync is an annotation:
it never moves `status`, the sort or the health counts. No surface's `reason`
carries the agent-written git error.

Real DB via db_harness; the handler is called directly with the Docker, Redis
and accessibility lookups stubbed on the module the handler resolves them from.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401

pytestmark = pytest.mark.unit

# A marker no reason string could contain by accident: if it reaches the
# payload, the raw git error leaked.
_ERROR_MARKER = "remote: LEAKMARKER-7f3a fatal: unable to access"

_SYNC_KEYS = {
    "binding",
    "auto_sync_enabled",
    "ahead",
    "behind",
    "dirty_files",
    "last_successful_push_at",
    "divergence_age_s",
    "state",
    "reason",
    "recommendation",
    "frozen",
}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@pytest.fixture
def db(db_backend):  # noqa: F811
    from database import db as database

    return database


def _bind(db, name, *, source_mode=0, auto_sync=True, freeze=True):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
        "VALUES (:n, 1, '2026-01-01T00:00:00Z')",
        n=name,
    )
    db.create_git_config(
        agent_name=name,
        github_repo=f"owner/{name}",
        working_branch=f"trinity/{name}/x",
        instance_id=f"inst-{name}",
        source_mode=bool(source_mode),
    )
    db.set_git_auto_sync_enabled(name, auto_sync)
    db.set_freeze_schedules_if_sync_failing(name, freeze)


def _observe(
    db,
    name,
    *,
    hours=None,
    ahead=0,
    behind=0,
    dirty=0,
    status="success",
    failures=0,
    error=None,
):
    now = datetime.now(timezone.utc)
    for _ in range(max(failures, 1)):
        db.upsert_sync_state(
            name,
            last_sync_status=status,
            last_error_summary=error,
            last_sync_at=_iso(now - timedelta(minutes=5)),
            ahead_working=ahead,
            behind_working=behind,
            diverged_since=_iso(now - timedelta(hours=hours)) if hours else None,
            dirty_files=dirty,
            last_successful_push_at="2026-09-26T09:30:00.000000Z",
            last_check_at=_iso(now - timedelta(seconds=30)),
        )


def _status(monkeypatch, db, names, *, role="user", accessible=None, sync_fault=False):
    mon = importlib.import_module("routers.monitoring")
    # importlib returns the sys.modules entry — the object the handler's
    # in-function `from services.X import y` resolves. `import services.X as m`
    # binds the PACKAGE attribute, which another unit module can leave pointing
    # at a different object (reproduced; the patch then silently misses).
    docker_service = importlib.import_module("services.docker_service")
    heartbeat_service = importlib.import_module("services.heartbeat_service")

    # Another unit module may have re-executed routers.monitoring against
    # stubs (test_fleet_status_resilience), so pin every collaborator the
    # handler reads on the module object it resolves them from.
    monkeypatch.setattr(mon, "db", db)
    monkeypatch.setattr(
        mon, "get_monitoring_service", lambda: SimpleNamespace(is_running=True)
    )
    visible = names if accessible is None else accessible
    monkeypatch.setattr(
        mon, "get_accessible_agents", lambda user: [{"name": n} for n in visible]
    )
    monkeypatch.setattr(
        docker_service,
        "list_all_agents_fast",
        lambda: [SimpleNamespace(name=n) for n in names],
    )
    monkeypatch.setattr(heartbeat_service, "heartbeat_status_bulk", lambda n: {})
    if role == "admin":
        agent_client = importlib.import_module("services.agent_client")

        monkeypatch.setattr(agent_client, "get_all_circuit_states", lambda: {})
    if sync_fault:
        view = importlib.import_module("services.sync_health_view")

        def boom(*a, **k):
            raise RuntimeError("sync reader down")

        monkeypatch.setattr(view, "fleet_sync", boom)
    body = asyncio.run(mon.get_fleet_status(current_user=SimpleNamespace(role=role)))
    return body


def _by_name(body):
    return {a.name: a for a in body.agents}


def test_each_agent_carries_the_sync_block(db, monkeypatch):
    _bind(db, "h-red")
    _observe(db, "h-red", hours=26, ahead=7, dirty=12)
    body = _status(monkeypatch, db, ["h-red"])
    sync = _by_name(body)["h-red"].sync
    assert sync is not None
    assert set(sync.model_dump()) == _SYNC_KEYS
    assert sync.binding == "agent"
    assert sync.auto_sync_enabled is True
    assert (sync.ahead, sync.behind, sync.dirty_files) == (7, 0, 12)
    assert sync.last_successful_push_at == "2026-09-26T09:30:00.000000Z"
    assert 26 * 3600 <= sync.divergence_age_s < 26 * 3600 + 60
    assert sync.state == "red"
    assert sync.reason.startswith("diverged 0 behind / 7 ahead for 26h")
    assert sync.frozen is True


def test_an_agent_without_a_git_binding_reports_sync_null(db, monkeypatch):
    _bind(db, "h-git")
    _observe(db, "h-git")
    body = _status(monkeypatch, db, ["h-git", "h-nogit"])
    agents = _by_name(body)
    assert agents["h-nogit"].sync is None
    assert agents["h-git"].sync.state == "green"
    # The JSON the MCP mapper reads says null, not an absent key.
    dumped = json.loads(body.model_dump_json())
    assert {"name": "h-nogit"}.items() <= next(
        a for a in dumped["agents"] if a["name"] == "h-nogit"
    ).items()
    assert next(a for a in dumped["agents"] if a["name"] == "h-nogit")["sync"] is None


def test_a_bound_agent_never_observed_is_unknown(db, monkeypatch):
    _bind(db, "h-new")
    sync = _by_name(_status(monkeypatch, db, ["h-new"]))["h-new"].sync
    assert sync.state == "unknown"
    assert (sync.ahead, sync.behind, sync.dirty_files) == (None, None, None)
    assert sync.frozen is False


def test_fleet_totals(db, monkeypatch):
    _bind(db, "t-red")  # work agent, diverged 26 h, frozen, dirty
    _observe(db, "t-red", hours=26, ahead=7, dirty=3)
    _bind(db, "t-yellow", source_mode=1, auto_sync=False)  # deployment behind
    _observe(db, "t-yellow", hours=26, behind=31)
    _bind(db, "t-green")
    _observe(db, "t-green")
    _bind(db, "t-off", source_mode=1, auto_sync=False)  # in sync, auto-sync off
    _observe(db, "t-off")
    body = _status(
        monkeypatch, db, ["t-red", "t-yellow", "t-green", "t-off", "t-nogit"]
    )
    assert body.sync_summary.model_dump() == {
        "git_bound": 4,
        "diverged": 2,
        "frozen": 1,
        "auto_sync_off": 2,
        "dirty": 1,
        "red": 1,
        "yellow": 1,
    }


def test_a_red_agent_gets_an_issue_with_the_recommendation(db, monkeypatch):
    _bind(db, "i-red", source_mode=0, auto_sync=False)
    _observe(db, "i-red", hours=26, ahead=7)
    _bind(db, "i-yellow", source_mode=1, auto_sync=False)
    _observe(db, "i-yellow", hours=2, behind=4)
    agents = _by_name(_status(monkeypatch, db, ["i-red", "i-yellow"]))
    red_issues = [i for i in agents["i-red"].issues if i.startswith("sync: ")]
    assert red_issues == [
        "sync: diverged 0 behind / 7 ahead for 26h — enable auto-sync"
    ]
    assert not [i for i in agents["i-yellow"].issues if i.startswith("sync: ")]


def test_sync_never_moves_status_or_the_health_counts(db, monkeypatch):
    _bind(db, "s-red")
    _observe(db, "s-red", hours=26, ahead=7)
    body = _status(monkeypatch, db, ["s-red"])
    agent = _by_name(body)["s-red"]
    assert agent.sync.state == "red"
    # No health-check row → the pre-#707 verdict, untouched by the red sync.
    assert agent.status == "unknown"
    assert body.summary.healthy == body.summary.degraded == body.summary.unhealthy == 0


def test_a_non_admin_sees_only_accessible_agents(db, monkeypatch):
    _bind(db, "a-mine")
    _observe(db, "a-mine", hours=26, ahead=7)
    _bind(db, "a-theirs")
    _observe(db, "a-theirs", hours=26, ahead=9)
    body = _status(monkeypatch, db, ["a-mine", "a-theirs"], accessible=["a-mine"])
    assert set(_by_name(body)) == {"a-mine"}
    assert body.sync_summary.git_bound == 1
    assert "a-theirs" not in body.model_dump_json()


def test_no_raw_error_text_anywhere_in_the_payload(db, monkeypatch):
    _bind(db, "e-failed")
    _observe(
        db,
        "e-failed",
        status="failed",
        failures=3,
        error=_ERROR_MARKER,
        hours=26,
        ahead=2,
    )
    body = _status(monkeypatch, db, ["e-failed"])
    payload = body.model_dump_json()
    assert "LEAKMARKER" not in payload
    assert "unable to access" not in payload
    sync = _by_name(body)["e-failed"].sync
    assert sync.state == "red"
    assert sync.reason.startswith("last sync failed (seen on 3 polls)")
    assert sync.recommendation == "push via git_sync strategy=pull_first"


def test_a_sync_reader_fault_degrades_to_null_and_keeps_the_rest(db, monkeypatch):
    _bind(db, "f-red")
    _observe(db, "f-red", hours=26, ahead=7)
    body = _status(monkeypatch, db, ["f-red"], sync_fault=True)
    agent = _by_name(body)["f-red"]
    assert agent.sync is None
    assert body.sync_summary is None
    # Not the "aggregation failed" branch: the health summary is intact.
    assert agent.issues == ["No health check data"]
    assert body.summary.total_agents == 1


def test_the_per_agent_reason_carries_no_raw_error_either(db):
    """The view layer, not each surface, keeps git stderr out of `reason`."""
    from services.sync_health_view import sync_view

    _bind(db, "v-failed")
    _observe(db, "v-failed", status="failed", failures=3, error=_ERROR_MARKER)
    view = sync_view(db.get_sync_state("v-failed"), db.get_git_config("v-failed"))
    assert "LEAKMARKER" not in view["reason"]
    assert "LEAKMARKER" not in (view["freeze_reason"] or "")
    assert view["reason"] == "last sync failed (seen on 3 polls)"
    # The recommendation still reads the error: a denied push is named.
    _bind(db, "v-denied")
    _observe(
        db,
        "v-denied",
        status="failed",
        failures=1,
        error="remote: Write access to repository not granted.",
    )
    denied = sync_view(db.get_sync_state("v-denied"), db.get_git_config("v-denied"))
    assert denied["recommendation"] == "credential is read-only"
    assert "Write access" not in denied["reason"]
