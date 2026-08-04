"""
Creation-time materialization of a template's declared schedules — ent#89.

Assertions land on **real DB rows**, not `mock.assert_called_with`. Two
recorded lessons make that non-negotiable here:

  * a mock-`db` suite is structurally blind to a facade gap (2026-07-06), and
  * a parameter threaded through a signature that only one branch actually
    consumes is a severed wire a mock will happily confirm (2026-07-31) —
    which is exactly the failure mode of the `github:` half of AC #2, whose
    whole history is "the declaration was silently ignored".

`_materialize_agent_files` is async, and `tests/unit/pytest.ini` overrides
`pyproject.toml` so `asyncio_mode = "auto"` does NOT apply here — a bare
`async def test_*` would silently never run. These are sync tests calling
`asyncio.run(...)`, the dominant idiom in this directory.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest
import yaml

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


@pytest.fixture
def live_agent(db_backend):
    """A live agent + its owner, so `db.create_schedule` clears all three of its
    silent `return None` gates (user exists, access, #1445 `is_agent_live`)."""
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    return AGENT


def _declared(**overrides) -> dict:
    entry = {
        "name": "daily-briefing",
        "cron": "0 9 * * *",
        "message": "/daily-briefing",
        "enabled": False,
        "timezone": "UTC",
        "description": None,
    }
    entry.update(overrides)
    return entry


def _rows(agent_name: str = AGENT):
    return db.list_agent_schedules(agent_name)


# ---------------------------------------------------------------------------
# The reconcile primitive — real rows
# ---------------------------------------------------------------------------

def test_declared_schedule_becomes_a_real_row(live_agent):
    crud.reconcile_declared_schedules(
        AGENT, [_declared(description="the morning sweep")], OWNER)

    (row,) = _rows()
    assert row.name == "daily-briefing"
    # The naming drift the issue calls out: template `cron` → column
    # `cron_expression`.
    assert row.cron_expression == "0 9 * * *"
    assert row.message == "/daily-briefing"
    assert row.timezone == "UTC"
    assert row.description == "the morning sweep"
    assert row.agent_name == AGENT


def test_enabled_true_is_honored(live_agent):
    """AC #3, honor half (D1)."""
    crud.reconcile_declared_schedules(AGENT, [_declared(enabled=True)], OWNER)
    (row,) = _rows()
    assert row.enabled is True
    # `db.create_schedule` computes next_run_at only for an enabled schedule —
    # honoring is what gives the UI a real "Next run" instead of a blank.
    assert row.next_run_at is not None


def test_unspecified_enabled_lands_disabled(live_agent):
    """AC #3, default half. `ScheduleCreate.enabled` defaults to True, so this
    passes only because the materializer sets it EXPLICITLY."""
    crud.reconcile_declared_schedules(AGENT, [_declared()], OWNER)
    (row,) = _rows()
    assert row.enabled is False
    assert row.next_run_at is None


def test_timeout_seconds_is_left_null(live_agent):
    """D6 — NULL means "inherit the agent's execution_timeout_seconds" (#913),
    which is what puts every materialized schedule inside the §35 agent-cap
    ceiling by construction."""
    crud.reconcile_declared_schedules(AGENT, [_declared()], OWNER)
    (row,) = _rows()
    assert row.timeout_seconds is None


def test_owner_username_is_threaded_through(live_agent):
    """A wrong/blank username makes `db.create_schedule` return None silently —
    the row simply never appears."""
    crud.reconcile_declared_schedules(AGENT, [_declared()], OWNER)
    (row,) = _rows()
    assert row.owner_id == 1


def test_unknown_owner_writes_nothing_and_warns(live_agent, caplog):
    """R6 — `db.create_schedule` RETURNS None here; it does not raise, so a
    try/except alone would catch nothing and a length-derived counter would
    report a schedule that was never written."""
    with caplog.at_level(logging.WARNING):
        crud.reconcile_declared_schedules(AGENT, [_declared()], "nobody")
    assert _rows() == []
    assert any("was not created" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Idempotency (AC #4)
# ---------------------------------------------------------------------------

def test_existing_name_is_skipped(live_agent):
    crud.reconcile_declared_schedules(AGENT, [_declared()], OWNER)
    crud.reconcile_declared_schedules(
        AGENT, [_declared(message="/changed")], OWNER)

    (row,) = _rows()
    assert row.message == "/daily-briefing", "re-run must not create a second row"


def test_duplicate_names_within_one_call_create_one_row(live_agent):
    """R8 — belt to the reader's braces. The reader dedupes, and the
    materializer's seen-set catches it too, since a future caller might feed
    this primitive an un-normalized list."""
    crud.reconcile_declared_schedules(
        AGENT,
        [_declared(message="/first"), _declared(message="/second")],
        OWNER,
    )
    (row,) = _rows()
    assert row.message == "/first"


def test_other_declared_schedules_still_land_alongside_a_skip(live_agent):
    crud.reconcile_declared_schedules(AGENT, [_declared()], OWNER)
    crud.reconcile_declared_schedules(
        AGENT,
        [_declared(), _declared(name="weekly", cron="0 9 * * MON")],
        OWNER,
    )
    assert sorted(r.name for r in _rows()) == ["daily-briefing", "weekly"]


def test_empty_declaration_is_a_no_op(live_agent):
    crud.reconcile_declared_schedules(AGENT, [], OWNER)
    crud.reconcile_declared_schedules(AGENT, None, OWNER)
    assert _rows() == []


# ---------------------------------------------------------------------------
# One bad entry never costs the rest
# ---------------------------------------------------------------------------

def test_a_raising_create_schedule_does_not_stop_the_others(live_agent, monkeypatch):
    real = db.create_schedule
    calls = {"n": 0}

    def flaky(agent_name, username, schedule_data):
        calls["n"] += 1
        if schedule_data.name == "boom":
            raise RuntimeError("db exploded")
        return real(agent_name, username, schedule_data)

    monkeypatch.setattr(db, "create_schedule", flaky)
    crud.reconcile_declared_schedules(
        AGENT,
        [_declared(name="boom"), _declared(name="survivor", cron="0 8 * * *")],
        OWNER,
    )
    assert calls["n"] == 2
    assert [r.name for r in _rows()] == ["survivor"]


# ---------------------------------------------------------------------------
# `_materialize_agent_files` — the non-fatal wiring (R11)
# ---------------------------------------------------------------------------

def _materialize(config, declared, owner=OWNER):
    return asyncio.run(crud._materialize_agent_files(
        config, {}, None, None, None, declared, owner,
    ))


def _config(name: str = AGENT, ephemeral: bool = False) -> AgentConfig:
    kwargs = {"name": name, "agent_type": "assistant"}
    if ephemeral:
        # `AgentConfig.ephemeral` is an EphemeralConfig budget, not a bool —
        # truthiness is what the ghost skip keys on (ent#69).
        kwargs["ephemeral"] = EphemeralConfig(max_executions=1)
    return AgentConfig(**kwargs)


def test_materialize_creates_the_declared_schedules(live_agent, monkeypatch):
    monkeypatch.setattr(crud.git_service, "materialize_persistent_state",
                        _noop_async())
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop_async())

    _materialize(_config(), [_declared()])
    assert [r.name for r in _rows()] == ["daily-briefing"]


def test_ghost_agents_are_skipped(live_agent, monkeypatch):
    """ent#69 fleet hygiene: schedules on an ephemeral agent are a 400, and a
    new caller must exclude ghosts itself."""
    monkeypatch.setattr(crud.git_service, "materialize_persistent_state",
                        _noop_async())
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop_async())

    _materialize(_config(ephemeral=True), [_declared()])
    assert _rows() == []


def test_a_raising_list_agent_schedules_is_not_fatal(live_agent, monkeypatch, caplog):
    """R11 — this function sits inside the destructive rollback fence, so an
    escaping raise would roll back a successful creation over a schedule. The
    try/except must wrap the ENTIRE call, `list_agent_schedules` included."""
    monkeypatch.setattr(crud.git_service, "materialize_persistent_state",
                        _noop_async())
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop_async())
    monkeypatch.setattr(db, "list_agent_schedules", _raise("read failed"))

    with caplog.at_level(logging.WARNING):
        _materialize(_config(), [_declared()])       # must not raise

    assert any("Failed to materialize declared schedules" in r.message
               for r in caplog.records)


