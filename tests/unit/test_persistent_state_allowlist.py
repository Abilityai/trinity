"""
Unit tests for S4 — Persistent State Allowlist (abilityai/trinity#383).

Verifies the allowlist primitive in services/git_service.py:

- `DEFAULT_PERSISTENT_STATE` — module-level constant with the five default
  patterns (workspace/**, .trinity/**, .mcp.json, .claude.json,
  .claude/.credentials.json).
- `materialize_persistent_state(agent_name, patterns)` — writes
  `.trinity/persistent-state.yaml` inside the agent container at creation
  time via `execute_command_in_container`.
- `_persistent_state_for(agent_name)` — reads the on-disk YAML back,
  falling back to the default list when the file is missing, empty, or
  malformed.

These tests mock `execute_command_in_container` so they can run without
Docker, a database, or a backend process. They also pin the observed
command shape (heredoc + mkdir -p + the exact file path) because the
agent-server reader and the future #384 reset subroutine both depend on
those invariants.

Module under test: src/backend/services/git_service.py
"""
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import yaml

_project_root = Path(__file__).resolve().parents[2]
_backend_path = str(_project_root / "src" / "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


def _load_git_service():
    """Import git_service with heavy dependencies mocked out.

    Mirrors tests/unit/test_github_init_push.py::_load_git_service so the
    two test modules stay in sync on how they stub the backend.
    """
    mock_modules = {}
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        mock_modules[mod] = Mock()

    mock_modules["database"].db = Mock()
    mock_modules["database"].AgentGitConfig = Mock
    mock_modules["database"].GitSyncResult = Mock

    with patch.dict("sys.modules", mock_modules):
        for key in list(sys.modules.keys()):
            if key.startswith("services.git_service"):
                del sys.modules[key]
        # #1028: `services/git_service.py` is a package; the alias names the module
    # that owns the functions under test, so `patch.object(gs, ...)` lands
    # where the code actually looks.
    import services.git_service.trinity_files as gs
    return gs


class _RecordingExec:
    """Async stand-in for execute_command_in_container.

    Records every command it receives and returns a canned result so tests
    can assert both call-shape and control flow.
    """

    def __init__(self, result=None):
        self.calls: list[tuple[str, str]] = []
        self._result = result or {"exit_code": 0, "output": ""}

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        self.calls.append((container_name, command))
        return dict(self._result)


# ---------------------------------------------------------------------------
# Default constant
# ---------------------------------------------------------------------------


def test_default_persistent_state_matches_spec():
    """DEFAULT_PERSISTENT_STATE contains the five patterns from proposal §S4."""
    gs = _load_git_service()
    assert gs.DEFAULT_PERSISTENT_STATE == [
        "workspace/**",
        ".trinity/**",
        ".mcp.json",
        ".claude.json",
        ".claude/.credentials.json",
    ]


# ---------------------------------------------------------------------------
# materialize_persistent_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_default_allowlist_materialized_when_template_missing_key():
    """Creation with no template override writes the five default patterns."""
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_persistent_state("agentA", gs.DEFAULT_PERSISTENT_STATE)

    assert len(fake.calls) == 1, f"expected one exec call, got {fake.calls}"
    container, command = fake.calls[0]
    assert container == "agent-agentA"
    assert "mkdir -p /home/developer/.trinity" in command
    assert "/home/developer/.trinity/persistent-state.yaml" in command

    body = command.split("<<'PSTATE_EOF'\n", 1)[1].rsplit("PSTATE_EOF", 1)[0]
    parsed = yaml.safe_load(body)
    assert parsed == {
        "persistent_state": [
            "workspace/**",
            ".trinity/**",
            ".mcp.json",
            ".claude.json",
            ".claude/.credentials.json",
        ]
    }


@pytest.mark.asyncio
async def test_template_allowlist_overrides_default():
    """Template-provided patterns are written verbatim, defaults suppressed."""
    gs = _load_git_service()
    fake = _RecordingExec()

    custom = ["only/this/**", "custom.yaml"]
    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_persistent_state("agentB", custom)

    _, command = fake.calls[0]
    body = command.split("<<'PSTATE_EOF'\n", 1)[1].rsplit("PSTATE_EOF", 1)[0]
    parsed = yaml.safe_load(body)

    assert parsed == {"persistent_state": ["only/this/**", "custom.yaml"]}
    assert "workspace/**" not in parsed["persistent_state"]
    assert ".trinity/**" not in parsed["persistent_state"]


# ---------------------------------------------------------------------------
# _persistent_state_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_returns_on_disk_list():
    """Reader returns whatever was persisted to .trinity/persistent-state.yaml."""
    gs = _load_git_service()
    on_disk_yaml = yaml.safe_dump({"persistent_state": ["custom/**"]})
    fake = _RecordingExec({"exit_code": 0, "output": on_disk_yaml})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._persistent_state_for("agentC")

    assert result == ["custom/**"]
    _, command = fake.calls[0]
    assert "/home/developer/.trinity/persistent-state.yaml" in command


@pytest.mark.asyncio
async def test_reader_falls_back_to_default_when_file_missing():
    """Empty output (cat || true on missing file) → default list."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": ""})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._persistent_state_for("agentD")

    assert result == gs.DEFAULT_PERSISTENT_STATE
    # Must return a fresh list, not a reference to the module constant —
    # callers mutating the result must not mutate the shared default.
    assert result is not gs.DEFAULT_PERSISTENT_STATE


@pytest.mark.asyncio
async def test_reader_falls_back_when_yaml_invalid():
    """Malformed YAML on disk → default list, no exception."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": "not: : valid: yaml:"})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._persistent_state_for("agentE")

    assert result == gs.DEFAULT_PERSISTENT_STATE


@pytest.mark.asyncio
async def test_reader_falls_back_when_patterns_key_missing():
    """YAML present but missing `persistent_state:` key → default list."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": "other_key: value\n"})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._persistent_state_for("agentF")

    assert result == gs.DEFAULT_PERSISTENT_STATE
