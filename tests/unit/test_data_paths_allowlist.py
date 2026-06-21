"""
Unit tests for #1169 — declared `data_paths` over the existing home volume.

Verifies the data-paths declaration primitives in services/git_service.py,
which are built on the shared `materialize_trinity_yaml_list` /
`_read_trinity_yaml_list` helpers extracted from the S4 persistent-state
functions (#383):

- `DEFAULT_DATA_PATHS` — empty list (opt-in; undeclared agents get nothing).
- `materialize_data_paths(agent_name, paths)` — writes
  `.trinity/data-paths.yaml` (key `data_paths`) AND appends the data root +
  declared paths to the agent's own `.gitignore`. A falsy/empty list is a
  no-op (no exec calls at all).
- `_data_paths_for(agent_name)` — reads the on-disk YAML back, falling back to
  an empty list when the file is missing, empty, or malformed.
- The shared `materialize_trinity_yaml_list` / `_read_trinity_yaml_list`
  helpers, parameterized by path/key/heredoc/default, round-trip cleanly.

These tests mock `execute_command_in_container` so they run without Docker,
a database, or a backend process. They mirror the stubbing in
tests/unit/test_persistent_state_allowlist.py so the two stay in sync.

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


# Backend deps git_service imports at module load — stubbed in _load_git_service
# and restored after each test by _restore_sys_modules so other test files get
# clean imports (the mock-built git_service must not leak either). This is the
# named snapshot/restore exception recognized by tests/lint_sys_modules.py
# (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "docker", "docker.errors", "docker.types",
    "redis", "redis.asyncio",
    "database",
    "services.docker_service",
    "services.git_service",
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot sys.modules before each test and restore after."""
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
    """Import git_service with heavy dependencies stubbed out.

    Installs lightweight stubs for the backend deps git_service imports at
    module load, evicts any cached copy, and re-imports against the stubs. The
    autouse `_restore_sys_modules` fixture restores sys.modules after each test.
    Mirrors tests/unit/test_persistent_state_allowlist.py on which deps it stubs.
    """
    for mod in [
        "docker", "docker.errors", "docker.types",
        "redis", "redis.asyncio",
        "database",
        "services.docker_service",
    ]:
        sys.modules[mod] = Mock()

    sys.modules["database"].db = Mock()
    sys.modules["database"].AgentGitConfig = Mock
    sys.modules["database"].GitSyncResult = Mock

    sys.modules.pop("services.git_service", None)
    import services.git_service as gs
    return gs


class _RecordingExec:
    """Async stand-in for execute_command_in_container."""

    def __init__(self, result=None):
        self.calls: list[tuple[str, str]] = []
        self._result = result or {"exit_code": 0, "output": ""}

    async def __call__(self, container_name: str, command: str, timeout: int = 60):
        self.calls.append((container_name, command))
        return dict(self._result)


# ---------------------------------------------------------------------------
# Default constant
# ---------------------------------------------------------------------------


def test_default_data_paths_is_empty():
    """DEFAULT_DATA_PATHS is opt-in — an empty list."""
    gs = _load_git_service()
    assert gs.DEFAULT_DATA_PATHS == []


# ---------------------------------------------------------------------------
# Shared helper — materialize_trinity_yaml_list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shared_helper_writes_parameterized_yaml():
    """The generic helper honors path/key/heredoc and writes valid YAML."""
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_trinity_yaml_list(
            "agentX",
            path="/home/developer/.trinity/example.yaml",
            key="example_key",
            patterns=["a/**", "b.txt"],
            heredoc="EXAMPLE_EOF",
        )

    assert len(fake.calls) == 1
    container, command = fake.calls[0]
    assert container == "agent-agentX"
    assert "mkdir -p /home/developer/.trinity" in command
    assert "/home/developer/.trinity/example.yaml" in command
    body = command.split("<<'EXAMPLE_EOF'\n", 1)[1].rsplit("EXAMPLE_EOF", 1)[0]
    assert yaml.safe_load(body) == {"example_key": ["a/**", "b.txt"]}


@pytest.mark.asyncio
async def test_shared_reader_round_trips_and_defaults():
    """The generic reader returns the on-disk list, else the provided default."""
    gs = _load_git_service()

    on_disk = yaml.safe_dump({"k": ["x/**"]})
    fake = _RecordingExec({"exit_code": 0, "output": on_disk})
    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._read_trinity_yaml_list(
            "agentY", path="/p.yaml", key="k", default=["fallback"]
        )
    assert result == ["x/**"]

    empty = _RecordingExec({"exit_code": 0, "output": ""})
    with patch.object(gs, "execute_command_in_container", empty):
        result = await gs._read_trinity_yaml_list(
            "agentY", path="/p.yaml", key="k", default=["fallback"]
        )
    assert result == ["fallback"]


