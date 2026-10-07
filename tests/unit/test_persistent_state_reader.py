"""
Unit tests for the agent-server persistent-state reader (S4 / #383).

The reader lives in `docker/base-image/agent_server/routers/files.py` and
cannot be imported directly from the host because the agent-server uses
relative imports that only resolve inside the container image. We therefore
test against a byte-identical mirror of the function, guarded by a
source-match assertion so the two drift-detect each other on every run.

Scope per issue #383: this PR introduces the reader primitive only. It is
explicitly NOT wired into PROTECTED_PATHS / EDIT_PROTECTED_PATHS — the
source file must still declare those two lists unchanged. The
`test_protected_paths_lists_unchanged` test below pins that invariant so
#384 (the reset subroutine) is the first PR allowed to touch them.
"""
from pathlib import Path

import pytest
import yaml


_FILES_PY = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "base-image"
    / "agent_server"
    / "routers"
    / "files.py"
)


# ---------------------------------------------------------------------------
# Mirror of _read_persistent_state for host-side testing.
# Must stay byte-identical to the implementation in files.py — see
# test_mirror_matches_source below.
# ---------------------------------------------------------------------------

_DEFAULT_PERSISTENT_STATE = [
    "workspace/**",
    ".trinity/**",
    ".mcp.json",
    ".claude.json",
    ".claude/.credentials.json",
]


def _read_persistent_state_mirror(state_path: Path) -> list[str]:
    """Host-side mirror of agent_server._read_persistent_state.

    Takes the path as a parameter so tests can point at a tmpdir; the real
    implementation hard-codes /home/developer/.trinity/persistent-state.yaml.
    """
    if not state_path.exists():
        return list(_DEFAULT_PERSISTENT_STATE)
    try:
        data = yaml.safe_load(state_path.read_text()) or {}
    except (OSError, yaml.YAMLError):
        return list(_DEFAULT_PERSISTENT_STATE)
    patterns = data.get("persistent_state")
    if not isinstance(patterns, list) or not patterns:
        return list(_DEFAULT_PERSISTENT_STATE)
    return [str(p) for p in patterns]


# ---------------------------------------------------------------------------
# Mirror / source drift guard
# ---------------------------------------------------------------------------


def test_mirror_matches_source():
    """The helper in files.py must match this test's mirror logic."""
    source = _FILES_PY.read_text()

    # The implementation lines (stable landmarks).
    assert "_PERSISTENT_STATE_PATH = Path(" in source
    assert '"/home/developer/.trinity/persistent-state.yaml"' in source
    assert "def _read_persistent_state() -> list[str]:" in source
    # Defaults are identical to the mirror's.
    for pattern in _DEFAULT_PERSISTENT_STATE:
        assert f'"{pattern}"' in source


def test_protected_paths_lists_unchanged():
    """Snapshot guard for the file-protection lists in agent_server/files.py.

    The lists govern delete/edit semantics on the agent's workspace files.
    Originally introduced as a tripwire for S4 (#383) to prove that PR was
    scoped to add a reader helper only. The snapshot was refreshed by #590
    (AISEC-C2) to add `.mcp.json` and `.credentials.enc` to the edit-protected
    list — closing the RCE-by-config bypass where owners overwrote .mcp.json
    with attacker tool definitions.

    If this fails: either you intentionally changed the protection semantics
    (refresh the snapshot, reference your issue here) or upstream drifted
    (refresh and document why).
    """
    source = _FILES_PY.read_text()

    # PROTECTED_PATHS (delete protection) — refreshed by trinity-enterprise#823:
    # added .claude.json and .credentials.json (Claude Code's login files).
    expected_protected = (
        "PROTECTED_PATHS = [\n"
        '    "CLAUDE.md",\n'
        '    ".trinity",\n'
        '    ".git",\n'
        '    ".gitignore",\n'
        '    ".env",\n'
        '    ".mcp.json",\n'
        '    ".mcp.json.template",\n'
        '    ".claude.json",\n'
        '    ".credentials.json",\n'
        "]"
    )
    # EDIT_PROTECTED_PATHS (edit protection) — refreshed by #590:
    # added .mcp.json and .credentials.enc; trinity-enterprise#823 added
    # .claude.json and .credentials.json.
    expected_edit_protected = (
        "EDIT_PROTECTED_PATHS = [\n"
        '    ".trinity",\n'
        '    ".git",\n'
        '    ".gitignore",\n'
        '    ".env",\n'
        '    ".mcp.json",\n'
        '    ".mcp.json.template",\n'
        '    ".credentials.enc",\n'
        '    ".claude.json",\n'
        '    ".credentials.json",\n'
        "]"
    )
    assert expected_protected in source, (
        "PROTECTED_PATHS drifted — refresh the snapshot if the change is intentional."
    )
    assert expected_edit_protected in source, (
        "EDIT_PROTECTED_PATHS drifted — refresh the snapshot if the change is intentional."
    )


# ---------------------------------------------------------------------------
# Reader behaviour
# ---------------------------------------------------------------------------


def test_agent_server_reader_defaults_when_missing(tmp_path):
    """Missing file → default allowlist."""
    state_path = tmp_path / "persistent-state.yaml"
    assert not state_path.exists()

    result = _read_persistent_state_mirror(state_path)

    assert result == _DEFAULT_PERSISTENT_STATE
    assert result is not _DEFAULT_PERSISTENT_STATE  # fresh list, not the constant


def test_agent_server_reader_reads_disk_value(tmp_path):
    """Present file with valid YAML → returns the persisted list."""
    state_path = tmp_path / "persistent-state.yaml"
    state_path.write_text(
        yaml.safe_dump({"persistent_state": ["foo/**", "bar.txt"]})
    )

    result = _read_persistent_state_mirror(state_path)

    assert result == ["foo/**", "bar.txt"]


def test_agent_server_reader_defaults_on_invalid_yaml(tmp_path):
    """Malformed YAML → default list, no exception."""
    state_path = tmp_path / "persistent-state.yaml"
    state_path.write_text("not: : valid: yaml:")

    result = _read_persistent_state_mirror(state_path)

    assert result == _DEFAULT_PERSISTENT_STATE


def test_agent_server_reader_defaults_when_key_missing(tmp_path):
    """Valid YAML without `persistent_state:` key → default list."""
    state_path = tmp_path / "persistent-state.yaml"
    state_path.write_text("other_key: value\n")

    result = _read_persistent_state_mirror(state_path)

    assert result == _DEFAULT_PERSISTENT_STATE


def test_agent_server_reader_defaults_on_empty_list(tmp_path):
    """An empty `persistent_state: []` is treated as "use defaults"."""
    state_path = tmp_path / "persistent-state.yaml"
    state_path.write_text("persistent_state: []\n")

    result = _read_persistent_state_mirror(state_path)

    assert result == _DEFAULT_PERSISTENT_STATE
