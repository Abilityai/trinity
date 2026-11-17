"""#4151 — a manifest schedule's `model` is never silently dropped.

A manifest schedule may carry `model` (MODEL-001: the model a scheduled run
uses). Before #4151 `_build_schedule_create` ignored it, so `apply_system`
created the row on the agent's default model while the dry run and the result
both reported a plain, successful create; a changed `model` on an existing row
was not compared at all; and the export never wrote it, so a round trip lost it.

Pinned here, over the REAL `db` on a full schema and the real apply engine
(Docker faked at its read seams only, `_ent695_world.py`):

* create: the row is written with the declared model, and the dry run and the
  result both name it (the create-mode `schedules_preview` shape is pinned
  byte-for-byte by the ent#695 golden and is left unchanged here);
* update: a different declared model is a `schedule_update` naming
  `model` (current -> desired) in the dry run and the result, and the real run
  writes it; an entry that does not declare `model` leaves a row's model alone
  (like `timezone` / `description`: only when declared);
* export writes `model` only when set, and export -> dry-run apply is a no-op.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _ent695_world as W  # noqa: E402
from database import db  # noqa: E402
from services import assignment_provider, system_service  # noqa: E402

pytestmark = pytest.mark.unit

SYSTEM = W.SYSTEM
AGENT = f"{SYSTEM}-a"
MODEL = "claude-sonnet-4-5"
OTHER = "claude-opus-4-1"


@pytest.fixture
def world(db_backend, monkeypatch, tmp_path):
    w = W.make_world(monkeypatch, tmp_path)
    W.install_executor_seams(w, monkeypatch)
    assignment_provider.clear_provider()
    yield w
    assignment_provider.clear_provider()


def _manifest(entry: str) -> str:
    return (f"name: {SYSTEM}\nagents:\n  a:\n    template: local:plain\n"
            f"    schedules:\n      - {entry}\n")


def _seed_row(model=None):
    db.create_schedule(AGENT, W.ALICE, system_service._build_schedule_create(
        {"name": "weekly", "cron": "0 9 * * 1", "message": "/w", "model": model}))


def _row():
    (row,) = [s for s in db.list_agent_schedules(AGENT) if s.name == "weekly"]
    return row


def _sched_changes(result):
    (member,) = [m for m in result.members if m.short_name == "a"]
    return [c for c in member.changes if c.field == "schedule:weekly"]


# ----------------------------------------------------------------- the mapping

def test_the_shared_mapping_carries_model():
    sc = system_service._build_schedule_create(
        {"name": "w", "cron": "0 9 * * 1", "message": "/w", "model": MODEL})
    assert sc.model == MODEL


def test_an_entry_without_model_keeps_the_agent_default():
    sc = system_service._build_schedule_create({"name": "w", "cron": "0 9 * * 1", "message": "/w"})
    assert sc.model is None


# ---------------------------------------------------------------------- create

class TestCreate:
    def test_dry_run_names_the_model_and_the_real_run_writes_it(self, world):
        world.seed_agent(AGENT)
        mf = _manifest(f"{{name: weekly, cron: '0 9 * * 1', message: /w, model: {MODEL}}}")

        dry = W.apply(world, mf, dry_run=True)
        (change,) = _sched_changes(dry)
        assert (change.action, change.code) == ("write", "schedule_create")
        assert change.desired["model"] == MODEL

        real = W.apply(world, mf, dry_run=False)
        assert real.status == "applied", real.failed
        (change,) = _sched_changes(real)
        assert change.desired["model"] == MODEL
        assert _row().model == MODEL


# ---------------------------------------------------------------------- update

class TestUpdate:
    def test_a_changed_model_is_an_update_named_in_the_dry_run_and_the_result(self, world):
        world.seed_agent(AGENT)
        _seed_row(model=OTHER)
        mf = _manifest(f"{{name: weekly, cron: '0 9 * * 1', message: /w, model: {MODEL}}}")

        dry = W.apply(world, mf, dry_run=True)
        (change,) = _sched_changes(dry)
        assert (change.action, change.code) == ("write", "schedule_update")
        assert change.current == {"model": OTHER}
        assert change.desired == {"model": MODEL}
        assert _row().model == OTHER  # the dry run wrote nothing

        real = W.apply(world, mf, dry_run=False)
        assert real.status == "applied", real.failed
        (change,) = _sched_changes(real)
        assert change.desired == {"model": MODEL}
        assert _row().model == MODEL

    def test_a_model_on_a_row_with_none_is_an_update(self, world):
        world.seed_agent(AGENT)
        _seed_row(model=None)
        mf = _manifest(f"{{name: weekly, cron: '0 9 * * 1', message: /w, model: {MODEL}}}")
        (change,) = _sched_changes(W.apply(world, mf, dry_run=True))
        assert change.code == "schedule_update" and change.desired == {"model": MODEL}

    def test_an_entry_that_does_not_declare_model_leaves_the_row_alone(self, world):
        world.seed_agent(AGENT)
        _seed_row(model=OTHER)
        mf = _manifest("{name: weekly, cron: '0 9 * * 1', message: /w}")
        dry = W.apply(world, mf, dry_run=True)
        assert [c for c in _sched_changes(dry) if c.action == "write"] == []
        W.apply(world, mf, dry_run=False)
        assert _row().model == OTHER

    def test_the_same_model_is_in_sync(self, world):
        world.seed_agent(AGENT)
        _seed_row(model=MODEL)
        mf = _manifest(f"{{name: weekly, cron: '0 9 * * 1', message: /w, model: {MODEL}}}")
        assert [c for c in _sched_changes(W.apply(world, mf, dry_run=True))
                if c.action == "write"] == []


# ---------------------------------------------------------------------- export

def _export():
    text = system_service.export_manifest(
        SYSTEM, [{"name": AGENT, "status": "running", "template": "local:plain"}],
        viewer=W.owner_viewer())
    return text, yaml.safe_load(text)


class TestExport:
    def test_export_writes_the_model_when_set_and_round_trips(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        world.seed_agent(AGENT)
        _seed_row(model=MODEL)
        text, doc = _export()
        (entry,) = doc["agents"]["a"]["schedules"]
        assert entry["model"] == MODEL

        dry = W.apply(world, text, dry_run=True)
        assert [c for c in _sched_changes(dry) if c.action == "write"] == []

    def test_export_omits_the_model_when_unset(self, world, monkeypatch):
        W.install_seat_reader(monkeypatch)
        world.seed_agent(AGENT)
        _seed_row(model=None)
        _text, doc = _export()
        (entry,) = doc["agents"]["a"]["schedules"]
        assert "model" not in entry
