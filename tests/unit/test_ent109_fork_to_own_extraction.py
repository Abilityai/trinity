"""
`inspect_or_create_destination_repo` extraction (trinity-enterprise#109 §4.5).

ent#109 AC #4 asks the post-creation rebind to reuse ent#93's machinery rather
than build a parallel path. The honest seam is a LOWER-level primitive, because
the create path's destination triage is interleaved with the template tip:
its "reuse" branch **is** the `template_sha` comparison, so lifting the triage
out whole is not expressible as one if/elif/else. What is shared is

    inspect_or_create_destination_repo() -> created | empty | branches

which *reports* and never *decides*; reuse/refuse POLICY stays in each caller.

Two things are asserted here, and only these two — the four create-path policy
branches themselves are covered by `test_fork_to_own.py`, which is the real
behaviour-preservation baseline and must keep passing unchanged:

1. The primitive's own contract: each of the three states, the error registry
   it owns, and — the seam property — that a destination holding unrelated data
   comes back as `branches` rather than a 409, because deciding that is the
   caller's job.
2. The ordering `fork_template_to_own_repo` depends on: the PAT is validated
   BEFORE the template tip is resolved. Folding `validate_token` into the
   inspect primitive (which runs after `_resolve_template_tip`) would silently
   turn "bad PAT + unreachable template" from FORK_PAT_INVALID into a template
   error. That is why `validate_destination_pat` is a sibling, not an internal.

Module: src/backend/services/agent_service/fork_to_own.py
Issue:  abilityai/trinity-enterprise#109 (Epic ent#122)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

# Env prerequisites before any backend import (repo test convention)
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_ent109_extraction.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

# Pinned at IMPORT time, never lazily inside a fixture: a module resolved
# during a fixture inherits whatever Mock a sibling test left in sys.modules
# at collection time (auto-memory: the sys.modules victim-side trap).
from fastapi import HTTPException  # noqa: E402

import services.agent_service.fork_to_own as f2o  # noqa: E402
from services.github_service import (  # noqa: E402
    GitHubCreateResult,
    GitHubError,
    OwnerType,
)

# ---------------------------------------------------------------------------
# Harness — a scriptable GitHubService stand-in, scoped to this module
# ---------------------------------------------------------------------------


class _RepoInfo:
    def __init__(self, exists, default_branch="main", private=True):
        self.exists = exists
        self.default_branch = default_branch
        self.private = private


class _GH:
    """Minimal scriptable GitHubService double.

    Any key may be set to an exception instance to make that call raise.
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.created = []

    async def validate_token(self):
        v = self.script.get("validate_token", (True, "alice"))
        if isinstance(v, Exception):
            raise v
        return v

    async def check_repo_exists(self, owner, name):
        v = self.script.get("repos", {}).get(f"{owner}/{name}", _RepoInfo(False))
        if isinstance(v, Exception):
            raise v
        return v

    async def list_branches(self, repo, limit=10):
        v = self.script.get("branches", {}).get(repo, [])
        if isinstance(v, Exception):
            raise v
        return v

    async def get_owner_type(self, owner):
        return self.script.get("owner_type", OwnerType.USER)

    async def create_repository(
        self, owner, name, private=True, description=None, auto_init=False
    ):
        self.created.append(
            {
                "repo": f"{owner}/{name}",
                "private": private,
                "description": description,
            }
        )
        scripted = self.script.get("create_result")
        if scripted is not None:
            return scripted
        # Mirror the real API: after a successful create the repo resolves, so
        # the caller's eventual-consistency poll can actually succeed.
        self.script.setdefault("repos", {})[f"{owner}/{name}"] = _RepoInfo(True)
        return GitHubCreateResult(
            success=True, repo_url=f"https://github.com/{owner}/{name}"
        )


@pytest.fixture(autouse=True)
def _no_real_polling(monkeypatch):
    """The visibility poll is a real sleep loop; make it instant and truthful."""
    monkeypatch.setattr(f2o, "_POLL_INTERVAL_S", 0.001)
    monkeypatch.setattr(f2o, "CREATE_VISIBILITY_TIMEOUT_S", 0.05)


async def _inspect(gh, dest="alice/brain", login="alice", private=True):
    return await f2o.inspect_or_create_destination_repo(
        gh,
        dest,
        login,
        private=private,
        description="desc",
        user_pat="ghp_u",
    )


