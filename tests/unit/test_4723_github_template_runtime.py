"""#4723 — a `github:` template's `runtime:` block reaches the agent config.

The `local:` branch of `_resolve_template` copies `runtime.type` /
`runtime.model` onto `config.runtime` / `config.runtime_model`; the `github:`
branch read the same `template.yaml` (as `source_metadata`) but never did, so
the container env (`AGENT_RUNTIME`, `AGENT_RUNTIME_MODEL`) fell back to
`claude-code` and an empty model.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit


def _resolve(monkeypatch, metadata, template="github:owner/repo"):
    pytest.importorskip("docker", reason="backend venv required (crud imports docker)")
    from models import AgentConfig, User
    from services.agent_service import crud

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
        lambda repo, pat=None, ref=None: (metadata, None),
    )
    config = AgentConfig(name="agent-1", agent_type="assistant", template=template)
    user = User(id=1, username="owner", role="creator")
    asyncio.run(crud._resolve_template(config, user))
    return config, crud


@pytest.mark.parametrize("runtime", ["gemini-cli", "codex", "opencode"])
def test_github_template_runtime_type_and_model_reach_config(monkeypatch, runtime):
    config, _ = _resolve(
        monkeypatch, {"runtime": {"type": runtime, "model": "provider/model-x"}}
    )
    assert config.runtime == runtime
    assert config.runtime_model == "provider/model-x"


def test_github_claude_template_model_reaches_config(monkeypatch):
    config, _ = _resolve(monkeypatch, {"runtime": {"model": "claude-opus-5"}})
    assert config.runtime == "claude-code"
    assert config.runtime_model == "claude-opus-5"


def test_github_template_runtime_as_string(monkeypatch):
    config, _ = _resolve(monkeypatch, {"runtime": "gemini-cli"})
    assert config.runtime == "gemini-cli"
    assert config.runtime_model is None


@pytest.mark.parametrize("metadata", [{}, {"name": "x"}, {"runtime": None}])
def test_github_template_without_runtime_keeps_defaults(monkeypatch, metadata):
    config, _ = _resolve(monkeypatch, metadata)
    assert config.runtime == "claude-code"
    assert config.runtime_model is None


def test_github_matches_local_for_same_template(monkeypatch, tmp_path):
    import yaml

    data = {"runtime": {"type": "codex", "model": "gpt-5-codex"}}
    gh_config, crud = _resolve(monkeypatch, data)

    from models import AgentConfig, User

    d = tmp_path / "demo"
    d.mkdir()
    (d / "template.yaml").write_text(yaml.safe_dump(data))
    monkeypatch.setattr(crud, "_resolve_local_template_dir", lambda name: d)
    local = AgentConfig(name="agent-2", agent_type="assistant", template="local:demo")
    asyncio.run(crud._resolve_template(local, User(id=1, username="owner", role="creator")))
    assert (gh_config.runtime, gh_config.runtime_model) == (local.runtime, local.runtime_model)
