"""Unit tests for #1704 — `git_service.materialize_plugins` + the commit seam.

Covers:
  * `materialize_plugins` writes nested `~/.trinity/plugins.yaml`
    (`{plugins: {marketplaces, installed}}`) via the shared injection-safe
    heredoc writer, with `sort_keys=True` byte-stability;
  * an empty declaration is a full no-op (no container write);
  * `.trinity/plugins.yaml` is in `_TRINITY_AUTHORED_PATHS` (so it rides the
    #2070 `!` re-include AND the rm-cached exemption — both derived from that
    tuple, proven end-to-end in `test_2070_trinity_authored_paths.py`);
  * **#1705 is intact** — `.claude/plugins/` and `.claude.json` STAY gitignored
    and are NOT re-included; the manifest is a plugin-only, secret-free
    declaration, never the cache or the raw Claude manifest.

Heavy backend deps are stubbed the same way `test_data_paths_allowlist.py`
does, so this runs without Docker, a database, or a backend process.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

# #1028: gitignore-owned names read as data by these tests.
from services.git_service import gitignore as gs_gitignore
import yaml

_project_root = Path(__file__).resolve().parents[2]
_backend_path = str(_project_root / "src" / "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    "docker",
    "docker.errors",
    "docker.types",
    "redis",
    "redis.asyncio",
    "database",
    "services.docker_service",
    "services.git_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


def _load_git_service():
    for mod in [
        "docker",
        "docker.errors",
        "docker.types",
        "redis",
        "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        sys.modules[mod] = Mock()
    sys.modules["database"].db = Mock()
    sys.modules["database"].AgentGitConfig = Mock
    sys.modules["database"].GitSyncResult = Mock
    sys.modules.pop("services.git_service", None)
    # #1028: git_service is a package; the alias names the module that
    # owns the functions under test, so patches land where the code looks.
    import services.git_service.trinity_files as gs
    from services.git_service import gitignore as gs_gitignore

    return gs


class _RecordingExec:
    def __init__(self, result=None):
        self.calls: list[tuple[str, str]] = []
        self._result = result or {"exit_code": 0, "output": ""}

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        self.calls.append((container_name, command))
        return dict(self._result)


def _body_of(command: str, heredoc: str = "PLUGINS_EOF") -> str:
    return command.split(f"<<'{heredoc}'\n", 1)[1].rsplit(heredoc, 1)[0]


# ---------------------------------------------------------------------------
# materialize_plugins
# ---------------------------------------------------------------------------


def test_materialize_plugins_writes_nested_yaml():
    gs = _load_git_service()
    fake = _RecordingExec()
    plugins = {
        "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
        "installed": ["trinity@abilityai"],
    }
    with patch.object(gs, "execute_command_in_container", fake):
        asyncio.run(gs.materialize_plugins("agentP", plugins))

    assert len(fake.calls) == 1
    container, command = fake.calls[0]
    assert container == "agent-agentP"
    assert "mkdir -p /home/developer/.trinity" in command
    assert "/home/developer/.trinity/plugins.yaml" in command
    parsed = yaml.safe_load(_body_of(command))
    assert parsed == {
        "plugins": {
            "marketplaces": [{"name": "abilityai", "source": "abilityai/abilities"}],
            "installed": ["trinity@abilityai"],
        }
    }


def test_materialize_plugins_is_byte_stable():
    gs = _load_git_service()
    plugins = {
        "marketplaces": [{"name": "a", "source": "o/a"}],
        "installed": ["p@a"],
    }
    first = _RecordingExec()
    with patch.object(gs, "execute_command_in_container", first):
        asyncio.run(gs.materialize_plugins("x", plugins))
    second = _RecordingExec()
    with patch.object(gs, "execute_command_in_container", second):
        asyncio.run(gs.materialize_plugins("x", dict(plugins)))
    assert first.calls[0][1] == second.calls[0][1]


@pytest.mark.parametrize(
    "plugins",
    [
        None,
        {},
        {"marketplaces": [], "installed": []},
        "not-a-dict",
        {"marketplaces": []},
    ],
)
def test_materialize_plugins_empty_is_no_op(plugins):
    gs = _load_git_service()
    fake = _RecordingExec()
    with patch.object(gs, "execute_command_in_container", fake):
        asyncio.run(gs.materialize_plugins("agentP", plugins))
    assert fake.calls == []  # opt-in: no container write at all


def test_materialize_plugins_does_not_touch_gitignore():
    """Unlike data_paths, the plugin manifest is COMMITTED via
    _TRINITY_AUTHORED_PATHS — it must not append anything to the agent's
    .gitignore (which would ignore the very file we want tracked)."""
    gs = _load_git_service()
    fake = _RecordingExec()
    with patch.object(gs, "execute_command_in_container", fake):
        asyncio.run(
            gs.materialize_plugins(
                "a", {"marketplaces": [{"name": "m", "source": "o/r"}], "installed": []}
            )
        )
    assert len(fake.calls) == 1  # only the yaml write, no gitignore append
    assert "gitignore" not in fake.calls[0][1].lower()


# ---------------------------------------------------------------------------
# Commit seam + #1705 intact
# ---------------------------------------------------------------------------


def test_plugins_yaml_is_an_authored_path():
    gs = _load_git_service()
    assert ".trinity/plugins.yaml" in gs_gitignore._TRINITY_AUTHORED_PATHS


def test_plugins_yaml_is_re_included():
    gs = _load_git_service()
    assert "!.trinity/plugins.yaml" in gs_gitignore._GITIGNORE_PATTERNS


def test_plugins_yaml_is_exempt_from_rm_cached():
    gs = _load_git_service()
    cmd = gs_gitignore._build_rm_cached_ignored_command("/home/developer")
    assert ":!.trinity/plugins.yaml" in cmd


def test_claude_plugins_cache_stays_gitignored_and_not_re_included():
    """#1705 intact — the plugin CACHE must not be committed."""
    gs = _load_git_service()
    assert ".claude/plugins/" in gs_gitignore._GITIGNORE_PATTERNS
    assert "!.claude/plugins/" not in gs_gitignore._GITIGNORE_PATTERNS
    assert ".claude/plugins/" not in gs_gitignore._TRINITY_AUTHORED_PATHS


def test_claude_json_stays_gitignored_and_not_re_included():
    """The raw Claude manifest (session state + secrets) must not be committed."""
    gs = _load_git_service()
    assert ".claude.json" in gs_gitignore._GITIGNORE_PATTERNS
    assert "!.claude.json" not in gs_gitignore._GITIGNORE_PATTERNS
    assert ".claude.json" not in gs_gitignore._TRINITY_AUTHORED_PATHS