def _noop_async():
    async def _f(*args, **kwargs):
        return None
    return _f


def _raise(msg):
    def _f(*args, **kwargs):
        raise RuntimeError(msg)
    return _f


# ---------------------------------------------------------------------------
# The two resolver branches — §0 / R2 regression
#
# Both must populate `tr.declared_schedules`. The `github:` branch NEVER
# populated `template_data` (which is why #383's persistent_state and #1169's
# data_paths are effectively local:-only), so a `template_data`-based reader
# would satisfy AC #2 for `local:` and quietly no-op for the exact half the
# plugin ecosystem cares about.
# ---------------------------------------------------------------------------

_TEMPLATE_YAML = {
    "name": "demo",
    "description": "d",
    "schedules": [
        {"name": "daily-briefing", "cron": "0 9 * * *",
         "message": "/daily-briefing", "enabled": True},
        {"name": "broken", "cron": "@daily", "message": "/nope"},
    ],
}


def _stub_github_resolution(monkeypatch, fetches: list):
    async def _passthrough_fork(config, user, gh, repo, pat, tier, branch):
        return repo, pat, tier, None

    async def _ok(*args, **kwargs):
        return None

    async def _instance(*args, **kwargs):
        return None, None

    monkeypatch.setattr(crud, "_resolve_github_repo_and_pat",
                        lambda *a, **k: (None, "owner/repo", "pat-per-user", "per_user"))
    monkeypatch.setattr(crud, "_apply_fork_to_own", _passthrough_fork)
    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)

    def _fetch(repo, pat=None, ref=None):
        fetches.append({"repo": repo, "pat": pat, "ref": ref})
        return _TEMPLATE_YAML

    monkeypatch.setattr(crud, "fetch_template_metadata_for_create", _fetch)


