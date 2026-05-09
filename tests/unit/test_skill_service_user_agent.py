"""
Unit tests for #184: skill_service git subprocesses must override
git's default User-Agent so outbound HTTP doesn't fingerprint the
backend stack.

Git's default UA is `git/<version> (libcurl/<version> ...)` which
leaks the runtime versions to whatever endpoint git hits. Even with
the SSRF allowlist (#179) restricting destinations to github.com, the
defense-in-depth principle is to override the UA on every git
subcommand that talks HTTP.

We assert two things:
1. `_git_clone` invocation includes `-c http.useragent=Trinity-Skills-Sync`
   between the `git` argv[0] and the `clone` subcommand (this is the only
   correct position for the `-c` flag).
2. `_git_pull`'s fetch invocation includes the same flag.

The local-only `git rev-parse` and `git reset --hard` calls intentionally
do not need the flag (they don't make HTTP), so we don't assert it there.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_skill_ua.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


# Stub heavy dependencies so we can import skill_service without dragging in
# the full backend (database, agent_client → tenacity, passlib, etc.). The
# helpers under test only use subprocess + module-level constants — none of
# the stubbed imports are exercised at runtime here.
import types as _types  # noqa: E402

for mod_name, attrs in [
    ("database", {"db": MagicMock()}),
    (
        "services.settings_service",
        {
            "get_skills_library_url": lambda: "https://github.com/owner/repo",
            "get_skills_library_branch": lambda: "main",
            "get_github_pat": lambda: None,
            # Additional attrs needed by services.agent_service.helpers/lifecycle
            # when those modules are transitively imported in the same pytest run.
            "get_anthropic_api_key": lambda: "sk-test",
            "get_agent_full_capabilities": lambda: False,
            "get_agent_default_resources": lambda: {"cpu": "2", "memory": "4g"},
            "settings_service": MagicMock(),
        },
    ),
    (
        "services.agent_client",
        {"get_agent_client": MagicMock(), "AgentClientError": Exception},
    ),
    # sys.modules["utils"] points to tests/utils/ (needed for api_client/cleanup),
    # not src/backend/utils/, so url_validation.py isn't found automatically.
    ("utils.url_validation", {"validate_skills_library_url": lambda url: url}),
]:
    if mod_name not in sys.modules:
        m = _types.ModuleType(mod_name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[mod_name] = m


import importlib.util as _ilu  # noqa: E402

_skill_path = _BACKEND / "services" / "skill_service.py"
_spec = _ilu.spec_from_file_location("services.skill_service", str(_skill_path))
skill_service_mod = _ilu.module_from_spec(_spec)
sys.modules["services.skill_service"] = skill_service_mod
_spec.loader.exec_module(skill_service_mod)


def _captured_cmd(call_args_list, sentinel):
    """Return the cmd argv from the subprocess.run call whose argv contains
    sentinel (e.g. 'clone' or 'fetch')."""
    for call in call_args_list:
        cmd = call.args[0] if call.args else call.kwargs.get("args") or []
        if sentinel in cmd:
            return cmd
    raise AssertionError(f"No subprocess.run call contained {sentinel!r}")


class TestSkillServiceGitUserAgent:

    def test_clone_includes_user_agent_override(self, tmp_path):
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"

        with patch.object(skill_service_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = svc._git_clone("https://github.com/owner/repo", "main")

        assert result["success"] is True
        cmd = _captured_cmd(mock_run.call_args_list, "clone")

        # `-c key=value` must come BEFORE the subcommand, immediately after argv[0]
        assert cmd[0] == "git"
        assert "-c" in cmd, f"expected -c flag in cmd, got: {cmd}"
        c_idx = cmd.index("-c")
        assert cmd[c_idx + 1] == "http.useragent=Trinity-Skills-Sync"
        # And the -c block is positioned before the clone subcommand
        assert cmd.index("clone") > c_idx + 1

    def test_pull_fetch_includes_user_agent_override(self, tmp_path):
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"
        svc.library_path.mkdir(parents=True)

        with patch.object(skill_service_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            result = svc._git_pull("main")

        assert result["success"] is True

        # The HTTP-bearing call is `git fetch` — must carry the UA flag.
        fetch_cmd = _captured_cmd(mock_run.call_args_list, "fetch")
        assert fetch_cmd[0] == "git"
        assert "-c" in fetch_cmd
        c_idx = fetch_cmd.index("-c")
        assert fetch_cmd[c_idx + 1] == "http.useragent=Trinity-Skills-Sync"
        assert fetch_cmd.index("fetch") > c_idx + 1

    def test_pull_reset_does_not_need_user_agent(self, tmp_path):
        """`git reset --hard` is a local-only operation — no HTTP, no UA leak,
        no need to thread the flag through. This is documentation as much as
        a test: if someone adds the flag here later, they should at least
        consider whether reset has started doing remote calls."""
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"
        svc.library_path.mkdir(parents=True)

        with patch.object(skill_service_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            svc._git_pull("main")

        reset_cmd = _captured_cmd(mock_run.call_args_list, "reset")
        # Either way is acceptable; this asserts the current minimal-scope choice.
        assert "-c" not in reset_cmd or reset_cmd.index("-c") > reset_cmd.index("reset")

    def test_get_current_commit_does_not_need_user_agent(self, tmp_path):
        """`git rev-parse HEAD` is a local query — same reasoning as reset."""
        svc = skill_service_mod.SkillService()
        svc.library_path = tmp_path / "skills-library"
        svc.library_path.mkdir(parents=True)

        with patch.object(skill_service_mod.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="abc123def456\n", stderr="")
            sha = svc._get_current_commit()

        assert sha == "abc123def456"
        rev_cmd = _captured_cmd(mock_run.call_args_list, "rev-parse")
        assert "-c" not in rev_cmd or rev_cmd.index("-c") > rev_cmd.index("rev-parse")