# ---------------------------------------------------------------------------
# The three states
# ---------------------------------------------------------------------------


class TestDestinationStates:
    @pytest.mark.asyncio
    async def test_absent_repo_is_created_and_polled_visible(self, monkeypatch):
        gh = _GH({"repos": {}})

        seen = {}

        async def fake_visible(_gh, repo):
            seen["polled"] = repo

        monkeypatch.setattr(f2o, "_wait_for_repo_visible", fake_visible)

        state = await _inspect(gh)

        assert state.state == "created"
        assert state.branches == []
        assert gh.created == [
            {"repo": "alice/brain", "private": True, "description": "desc"}
        ]
        # "created" must mean *safe to push to*, not merely "the API returned
        # 201" — GitHub's create is eventually consistent (#1439 class).
        assert seen["polled"] == "alice/brain"

    @pytest.mark.asyncio
    async def test_existing_repo_with_no_branches_is_empty(self):
        gh = _GH(
            {
                "repos": {"alice/brain": _RepoInfo(True)},
                "branches": {"alice/brain": []},
            }
        )
        state = await _inspect(gh)
        assert state.state == "empty"
        assert state.branches == []
        assert gh.created == []  # never re-creates an existing repo

    @pytest.mark.asyncio
    async def test_existing_repo_with_refs_reports_branches_and_does_not_refuse(self):
        """The seam property: reporting, not deciding.

        A destination holding unrelated data is a 409 for BOTH callers today,
        but they reach that verdict differently (the create path first checks
        whether the single branch is the template tip). If the primitive
        raised here, that comparison would be unreachable and the extraction
        would have changed create-path behaviour.
        """
        refs = [
            {"name": "main", "sha": "aaa"},
            {"name": "dev", "sha": "bbb"},
        ]
        gh = _GH(
            {
                "repos": {"alice/brain": _RepoInfo(True)},
                "branches": {"alice/brain": refs},
            }
        )
        state = await _inspect(gh)
        assert state.state == "branches"
        assert state.branches == refs

    @pytest.mark.asyncio
    async def test_private_flag_is_passed_through(self):
        gh = _GH({"repos": {}})
        await _inspect(gh, private=False)
        assert gh.created[0]["private"] is False


# ---------------------------------------------------------------------------
# The error registry the primitive owns
# ---------------------------------------------------------------------------


class TestSharedErrorRegistry:
    @pytest.mark.asyncio
    async def test_existence_check_failure_is_502_unreachable(self):
        gh = _GH({"repos": {"alice/brain": GitHubError("boom")}})
        with pytest.raises(HTTPException) as exc:
            await _inspect(gh)
        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "FORK_DESTINATION_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_branch_listing_failure_is_502_unreachable(self):
        gh = _GH(
            {
                "repos": {"alice/brain": _RepoInfo(True)},
                "branches": {"alice/brain": GitHubError("boom")},
            }
        )
        with pytest.raises(HTTPException) as exc:
            await _inspect(gh)
        assert exc.value.status_code == 502
        assert exc.value.detail["code"] == "FORK_DESTINATION_UNREACHABLE"

    @pytest.mark.asyncio
    async def test_user_owner_mismatch_is_forbidden(self):
        gh = _GH({"repos": {}, "owner_type": OwnerType.USER})
        with pytest.raises(HTTPException) as exc:
            await _inspect(gh, dest="bob/brain", login="alice")
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "FORK_DESTINATION_FORBIDDEN"
        assert gh.created == []

    @pytest.mark.asyncio
    async def test_org_destination_is_not_owner_checked(self, monkeypatch):
        """Org rights are enforced by GitHub's own org endpoint, not by us."""
        monkeypatch.setattr(f2o, "_wait_for_repo_visible", lambda *a, **k: _noop())
        gh = _GH({"repos": {}, "owner_type": OwnerType.ORGANIZATION})
        state = await _inspect(gh, dest="acme-org/brain", login="alice")
        assert state.state == "created"

    @pytest.mark.asyncio
    async def test_create_failure_is_400_and_scrubs_the_pat(self, monkeypatch):
        gh = _GH(
            {
                "repos": {},
                "create_result": GitHubCreateResult(
                    success=False, error="denied for token ghp_u"
                ),
            }
        )
        with pytest.raises(HTTPException) as exc:
            await _inspect(gh)
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "FORK_REPO_CREATE_FAILED"
        # The PAT reached the error string from GitHub; it must not survive.
        assert "ghp_u" not in exc.value.detail["error"]
        assert "***" in exc.value.detail["error"]

    @pytest.mark.asyncio
    async def test_invalid_pat_rejected_by_the_sibling_validator(self):
        gh = _GH({"validate_token": (False, None)})
        with pytest.raises(HTTPException) as exc:
            await f2o.validate_destination_pat(gh)
        assert exc.value.status_code == 400
        assert exc.value.detail["code"] == "FORK_PAT_INVALID"

    @pytest.mark.asyncio
    async def test_valid_pat_returns_login_and_never_none(self):
        assert await f2o.validate_destination_pat(_GH()) == "alice"
        # A valid token whose login GitHub omitted must degrade to "", not
        # None — the owner check is `login and ...`, and None would read as
        # "skip the check" while also being unformattable into the message.
        gh = _GH({"validate_token": (True, None)})
        assert await f2o.validate_destination_pat(gh) == ""


