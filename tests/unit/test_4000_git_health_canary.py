"""#4000 — canary RD-04 (git measurement blindness) and RD-05 (git maintenance
not keeping up).

A self-pushing agent never ran git maintenance or measured its repo, so
`agent_sync_state.git_dir_bytes / pack_count / loose_objects` stayed NULL and
the `git_bloat` alert could never fire. RD-04 pages when a running git-bound
agent has no fresh measurement; RD-05 pages when the measured backlog is
several times past the agent's own maintenance trigger.

Synthetic-snapshot arms first, then one end-to-end test per invariant driving
the real `collect_snapshot()` over the full schema (learnings #1446: a check
with only synthetic tests is untested where it runs). Every time is an offset
from one literal instant — never a live clock.
"""
from __future__ import annotations

import ast
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from db_harness import db_backend, run as _hrun  # noqa: F401  (fixture)

pytestmark = pytest.mark.unit

T0 = datetime(2026, 10, 27, 12, 0, 0, tzinfo=timezone.utc)
REPO = Path(__file__).resolve().parents[2]
AGENT_GIT_PY = REPO / "docker/base-image/agent_server/routers/git.py"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _ago(**kw) -> str:
    return _iso(T0 - timedelta(**kw))


def _row(**over):
    row = {
        "git_dir_bytes": 1_000_000,
        "pack_count": 3,
        "loose_objects": 100,
        "maintenance_failures": 0,
        "git_stats_at": _ago(minutes=20),
        "last_sync_at": _ago(minutes=20),
    }
    row.update(over)
    return row


def _snap(rows, *, running=None, started=None, ran=True):
    from canary.snapshot import COLLECTOR_GIT_STATS, Snapshot

    names = set(rows) if running is None else set(running)
    if started is None:
        started = {n: _ago(hours=5) for n in names}
    return Snapshot(
        snapshot_time=_iso(T0),
        docker_agent_names=names,
        zombie_container_started_at=started,
        git_stats=rows,
        collectors_ran={COLLECTOR_GIT_STATS} if ran else set(),
    )


def _rd04(snap):
    from canary.invariants import rd04_git_measurement_blind as rd04
    return rd04.check(snap)


def _rd05(snap):
    from canary.invariants import rd05_git_maintenance_behind as rd05
    return rd05.check(snap)


# ---------------------------------------------------------------------------
# RD-04 — fire arms
# ---------------------------------------------------------------------------


def test_rd04_fires_when_stats_were_never_measured():
    nulls = dict(git_dir_bytes=None, pack_count=None, loose_objects=None,
                 git_stats_at=None, last_sync_at=None)
    v = _rd04(_snap({"alpha": _row(**nulls)}))
    assert [x.observed_state["agent_name"] for x in v] == ["alpha"]
    assert v[0].observed_state["reason"] == "never_measured"
    assert v[0].invariant_id == "RD-04"


def test_rd04_fires_when_the_measurement_is_older_than_a_day():
    v = _rd04(_snap({"alpha": _row(git_stats_at=_ago(hours=25))}))
    assert len(v) == 1
    assert v[0].observed_state["reason"] == "stale"
    assert v[0].observed_state["measured_age_seconds"] == 25 * 3600


def test_rd04_stale_git_stats_at_is_not_rescued_by_a_fresh_last_sync_at():
    """git_stats_at is authoritative when present — a sync that keeps landing
    says nothing about whether the repo was measured."""
    v = _rd04(_snap({"alpha": _row(git_stats_at=_ago(hours=30),
                                   last_sync_at=_ago(minutes=5))}))
    assert len(v) == 1


# ---------------------------------------------------------------------------
# RD-04 — pass arms
# ---------------------------------------------------------------------------


def test_rd04_passes_on_a_fresh_measurement():
    assert _rd04(_snap({"alpha": _row()})) == []


def test_rd04_passes_inside_the_startup_grace():
    """First tick after boot = interval + jitter + repack budget; a container
    up 30 minutes with nothing measured yet is not blind."""
    rows = {"alpha": _row(git_dir_bytes=None, pack_count=None, loose_objects=None,
                          git_stats_at=None)}
    assert _rd04(_snap(rows, started={"alpha": _ago(minutes=30)})) == []


def test_rd04_passes_when_container_start_is_unknown():
    rows = {"alpha": _row(git_stats_at=_ago(days=3))}
    assert _rd04(_snap(rows, started={})) == []


