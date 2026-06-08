"""
Unit tests for the read-only guard script.

Tests docker/base-image/hooks/read-only-guard.py in isolation, covering:
- Disabled / missing config exits 0 (allow)
- Blocked paths exit 2 (deny)
- Allowed paths override blocked patterns
- MultiEdit edits[] array checked per-file
- Fail-closed on guard errors (via lib.run_hook)

Issue: #887 — Read-only mode hooks don't block Write/Edit tools
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Path to the guard script under test
_GUARD_SCRIPT = Path(__file__).parent.parent.parent / "docker/base-image/hooks/read-only-guard.py"

_DEFAULT_CONFIG = {
    "enabled": True,
    "blocked_patterns": [
        "*.py", "*.js", "*.ts", "CLAUDE.md", ".claude/*",
    ],
    "allowed_patterns": ["content/*", "output/*", "*.log", "*.txt"],
}


def _run_guard(tool_input: dict, config: dict | None, tmp_path: Path) -> subprocess.CompletedProcess:
    """Execute the guard script with a given tool_input JSON and optional config file."""
    if config is not None:
        cfg_path = tmp_path / ".trinity" / "read-only-config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(config))
        home = str(tmp_path)
    else:
        home = str(tmp_path)  # no config file → guard exits 0

    stdin_data = json.dumps({"tool_input": tool_input, "tool_name": "Write"})

    result = subprocess.run(
        [sys.executable, str(_GUARD_SCRIPT)],
        input=stdin_data,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": home},
    )
    return result


# ---------------------------------------------------------------------------
# Disabled / missing config
# ---------------------------------------------------------------------------

class TestGuardDisabledConfig:
    def test_no_config_file_allows(self, tmp_path):
        """Guard exits 0 when no config file exists."""
        result = _run_guard({"file_path": "/home/developer/main.py"}, config=None, tmp_path=tmp_path)
        assert result.returncode == 0

    def test_enabled_false_allows(self, tmp_path):
        """Guard exits 0 when config has enabled: false."""
        cfg = {"enabled": False, "blocked_patterns": ["*.py"], "allowed_patterns": []}
        result = _run_guard({"file_path": "/home/developer/main.py"}, config=cfg, tmp_path=tmp_path)
        assert result.returncode == 0

    def test_missing_enabled_field_allows(self, tmp_path):
        """Config without 'enabled' key treats as disabled (exits 0)."""
        cfg = {"blocked_patterns": ["*.py"], "allowed_patterns": []}
        result = _run_guard({"file_path": "/home/developer/main.py"}, config=cfg, tmp_path=tmp_path)
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Blocked paths
# ---------------------------------------------------------------------------

class TestGuardBlockedPaths:
    def test_blocked_py_file_denied(self, tmp_path):
        """*.py in blocked_patterns is denied."""
        result = _run_guard(
            {"file_path": "/home/developer/agent.py"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2
        assert "read-only mode" in result.stderr

    def test_blocked_js_file_denied(self, tmp_path):
        """*.js in blocked_patterns is denied."""
        result = _run_guard(
            {"file_path": "/home/developer/app.js"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_blocked_claude_md_denied(self, tmp_path):
        """CLAUDE.md in blocked_patterns is denied."""
        result = _run_guard(
            {"file_path": "/home/developer/CLAUDE.md"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_blocked_dot_claude_dir_denied(self, tmp_path):
        """.claude/* in blocked_patterns blocks files inside .claude/."""
        result = _run_guard(
            {"file_path": "/home/developer/.claude/settings.json"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_relative_path_blocked(self, tmp_path):
        """Relative file_path is resolved before pattern matching."""
        result = _run_guard(
            {"file_path": "agent.py"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_notebook_path_blocked(self, tmp_path):
        """notebook_path is checked for NotebookEdit tool."""
        cfg = {**_DEFAULT_CONFIG, "blocked_patterns": ["*.ipynb"]}
        result = _run_guard(
            {"notebook_path": "/home/developer/analysis.ipynb"},
            config=cfg,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Allowed paths override blocked
# ---------------------------------------------------------------------------

class TestGuardAllowedPaths:
    def test_allowed_path_wins_over_blocked(self, tmp_path):
        """allowed_patterns takes precedence over blocked_patterns."""
        result = _run_guard(
            {"file_path": "/home/developer/output/report.txt"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 0

    def test_unblocked_unallowed_path_allowed(self, tmp_path):
        """Paths not matching any blocked pattern are allowed."""
        result = _run_guard(
            {"file_path": "/home/developer/data/notes.md"},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 0

    def test_empty_file_path_allowed(self, tmp_path):
        """Empty file_path exits 0 (no path to check)."""
        result = _run_guard({"file_path": ""}, config=_DEFAULT_CONFIG, tmp_path=tmp_path)
        assert result.returncode == 0

    def test_no_file_path_key_allowed(self, tmp_path):
        """Missing file_path / notebook_path key exits 0."""
        result = _run_guard({}, config=_DEFAULT_CONFIG, tmp_path=tmp_path)
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# MultiEdit edits[] array
# ---------------------------------------------------------------------------

class TestGuardMultiEdit:
    def test_multiedit_all_allowed_passes(self, tmp_path):
        """MultiEdit with all edits in allowed paths exits 0."""
        result = _run_guard(
            {
                "edits": [
                    {"file_path": "/home/developer/output/a.txt"},
                    {"file_path": "/home/developer/output/b.txt"},
                ]
            },
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 0

    def test_multiedit_one_blocked_edit_denied(self, tmp_path):
        """MultiEdit with one blocked file among edits is denied."""
        result = _run_guard(
            {
                "edits": [
                    {"file_path": "/home/developer/output/safe.txt"},
                    {"file_path": "/home/developer/agent.py"},  # blocked
                ]
            },
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_multiedit_all_blocked_denied(self, tmp_path):
        """MultiEdit with all edits blocked is denied."""
        result = _run_guard(
            {
                "edits": [
                    {"file_path": "/home/developer/a.py"},
                    {"file_path": "/home/developer/b.py"},
                ]
            },
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 2

    def test_multiedit_empty_edits_allowed(self, tmp_path):
        """MultiEdit with empty edits list exits 0."""
        result = _run_guard({"edits": []}, config=_DEFAULT_CONFIG, tmp_path=tmp_path)
        assert result.returncode == 0

    def test_multiedit_no_file_path_in_edit_skipped(self, tmp_path):
        """MultiEdit edit dict missing file_path key is skipped (not denied)."""
        result = _run_guard(
            {"edits": [{"old_string": "foo", "new_string": "bar"}]},
            config=_DEFAULT_CONFIG,
            tmp_path=tmp_path,
        )
        assert result.returncode == 0
