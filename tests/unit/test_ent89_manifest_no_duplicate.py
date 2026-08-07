"""
Manifest deploy must not duplicate a schedule the template already
materialized — ent#89 R5.

`deploy_manifest` creates each agent (`create_agent_internal`) and THEN calls
`create_schedules`. Post-ent#89 that makes it the **second** schedule producer
for the same agent: the first call materializes the *template's* declared
block, the second adds the *manifest's*. There is no
`UNIQUE(agent_name, name)` index on `agent_schedules` — and adding one is a
dual-track schema change that would fail on installs already holding
duplicate-named rows — so idempotency has to be an explicit read-then-skip in
whichever caller runs second.

This is a regression **ent#89 itself creates**, which is why the guard and its
test ship with the feature rather than as a follow-up.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import count, db_backend, seed_agent, seed_user  # noqa: E402,F401

pytest.importorskip("docker", reason="backend venv required (system_service chain)")

from database import db  # noqa: E402
from models import SystemAgentConfig  # noqa: E402
from services import system_service  # noqa: E402
from services.agent_service import crud  # noqa: E402


OWNER = "owner"
AGENT = "recon-1"


@pytest.fixture
def live_agent(db_backend):
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    return AGENT


def _manifest_schedule(name: str = "daily-briefing", **overrides) -> dict:
    entry = {"name": name, "cron": "0 9 * * *", "message": "/daily-briefing"}
    entry.update(overrides)
    return entry


def _deploy_schedules(schedules: list) -> int:
    return system_service.create_schedules(
        agent_names={"recon": AGENT},
        agents_config={"recon": SystemAgentConfig(
            template="local:recon", schedules=schedules)},
        owner_username=OWNER,
    )


def test_manifest_does_not_duplicate_a_template_materialized_schedule(live_agent):
    """The end-to-end R5 case, in creation order: template first, manifest
    second, one row."""
    crud.reconcile_declared_schedules(
        AGENT,
        [{"name": "daily-briefing", "cron": "0 9 * * *",
          "message": "/daily-briefing", "enabled": False,
          "timezone": "UTC", "description": None}],
        OWNER,
    )
    assert count("agent_schedules") == 1

    created = _deploy_schedules([_manifest_schedule()])

    assert created == 0
    assert count("agent_schedules") == 1
    (row,) = db.list_agent_schedules(AGENT)
    # The template's row survives untouched — the guard SKIPS, it does not
    # overwrite. (`create_schedules` defaults `enabled=True`, so a row written
    # by the manifest would be enabled; the template's is not.)
    assert row.enabled is False


def test_manifest_only_schedules_still_land(live_agent):
    """The guard must not cost a manifest its own schedules."""
    created = _deploy_schedules([
        _manifest_schedule(),
        _manifest_schedule("weekly-report", cron="0 9 * * MON"),
    ])
    assert created == 2
    assert sorted(r.name for r in db.list_agent_schedules(AGENT)) == [
        "daily-briefing", "weekly-report"]


def test_manifest_deduplicates_within_its_own_block(live_agent):
    """The seen-set is updated as rows are created, so a manifest declaring the
    same name twice yields one row too."""
    created = _deploy_schedules([
        _manifest_schedule(message="/first"),
        _manifest_schedule(message="/second"),
    ])
    assert created == 1
    (row,) = db.list_agent_schedules(AGENT)
    assert row.message == "/first"


def test_redeploying_the_same_manifest_is_idempotent(live_agent):
    assert _deploy_schedules([_manifest_schedule()]) == 1
    assert _deploy_schedules([_manifest_schedule()]) == 0
    assert count("agent_schedules") == 1


def test_a_failing_existing_read_fails_open(live_agent, monkeypatch):
    """Dropping a manifest's schedules would be worse than the duplicate the
    guard prevents, so an unreadable existing-set creates unfiltered."""
    def _boom(agent_name):
        raise RuntimeError("read failed")

    monkeypatch.setattr(db, "list_agent_schedules", _boom)
    assert _deploy_schedules([_manifest_schedule()]) == 1
