"""Creation-time reconcile of a template's declared metrics — ent#477 (R3).

A mirror of `test_ent89_schedule_materialization.py`, for the same reasons it
was written that way: assertions land on **real DB rows**, because a mock-`db`
suite is blind to a facade gap (2026-07-06), and because a parameter threaded
through a signature that only one branch consumes is a severed wire a mock will
happily confirm (2026-07-31).

The severed-wire risk is concrete here. `declared_metrics` has to be populated
by **all three** resolver branches — `github:`, `local:` and the copy-intent
snapshot import — and the `github:` branch has never populated `template_data`
at all, which is exactly why #383's `persistent_state` and #1169's `data_paths`
ended up silently `local:`-only. A reader hung off `template_data` would satisfy
this issue for `local:` and quietly no-op for the half the ecosystem uses.

`_materialize_agent_files` is async and `tests/unit/pytest.ini` does NOT enable
`asyncio_mode = auto`, so these are sync tests calling `asyncio.run(...)` — the
dominant idiom in this directory. A bare `async def test_` would never run.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (crud imports docker)")

from database import db  # noqa: E402
from models import AgentConfig, EphemeralConfig, User  # noqa: E402
from services.agent_service import crud  # noqa: E402

OWNER = "owner"
AGENT = "agent-1"

_METRICS_BLOCK = [
    {"name": "research_cycles", "type": "counter", "label": "Cycles",
     "cadence": "1h"},
    {"name": "mood", "type": "status",
     "values": [{"value": "ok", "color": "green"}]},
    {"name": "BROKEN", "type": "counter"},
]

_TEMPLATE_YAML = {
    "name": "demo",
    "description": "d",
    "metrics": _METRICS_BLOCK,
}


@pytest.fixture
def live_agent(db_backend):
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    return AGENT


def _rows(agent_name: str = AGENT):
    return {r["name"]: r for r in db.list_metric_definitions(
        agent_name, include_retired=True)}


def _noop_async():
    async def _f(*args, **kwargs):
        return None
    return _f


def _config(name: str = AGENT, ephemeral: bool = False) -> AgentConfig:
    kwargs = {"name": name, "agent_type": "assistant"}
    if ephemeral:
        # `AgentConfig.ephemeral` is an EphemeralConfig budget, not a bool —
        # truthiness is what the ghost skip keys on (ent#69).
        kwargs["ephemeral"] = EphemeralConfig(max_executions=1)
    return AgentConfig(**kwargs)


def _materialize(config, declared_metrics, monkeypatch):
    monkeypatch.setattr(crud.git_service, "materialize_persistent_state",
                        _noop_async())
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop_async())
    return asyncio.run(crud._materialize_agent_files(
        config, {}, None, None, None, None, OWNER, None, declared_metrics,
    ))


# ---------------------------------------------------------------------------
# The materialize hook writes real rows
# ---------------------------------------------------------------------------

def test_creation_writes_the_declared_metrics(live_agent, monkeypatch):
    declared = crud.metric_registry.declared_metrics_from_template(_TEMPLATE_YAML)
    _materialize(_config(), declared, monkeypatch)

    rows = _rows()
    assert sorted(rows) == ["mood", "research_cycles"], \
        "the malformed third entry must be dropped, the good ones kept"
    assert rows["research_cycles"]["type"] == "counter"
    assert rows["research_cycles"]["cadence_seconds"] == 3600
    assert rows["mood"]["values"] == [
        {"value": "ok", "color": "green", "label": None}]
    assert rows["research_cycles"]["source"] == "create"


def test_an_agent_with_no_metrics_block_gets_no_rows(live_agent, monkeypatch):
    _materialize(_config(), [], monkeypatch)
    assert _rows() == {}


def test_ghost_agents_are_skipped(live_agent, monkeypatch):
    """ent#69 fleet hygiene: an ephemeral agent is deleted whole, so its
    definitions would exist only to cascade."""
    declared = crud.metric_registry.declared_metrics_from_template(_TEMPLATE_YAML)
    _materialize(_config(ephemeral=True), declared, monkeypatch)
    assert _rows() == {}


def test_a_raising_reconcile_never_costs_a_creation(live_agent, monkeypatch, caplog):
    """This block sits inside the destructive rollback fence — a raise that
    escapes here rolls back a successful creation over a metric declaration."""
    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(crud.metric_registry, "reconcile_declared_metrics", _boom)
    declared = crud.metric_registry.declared_metrics_from_template(_TEMPLATE_YAML)

    with caplog.at_level(logging.WARNING):
        _materialize(_config(), declared, monkeypatch)   # must not raise

    assert any("Failed to reconcile declared metrics" in r.message
               for r in caplog.records)


def test_the_hook_is_wired_into_the_creation_call(live_agent):
    """Mutation evidence: the parameter exists AND the orchestrator passes the
    resolver's value into it. A default-only parameter is a severed wire."""
    import inspect

    sig = inspect.signature(crud._materialize_agent_files)
    assert "declared_metrics" in sig.parameters

    source = inspect.getsource(crud)
    assert "tr.declared_metrics,\n" in source, \
        "create_agent_logic must pass tr.declared_metrics into the materializer"


