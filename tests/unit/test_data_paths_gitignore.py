"""
Unit tests for #1169 — the per-agent data_paths `.gitignore` append.

When an agent declares `data_paths`, `materialize_data_paths` appends the
runtime-data root (`data/`) and each declared path to the agent's OWN
`.gitignore` (never the fleet-wide `_GITIGNORE_PATTERNS`). The append is built
by `_build_gitignore_append_command` and must be:

- correct      — every entry ends up in `.gitignore`
- idempotent   — a second run adds no duplicates (the `grep -qxF` gate)
- non-clobbering — pre-existing user rules survive

These run the REAL bash command against a temp dir (mirroring
tests/unit/test_github_init_gitignore.py) so the idempotence guarantee is
proven on a real filesystem, not just asserted against a mock.

Module under test: src/backend/services/git_service.py
"""
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

_project_root = Path(__file__).resolve().parents[2]
_backend_path = str(_project_root / "src" / "backend")
if _backend_path not in sys.path:
    sys.path.insert(0, _backend_path)


def _load_git_service():
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
        import services.git_service as gs
    return gs


def _run_append(tmp_path: Path, patterns: list[str]) -> str:
    gs = _load_git_service()
    cmd = gs._build_gitignore_append_command(str(tmp_path), patterns)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, (
        f"append command failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
    return (tmp_path / ".gitignore").read_text()


# ---------------------------------------------------------------------------
# Entry derivation
# ---------------------------------------------------------------------------


def test_gitignore_entries_lead_with_data_root_and_dedup():
    """The data/ root leads; declared paths follow; duplicates collapse."""
    gs = _load_git_service()
    entries = gs._data_paths_gitignore_entries(["data/**", "data/db", "data/"])
    assert entries == ["data/", "data/**", "data/db"]


# ---------------------------------------------------------------------------
# Real-bash append behaviour
# ---------------------------------------------------------------------------


def test_append_writes_data_root_and_declared_paths(tmp_path):
    """A fresh agent gets data/ and every declared path in .gitignore."""
    assert not (tmp_path / ".gitignore").exists()

    content = _run_append(tmp_path, ["data/", "data/datasets/**", "data/app.db"])
    lines = content.splitlines()

    for entry in ("data/", "data/datasets/**", "data/app.db"):
        assert entry in lines, f"{entry!r} missing — got:\n{content}"


def test_append_is_idempotent(tmp_path):
    """Running the same append twice must not duplicate any line."""
    patterns = ["data/", "data/app.db"]
    _run_append(tmp_path, patterns)
    content = _run_append(tmp_path, patterns)
    lines = content.splitlines()

    for entry in patterns:
        assert lines.count(entry) == 1, (
            f"{entry!r} duplicated after second run — got:\n{content}"
        )


def test_append_preserves_preexisting_rules(tmp_path):
    """Pre-existing user rules survive the append."""
    (tmp_path / ".gitignore").write_text("# user rules\nnode_modules/\n*.log\n")

    content = _run_append(tmp_path, ["data/"])
    lines = content.splitlines()

    for line in ("# user rules", "node_modules/", "*.log", "data/"):
        assert line in lines, f"{line!r} lost/missing — got:\n{content}"
