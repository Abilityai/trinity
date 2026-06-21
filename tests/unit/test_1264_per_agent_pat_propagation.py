"""Tests for #1264 — per-agent GitHub PAT propagation to existing containers.

Covers the decision logic added so a per-agent PAT configured AFTER a (possibly
tokenless) container was created actually reaches the container:

  * github_pat_propagation_service._patch_env_github_pat — ADDS the GITHUB_PAT
    line when the .env lacks it (the tokenless-container case), replaces when present.
  * github_pat_propagation_service.propagate_pat_to_single_agent — short-circuits
    for a stopped agent; on a running agent injects .env + re-templates the remote.
  * agent_service.check_github_pat_env_matches — a tokenless container that
    now has an effective PAT + git config reports a mismatch (recreate needed).

Service-level unit tests; rely on the repo conftest for the REDIS_URL stub.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

# Bind the real modules/callables at import time and use these bound names in the
# tests rather than re-importing (which could pick up a sibling's sys.modules stub
# at run time). Imported at module top so they resolve before any per-test state.
import services.agent_service.helpers as helpers_mod          # noqa: E402
import services.github_pat_propagation_service as svc          # noqa: E402
import routers.git as gitmod                                   # noqa: E402
from services import git_service                               # noqa: E402

check_github_pat_env_matches = helpers_mod.check_github_pat_env_matches
_patch_env_github_pat = svc._patch_env_github_pat
_env_has_github_pat = svc._env_has_github_pat
propagate_pat_to_single_agent = svc.propagate_pat_to_single_agent


def _patch_get_agent_container(monkeypatch, fn):
    """Patch get_agent_container as a module global on the bound propagation
    service. It's imported at that module's top, so the function closes over this
    global — patching it here is robust to sibling sys.modules pollution (no
    call-time `from services.docker_service import …` resolution to race)."""
    monkeypatch.setattr(svc, "get_agent_container", fn)


# ---------------------------------------------------------------------------
# _patch_env_github_pat — add-if-missing (the #1264 tokenless case)
# ---------------------------------------------------------------------------

def test_patch_adds_github_pat_line_when_missing():
    env = "ANTHROPIC_API_KEY=\"sk-x\"\nFOO=bar\n"
    out = _patch_env_github_pat(env, "ghp_token")
    assert _env_has_github_pat(out)
    assert 'GITHUB_PAT="ghp_token"' in out
    # original lines preserved
    assert "FOO=bar" in out


def test_patch_replaces_existing_github_pat_line():
    env = 'GITHUB_PAT="old"\nFOO=bar\n'
    out = _patch_env_github_pat(env, "newtok")
    assert "newtok" in out and "old" not in out
    assert out.count("GITHUB_PAT=") == 1


# ---------------------------------------------------------------------------
# propagate_pat_to_single_agent
# ---------------------------------------------------------------------------

class _Container:
    def __init__(self, status):
        self.status = status


def test_propagate_single_agent_stopped_short_circuits(monkeypatch):
    _patch_get_agent_container(monkeypatch, lambda n: _Container("exited"))
    res = asyncio.run(svc.propagate_pat_to_single_agent("a1", "ghp_x"))
    assert res == {"applied": False, "reason": "agent_not_running"}


def test_propagate_single_agent_missing_container_short_circuits(monkeypatch):
    _patch_get_agent_container(monkeypatch, lambda n: None)
    res = asyncio.run(svc.propagate_pat_to_single_agent("a1", "ghp_x"))
    assert res == {"applied": False, "reason": "agent_not_running"}


def test_propagate_single_agent_running_injects_and_retemplates(monkeypatch):
    # Patch at the seams the function owns, robust to sibling sys.modules
    # pollution: get_agent_container + git_service.update_remote_pat via the LIVE
    # module (string target, resolved at call time), and _apply_pat_to_env / db on
    # the bound svc module (the function closes over svc's own globals). Mocking
    # _apply_pat_to_env avoids a fragile httpx client double.
    _patch_get_agent_container(monkeypatch, lambda n: _Container("running"))

    apply_calls = {}
    async def _fake_apply(client, agent_name, base_url, pat, *, add_if_missing):
        apply_calls.update(agent_name=agent_name, pat=pat, add_if_missing=add_if_missing)
        return "updated"
    monkeypatch.setattr(svc, "_apply_pat_to_env", _fake_apply)

    monkeypatch.setattr(svc.db, "get_git_config",
                        lambda n: type("G", (), {"github_repo": "org/repo"})())
    called = {}
    async def _fake_update(agent_name, pat, repo):
        called.update(agent=agent_name, pat=pat, repo=repo)
        return True
    monkeypatch.setattr("services.git_service.update_remote_pat", _fake_update, raising=False)

    res = asyncio.run(svc.propagate_pat_to_single_agent("a1", "ghp_tok"))

    assert res["applied"] is True
    assert res["env_updated"] is True
    assert res["remote_updated"] is True
    assert apply_calls == {"agent_name": "a1", "pat": "ghp_tok", "add_if_missing": True}
    assert called == {"agent": "a1", "pat": "ghp_tok", "repo": "org/repo"}


# ---------------------------------------------------------------------------
# check_github_pat_env_matches — recreate decision
# ---------------------------------------------------------------------------

def _fake_container(env_lines):
    class _C:
        attrs = {"Config": {"Env": env_lines}}
    return _C()


def test_tokenless_container_with_per_agent_pat_and_git_needs_recreate(monkeypatch):
    # #1264 review: tokenless recreate keys on the PER-AGENT PAT, not the global fallback.
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: "ghp_peragent")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())  # git configured
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is False


def test_tokenless_container_global_pat_only_no_recreate(monkeypatch):
    # No per-agent PAT (only a global one would resolve) → must NOT force a recreate.
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: None)
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is True


def test_tokenless_container_without_git_no_recreate(monkeypatch):
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: "ghp_peragent")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: None)  # no git sync
    assert check_github_pat_env_matches(_fake_container(["FOO=bar"]), "a1") is True


def test_existing_pat_match_and_mismatch_preserved(monkeypatch):
    monkeypatch.setattr(gitmod, "get_github_pat_for_agent", lambda n: "ghp_current")
    # matching → True (no recreate)
    assert check_github_pat_env_matches(
        _fake_container(["GITHUB_PAT=ghp_current"]), "a1") is True
    # stale → False (recreate)
    assert check_github_pat_env_matches(
        _fake_container(["GITHUB_PAT=ghp_old"]), "a1") is False


def test_needs_per_agent_pat_injection_predicate(monkeypatch):
    # The shared gate matcher and lifecycle both use — per-agent PAT AND git config.
    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: "ghp_p")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: object())
    assert helpers_mod.needs_per_agent_pat_injection("a1") is True

    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: None)
    assert helpers_mod.needs_per_agent_pat_injection("a1") is False

    monkeypatch.setattr(helpers_mod.db, "get_agent_github_pat", lambda n: "ghp_p")
    monkeypatch.setattr(helpers_mod.db, "get_git_config", lambda n: None)
    assert helpers_mod.needs_per_agent_pat_injection("a1") is False
