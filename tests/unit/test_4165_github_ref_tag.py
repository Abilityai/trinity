"""
A dotted tag in `github:owner/repo@ref` pins the clone (trinity#4165).

`_parse_github_ref` accepted only `[A-Za-z0-9_-/]` after the `@`, so a release
tag such as `v1.2.0` failed the check and was dropped without a word: the
agent was created, `source_branch` stayed `main`, and a person who pinned a
template to a release tracked `main` while believing it pinned.

The ref is interpolated into startup.sh's eval-built clone command, so the
charset stays a shell-safety barrier: `.` is added, the git ref-format rules
that make a dotted ref unusable (`..`, a component starting with `.`, a
trailing `.` or `.lock`) are refused, and a ref that fails the check is now a
named 400 instead of a silent fall-back to `main`.

Harness: the purge-and-mock `_load_crud` copied from
test_ent123_tokenless_clone.py (deliberately not shared cross-file — see the
D6 note in test_1484_create_agent_characterization.py on sys.modules leaks).
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_4165_github_ref_tag.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from fastapi import HTTPException  # noqa: E402

from models import AgentConfig  # noqa: E402


class _NotFound(Exception):
    pass


class _GitHubError(Exception):
    """Real exception class for the mocked github_service module — an
    `except <MagicMock>` clause raises TypeError at match time."""


def _purge_real_services(monkeypatch, mocks):
    """Drop every real `services*` module not explicitly mocked so a fresh
    `from services import X` resolves the sys.modules mock, not a stale
    attribute of a previously-imported real package (1484 harness shape)."""
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)


# ---------------------------------------------------------------------------
# crud harness
# ---------------------------------------------------------------------------
def _load_crud(monkeypatch):
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    settings_service = MagicMock()
    settings_service.resolve_github_pat = MagicMock(return_value=("", "none"))
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant")
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_quota_for_role = MagicMock(return_value=0)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"})
    settings_service.get_agent_default_require_email = MagicMock(return_value=False)
    settings_service.get_ephemeral_agent_quota = MagicMock(return_value=5)
    settings_service.get_ephemeral_ttl_ceiling_seconds = MagicMock(return_value=86400)

    git_service = MagicMock()
    git_service.DEFAULT_PERSISTENT_STATE = ["memory/"]
    git_service.probe_anonymous_repo_access = AsyncMock(return_value="ok")
    git_service.check_remote_branch_exists = AsyncMock(return_value=True)
    git_service.reserve_and_generate_instance_id = AsyncMock(
        return_value=("iid-1", "main"))
    # #2069: `_apply_github_env` now gates GIT_SYNC_AUTO on the real
    # `_git_auto_sync_baked` predicate (the single owner). A bare MagicMock is
    # TRUTHY, which would set GIT_SYNC_AUTO for every case; give the mock a
    # faithful copy so these tests exercise the real gating. The REAL predicate's
    # matrix is guard-tested in `test_2069_gitignore_at_creation::TestBakePredicate`.
    git_service._git_auto_sync_baked = (
        lambda config, github_repo, github_pat, fork_upstream: bool(github_repo)
        and bool(github_pat)
        and (not config.source_mode or bool(fork_upstream))
    )

    github_service_mod = MagicMock()
    github_service_mod.GitHubError = _GitHubError
    github_service_mod.GitHubService = MagicMock()

    template_service = MagicMock()
    template_service.get_github_template = MagicMock(return_value=None)

    pkg = "services.agent_service"
    sibling_mocks = {
        f"{pkg}.{sib}": MagicMock()
        for sib in ["api_key", "autonomy", "dashboard", "deploy", "file_sharing",
                    "files", "folders", "helpers", "lifecycle", "mcp_tool_names",
                    "metrics", "permissions", "queue", "read_only", "stats",
                    "terminal", "capabilities", "ephemeral", "pull_mode"]
    }

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": MagicMock(),
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": template_service,
        "services.git_service": git_service,
        "services.settings_service": settings_service,
        "services.github_service": github_service_mod,
        "services.entitlement_service": MagicMock(),
        "services.rate_limiter": MagicMock(),
        "services.agent_runtime_state": MagicMock(),
        "services.agent_auth": MagicMock(),
        **sibling_mocks,
    }

    patcher = patch.dict("sys.modules", mocks)
    patcher.start()
    _purge_real_services(monkeypatch, mocks)
    import services.agent_service.crud as crud_mod
    return crud_mod, patcher, git_service


@pytest.fixture()
def crud(monkeypatch):
    crud_mod, patcher, _git_service = _load_crud(monkeypatch)
    try:
        yield crud_mod
    finally:
        patcher.stop()


class TestRefPins:
    @pytest.mark.parametrize("ref", [
        "v1.2.0",
        "release-1.0",
        "1.0",
        "v2.0.0-rc.1",
        "releases/v1.2.0",
        # branch names that always parsed keep parsing unchanged
        "main",
        "feature-x",
        "feature/sub_branch",
    ])
    def test_ref_is_taken_whole_and_pins_the_clone(self, crud, ref):
        cfg = AgentConfig(name="a1", template=f"github:owner/repo@{ref}")
        lookup, repo_path, url_branch = crud._parse_github_ref(cfg)
        assert url_branch == ref
        assert cfg.source_branch == ref
        assert repo_path == "owner/repo"
        assert lookup == "github:owner/repo"

    def test_no_ref_leaves_the_default_branch(self, crud):
        cfg = AgentConfig(name="a1", template="github:owner/repo")
        lookup, repo_path, url_branch = crud._parse_github_ref(cfg)
        assert url_branch is None
        assert cfg.source_branch == "main"
        assert lookup == "github:owner/repo"
        assert repo_path == "owner/repo"


class TestRefRefused:
    @pytest.mark.parametrize("ref", [
        "v1..2",            # git ref-format: no `..`
        "..",
        ".hidden",          # a component may not start with `.`
        "releases/.v1",
        "v1.",              # nor end with `.`
        "v1.lock",          # nor `.lock`
        "-foo",             # a leading `-` reads as an option to `git checkout` (review I5)
        "re lease",         # whitespace
        "v1;id",            # shell metacharacters (eval-built clone command)
        "$(id)",
        "v1`x`",
    ])
    def test_unusable_ref_is_a_named_400_not_a_silent_main(self, crud, ref):
        cfg = AgentConfig(name="a1", template=f"github:owner/repo@{ref}")
        with pytest.raises(HTTPException) as exc:
            crud._parse_github_ref(cfg)
        assert exc.value.status_code == 400
        assert "Invalid ref" in exc.value.detail
        # the config was never pointed at the refused ref
        assert cfg.source_branch == "main"