def test_rd04_passes_when_the_agent_is_not_running():
    rows = {"alpha": _row(git_stats_at=_ago(days=3))}
    assert _rd04(_snap(rows, running=set())) == []


def test_rd04_old_base_image_falls_back_to_last_sync_at():
    """An agent built before #4000 never writes git_stats_at but still measures
    on its sync cycle. Backend ships first; that must not page the fleet."""
    assert _rd04(_snap({"alpha": _row(git_stats_at=None,
                                      last_sync_at=_ago(hours=2))})) == []


def test_rd04_old_base_image_with_a_stale_last_sync_at_fires():
    """The fallback ages the stats; it does not excuse them."""
    v = _rd04(_snap({"alpha": _row(git_stats_at=None, last_sync_at=_ago(hours=30))}))
    assert [x.observed_state["reason"] for x in v] == ["stale"]


def test_rd04_silent_when_its_collector_did_not_run():
    rows = {"alpha": _row(git_dir_bytes=None, pack_count=None, loose_objects=None,
                          git_stats_at=None)}
    assert _rd04(_snap(rows, ran=False)) == []


# ---------------------------------------------------------------------------
# RD-05
# ---------------------------------------------------------------------------


def test_rd05_fires_on_a_loose_object_backlog():
    v = _rd05(_snap({"alpha": _row(loose_objects=3 * 6700 + 1, maintenance_failures=4)}))
    assert len(v) == 1
    obs = v[0].observed_state
    assert obs["agent_name"] == "alpha"
    assert obs["loose_objects"] == 3 * 6700 + 1
    assert obs["maintenance_failures"] == 4
    assert {"pack_count", "git_dir_bytes"} <= set(obs)
    assert v[0].invariant_id == "RD-05"


def test_rd05_fires_on_a_pack_backlog():
    assert len(_rd05(_snap({"alpha": _row(pack_count=3 * 20 + 1)}))) == 1


def test_rd05_passes_at_the_boundary_and_on_null_stats():
    rows = {
        "edge": _row(loose_objects=3 * 6700, pack_count=3 * 20),
        "unmeasured": _row(loose_objects=None, pack_count=None),
    }
    assert _rd05(_snap(rows)) == []


def test_rd05_skips_an_agent_that_is_not_running():
    """A stopped agent's row is frozen: its numbers are not current."""
    rows = {"stopped": _row(loose_objects=10**6)}
    assert _rd05(_snap(rows, running=set())) == []


def test_rd05_silent_when_its_collector_did_not_run():
    assert _rd05(_snap({"alpha": _row(loose_objects=10**6)}, ran=False)) == []


def test_both_silent_when_the_git_stats_read_failed():
    """`collectors_ran` records ATTEMPTS (#1813 convention): a read that
    raised still ran, and its failure lives in `sources_unavailable`."""
    from canary.snapshot import COLLECTOR_GIT_STATS

    snap = _snap({"alpha": _row(loose_objects=10**6, git_stats_at=None,
                                last_sync_at=None, git_dir_bytes=None,
                                pack_count=None)})
    snap.sources_unavailable.append(f"{COLLECTOR_GIT_STATS}: boom")
    assert _rd04(snap) == []
    assert _rd05(snap) == []