# ---------------------------------------------------------------------------
# All three resolver branches populate `tr.declared_metrics`
# ---------------------------------------------------------------------------

def _stub_github_resolution(monkeypatch):
    async def _passthrough_fork(config, user, gh, repo, pat, tier, branch,
                                *, source_metadata=None,
                                source_metadata_reason=None):
        return repo, pat, tier, None

    async def _ok(*args, **kwargs):
        return None

    async def _instance(*args, **kwargs):
        return None, None

    monkeypatch.setattr(
        crud, "_resolve_github_repo_and_pat",
        lambda *a, **k: (None, "owner/repo", "pat-per-user", "per_user"))
    monkeypatch.setattr(crud, "_apply_fork_to_own", _passthrough_fork)
    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)
    monkeypatch.setattr(
        crud, "fetch_template_metadata_result_for_create",
        lambda repo, pat=None, ref=None: (_TEMPLATE_YAML, None))


def test_github_branch_populates_declared_metrics(db_backend, monkeypatch):
    _stub_github_resolution(monkeypatch)
    config = AgentConfig(name=AGENT, agent_type="assistant",
                         template="github:owner/repo@feature-x")
    tr = asyncio.run(crud._resolve_template(
        config, User(id=1, username=OWNER, role="creator")))

    assert [m["name"] for m in tr.declared_metrics] == ["research_cycles", "mood"]
    assert tr.declared_metrics[0]["cadence_seconds"] == 3600


def test_local_branch_populates_declared_metrics(db_backend, monkeypatch, tmp_path):
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    monkeypatch.setattr(crud, "_resolve_local_template_dir",
                        lambda name: template_dir)

    config = AgentConfig(name=AGENT, agent_type="assistant", template="local:demo")
    tr = asyncio.run(crud._resolve_template(
        config, User(id=1, username=OWNER, role="creator")))

    assert [m["name"] for m in tr.declared_metrics] == ["research_cycles", "mood"]


def test_snapshot_import_reads_the_staged_tree(db_backend, tmp_path):
    """ent#15: the snapshot's exact content is the truth — no API re-fetch,
    which could see a different ref than the one that was staged."""
    from types import SimpleNamespace

    staging = tmp_path / "staged"
    staging.mkdir()
    (staging / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    snapshot = SimpleNamespace(staging_dir=str(staging), source_repo="o/r")

    declared = crud._declared_metrics_for_snapshot(snapshot)
    assert [m["name"] for m in declared] == ["research_cycles", "mood"]


def test_snapshot_import_without_a_template_is_not_fatal(db_backend, tmp_path):
    from types import SimpleNamespace

    staging = tmp_path / "empty"
    staging.mkdir()
    snapshot = SimpleNamespace(staging_dir=str(staging), source_repo="o/r")
    assert crud._declared_metrics_for_snapshot(snapshot) == []


def test_snapshot_import_with_unreadable_yaml_is_not_fatal(
        db_backend, tmp_path, caplog):
    from types import SimpleNamespace

    staging = tmp_path / "bad"
    staging.mkdir()
    (staging / "template.yaml").write_text("{{{ not yaml")
    snapshot = SimpleNamespace(staging_dir=str(staging), source_repo="o/r")

    with caplog.at_level(logging.WARNING):
        assert crud._declared_metrics_for_snapshot(snapshot) == []
    assert any("could not read template.yaml metrics" in r.message
               for r in caplog.records)