async def _noop():
    return None


# ---------------------------------------------------------------------------
# Behaviour preservation of the create path (ent#109 §4.5, decision #11)
# ---------------------------------------------------------------------------


class TestCreatePathOrderingPreserved:
    """`test_fork_to_own.py` covers the four policy branches; this covers the
    one property the extraction could plausibly have broken silently."""

    @pytest.mark.asyncio
    async def test_pat_is_validated_before_the_template_tip_is_resolved(
        self, monkeypatch
    ):
        order = []

        async def spy_resolve(template_repo, read_pat):
            order.append("template")
            raise f2o._http_error(502, "FORK_TEMPLATE_UNREACHABLE", "template is down")

        real_validate = f2o.validate_destination_pat

        async def spy_validate(gh):
            order.append("pat")
            return await real_validate(gh)

        monkeypatch.setattr(f2o, "_resolve_template_tip", spy_resolve)
        monkeypatch.setattr(f2o, "validate_destination_pat", spy_validate)
        monkeypatch.setattr(
            f2o,
            "GitHubService",
            lambda pat: _GH({"validate_token": (False, None)}),
        )

        with pytest.raises(HTTPException) as exc:
            await f2o.fork_template_to_own_repo(
                "Abilityai/cornelius", "alice/brain", "badpat", "", True
            )

        # Both are broken; the PAT verdict is the one the user can act on.
        assert exc.value.detail["code"] == "FORK_PAT_INVALID"
        assert order == ["pat"], (
            "validate_destination_pat must run before _resolve_template_tip; "
            "folding it into inspect_or_create_destination_repo would reorder "
            "this into a template error"
        )

    @pytest.mark.asyncio
    async def test_create_path_delegates_to_the_shared_primitive(self, monkeypatch):
        """Guards against the extraction being quietly re-inlined later."""
        calls = []

        async def spy_inspect(gh, dest, login, *, private, description, user_pat):
            calls.append(
                {
                    "dest": dest,
                    "login": login,
                    "private": private,
                    "description": description,
                }
            )
            return f2o.DestinationState("empty", [])

        async def fake_run_git(args, timeout, auth_pat=""):
            return 0, "ok"

        async def fake_resolve(template_repo, read_pat):
            return "main", "tipsha"

        async def fake_branch_visible(repo, branch, pat):
            return None

        monkeypatch.setattr(f2o, "inspect_or_create_destination_repo", spy_inspect)
        monkeypatch.setattr(f2o, "_resolve_template_tip", fake_resolve)
        monkeypatch.setattr(f2o, "_run_git", fake_run_git)
        monkeypatch.setattr(f2o, "_wait_for_branch_on_git_plane", fake_branch_visible)
        monkeypatch.setattr(f2o, "GitHubService", lambda pat: _GH())

        result = await f2o.fork_template_to_own_repo(
            "Abilityai/cornelius", "alice/brain", "ghp_u", "readpat", True
        )

        assert result.reused_existing is True
        assert len(calls) == 1
        assert calls[0]["dest"] == "alice/brain"
        assert calls[0]["login"] == "alice"
        assert calls[0]["private"] is True
        # The template-specific description stays owned by the create path.
        assert "Abilityai/cornelius" in calls[0]["description"]
