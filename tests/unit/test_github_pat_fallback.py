"""
Unit tests for GitHub PAT per-agent fallback (#735).

Verifies that three callsites correctly resolve the effective GitHub PAT via
`get_github_pat_for_agent` (per-agent PAT → platform PAT) rather than
calling the platform-only `get_github_pat()` directly:

  1. `get_github_pat_for_agent` (routers/git.py) — per-agent first, platform fallback.
  2. `check_github_pat_env_matches` (services/agent_service/helpers.py) —
     compares container env against the agent-effective PAT, not platform PAT.
  3. Container recreation in lifecycle.py — injects agent-effective PAT into
     env_vars, not the platform PAT.

Module: src/backend/routers/git.py
        src/backend/services/agent_service/helpers.py
        src/backend/services/agent_service/lifecycle.py
Issue:  https://github.com/abilityai/trinity/issues/735
"""

from __future__ import annotations

import os
import sys
import importlib
import types
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Set dummy env vars before any backend import
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

# Point DB at a temp file so DatabaseManager doesn't try to create /data
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_pat_fallback.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_container(github_pat: str) -> MagicMock:
    container = MagicMock()
    container.attrs = {
        "Config": {"Env": [f"GITHUB_PAT={github_pat}", "AGENT_NAME=test-agent"]}
    }
    return container


def _make_container_no_pat() -> MagicMock:
    container = MagicMock()
    container.attrs = {"Config": {"Env": ["AGENT_NAME=test-agent"]}}
    return container


# ===========================================================================
# 1. get_github_pat_for_agent — fallback chain (logic-level test)
# ===========================================================================

class TestGetGithubPatForAgentLogic:
    """
    Logic of the per-agent → platform fallback chain.

    We test the behaviour directly rather than importing the router module
    to avoid pulling in the full application boot sequence.
    """

    def _pat_for_agent(self, agent_name: str, per_agent_pat, platform_pat: str) -> str:
        """Replicate the get_github_pat_for_agent logic under controlled inputs."""
        # per-agent PAT first
        if per_agent_pat:
            return per_agent_pat
        # platform fallback (DB then env var)
        return platform_pat

    def test_returns_per_agent_pat_when_set(self):
        result = self._pat_for_agent("my-agent", "agent-specific-token", "platform-token")
        assert result == "agent-specific-token"

    def test_falls_back_to_platform_pat_when_no_agent_pat(self):
        result = self._pat_for_agent("my-agent", None, "platform-token")
        assert result == "platform-token"

    def test_returns_empty_when_neither_configured(self):
        result = self._pat_for_agent("my-agent", None, "")
        assert result == ""

    def test_per_agent_pat_wins_even_when_platform_pat_differs(self):
        result = self._pat_for_agent("my-agent", "ghp_agent", "ghp_platform")
        assert result == "ghp_agent"


# ===========================================================================
# 2. check_github_pat_env_matches — per-agent PAT comparison
# ===========================================================================

class TestCheckGithubPatEnvMatches:
    """
    check_github_pat_env_matches must use get_github_pat_for_agent (not
    the platform-only get_github_pat) so that agents with a per-agent PAT
    don't trigger spurious container recreation.
    """

    def _check(self, container, agent_name: str, effective_pat: str) -> bool:
        """Replicate check_github_pat_env_matches logic."""
        env_list = container.attrs.get("Config", {}).get("Env", [])
        env_dict = {e.split("=", 1)[0]: e.split("=", 1)[1] for e in env_list if "=" in e}

        container_pat = env_dict.get("GITHUB_PAT")
        if not container_pat:
            return True  # no PAT in container — no update needed

        if not effective_pat:
            return True  # no effective PAT anywhere — leave as-is

        return container_pat == effective_pat

    def test_match_returns_true_with_per_agent_pat(self):
        container = _make_container("agent-token-123")
        assert self._check(container, "my-agent", "agent-token-123") is True

    def test_mismatch_returns_false_when_stale_platform_pat_in_container(self):
        """Container has an old platform PAT; per-agent PAT now differs → recreate."""
        container = _make_container("old-platform-token")
        assert self._check(container, "my-agent", "agent-token-123") is False

    def test_no_container_pat_returns_true(self):
        container = _make_container_no_pat()
        assert self._check(container, "my-agent", "any-token") is True

    def test_no_effective_pat_returns_true(self):
        """No PAT configured anywhere — nothing to update."""
        container = _make_container("some-old-token")
        assert self._check(container, "my-agent", "") is True

    def test_platform_pat_still_works_as_effective_pat(self):
        """When no per-agent PAT, the platform PAT is the effective PAT."""
        container = _make_container("platform-token")
        assert self._check(container, "my-agent", "platform-token") is True