def test_rd05_thresholds_match_the_agent_defaults():
    """The agent repacks at `GIT_MAINTENANCE_{LOOSE,PACK}_THRESHOLD`; RD-05's
    base constants must equal those defaults or "3x the trigger" means nothing.

    The agent module is read with `ast`, not imported: `routers/git.py` pulls in
    FastAPI and the agent server package, which this backend test environment
    does not provide. The defaults are the string literals passed to
    `os.getenv(<name>, <default>)`, found by env-var name so a rename of the
    Python constant does not blind the check.
    """
    from canary.invariants import rd05_git_maintenance_behind as rd05

    defaults = {}
    for node in ast.walk(ast.parse(AGENT_GIT_PY.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "getenv"
                and len(node.args) == 2
                and all(isinstance(a, ast.Constant) for a in node.args)):
            defaults[node.args[0].value] = node.args[1].value
    assert int(defaults["GIT_MAINTENANCE_LOOSE_THRESHOLD"]) == rd05.AGENT_LOOSE_THRESHOLD
    assert int(defaults["GIT_MAINTENANCE_PACK_THRESHOLD"]) == rd05.AGENT_PACK_THRESHOLD


# ---------------------------------------------------------------------------
# End to end — real collect_snapshot() over the full schema
# ---------------------------------------------------------------------------


@pytest.fixture
def e2e(db_backend, monkeypatch):
    """Full schema (db_harness), Docker listing one running `agent-alpha`, Redis
    and the `database` facade absent (their collectors record themselves
    unavailable; RD-04/RD-05 do not read them), snapshot time pinned to T0."""
    started = {"value": _ago(hours=5)}

    class _Container:
        name = "agent-alpha"

        @property
        def attrs(self):
            return {"State": {"StartedAt": started["value"]}, "Config": {"Env": []}}

        def exec_run(self, cmd):
            return types.SimpleNamespace(exit_code=0, output=b"")

    docker = types.ModuleType("services.docker_service")
    docker.docker_client = types.SimpleNamespace(
        containers=types.SimpleNamespace(list=lambda filters=None: [_Container()]))
    monkeypatch.setitem(sys.modules, "services.docker_service", docker)
    slot = types.ModuleType("services.slot_service")

    def _no_redis():
        raise RuntimeError("redis not in this test")
    slot.get_slot_service = _no_redis
    monkeypatch.setitem(sys.modules, "services.slot_service", slot)
    monkeypatch.setitem(sys.modules, "database", types.ModuleType("database"))

    import canary.snapshot as snapshot_mod
    monkeypatch.setattr(snapshot_mod, "utc_now_iso", lambda: _iso(T0))

    _hrun("INSERT INTO agent_ownership (agent_name, owner_id, created_at) "
          "VALUES ('alpha', 1, '2026-01-01T00:00:00Z')")
    _hrun("INSERT INTO agent_git_config (id, agent_name, github_repo, working_branch, "
          "instance_id, created_at, sync_enabled, auto_sync_enabled) VALUES "
          "('g1', 'alpha', 'org/repo', 'trinity/alpha/x', 'x', '2026-01-01T00:00:00Z', 1, 0)")

    def _state(**cols):
        cols.setdefault("updated_at", _iso(T0))
        names = ", ".join(cols)
        binds = ", ".join(f":{c}" for c in cols)
        _hrun(f"INSERT INTO agent_sync_state (agent_name, {names}) VALUES ('alpha', {binds})",
              **cols)

    return types.SimpleNamespace(snapshot=snapshot_mod, state=_state, started=started)


def _run(e2e, inv_id):
    from canary.invariants import run_invariants
    snap = e2e.snapshot.collect_snapshot()
    return snap, run_invariants(snap, ids=[inv_id])[inv_id]


def test_e2e_rd04_fires_for_a_running_agent_with_no_git_stats(e2e):
    e2e.state(last_sync_status="never")
    snap, v = _run(e2e, "RD-04")
    assert e2e.snapshot.COLLECTOR_GIT_STATS in snap.collectors_ran
    assert [x.observed_state["agent_name"] for x in v] == ["alpha"]


def test_e2e_rd04_passes_once_git_stats_at_is_fresh(e2e):
    e2e.state(pack_count=2, loose_objects=10, git_dir_bytes=1024,
              git_stats_at=_ago(minutes=10))
    _snap_, v = _run(e2e, "RD-04")
    assert v == []


def test_e2e_rd05_fires_on_a_stored_loose_backlog(e2e):
    e2e.state(pack_count=2, loose_objects=49_289, git_dir_bytes=10**9,
              maintenance_failures=3, git_stats_at=_ago(minutes=10))
    _snap_, v = _run(e2e, "RD-05")
    assert len(v) == 1
    assert v[0].observed_state["loose_objects"] == 49_289


def test_e2e_collector_failure_is_recorded_and_silences_both(e2e, monkeypatch):
    e2e.state(pack_count=2, loose_objects=49_289)

    def _boom():
        raise RuntimeError("engine down")
    monkeypatch.setattr(e2e.snapshot, "_collect_git_stats", _boom)
    from canary.invariants import run_invariants
    snap = e2e.snapshot.collect_snapshot()
    # Attempted (#1813 convention) but failed: ran, not succeeded.
    assert e2e.snapshot.COLLECTOR_GIT_STATS in snap.collectors_ran
    assert not snap.collector_succeeded(e2e.snapshot.COLLECTOR_GIT_STATS)
    assert any(s.startswith(e2e.snapshot.COLLECTOR_GIT_STATS) for s in snap.sources_unavailable)
    assert run_invariants(snap, ids=["RD-04", "RD-05"]) == {"RD-04": [], "RD-05": []}
