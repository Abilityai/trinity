"""#4387 — template-declared library skills: `library_skills:` in template.yaml.

A template names skills from the platform skills library; creating an agent
from it assigns them, as the owner, and delivers them once the agent is ready.
Requirements: docs/memory/requirements/skills.md §21.3.4.

Covered, in the shape of `test_1704_crud_materialize_plugins.py`:

  * the total reader (`services/template_skills.py`) — every input shape,
    charset, de-duplication, the cap, `set:` refused by name, never raises;
  * both catalog builders surface `library_skills` + `library_skill_errors`;
  * ALL THREE resolver branches fill `tr.declared_library_skills` (the
    `github:` branch never populates `template_data`, so a reader keyed on it
    would silently no-op for every GitHub template);
  * the §21.3.1 fence at creation — an agent key without `skills.manage` gets
    no assignment, people and the system key do;
  * `_materialize_agent_files` assigns only names the library has, ghost-skips,
    is non-fatal, and spawns delivery only when something was assigned;
  * delivery waits for readiness and reuses the start-path injection.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend, seed_agent, seed_user  # noqa: E402,F401

from services.template_skills import (  # noqa: E402
    MAX_LIBRARY_SKILLS,
    library_skill_errors,
    normalize_declared_library_skills,
)

pytestmark = pytest.mark.unit

OWNER = "owner"
AGENT = "agent-1"
LIBRARY = {"user-research", "deutero-setup", "plan-interview-research"}


# ---------------------------------------------------------------------------
# The reader — total, never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("block", [None, [], "", {}])
def test_absent_or_empty_declares_nothing_and_is_not_an_error(block):
    assert normalize_declared_library_skills(block) == []
    assert library_skill_errors(block) == []


@pytest.mark.parametrize("block", ["user-research", {"a": 1}, 42, True])
def test_non_list_is_a_named_error(block):
    assert normalize_declared_library_skills(block) == []
    errors = library_skill_errors(block)
    assert len(errors) == 1 and "must be a list" in errors[0]


def test_valid_names_kept_in_order_and_deduplicated():
    block = ["user-research", "deutero-setup", "user-research", " deutero-setup "]
    assert normalize_declared_library_skills(block) == ["user-research", "deutero-setup"]
    assert library_skill_errors(block) == []


@pytest.mark.parametrize(
    "bad",
    ["../etc", "has space", "-leading-dash", "a/b", "", 7, None, {"name": "x"}, "x" * 65],
)
def test_invalid_entries_are_dropped_with_a_named_error(bad):
    block = ["user-research", bad]
    assert normalize_declared_library_skills(block) == ["user-research"]
    assert len(library_skill_errors(block)) == 1


def test_skill_sets_are_refused_by_name_not_silently_dropped():
    block = ["set:deutero"]
    assert normalize_declared_library_skills(block) == []
    errors = library_skill_errors(block)
    assert len(errors) == 1 and "set" in errors[0].lower()


def test_cap_bounds_the_declaration():
    block = [f"skill-{i}" for i in range(MAX_LIBRARY_SKILLS + 5)]
    assert len(normalize_declared_library_skills(block)) == MAX_LIBRARY_SKILLS
    assert any("at most" in e for e in library_skill_errors(block))


def test_error_echo_is_bounded():
    errors = library_skill_errors(["x" * 5000])
    assert errors and len(errors[0]) < 300


# ---------------------------------------------------------------------------
# Catalog builders surface the declaration
# ---------------------------------------------------------------------------

_TEMPLATE_YAML = {
    "name": "demo",
    "description": "d",
    "library_skills": ["user-research", "deutero-setup"],
}


def test_local_builder_surfaces_library_skills(tmp_path):
    from services import template_service

    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    entry = template_service._build_local_template(template_dir, is_bundled=True)
    assert entry["library_skills"] == ["user-research", "deutero-setup"]
    assert entry["library_skill_errors"] == []


def test_github_builder_surfaces_library_skills_and_errors_without_raising():
    from services import template_service

    entry = template_service._build_template("owner/repo", _TEMPLATE_YAML)
    assert entry["library_skills"] == ["user-research", "deutero-setup"]

    bad = template_service._build_template("owner/repo", {"name": "d", "library_skills": "nope"})
    assert bad["library_skills"] == []
    assert bad["library_skill_errors"]


# ---------------------------------------------------------------------------
# Resolver branches fill tr.declared_library_skills
# ---------------------------------------------------------------------------


def _crud():
    pytest.importorskip("docker", reason="backend venv required (crud imports docker)")
    from services.agent_service import crud

    return crud


def _user(**kw):
    from models import User

    return User(id=1, username=OWNER, role="creator", **kw)


def test_local_branch_populates_declared_library_skills(db_backend, monkeypatch, tmp_path):
    from models import AgentConfig

    crud = _crud()
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    monkeypatch.setattr(crud, "_resolve_local_template_dir", lambda name: template_dir)

    config = AgentConfig(name=AGENT, agent_type="assistant", template="local:demo")
    tr = asyncio.run(crud._resolve_template(config, _user()))
    assert tr.declared_library_skills == ["user-research", "deutero-setup"]


def test_github_branch_populates_declared_library_skills(db_backend, monkeypatch):
    from models import AgentConfig

    crud = _crud()

    async def _passthrough_fork(config, user, gh, repo, pat, tier, branch, **_):
        return repo, pat, tier, None

    async def _ok(*a, **k):
        return None

    async def _instance(*a, **k):
        return None, None

    monkeypatch.setattr(
        crud, "_resolve_github_repo_and_pat",
        lambda *a, **k: (None, "owner/repo", "pat", "per_user"),
    )
    monkeypatch.setattr(crud, "_apply_fork_to_own", _passthrough_fork)
    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)
    monkeypatch.setattr(
        crud, "fetch_template_metadata_result_for_create",
        lambda repo, pat=None, ref=None: (_TEMPLATE_YAML, None),
    )
    config = AgentConfig(name=AGENT, agent_type="assistant", template="github:owner/repo")
    tr = asyncio.run(crud._resolve_template(config, _user()))
    assert tr.declared_library_skills == ["user-research", "deutero-setup"]


def test_snapshot_reader_reads_the_staged_tree(tmp_path):
    crud = _crud()
    (tmp_path / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    snap = SimpleNamespace(staging_dir=str(tmp_path), source_repo="owner/repo")
    assert crud._declared_library_skills_for_snapshot(snap) == [
        "user-research", "deutero-setup",
    ]
    empty = SimpleNamespace(staging_dir=str(tmp_path / "missing"), source_repo="o/r")
    assert crud._declared_library_skills_for_snapshot(empty) == []


def test_templateless_creation_declares_no_library_skills(db_backend):
    from models import AgentConfig

    crud = _crud()
    tr = asyncio.run(
        crud._resolve_template(AgentConfig(name=AGENT, agent_type="assistant"), _user())
    )
    assert tr.declared_library_skills == []


# ---------------------------------------------------------------------------
# The §21.3.1 fence at creation
# ---------------------------------------------------------------------------


def test_people_and_system_may_assign_declared_skills(db_backend):
    crud = _crud()
    declared = ["user-research"]
    assert crud._permitted_library_skills(_user(mcp_scope=None), declared) == declared
    assert crud._permitted_library_skills(_user(mcp_scope="user"), declared) == declared
    assert crud._permitted_library_skills(_user(mcp_scope="system"), declared) == declared


def test_agent_key_without_skills_manage_assigns_nothing(db_backend, caplog):
    crud = _crud()
    seed_user(1, OWNER, "creator")
    seed_agent("orchestrator", owner_id=1)
    creator = _user(mcp_scope="agent", agent_name="orchestrator")
    with caplog.at_level(logging.WARNING):
        assert crud._permitted_library_skills(creator, ["user-research"]) == []
    assert any("skills.manage" in r.message for r in caplog.records)


def test_agent_key_holding_skills_manage_may_assign(db_backend, monkeypatch):
    crud = _crud()
    from database import db

    monkeypatch.setattr(db, "agent_has_capability", lambda agent, cap: True)
    creator = _user(mcp_scope="agent", agent_name="orchestrator")
    assert crud._permitted_library_skills(creator, ["user-research"]) == ["user-research"]


def test_connector_and_absent_scope_assign_nothing(db_backend):
    crud = _crud()
    assert crud._permitted_library_skills(_user(mcp_scope="connector"), ["user-research"]) == []
    no_scope = SimpleNamespace(username=OWNER)  # no mcp_scope attribute at all
    assert crud._permitted_library_skills(no_scope, ["user-research"]) == []


def test_empty_declaration_short_circuits(db_backend):
    crud = _crud()
    assert crud._permitted_library_skills(_user(mcp_scope="agent", agent_name="x"), []) == []


# ---------------------------------------------------------------------------
# _materialize_agent_files — assignment + delivery wiring
# ---------------------------------------------------------------------------


def _materialize(crud, monkeypatch, config, skills, by_agent=None):
    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(crud.git_service, "materialize_persistent_state", _noop)
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop)
    return asyncio.run(
        crud._materialize_agent_files(
            config, {}, None, None, None, None, OWNER, None, None,
            declared_library_skills=skills,
            library_skills_assigned_by_agent=by_agent,
        )
    )


def _config(ephemeral=False):
    from models import AgentConfig, EphemeralConfig

    kw = {"name": AGENT, "agent_type": "assistant"}
    if ephemeral:
        kw["ephemeral"] = EphemeralConfig(max_executions=1)
    return AgentConfig(**kw)


@pytest.fixture
def library(monkeypatch):
    from services.skill_service import skill_service

    monkeypatch.setattr(
        skill_service, "get_skill",
        lambda name: {"name": name} if name in LIBRARY else None,
    )


@pytest.fixture
def spawned(monkeypatch):
    crud = _crud()
    sink = []
    monkeypatch.setattr(crud, "_spawn_library_skill_delivery", lambda name: sink.append(name))
    return sink


def test_assigns_declared_skills_as_the_owner(db_backend, monkeypatch, library, spawned):
    crud = _crud()
    from database import db

    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    _materialize(crud, monkeypatch, _config(), ["user-research", "deutero-setup"])
    assert sorted(db.get_agent_skill_names(AGENT)) == ["deutero-setup", "user-research"]
    rows = {s.skill_name: s for s in db.get_agent_skills(AGENT)}
    assert rows["user-research"].assigned_by == OWNER
    assert spawned == [AGENT]


def test_records_the_creating_agent_for_attribution(db_backend, monkeypatch, library, spawned):
    crud = _crud()
    from database import db

    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    _materialize(crud, monkeypatch, _config(), ["user-research"], by_agent="orchestrator")
    (row,) = db.get_agent_skills(AGENT)
    assert row.assigned_by_agent == "orchestrator"


def test_names_missing_from_the_library_are_skipped_and_logged(
    db_backend, monkeypatch, library, spawned, caplog
):
    crud = _crud()
    from database import db

    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    with caplog.at_level(logging.WARNING):
        _materialize(crud, monkeypatch, _config(), ["user-research", "not-in-library"])
    assert db.get_agent_skill_names(AGENT) == ["user-research"]
    assert any("not-in-library" in r.message for r in caplog.records)


def test_nothing_in_the_library_assigns_nothing_and_spawns_nothing(
    db_backend, monkeypatch, library, spawned
):
    crud = _crud()
    from database import db

    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    _materialize(crud, monkeypatch, _config(), ["not-in-library"])
    assert db.get_agent_skill_names(AGENT) == []
    assert spawned == []


def test_ghost_agents_skip_library_skills(db_backend, monkeypatch, library, spawned):
    crud = _crud()
    from database import db

    _materialize(crud, monkeypatch, _config(ephemeral=True), ["user-research"])
    assert db.get_agent_skill_names(AGENT) == []
    assert spawned == []


def test_empty_declaration_is_a_no_op(db_backend, monkeypatch, library, spawned):
    crud = _crud()
    _materialize(crud, monkeypatch, _config(), [])
    assert spawned == []


def test_a_raising_library_is_not_fatal(db_backend, monkeypatch, spawned, caplog):
    """Sits inside the destructive rollback fence — a raise must never roll back
    a successful creation over a skill assignment."""
    crud = _crud()
    from services.skill_service import skill_service

    def _boom(name):
        raise RuntimeError("library unreadable")

    monkeypatch.setattr(skill_service, "get_skill", _boom)
    with caplog.at_level(logging.WARNING):
        _materialize(crud, monkeypatch, _config(), ["user-research"])  # must not raise
    assert any("library skills" in r.message.lower() for r in caplog.records)
    assert spawned == []


# ---------------------------------------------------------------------------
# Delivery — readiness-gated, reuses the start-path injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ready, expect_inject", [(True, True), (False, False)])
def test_delivery_waits_for_readiness_then_injects(monkeypatch, ready, expect_inject):
    crud = _crud()
    from services.agent_service import lifecycle

    injected = []

    async def _ready(name, timeout_s=None, **k):
        return ready

    async def _inject(name):
        injected.append(name)
        return {"status": "success"}

    monkeypatch.setattr(lifecycle, "wait_for_agent_ready", _ready)
    monkeypatch.setattr(lifecycle, "inject_assigned_skills", _inject)
    asyncio.run(crud._deliver_library_skills_when_ready(AGENT))
    assert injected == ([AGENT] if expect_inject else [])


def test_delivery_failure_is_swallowed_and_logged(monkeypatch, caplog):
    crud = _crud()
    from services.agent_service import lifecycle

    async def _ready(name, timeout_s=None, **k):
        return True

    async def _inject(name):
        raise RuntimeError("agent server down")

    monkeypatch.setattr(lifecycle, "wait_for_agent_ready", _ready)
    monkeypatch.setattr(lifecycle, "inject_assigned_skills", _inject)
    with caplog.at_level(logging.WARNING):
        asyncio.run(crud._deliver_library_skills_when_ready(AGENT))  # must not raise
    assert any("next start" in r.message for r in caplog.records)


def test_spawn_without_a_running_loop_is_skipped_not_raised():
    crud = _crud()
    crud._spawn_library_skill_delivery(AGENT)  # no loop here: logged, never raised