def test_github_branch_populates_declared_schedules(db_backend, monkeypatch):
    fetches = []
    _stub_github_resolution(monkeypatch, fetches)

    config = AgentConfig(name=AGENT, agent_type="assistant",
                         template="github:owner/repo@feature-x")
    user = User(id=1, username=OWNER, role="creator")
    tr = asyncio.run(crud._resolve_template(config, user))

    assert [s["name"] for s in tr.declared_schedules] == ["daily-briefing"], \
        "the malformed second entry must be dropped, the good one kept"
    assert tr.declared_schedules[0]["enabled"] is True


def test_github_fetch_uses_the_creation_resolved_pat_and_the_parsed_ref(
        db_backend, monkeypatch):
    """R2 — the catalog's own metadata fetch uses the GLOBAL platform PAT with
    no `?ref=`. Reading it here would give a per-user-PAT private-repo creator
    zero schedules with no signal, and would materialize the default branch's
    declarations for an `@branch` create. Without this assertion the §0
    regression test is self-attestation."""
    fetches = []
    _stub_github_resolution(monkeypatch, fetches)

    config = AgentConfig(name=AGENT, agent_type="assistant",
                         template="github:owner/repo@feature-x")
    asyncio.run(crud._resolve_template(
        config, User(id=1, username=OWNER, role="creator")))

    assert fetches == [
        {"repo": "owner/repo", "pat": "pat-per-user", "ref": "feature-x"}]


def test_local_branch_populates_declared_schedules(db_backend, monkeypatch, tmp_path):
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    monkeypatch.setattr(crud, "_resolve_local_template_dir",
                        lambda name: template_dir)

    config = AgentConfig(name=AGENT, agent_type="assistant", template="local:demo")
    tr = asyncio.run(crud._resolve_template(
        config, User(id=1, username=OWNER, role="creator")))

    assert [s["name"] for s in tr.declared_schedules] == ["daily-briefing"]
    # Same normalizer as the github branch — symmetric by construction.
    assert tr.declared_schedules[0]["enabled"] is True


def test_templateless_creation_declares_nothing(db_backend):
    config = AgentConfig(name=AGENT, agent_type="assistant")
    tr = asyncio.run(crud._resolve_template(
        config, User(id=1, username=OWNER, role="creator")))
    assert tr.declared_schedules == []
