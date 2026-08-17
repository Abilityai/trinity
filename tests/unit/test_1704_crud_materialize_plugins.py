"""#1704 — declared `plugins:` flow through the create path.

Mirrors `test_ent89_schedule_materialization.py`: real DB harness, real
`_resolve_template` / `_materialize_agent_files`, `git_service.materialize_*`
mocked. The two properties that a mock-`db` suite would be blind to
(2026-07-31 lesson):

  * `tr.declared_plugins` is populated by BOTH resolver branches (github source
    metadata + local `template_data`) — the `github:` branch never populates
    `template_data`, so a `template_data`-based reader would silently no-op for
    exactly the steered case (`github:Abilityai/cornelius` + abilities);
  * `_materialize_agent_files` calls `git_service.materialize_plugins` with the
    threaded `declared_plugins`, ghost-skips, and is non-fatal.

And the catalog builders surface the normalized `plugins` + `plugin_errors`.
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

from models import AgentConfig, EphemeralConfig, User  # noqa: E402
from services.agent_service import crud  # noqa: E402
from services import template_service  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "owner"
AGENT = "agent-1"

_PLUGINS_BLOCK = {
    "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
    "installed": ["trinity@abilityai"],
}
_NORMALIZED = {
    "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
    "installed": ["trinity@abilityai"],
}

_TEMPLATE_YAML = {
    "name": "demo",
    "description": "d",
    "plugins": _PLUGINS_BLOCK,
}


# ---------------------------------------------------------------------------
# Resolver branches populate tr.declared_plugins
# ---------------------------------------------------------------------------


def _stub_github_resolution(monkeypatch):
    async def _passthrough_fork(
        config,
        user,
        gh,
        repo,
        pat,
        tier,
        branch,
        *,
        source_metadata=None,
        source_metadata_reason=None,
    ):
        return repo, pat, tier, None

    async def _ok(*a, **k):
        return None

    async def _instance(*a, **k):
        return None, None

    monkeypatch.setattr(
        crud,
        "_resolve_github_repo_and_pat",
        lambda *a, **k: (None, "owner/repo", "pat", "per_user"),
    )
    monkeypatch.setattr(crud, "_apply_fork_to_own", _passthrough_fork)
    monkeypatch.setattr(crud, "_validate_github_access", _ok)
    monkeypatch.setattr(crud, "_reserve_git_instance", _instance)
    monkeypatch.setattr(
        crud,
        "fetch_template_metadata_result_for_create",
        lambda repo, pat=None, ref=None: (_TEMPLATE_YAML, None),
    )


def test_github_branch_populates_declared_plugins(db_backend, monkeypatch):
    _stub_github_resolution(monkeypatch)
    config = AgentConfig(
        name=AGENT, agent_type="assistant", template="github:owner/repo@feature-x"
    )
    tr = asyncio.run(
        crud._resolve_template(config, User(id=1, username=OWNER, role="creator"))
    )
    assert tr.declared_plugins == _NORMALIZED


def test_local_branch_populates_declared_plugins(db_backend, monkeypatch, tmp_path):
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    monkeypatch.setattr(crud, "_resolve_local_template_dir", lambda name: template_dir)

    config = AgentConfig(name=AGENT, agent_type="assistant", template="local:demo")
    tr = asyncio.run(
        crud._resolve_template(config, User(id=1, username=OWNER, role="creator"))
    )
    assert tr.declared_plugins == _NORMALIZED


def test_templateless_creation_declares_no_plugins(db_backend):
    config = AgentConfig(name=AGENT, agent_type="assistant")
    tr = asyncio.run(
        crud._resolve_template(config, User(id=1, username=OWNER, role="creator"))
    )
    assert tr.declared_plugins == {}


# ---------------------------------------------------------------------------
# _materialize_agent_files — the non-fatal wiring
# ---------------------------------------------------------------------------


def _noop_async():
    async def _f(*a, **k):
        return None

    return _f


def _recording_async(sink):
    async def _f(name, plugins):
        sink.append((name, plugins))

    return _f


def _raise_async(msg):
    async def _f(*a, **k):
        raise RuntimeError(msg)

    return _f


def _config(name=AGENT, ephemeral=False):
    kwargs = {"name": name, "agent_type": "assistant"}
    if ephemeral:
        kwargs["ephemeral"] = EphemeralConfig(max_executions=1)
    return AgentConfig(**kwargs)


def _materialize(config, declared_plugins, owner=OWNER, monkeypatch=None):
    monkeypatch.setattr(crud.git_service, "materialize_persistent_state", _noop_async())
    monkeypatch.setattr(crud.git_service, "materialize_data_paths", _noop_async())
    return asyncio.run(
        crud._materialize_agent_files(
            config,
            {},
            None,
            None,
            None,
            None,
            owner,
            declared_plugins,
        )
    )


def test_materialize_calls_materialize_plugins(db_backend, monkeypatch):
    seed_user(1, OWNER, "creator")
    seed_agent(AGENT, owner_id=1)
    sink = []
    monkeypatch.setattr(crud.git_service, "materialize_plugins", _recording_async(sink))
    _materialize(_config(), _NORMALIZED, monkeypatch=monkeypatch)
    assert sink == [(AGENT, _NORMALIZED)]


def test_ghost_agents_skip_plugins(db_backend, monkeypatch):
    sink = []
    monkeypatch.setattr(crud.git_service, "materialize_plugins", _recording_async(sink))
    _materialize(_config(ephemeral=True), _NORMALIZED, monkeypatch=monkeypatch)
    assert sink == []  # ephemeral agent never persists → nothing to protect


def test_empty_declaration_skips_plugins(db_backend, monkeypatch):
    sink = []
    monkeypatch.setattr(crud.git_service, "materialize_plugins", _recording_async(sink))
    _materialize(_config(), {}, monkeypatch=monkeypatch)
    assert sink == []


def test_raising_materialize_plugins_is_not_fatal(db_backend, monkeypatch, caplog):
    """Sits inside the destructive rollback fence — an escaping raise would roll
    back a successful creation over a plugin manifest."""
    monkeypatch.setattr(crud.git_service, "materialize_plugins", _raise_async("boom"))
    with caplog.at_level(logging.WARNING):
        _materialize(_config(), _NORMALIZED, monkeypatch=monkeypatch)  # must not raise
    assert any(
        "Failed to materialize plugins.yaml" in r.message for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Catalog builders surface normalized plugins + errors
# ---------------------------------------------------------------------------


def test_local_builder_surfaces_plugins(tmp_path):
    template_dir = tmp_path / "demo"
    template_dir.mkdir()
    (template_dir / "template.yaml").write_text(yaml.safe_dump(_TEMPLATE_YAML))
    entry = template_service._build_local_template(template_dir, is_bundled=True)
    assert entry["plugins"] == _NORMALIZED
    assert entry["plugin_errors"] == []


def test_github_builder_surfaces_plugins():
    entry = template_service._build_template("owner/repo", _TEMPLATE_YAML)
    assert entry["plugins"] == _NORMALIZED
    assert entry["plugin_errors"] == []


def test_builder_surfaces_named_errors_without_raising():
    bad = {
        "name": "demo",
        "plugins": {
            "marketplaces": [
                {"name": "ev", "source": "https://user:tok@github.com/o/r"}
            ]
        },
    }
    entry = template_service._build_template("owner/repo", bad)
    assert entry["plugins"] == {}
    assert entry["plugin_errors"]  # named error, catalog still built
