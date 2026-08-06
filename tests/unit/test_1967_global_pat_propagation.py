"""#1967 — a global GitHub PAT rotation must actually reach running agents.

Rotating the global PAT in Settings reported success while updating nothing. Two
independent gaps each fully defeated it, and both existed because the global path
and the per-agent path (#1264) had drifted apart:

1. **The eligibility gate was `.env`-shaped.** `_propagate_to_agent` passed
   `add_if_missing=False`, so an agent with no `/home/developer/.env` — which is
   *every* agent provisioned from a GitHub template, since those ship
   `.env.example` — returned `skipped_no_pat`. On such a fleet, 100% skipped and
   the endpoint still answered `success: true`.

2. **`.env` is not where git authenticates from.** Clones are created as
   `https://oauth2:<PAT>@github.com/...` and that URL is persisted in
   `.git/config` on the workspace volume, so rewriting `.env` changes nothing for
   the running `git` process. Only `git_service.update_remote_pat` restores
   fetch/push before a restart — and only the per-agent path called it.

Observed consequence: agents authenticating with a revoked token for 11–13 days,
with nothing in the UI saying so.

The fix routes both paths through one `_apply_pat_to_agent` body and derives
eligibility from the agent's **git config** rather than its `.env`. These tests
pin both halves, and — as importantly — pin that the fix did NOT become "spray
the token into every running container", which is the obvious over-correction.
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

import services.github_pat_propagation_service as svc  # noqa: E402


class _Agent:
    def __init__(self, name, status="running"):
        self.name = name
        self.status = status


class _Git:
    def __init__(self, repo):
        self.github_repo = repo


def _wire(
    monkeypatch,
    *,
    agents,
    git_configs,
    per_agent_pats=(),
    env_status="updated",
    remote_ok=True,
):
    """Patch the four seams `propagate_github_pat` reaches through.

    `_apply_pat_to_env` is mocked rather than driven through an httpx double —
    the contract under test is which agents are attempted and with what
    `add_if_missing`, not the HTTP mechanics (covered by #1264's tests).
    """
    calls = {"env": [], "remote": []}

    monkeypatch.setattr(svc, "list_all_agents_fast", lambda: list(agents))
    monkeypatch.setattr(
        svc.db, "has_agent_github_pat", lambda n: n in per_agent_pats
    )
    monkeypatch.setattr(
        svc.db, "get_git_config",
        lambda n: _Git(git_configs[n]) if git_configs.get(n) else None,
    )

    async def _fake_apply(client, agent_name, base_url, pat, *, add_if_missing):
        calls["env"].append((agent_name, add_if_missing))
        if callable(env_status):
            return env_status(agent_name)
        return env_status

    monkeypatch.setattr(svc, "_apply_pat_to_env", _fake_apply)

    async def _fake_remote(agent_name, pat, repo):
        calls["remote"].append((agent_name, pat, repo))
        return remote_ok

    monkeypatch.setattr(
        "services.git_service.update_remote_pat", _fake_remote, raising=False
    )
    return calls


# ---------------------------------------------------------------------------
# Gap 1 — the template-provisioned agent that skipped every rotation.
# ---------------------------------------------------------------------------


def test_agent_with_git_config_but_no_env_is_updated(monkeypatch):
    """The reported population: a GitHub-template agent ships `.env.example` and
    no `.env`, so the old `add_if_missing=False` gate skipped it forever."""
    calls = _wire(
        monkeypatch,
        agents=[_Agent("tmpl")],
        git_configs={"tmpl": "org/repo"},
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert calls["env"] == [("tmpl", True)], (
        "an agent Trinity manages a repo for must get the GITHUB_PAT line "
        "written even when its .env does not exist (#1967)"
    )
    assert result.updated == ["tmpl"]
    assert result.skipped == []


def test_agent_without_git_config_is_not_sprayed(monkeypatch):
    """The over-correction this fix must NOT be.

    The issue offers `add_if_missing=True` unconditionally. That would inject the
    global PAT into every running container, including agents that never touched
    GitHub — and the module's own docstring says the original gate existed to
    avoid exactly that. Eligibility moved to the git config instead, so a
    non-GitHub agent keeps the conservative behaviour: update an existing line,
    never create one.
    """
    calls = _wire(
        monkeypatch,
        agents=[_Agent("plain")],
        git_configs={},
        env_status="skipped_no_pat",
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert calls["env"] == [("plain", False)], (
        "an agent with no git config must not have a token created in its .env"
    )
    assert [s.status for s in result.skipped] == ["skipped_no_pat"]
    assert calls["remote"] == [], "no repo, so nothing to re-template"


def test_agent_without_git_config_but_with_an_existing_line_still_rotates(monkeypatch):
    """No-regression guard. An agent may carry GITHUB_PAT for the `gh` CLI with
    no Trinity-managed repo. It was updated before this change and must still
    be — the new gate is a UNION with the old behaviour, not a replacement."""
    calls = _wire(
        monkeypatch,
        agents=[_Agent("ghcli")],
        git_configs={},
        env_status="updated",  # the line existed, so the patch applied
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert calls["env"] == [("ghcli", False)]
    assert result.updated == ["ghcli"]


# ---------------------------------------------------------------------------
# Gap 2 — the half that makes git work NOW.
# ---------------------------------------------------------------------------


def test_the_live_remote_is_retemplated(monkeypatch):
    """The load-bearing fix. Git authenticates from the remote URL persisted in
    `.git/config`, not from `GITHUB_PAT`, so a rotation that only rewrites `.env`
    leaves every clone on the revoked token until restart."""
    calls = _wire(
        monkeypatch,
        agents=[_Agent("a1")],
        git_configs={"a1": "org/repo"},
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert calls["remote"] == [("a1", "ghp_new", "org/repo")], (
        "the global path still does not re-template the live git remote (#1967)"
    )
    assert result.remotes_updated == 1


def test_a_failed_remote_rewrite_does_not_fail_the_agent(monkeypatch):
    """`update_remote_pat` is best-effort by contract. A container that cannot be
    exec'd into must not cost the `.env` write, nor the other agents."""
    calls = _wire(
        monkeypatch,
        agents=[_Agent("a1")],
        git_configs={"a1": "org/repo"},
        remote_ok=False,
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert result.updated == ["a1"]
    assert result.remotes_updated == 0
    assert calls["remote"] == [("a1", "ghp_new", "org/repo")]


def test_remotes_updated_is_reported_separately_from_updated(monkeypatch):
    """`updated` alone overstates the fix: an agent whose `.env` was rewritten but
    whose remote was not is still broken for git until it restarts. The counts
    must be distinguishable or the response repeats this issue's own mistake of
    reporting success for a partial effect."""
    def _remote(agent_name, pat, repo):
        raise AssertionError("replaced below")

    calls = _wire(
        monkeypatch,
        agents=[_Agent("with_git"), _Agent("no_git")],
        git_configs={"with_git": "org/repo"},
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert sorted(result.updated) == ["no_git", "with_git"]
    assert result.remotes_updated == 1, (
        "only the agent with a repo could have its remote re-templated"
    )
    assert len(calls["remote"]) == 1


# ---------------------------------------------------------------------------
# Unchanged behaviour that must stay unchanged.
# ---------------------------------------------------------------------------


def test_per_agent_pat_agents_are_still_skipped(monkeypatch):
    """A per-agent PAT (#347) overrides the global one and is managed
    separately — rotating the global token must not clobber it."""
    calls = _wire(
        monkeypatch,
        agents=[_Agent("owned")],
        git_configs={"owned": "org/repo"},
        per_agent_pats={"owned"},
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert [s.status for s in result.skipped] == ["skipped_per_agent_pat"]
    assert calls["env"] == [] and calls["remote"] == [], (
        "an agent with its own PAT must not be contacted at all"
    )


def test_stopped_agents_are_not_counted_or_contacted(monkeypatch):
    calls = _wire(
        monkeypatch,
        agents=[_Agent("up"), _Agent("down", status="exited")],
        git_configs={"up": "org/repo", "down": "org/repo"},
    )

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert result.total_running == 1
    assert [c[0] for c in calls["env"]] == ["up"]


def test_one_agent_failing_does_not_stop_the_rotation(monkeypatch):
    """Per-agent failures are captured, never raised — otherwise one unreachable
    container blocks the token reaching the rest of the fleet."""
    import httpx

    async def _apply(client, agent_name, base_url, pat, *, add_if_missing):
        if agent_name == "bad":
            raise httpx.RequestError("boom")
        return "updated"

    _wire(monkeypatch, agents=[_Agent("good"), _Agent("bad")],
          git_configs={"good": "o/r", "bad": "o/r"})
    monkeypatch.setattr(svc, "_apply_pat_to_env", _apply)

    result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert result.updated == ["good"]
    assert [f.agent_name for f in result.failed] == ["bad"]


def test_eligibility_check_fails_safe_on_a_db_error(monkeypatch):
    """A git-config read that raises must degrade to the conservative gate, not
    take down the rotation for every agent."""
    _wire(monkeypatch, agents=[_Agent("a1")], git_configs={"a1": "o/r"})

    def _boom(name):
        raise RuntimeError("db down")

    monkeypatch.setattr(svc.db, "get_git_config", _boom)

    # Must not raise; the agent is attempted with the conservative gate.
    result = asyncio.run(svc.propagate_github_pat("ghp_new"))
    assert result.total_running == 1


# ---------------------------------------------------------------------------
# The two paths must not drift again — that drift IS this bug.
# ---------------------------------------------------------------------------


def test_both_paths_share_one_body():
    """The global path wrote `.env` only while the per-agent path did both, for
    two releases, because they were separate code. Pin the shared body so the
    next divergence has to be deliberate."""
    src = (
        _BACKEND / "services" / "github_pat_propagation_service.py"
    ).read_text(encoding="utf-8")
    assert "async def _apply_pat_to_agent(" in src
    # Both callers reach it.
    single = src[src.index("async def propagate_pat_to_single_agent("):]
    single = single[: single.index("async def _propagate_to_agent(")]
    assert "_apply_pat_to_agent(" in single
    per_agent = src[src.index("async def _propagate_to_agent("):]
    per_agent = per_agent[: per_agent.index("async def propagate_github_pat(")]
    assert "_apply_pat_to_agent(" in per_agent


def test_the_global_path_no_longer_hardcodes_add_if_missing_false():
    """The literal line from the issue. A future edit putting it back would
    silently restore the skip-everything behaviour."""
    src = (
        _BACKEND / "services" / "github_pat_propagation_service.py"
    ).read_text(encoding="utf-8")
    per_agent = src[src.index("async def _propagate_to_agent("):]
    per_agent = per_agent[: per_agent.index("async def propagate_github_pat(")]
    assert "add_if_missing=False" not in per_agent
    assert "add_if_missing=has_git" in per_agent


def test_a_zero_reach_rotation_is_logged_loudly(monkeypatch, caplog):
    """The failure was silent for 11–13 days. A Settings panel nobody is looking
    at during an incident is not the only place this should appear."""
    import logging

    _wire(
        monkeypatch,
        agents=[_Agent("a1")],
        git_configs={},
        env_status="skipped_no_pat",
    )

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(svc.propagate_github_pat("ghp_new"))

    assert result.updated == []
    # `getMessage()` interpolates args safely; the hand-rolled `r.message % r.args`
    # I wrote first raised TypeError on any sibling record with args but no
    # placeholders — a broken assertion masquerading as a broken feature.
    messages = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("0 of 1 running agents" in m for m in messages), (
        f"a rotation that reached nothing must WARN; got: {messages}"
    )