# ---------------------------------------------------------------------------
# materialize_data_paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_materialize_data_paths_writes_yaml_and_gitignore():
    """Non-empty declaration writes data-paths.yaml AND appends to .gitignore."""
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_data_paths("agentA", ["data/**", "data/db.sqlite"])

    # Two exec calls: (1) yaml write, (2) gitignore append.
    assert len(fake.calls) == 2, f"expected yaml + gitignore calls, got {fake.calls}"

    _, write_cmd = fake.calls[0]
    assert "/home/developer/.trinity/data-paths.yaml" in write_cmd
    body = write_cmd.split("<<'DATAPATHS_EOF'\n", 1)[1].rsplit("DATAPATHS_EOF", 1)[0]
    assert yaml.safe_load(body) == {"data_paths": ["data/**", "data/db.sqlite"]}

    _, ignore_cmd = fake.calls[1]
    assert ".gitignore" in ignore_cmd
    assert "grep -qxF" in ignore_cmd
    # The data/ root is always ignored, plus every declared path.
    assert "data/" in ignore_cmd
    assert "data/db.sqlite" in ignore_cmd


@pytest.mark.asyncio
async def test_materialize_data_paths_empty_is_noop():
    """An empty (undeclared) list writes nothing and touches no .gitignore."""
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_data_paths("agentB", [])
        await gs.materialize_data_paths("agentB", None)
        await gs.materialize_data_paths("agentB", ["   "])  # whitespace-only

    assert fake.calls == [], f"expected no exec calls for empty declaration, got {fake.calls}"


@pytest.mark.asyncio
async def test_materialize_data_paths_drops_shell_metachar_entries():
    """A template-supplied entry with shell metacharacters is dropped, not
    written — the safe entries still materialize (#1169 L1 hardening).

    The heredoc write runs in the agent's own container (no privilege
    boundary), but an unescaped quote/`$`/backtick would still corrupt the
    `bash -c` tokenization and fail the whole materialization. Dropping the
    unsafe entry keeps the safe declaration intact.
    """
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_data_paths(
            "agentC",
            ['data/ok/**', 'data/bad"; rm -rf /', 'data/db'],
        )

    # Still two calls (yaml + gitignore) — the safe entries survived.
    assert len(fake.calls) == 2, f"expected yaml + gitignore calls, got {fake.calls}"
    _, write_cmd = fake.calls[0]
    body = write_cmd.split("<<'DATAPATHS_EOF'\n", 1)[1].rsplit("DATAPATHS_EOF", 1)[0]
    written = yaml.safe_load(body)["data_paths"]
    assert written == ["data/ok/**", "data/db"]
    assert all('"' not in w and ";" not in w for w in written)
    # The dropped entry never reaches the gitignore append either.
    _, ignore_cmd = fake.calls[1]
    assert "rm -rf" not in ignore_cmd


@pytest.mark.asyncio
async def test_materialize_data_paths_all_unsafe_is_noop():
    """If every declared entry is unsafe, nothing is written (no partial
    materialization, no exec calls)."""
    gs = _load_git_service()
    fake = _RecordingExec()

    with patch.object(gs, "execute_command_in_container", fake):
        await gs.materialize_data_paths(
            "agentD",
            ['data/`whoami`', 'data/$(id)', 'data/"q', 'data/a|b'],
        )

    assert fake.calls == [], f"expected no exec calls when all entries unsafe, got {fake.calls}"


def test_is_safe_data_path_charset():
    """`_is_safe_data_path` accepts plain globs and rejects shell metacharacters."""
    gs = _load_git_service()
    for ok in ("data/**", "data/db.sqlite", "data/sets/*.csv",
               "data/a-b_c/[0-9]?.json", "data/x{1,2}"):
        assert gs._is_safe_data_path(ok), f"should accept {ok!r}"
    for bad in ('data/"q', "data/`x`", "data/$(x)", "data/a;b", "data/a|b",
                "data/a&b", "data/a\\b", "data/a\nb", "data/x>y", "data/x<y",
                "data/x'y", ""):
        assert not gs._is_safe_data_path(bad), f"should reject {bad!r}"


# ---------------------------------------------------------------------------
# _data_paths_for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_data_paths_reader_returns_on_disk_list():
    """Reader returns whatever was persisted to .trinity/data-paths.yaml."""
    gs = _load_git_service()
    on_disk = yaml.safe_dump({"data_paths": ["data/mydb.sqlite", "data/cache/**"]})
    fake = _RecordingExec({"exit_code": 0, "output": on_disk})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._data_paths_for("agentC")

    assert result == ["data/mydb.sqlite", "data/cache/**"]
    _, command = fake.calls[0]
    assert "/home/developer/.trinity/data-paths.yaml" in command


@pytest.mark.asyncio
async def test_data_paths_reader_defaults_empty_when_missing():
    """Missing file → empty list (a fresh copy, not the shared constant)."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": ""})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._data_paths_for("agentD")

    assert result == []
    assert result is not gs.DEFAULT_DATA_PATHS


@pytest.mark.asyncio
async def test_data_paths_reader_defaults_on_invalid_yaml():
    """Malformed YAML → empty list, no exception."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": "not: : valid: yaml:"})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._data_paths_for("agentE")

    assert result == []


@pytest.mark.asyncio
async def test_data_paths_reader_defaults_when_key_missing():
    """Valid YAML without `data_paths:` key → empty list."""
    gs = _load_git_service()
    fake = _RecordingExec({"exit_code": 0, "output": "other_key: value\n"})

    with patch.object(gs, "execute_command_in_container", fake):
        result = await gs._data_paths_for("agentF")

    assert result == []