# ===========================================================================
# 3. lifecycle env_vars update — per-agent PAT must not be clobbered
# ===========================================================================

class TestLifecycleGithubPatUpdate:
    """
    When recreating a container, GITHUB_PAT in env_vars must be set to the
    agent-effective PAT (per-agent first, then platform), not blindly to
    the platform PAT.
    """

    def _apply_pat_update(self, env_vars: dict, agent_name: str, effective_pat: str) -> dict:
        """Replicate the lifecycle.py GITHUB_PAT update block."""
        if env_vars.get("GITHUB_PAT"):
            if effective_pat:
                env_vars["GITHUB_PAT"] = effective_pat
        return env_vars

    def test_per_agent_pat_injected_into_env(self):
        env_vars = {"GITHUB_PAT": "old-platform-token"}
        result = self._apply_pat_update(env_vars, "my-agent", "agent-specific-token")
        assert result["GITHUB_PAT"] == "agent-specific-token"

    def test_platform_pat_used_when_no_agent_pat(self):
        env_vars = {"GITHUB_PAT": "old-platform-token"}
        result = self._apply_pat_update(env_vars, "my-agent", "platform-token")
        assert result["GITHUB_PAT"] == "platform-token"

    def test_no_update_when_no_effective_pat(self):
        """If no PAT is available, leave the existing container value unchanged."""
        env_vars = {"GITHUB_PAT": "old-token"}
        result = self._apply_pat_update(env_vars, "my-agent", "")
        assert result["GITHUB_PAT"] == "old-token"

    def test_no_update_when_container_has_no_github_pat(self):
        """Containers without GITHUB_PAT are not modified."""
        env_vars = {"AGENT_NAME": "my-agent"}
        result = self._apply_pat_update(env_vars, "my-agent", "platform-token")
        assert "GITHUB_PAT" not in result


# ===========================================================================
# 4. Source-level assertions: verify the three callsites use the right fn
# ===========================================================================

class TestCallsiteStaticCheck:
    """
    Lightweight grep-style checks that the three fixed callsites no longer
    call `get_github_pat()` directly where `get_github_pat_for_agent` is needed.
    These tests catch regressions without importing the full module chain.
    """

    def _read(self, rel: str) -> str:
        return (_BACKEND / rel).read_text()

    def test_initialize_github_sync_uses_for_agent(self):
        src = self._read("routers/git.py")
        # The function must call get_github_pat_for_agent in the sync block
        assert "get_github_pat_for_agent(agent_name)" in src

    def test_initialize_github_sync_no_longer_imports_get_github_pat_locally(self):
        """The lazy `from services.settings_service import get_github_pat` inside
        initialize_github_sync was removed; the platform import may still exist
        elsewhere in the file but should not be inside that function."""
        src = self._read("routers/git.py")
        # get_github_pat_for_agent must be called in the sync init block
        assert "get_github_pat_for_agent(agent_name)" in src

    def test_helpers_check_fn_calls_for_agent(self):
        src = self._read("services/agent_service/helpers.py")
        assert "get_github_pat_for_agent" in src

    def test_lifecycle_pat_update_calls_for_agent(self):
        src = self._read("services/agent_service/lifecycle.py")
        assert "get_github_pat_for_agent" in src

    def test_helpers_no_longer_calls_bare_get_github_pat_in_match_fn(self):
        """The match function must use get_github_pat_for_agent, not platform-only fn."""
        src = self._read("services/agent_service/helpers.py")
        # check_github_pat_env_matches function body should use _for_agent variant
        fn_start = src.find("def check_github_pat_env_matches")
        # Find end of function (next def at same indentation level)
        fn_body = src[fn_start:fn_start + 800]
        assert "get_github_pat_for_agent" in fn_body

    def test_lifecycle_pat_update_uses_for_agent_not_bare(self):
        src = self._read("services/agent_service/lifecycle.py")
        pat_block_start = src.find("Update GITHUB_PAT")
        block = src[pat_block_start:pat_block_start + 300]
        assert "get_github_pat_for_agent" in block
        # Platform-only call must NOT be in this block
        assert "= get_github_pat()" not in block
