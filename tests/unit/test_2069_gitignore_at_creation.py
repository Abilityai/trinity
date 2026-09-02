"""#2069 — the fleet-wide `.gitignore` merge now runs at agent creation.

The canonical `_GITIGNORE_PATTERNS` was applied at exactly two operator-initiated
moments (the on-Push migration + the init-path merge) and NEVER at agent creation.
Meanwhile the in-container 15-min auto-sync loop is on FROM BIRTH for the
`GIT_SYNC_AUTO` set (non-source-mode / fork-to-own `github:` agents, ephemeral
ghosts included), so such an agent auto-committed `.trinity/` runtime state,
`.claude/projects/`, `content/` and the root-level `.env`/`.mcp.json` into its
user-owned repo before any Push could migrate the list.

This suite proves:
  1. **Merge behaviour (AC#2)** — the real `_build_gitignore_merge_command` output,
     run against a tmp repo, ignores every canonical pattern (so `git add -A` stages
     no `.trinity/`, `.env`, `.mcp.json`, `content/` path) while the #2070 authored
     hooks stay includable.
  2. **Idempotence / #953 (AC#4)** — an already-canonical template shows no
     `M .gitignore` drift; a run-twice is a no-op; a stale wholesale `.trinity/`
     line yields a *legitimate* supersede→append (#2070).
  3. **`merge_gitignore_after_clone` readiness gating (AC#1)** — waits on `/health`
     ∧ `.git`, runs merge-only (never the untrack sweep), skips a failed clone /
     unresolved toplevel, times out cleanly, and every exec/HTTP is
     `asyncio.wait_for`-bounded.
  4. **Creation spawn wiring — the AC#6 regression (R2)** — `_git_auto_sync_baked`
     matrix (fork-to-own / non-source / ephemeral INCLUDED) + `_materialize_agent_files`
     fires the spawn on exactly that predicate.
  5. **Start/recreate spawn wiring (T1)** — the fleet-remediation call site is gated
     on the DB `auto_sync_enabled` flag (an AST structural guard — the full
     `start_agent_internal` boot harness would be brittle and prove less).

Harness: the git_service module is loaded REAL (its heavy deps mocked), so the
predicate, the merge command builders, and the readiness poller are all the
production code — reached through `crud.git_service`, which crud binds via
`from services import git_service`. The purge-and-mock shape is the 1484 harness
(copied from `test_ent123_tokenless_clone`, deliberately not shared — sys.modules
leaks).

Issue: abilityai/trinity#2069 (Epic #1045)
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.unit

# Env prerequisites before any backend import (repo test convention).
os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
_TMP_DB = Path(tempfile.gettempdir()) / "trinity_test_2069_gitignore.db"
os.environ.setdefault("TRINITY_DB_PATH", str(_TMP_DB))

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_BACKEND = str(_PROJECT_ROOT / "src" / "backend")
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

from models import AgentConfig, EphemeralConfig  # noqa: E402

_LIFECYCLE_SRC = (
    _PROJECT_ROOT / "src" / "backend" / "services" / "agent_service" / "lifecycle.py"
)


def _run(coro):
    return asyncio.run(coro)


class _NotFound(Exception):
    pass


def _purge_real_services(monkeypatch, mocks):
    """Drop every real `services*` module not explicitly mocked so a fresh
    `from services import X` resolves the sys.modules mock (or, when we
    deliberately omit `services.git_service`, the REAL module) rather than a
    stale attribute of a previously-imported real package (1484 harness shape)."""
    for key in list(sys.modules.keys()):
        if (key == "services" or key.startswith("services.")) and key not in mocks:
            monkeypatch.delitem(sys.modules, key, raising=False)


def _load_crud(monkeypatch):
    """Load `services.agent_service.crud` with heavy deps mocked but with the
    REAL `services.git_service` — so `crud.git_service._git_auto_sync_baked`,
    `_build_gitignore_merge_command`, `merge_gitignore_after_clone` and the
    spawn wrapper are all production code. git_service's own heavy imports
    (docker / redis / database / services.docker_service / services.agent_auth)
    are the mocks below; `utils.credential_sanitizer` / `utils.safe_yaml` stay
    real (pure)."""
    docker_mod = MagicMock()
    docker_mod.errors.NotFound = _NotFound

    settings_service = MagicMock()
    settings_service.resolve_github_pat = MagicMock(return_value=("", "none"))
    settings_service.get_anthropic_api_key = MagicMock(return_value="sk-ant")
    settings_service.get_agent_full_capabilities = MagicMock(return_value=False)
    settings_service.get_agent_default_resources = MagicMock(
        return_value={"cpu": "2", "memory": "4g"}
    )

    pkg = "services.agent_service"
    sibling_mocks = {
        f"{pkg}.{sib}": MagicMock()
        for sib in [
            "api_key",
            "autonomy",
            "dashboard",
            "deploy",
            "file_sharing",
            "files",
            "folders",
            "helpers",
            "lifecycle",
            "mcp_tool_names",
            "metrics",
            "permissions",
            "queue",
            "read_only",
            "stats",
            "terminal",
            "capabilities",
            "ephemeral",
            "pull_mode",
        ]
    }

    mocks = {
        "docker": docker_mod,
        "docker.errors": docker_mod.errors,
        "redis": MagicMock(),
        "redis.asyncio": MagicMock(),
        "database": MagicMock(),
        "services.docker_service": MagicMock(),
        "services.docker_utils": MagicMock(),
        "services.template_service": MagicMock(),
        # services.git_service intentionally NOT mocked → the real module loads.
        "services.settings_service": settings_service,
        "services.github_service": MagicMock(),
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

    # #1028: git_service is a package; collaborator patches in these tests
    # land on `gs.gitignore`, the module that owns the merge machinery.
    return crud_mod, patcher, crud_mod.git_service


@pytest.fixture()
def crud_gs(monkeypatch):
    crud_mod, patcher, gs = _load_crud(monkeypatch)
    try:
        yield crud_mod, gs
    finally:
        patcher.stop()


@pytest.fixture()
def gs(crud_gs):
    return crud_gs[1]


# ---------------------------------------------------------------------------
# 1. Merge behaviour (AC#2) — real command against a tmp repo
# ---------------------------------------------------------------------------
def _run_merge_command(gs, path: Path) -> str:
    cmd = gs.gitignore._build_gitignore_merge_command(str(path))
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=15)
    assert (
        result.returncode == 0
    ), f"merge command failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    return (path / ".gitignore").read_text()


def _git(path: Path, *args, check=True):
    return subprocess.run(
        ["git", *args],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=check,
        timeout=15,
    )


class TestMergeBehaviour:
    def test_external_template_with_no_gitignore_gets_full_list(self, gs, tmp_path):
        """AC#2 core: an external `github:` template shipping NO `.gitignore`
        gets every canonical pattern."""
        assert not (tmp_path / ".gitignore").exists()
        content = _run_merge_command(gs, tmp_path).splitlines()
        for pattern in gs._GITIGNORE_PATTERNS:
            assert pattern in content, f"missing canonical pattern {pattern!r}"

    def test_partial_gitignore_preserved_and_completed(self, gs, tmp_path):
        """A template shipping SOME rules keeps them and gains the rest."""
        (tmp_path / ".gitignore").write_text("# mine\nbuild/\n*.log\n")
        content = _run_merge_command(gs, tmp_path).splitlines()
        for line in ("# mine", "build/"):
            assert line in content, f"user rule {line!r} lost"
        for pattern in gs._GITIGNORE_PATTERNS:
            assert pattern in content, f"missing canonical pattern {pattern!r}"

    def test_first_add_stages_no_ignored_runtime_or_credential_path(self, gs, tmp_path):
        """AC#2: after the merge, a bare `git add -A` (what the in-container
        auto-sync loop runs) stages none of the ignored runtime/credential
        paths — the whole point of the fix."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "T")
        _git(tmp_path, "config", "commit.gpgsign", "false")

        # The generated creds + runtime state land as UNTRACKED files post-clone.
        for rel, body in [
            (".trinity/pending-results/x.json", "{}"),
            (".trinity/persistent-state.yaml", "a: 1"),
            (".env", "SECRET=abc"),
            (".mcp.json", "{}"),
            ("content/y.txt", "big"),
            (".claude/projects/p.jsonl", "{}"),
            ("real_code.py", "print('keep me')"),
        ]:
            f = tmp_path / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(body)

        _run_merge_command(gs, tmp_path)
        _git(tmp_path, "add", "-A")
        staged = {
            ln[3:] for ln in _git(tmp_path, "status", "--porcelain").stdout.splitlines()
        }

        for leaked in (
            ".env",
            ".mcp.json",
            ".trinity/pending-results/x.json",
            ".trinity/persistent-state.yaml",
            "content/y.txt",
            ".claude/projects/p.jsonl",
        ):
            assert leaked not in staged, f"{leaked} was staged despite the merge"
        # ...and genuine code + the merged .gitignore itself ARE staged.
        assert "real_code.py" in staged
        assert ".gitignore" in staged

    def test_authored_trinity_hooks_stay_includable(self, gs, tmp_path):
        """#2070 re-includes survive the fleet merge: a template that commits
        `.trinity/pre-check` etc. keeps them trackable."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "T")
        _git(tmp_path, "config", "commit.gpgsign", "false")

        hook = tmp_path / ".trinity" / "pre-check"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/usr/bin/env bash\n")
        leaked = tmp_path / ".trinity" / "runtime.yaml"
        leaked.write_text("x: 1\n")

        _run_merge_command(gs, tmp_path)
        _git(tmp_path, "add", "-A")
        staged = {
            ln[3:] for ln in _git(tmp_path, "status", "--porcelain").stdout.splitlines()
        }
        assert ".trinity/pre-check" in staged, "authored hook wrongly ignored"
        assert ".trinity/runtime.yaml" not in staged, "runtime state leaked"


# ---------------------------------------------------------------------------
# 2. Idempotence / #953 (AC#4) — R5
# ---------------------------------------------------------------------------
class TestIdempotence:
    def test_exact_seed_shows_no_drift_against_origin(self, gs, tmp_path):
        """AC#4 / #953: a template already shipping EXACTLY the canonical list
        shows no `M .gitignore` after the merge (the exact regression #953
        removed the shell-level append for)."""
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(tmp_path, "clone", "-q", str(origin), str(work))
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "T")
        _git(work, "config", "commit.gpgsign", "false")

        (work / ".gitignore").write_text("\n".join(gs._GITIGNORE_PATTERNS) + "\n")
        _git(work, "add", ".gitignore")
        _git(work, "commit", "-q", "-m", "seed canonical .gitignore")
        _git(work, "push", "-q", "origin", "HEAD")

        _run_merge_command(gs, work)

        porcelain = _git(work, "status", "--porcelain").stdout
        assert (
            "M .gitignore" not in porcelain
        ), f"#953 regression: merge manufactured .gitignore drift:\n{porcelain}"
        assert porcelain.strip() == "", f"unexpected drift:\n{porcelain}"

    def test_run_twice_is_a_noop(self, gs, tmp_path):
        """Seed-independent proof: a second merge yields an empty
        `git status --porcelain`."""
        _git(tmp_path, "init", "-q")
        _git(tmp_path, "config", "user.email", "t@example.com")
        _git(tmp_path, "config", "user.name", "T")
        _git(tmp_path, "config", "commit.gpgsign", "false")
        (tmp_path / ".gitignore").write_text("# start\nfoo/\n")
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "seed")

        _run_merge_command(gs, tmp_path)  # first: adds the canonical list
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", "first merge")

        _run_merge_command(gs, tmp_path)  # second: must be a no-op
        assert (
            _git(tmp_path, "status", "--porcelain").stdout.strip() == ""
        ), "second merge was not idempotent"
        # No pattern duplicated.
        lines = (tmp_path / ".gitignore").read_text().splitlines()
        for pattern in gs._GITIGNORE_PATTERNS:
            assert lines.count(pattern) == 1, f"{pattern!r} duplicated"

    def test_stale_wholesale_trinity_line_is_a_legitimate_edit(self, gs, tmp_path):
        """The 'no drift' contract is scoped to already-canonical templates: a
        template carrying the OLD wholesale `.trinity/` line gets a *legitimate*
        `M .gitignore` (supersede→append, #2070) — not a #953 regression."""
        origin = tmp_path / "origin.git"
        work = tmp_path / "work"
        _git(tmp_path, "init", "-q", "--bare", str(origin))
        _git(tmp_path, "clone", "-q", str(origin), str(work))
        _git(work, "config", "user.email", "t@example.com")
        _git(work, "config", "user.name", "T")
        _git(work, "config", "commit.gpgsign", "false")

        (work / ".gitignore").write_text(".trinity/\n")
        _git(work, "add", ".gitignore")
        _git(work, "commit", "-q", "-m", "seed stale line")
        _git(work, "push", "-q", "origin", "HEAD")

        _run_merge_command(gs, work)
        content = (work / ".gitignore").read_text()
        assert ".trinity/\n" not in content or ".trinity/*" in content
        assert ".trinity/*" in content.splitlines(), "supersede→append did not run"
        assert (
            "M .gitignore" in _git(work, "status", "--porcelain").stdout
        ), "the stale-line supersede must be a real edit"


# ---------------------------------------------------------------------------
# 3. merge_gitignore_after_clone readiness gating (AC#1) — R1
# ---------------------------------------------------------------------------
class TestReadinessGating:
    @staticmethod
    def _fast(monkeypatch, gs):
        """Tiny deadline + a fresh loop-unbound semaphore per run."""
        monkeypatch.setattr(gs.gitignore, "_MERGE_READY_TIMEOUT_SECONDS", 0.5)
        monkeypatch.setattr(gs.gitignore, "_MERGE_READY_INTERVAL_SECONDS", 0.02)
        monkeypatch.setattr(gs.gitignore, "_MERGE_EXEC_TIMEOUT_SECONDS", 0.5)
        monkeypatch.setattr(gs.gitignore, "_gitignore_merge_semaphore", asyncio.Semaphore(8))

    def test_waits_then_never_ready_times_out_cleanly(self, gs, monkeypatch):
        """(a)/(c): while /health is down the merge is never attempted; when the
        server never comes up the poll times out WITHOUT raising."""
        self._fast(monkeypatch, gs)
        monkeypatch.setattr(
            gs.gitignore, "_probe_agent_server_ready", AsyncMock(return_value=False)
        )
        exec_mock = AsyncMock()
        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", exec_mock)
        build = MagicMock(side_effect=AssertionError("merge must not build a command"))
        monkeypatch.setattr(gs.gitignore, "_build_gitignore_merge_command", build)

        _run(gs.merge_gitignore_after_clone("a1"))  # returns, no exception
        exec_mock.assert_not_called()
        build.assert_not_called()

    def test_ready_and_git_present_runs_merge_only(self, gs, monkeypatch):
        """(b): once /health responds ∧ .git present, run
        `_build_gitignore_merge_command` against the toplevel and NEVER the
        untrack sweep (`_build_rm_cached_ignored_command`) — creation is PREVENT."""
        self._fast(monkeypatch, gs)
        monkeypatch.setattr(
            gs.gitignore, "_probe_agent_server_ready", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(gs.gitignore, "_container_has_git_dir", AsyncMock(return_value=True))
        monkeypatch.setattr(
            gs.gitignore, "_git_toplevel", AsyncMock(return_value="/home/developer")
        )
        exec_mock = AsyncMock(return_value={"exit_code": 0, "output": ""})
        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", exec_mock)
        real_builder = gs.gitignore._build_gitignore_merge_command  # captured before patching
        merge_spy = MagicMock(side_effect=real_builder)  # returns the REAL command
        monkeypatch.setattr(gs.gitignore, "_build_gitignore_merge_command", merge_spy)
        rm_cached = MagicMock(
            side_effect=AssertionError("untrack must NOT run at creation")
        )
        monkeypatch.setattr(gs.gitignore, "_build_rm_cached_ignored_command", rm_cached)

        _run(gs.merge_gitignore_after_clone("a1"))

        merge_spy.assert_called_once_with("/home/developer")
        rm_cached.assert_not_called()
        exec_mock.assert_awaited_once()
        # The merge command built for the toplevel is exactly what was exec'd.
        assert exec_mock.await_args.kwargs["command"] == real_builder("/home/developer")

    def test_ready_but_no_git_skips(self, gs, monkeypatch):
        """(e): server up but `.git` absent (failed clone) → skip; the toplevel
        is never resolved and the merge never runs."""
        self._fast(monkeypatch, gs)
        monkeypatch.setattr(
            gs.gitignore, "_probe_agent_server_ready", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(gs.gitignore, "_container_has_git_dir", AsyncMock(return_value=False))
        top = AsyncMock(side_effect=AssertionError("toplevel must not be resolved"))
        monkeypatch.setattr(gs.gitignore, "_git_toplevel", top)
        exec_mock = AsyncMock()
        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", exec_mock)

        _run(gs.merge_gitignore_after_clone("a1"))
        top.assert_not_called()
        exec_mock.assert_not_called()

    def test_unresolved_toplevel_skips(self, gs, monkeypatch):
        """(d): `_git_toplevel` None → skip (never merge against a guessed path)."""
        self._fast(monkeypatch, gs)
        monkeypatch.setattr(
            gs.gitignore, "_probe_agent_server_ready", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(gs.gitignore, "_container_has_git_dir", AsyncMock(return_value=True))
        monkeypatch.setattr(gs.gitignore, "_git_toplevel", AsyncMock(return_value=None))
        exec_mock = AsyncMock()
        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", exec_mock)

        _run(gs.merge_gitignore_after_clone("a1"))
        exec_mock.assert_not_called()

    def test_hung_exec_is_wait_for_bounded(self, gs, monkeypatch):
        """(e): a hung `docker exec` cannot pin the fire-and-forget task —
        `asyncio.wait_for` frees it (the task, not the pinned pool thread). If
        the merge exec were not wrapped, this test would hang."""
        self._fast(monkeypatch, gs)
        monkeypatch.setattr(
            gs.gitignore, "_probe_agent_server_ready", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(gs.gitignore, "_container_has_git_dir", AsyncMock(return_value=True))
        monkeypatch.setattr(
            gs.gitignore, "_git_toplevel", AsyncMock(return_value="/home/developer")
        )

        async def _hang(*a, **k):
            await asyncio.sleep(100)

        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", _hang)

        async def _bounded():
            # An outer belt so a MISSING wait_for hangs THIS test (not the suite),
            # then fails on TimeoutError rather than blocking forever.
            await asyncio.wait_for(gs.merge_gitignore_after_clone("a1"), timeout=5)

        _run(_bounded())  # completes well under 5s via the inner 0.5s wait_for

    def test_readiness_poll_runs_outside_semaphore(self, gs, monkeypatch):
        """Concurrency invariant (R1): the `/health` readiness poll must run
        OUTSIDE `_gitignore_merge_semaphore` — the cap bounds only the Docker-exec
        section. A slow-/never-booting agent parked in its readiness wait must not
        head-of-line-block a healthy, already-ready agent's merge exec; otherwise
        a handful of slow agents exhaust the shared 4-thread-pool cap and push a
        fast agent's merge past its own first auto-sync cycle — re-opening the
        exact leak #2069 closes. Pinned with a 1-permit semaphore: pre-fix the
        poll ran INSIDE it, so the fast agent would block forever on `async with`
        and this test would time out."""
        monkeypatch.setattr(gs.gitignore, "_gitignore_merge_semaphore", asyncio.Semaphore(1))
        monkeypatch.setattr(gs.gitignore, "_MERGE_READY_TIMEOUT_SECONDS", 30)
        monkeypatch.setattr(gs.gitignore, "_MERGE_READY_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(gs.gitignore, "_MERGE_EXEC_TIMEOUT_SECONDS", 30)

        slow_parked = asyncio.Event()
        never_ready = asyncio.Event()

        async def _probe(name):
            if name == "slow":
                slow_parked.set()  # the slow poller has entered its readiness wait
                await never_ready.wait()  # ...and stays there (never becomes ready)
                return False
            return True  # "fast" is ready on its first probe

        monkeypatch.setattr(gs.gitignore, "_probe_agent_server_ready", _probe)
        monkeypatch.setattr(gs.gitignore, "_container_has_git_dir", AsyncMock(return_value=True))
        monkeypatch.setattr(
            gs.gitignore, "_git_toplevel", AsyncMock(return_value="/home/developer")
        )
        exec_mock = AsyncMock(return_value={"exit_code": 0, "output": ""})
        monkeypatch.setattr(gs.gitignore, "execute_command_in_container", exec_mock)

        async def _body():
            slow = asyncio.create_task(gs.merge_gitignore_after_clone("slow"))
            # Let the slow poller reach its readiness wait. Pre-fix it has already
            # taken the sole semaphore permit by this point.
            await asyncio.wait_for(slow_parked.wait(), timeout=2)
            fast = asyncio.create_task(gs.merge_gitignore_after_clone("fast"))
            # The fast agent must acquire the permit and finish its Docker-exec
            # merge while the slow agent is still parked. Pre-fix this blocks on
            # `async with _gitignore_merge_semaphore` and wait_for raises.
            await asyncio.wait_for(fast, timeout=2)
            assert not slow.done(), "the slow agent must still be parked in its poll"
            slow.cancel()
            try:
                await slow
            except (asyncio.CancelledError, Exception):
                pass

        _run(_body())

        # Exactly the fast agent's merge ran; the slow one never became ready.
        assert exec_mock.await_count == 1
        expected = gs.gitignore._build_gitignore_merge_command("/home/developer")
        assert exec_mock.await_args.kwargs["command"] == expected


# ---------------------------------------------------------------------------
# 4. _git_auto_sync_baked matrix + creation spawn wiring (AC#6) — R2
# ---------------------------------------------------------------------------
def _cfg(name, *, source_mode=True, ephemeral=False):
    kw = dict(name=name, template="github:o/r", source_mode=source_mode)
    if ephemeral:
        kw["ephemeral"] = EphemeralConfig(max_executions=5)
    return AgentConfig(**kw)


class TestBakePredicate:
    """The single owner of the `GIT_SYNC_AUTO`-baking predicate. The matrix must
    match the in-container loop's ENV gate, NOT the DB-flag block — ephemeral
    ghosts INCLUDED (Codex #2 / R2)."""

    def test_github_non_source_mode_bakes(self, gs):
        assert gs._git_auto_sync_baked(
            _cfg("a", source_mode=False), "o/r", "ghp_x", None
        )

    def test_fork_to_own_source_mode_bakes(self, gs):
        # fork-to-own owns its repo: it auto-pushes even in source_mode.
        assert gs._git_auto_sync_baked(
            _cfg("a", source_mode=True), "o/r", "ghp_x", "up/stream"
        )

    def test_ephemeral_non_source_github_pat_bakes(self, gs):
        # THE R2 case: a ghost bakes GIT_SYNC_AUTO and is never operator-Pushed,
        # so the merge MUST cover it. The predicate ignores `config.ephemeral`.
        assert gs._git_auto_sync_baked(
            _cfg("g", source_mode=False, ephemeral=True), "o/r", "ghp_x", None
        )

    def test_source_mode_non_fork_does_not_bake(self, gs):
        assert not gs._git_auto_sync_baked(
            _cfg("a", source_mode=True), "o/r", "ghp_x", None
        )

    def test_no_repo_local_does_not_bake(self, gs):
        assert not gs._git_auto_sync_baked(
            _cfg("a", source_mode=False), None, "ghp_x", None
        )

    def test_no_pat_does_not_bake(self, gs):
        assert not gs._git_auto_sync_baked(
            _cfg("a", source_mode=False), "o/r", None, None
        )

    def test_ephemeral_source_mode_non_fork_does_not_bake(self, gs):
        assert not gs._git_auto_sync_baked(
            _cfg("g", source_mode=True, ephemeral=True), "o/r", "ghp_x", None
        )


class TestCreationSpawnWiring:
    """`_materialize_agent_files` fires the spawn on EXACTLY `_git_auto_sync_baked`
    (real predicate flows through). AC#6 regression: fork-to-own / non-source /
    ephemeral fire; source-mode / local / no-pat do not."""

    def _materialize(self, crud, gs, monkeypatch, config, repo, fork, pat):
        monkeypatch.setattr(gs, "materialize_persistent_state", AsyncMock())
        monkeypatch.setattr(gs, "materialize_data_paths", AsyncMock())
        spawn = MagicMock()
        monkeypatch.setattr(gs, "spawn_gitignore_merge_after_clone", spawn)
        _run(crud._materialize_agent_files(config, {}, repo, fork, pat, None, "owner"))
        return spawn

    def test_fires_for_github_non_source_mode(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud, gs, monkeypatch, _cfg("a", source_mode=False), "o/r", None, "ghp_x"
        )
        spawn.assert_called_once_with("a")

    def test_fires_for_fork_to_own(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud,
            gs,
            monkeypatch,
            _cfg("a", source_mode=True),
            "o/r",
            "up/stream",
            "ghp_x",
        )
        spawn.assert_called_once_with("a")

    def test_fires_for_ephemeral_non_source_github_pat(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud,
            gs,
            monkeypatch,
            _cfg("g", source_mode=False, ephemeral=True),
            "o/r",
            None,
            "ghp_x",
        )
        spawn.assert_called_once_with("g")

    def test_not_fired_for_source_mode_non_fork(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud, gs, monkeypatch, _cfg("a", source_mode=True), "o/r", None, "ghp_x"
        )
        spawn.assert_not_called()

    def test_not_fired_for_local_no_repo(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud, gs, monkeypatch, _cfg("a", source_mode=False), None, None, None
        )
        spawn.assert_not_called()

    def test_not_fired_for_ephemeral_source_mode(self, crud_gs, monkeypatch):
        crud, gs = crud_gs
        spawn = self._materialize(
            crud,
            gs,
            monkeypatch,
            _cfg("g", source_mode=True, ephemeral=True),
            "o/r",
            None,
            "ghp_x",
        )
        spawn.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Start/recreate spawn wiring (T1) — AST structural guard
# ---------------------------------------------------------------------------
class TestStartRecreateWiring:
    """The T1 fleet-remediation call site lives inline in `start_agent_internal`;
    a full boot harness would be brittle and prove less than an AST guard that
    the spawn is gated on the DB `auto_sync_enabled` flag (which returns the
    persisted owner intent). Ghosts never recreate, so the DB flag is the correct
    and complete gate on this path."""

    def _start_fn(self):
        tree = ast.parse(_LIFECYCLE_SRC.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "start_agent_internal"
            ):
                return node
        raise AssertionError("start_agent_internal not found in lifecycle.py")

    def test_spawn_is_gated_on_db_auto_sync_flag(self):
        fn = self._start_fn()
        gated = False
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            test_calls = {
                n.func.attr
                for n in ast.walk(node.test)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            body_calls = {
                n.func.attr
                for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            }
            if (
                "get_git_auto_sync_enabled" in test_calls
                and "spawn_gitignore_merge_after_clone" in body_calls
            ):
                gated = True
        assert gated, (
            "start_agent_internal must fire spawn_gitignore_merge_after_clone "
            "gated on db.get_git_auto_sync_enabled (T1 fleet remediation)"
        )

    def test_spawn_not_called_unconditionally(self):
        """Belt: the spawn appears nowhere OUTSIDE such a gate — a source-mode /
        auto-sync-off agent (DB flag False) must never trigger it."""
        fn = self._start_fn()
        # Every spawn call must have an enclosing `if get_git_auto_sync_enabled`.
        gated_spawn_lines = set()
        for node in ast.walk(fn):
            if not isinstance(node, ast.If):
                continue
            if any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get_git_auto_sync_enabled"
                for n in ast.walk(node.test)
            ):
                for n in ast.walk(node):
                    if (
                        isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "spawn_gitignore_merge_after_clone"
                    ):
                        gated_spawn_lines.add(n.lineno)
        all_spawn_lines = {
            n.lineno
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "spawn_gitignore_merge_after_clone"
        }
        assert all_spawn_lines, "spawn_gitignore_merge_after_clone not wired at all"
        assert all_spawn_lines == gated_spawn_lines, (
            "an ungated spawn_gitignore_merge_after_clone in start_agent_internal "
            f"(gated: {gated_spawn_lines}, all: {all_spawn_lines})"
        )
