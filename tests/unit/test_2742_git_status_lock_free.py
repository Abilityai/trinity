"""#2742 — the sync-health poll must not take, or orphan, `.git/index.lock`.

`GET /api/git/status` is driven like a loop: the backend polls it every 60 s for
every git-enabled agent, the UI git panel polls it while open, and the MCP
`get_git_status` tool hits the same route. It ran ~8 bare `subprocess.run` git
children **on the agent's event loop**, one of which — `git status --porcelain` —
takes `.git/index.lock` on every invocation in order to refresh the index. So the
platform raced the agent's own `git add` ~2x/min in every workspace, outside
`_REPO_LOCK`, and a sweep-killed child orphaned a 0-byte lock that then failed
every later git write *silently* (a stale lock does not fail `git status`; only
writers surface it).

This file covers the agent-server half:

  AC1 — the read takes no index lock, and every child is sweep-registered
  AC2 — concurrent callers coalesce onto ONE computation and ONE `git fetch`
  AC3 — a stuck lock is REPORTED, never deleted, and the boot reap is observable
  AC4 — an agent commit survives a concurrent poll

Two test-construction rules this file obeys, both learned by measurement:

  - **Assert the lock SIGHTING, never an index rewrite.** "A plain status
    afterwards rewrites `.git/index`" fails 3/3 once fixture setup crosses ~1 s
    (git's racily-clean rule: a fresh fixture's index mtime shares a second with
    its files, so every status rewrites; after a settle, plain status stops
    rewriting while STILL taking the lock). Under CI's `-n auto` on 4 CPUs,
    setup crossing 1 s is ordinary.
  - **Classify writer failures by `"index.lock" in stderr`, never by return
    code.** `git commit` exits 1 with EMPTY stderr ("nothing to commit" on
    stdout) whenever a round writes content identical to the previous one —
    observed in 5/60 trials — which under `check=True` is indistinguishable from
    a lock failure. Every writer round therefore writes `time.time_ns()`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parents[2] / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Explicit file-based loader, evicting any previously cached `agent_server`
# (shadow or real) so it wins regardless of sys.path order — same pattern as
# test_1595_git_maintenance.py / test_agent_server_auto_sync.py.
for _mod in list(sys.modules):
    if _mod == "agent_server" or _mod.startswith("agent_server."):
        sys.modules.pop(_mod, None)

_AS_INIT = _BASE_IMAGE / "agent_server" / "__init__.py"
_as_spec = importlib.util.spec_from_file_location(
    "agent_server", str(_AS_INIT),
    submodule_search_locations=[str(_BASE_IMAGE / "agent_server")],
)
_as_mod = importlib.util.module_from_spec(_as_spec)
sys.modules["agent_server"] = _as_mod
_as_spec.loader.exec_module(_as_mod)

from agent_server.routers import git as git_mod  # noqa: E402

pytestmark = pytest.mark.unit

_STUBBED_MODULE_NAMES = [
    name for name in sys.modules
    if name == "agent_server" or name.startswith("agent_server.")
]


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value


# ---------------------------------------------------------------------------
# Fixtures — real throwaway git repos (local + bare remote, so `git fetch
# origin` works offline), borrowed from test_1595_git_maintenance.py.
# ---------------------------------------------------------------------------


def _run(cmd, cwd, **kw):
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, timeout=60, **kw
    )


def _init_repo(local_dir: Path, remote_dir: Path) -> None:
    _run(["git", "init", "--bare", "-b", "main"], remote_dir)
    _run(["git", "init", "-b", "main"], local_dir)
    _run(["git", "config", "user.email", "test@test.com"], local_dir)
    _run(["git", "config", "user.name", "Test"], local_dir)
    _run(["git", "config", "commit.gpgsign", "false"], local_dir)
    _run(["git", "remote", "add", "origin", str(remote_dir)], local_dir)
    (local_dir / "README.md").write_text("hello")
    _run(["git", "add", "."], local_dir)
    _run(["git", "commit", "-m", "initial"], local_dir)
    _run(["git", "push", "-u", "origin", "main"], local_dir)


@pytest.fixture
def repo(tmp_path):
    local = tmp_path / "local"
    remote = tmp_path / "remote"
    local.mkdir()
    remote.mkdir()
    _init_repo(local, remote)
    (local / ".trinity").mkdir()
    yield local
    shutil.rmtree(local, ignore_errors=True)
    shutil.rmtree(remote, ignore_errors=True)


@pytest.fixture
def status_home(repo, monkeypatch):
    """Point the status handler at the throwaway repo.

    `_STATUS_HOME_DIR` is a module constant; repointing it is exactly the case
    that makes a module-global in-flight slot cross-serve between tests, so the
    slot is asserted clear on the way out.
    """
    monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", repo)
    yield repo


class _RecordingRegistry:
    def __init__(self):
        self.events = []

    def add_transient_pid(self, pid, *, ttl_seconds=None):
        self.events.append(("add", pid, ttl_seconds))

    def remove_transient_pid(self, pid):
        self.events.append(("remove", pid))


@pytest.fixture
def registry(monkeypatch):
    """Own the process-registry module key so `run_registered`'s call-time lazy
    import resolves to our recorder."""
    rec = _RecordingRegistry()
    stub = types.ModuleType("agent_server.services.process_registry")
    stub.get_process_registry = lambda: rec
    monkeypatch.setitem(
        sys.modules, "agent_server.services.process_registry", stub
    )
    return rec


@pytest.fixture
def recorded_argvs(monkeypatch):
    """Record every argv `run_registered` is asked to run, still running it."""
    seen = []
    real = git_mod.run_registered

    def _spy(argv, **kwargs):
        seen.append(list(argv))
        return real(argv, **kwargs)

    monkeypatch.setattr(git_mod, "run_registered", _spy)
    return seen


# ---------------------------------------------------------------------------
# AC1 — the poll acquires no index lock
# ---------------------------------------------------------------------------


class TestStatusReadIsLockFree:

    def test_status_argv_uses_no_optional_locks(self, status_home, recorded_argvs):
        """Deterministic form of AC1: the flagged argv is issued and the plain
        one is not. Asserted on argv rather than on behaviour because argv is the
        thing a future edit would silently change."""
        git_mod._compute_git_status(status_home)

        assert ["git", "--no-optional-locks", "status", "--porcelain"] in recorded_argvs
        assert ["git", "status", "--porcelain"] not in recorded_argvs, (
            "the unflagged status argv is what takes .git/index.lock"
        )

    def test_the_flag_is_scoped_to_the_status_read(self):
        """The auto-sync cycle's own status must KEEP the plain form: it runs
        under `_REPO_LOCK` right after `git add -A`, proceeds to commit, and
        wants the refreshed stat cache. A blanket sed over all five
        `--porcelain` sites is the mistake this pins against."""
        src = (
            _BASE_IMAGE / "agent_server" / "routers" / "git.py"
        ).read_text(encoding="utf-8")
        assert src.count('"--no-optional-locks"') == 1, (
            "exactly one call site may carry the flag (the status read)"
        )
        assert '["git", "status", "--porcelain"]' in src, (
            "the auto-sync cycle's status must keep the plain, index-refreshing form"
        )

    def test_status_read_is_never_seen_holding_the_index_lock(self, status_home):
        """The lock PROPERTY, with its control — the shape that has teeth.

        A busy-poll existence sampler runs while each argv runs. The control (the
        plain argv) must be SEEN holding `.git/index.lock`; the shipped argv must
        never be. Asserting the sighting rather than an index rewrite is
        deliberate: git's racily-clean rule makes a rewrite assertion fail once
        fixture setup crosses ~1 s, which is ordinary under CI's `-n auto`.
        """
        lock = status_home / ".git" / "index.lock"
        # Make the index stat-dirty so a refresh has work to do.
        for i in range(200):
            (status_home / f"f{i}.txt").write_text(f"x{i}")
        _run(["git", "add", "-A"], status_home)
        _run(["git", "commit", "-m", "bulk"], status_home)
        for i in range(200):
            os.utime(status_home / f"f{i}.txt", None)

        def _sample(argv):
            import threading
            sightings = {"n": 0}
            stop = threading.Event()

            def _poll():
                while not stop.is_set():
                    if lock.exists():
                        sightings["n"] += 1

            t = threading.Thread(target=_poll, daemon=True)
            t.start()
            try:
                for _ in range(6):
                    _run(argv, status_home)
            finally:
                stop.set()
                t.join(timeout=5)
            return sightings["n"]

        plain = _sample(["git", "status", "--porcelain"])
        flagged = _sample(["git", "--no-optional-locks", "status", "--porcelain"])

        assert plain > 0, (
            "control lost its teeth: the plain argv was never observed holding "
            "index.lock, so a zero for the flagged argv proves nothing"
        )
        assert flagged == 0, (
            f"the flagged argv was observed holding index.lock {flagged}x"
        )

    def test_status_read_never_rewrites_the_index(self, status_home):
        """Corollary, asserted on the shipped path only (no rewrite CONTROL —
        see the module docstring: the control form of this assertion is a live
        flake). Dirty the tree with a fresh, never-recorded mtime first:
        re-applying an mtime the index already holds does not re-dirty it."""
        import time
        index = status_home / ".git" / "index"
        (status_home / "touched.txt").write_text("v1")
        _run(["git", "add", "-A"], status_home)
        _run(["git", "commit", "-m", "touched"], status_home)
        stamp = time.time() - 5000
        os.utime(status_home / "touched.txt", (stamp, stamp))

        before = (index.stat().st_mtime_ns, index.stat().st_ino)
        git_mod._compute_git_status(status_home)
        after = (index.stat().st_mtime_ns, index.stat().st_ino)

        assert before == after, "the status read rewrote .git/index"
        assert not (status_home / ".git" / "index.lock").exists()


class TestStatusChildrenAreSweepRegistered:
    """`--no-optional-locks` closes only the index-lock half. A sweep tick
    straddling the 30 s `git fetch` still SIGKILLs the child, and a killed fetch
    orphans `FETCH_HEAD.lock` / `packed-refs.lock` — which NO reaper covers, not
    even `startup.sh`'s (its find is scoped to refs/ and logs/)."""

    def test_every_status_child_is_registered(self, status_home, registry, monkeypatch):
        """No bare `subprocess.run` survives on the status path: the module's
        `subprocess.run` is made to raise for the duration, so any unconverted
        site fails loudly rather than merely going unregistered."""
        def _boom(*a, **k):
            raise AssertionError(
                "a bare subprocess.run survived on the status path: "
                f"{a[0] if a else k}"
            )

        monkeypatch.setattr(git_mod.subprocess, "run", _boom)
        git_mod._compute_git_status(status_home)

        adds = [e for e in registry.events if e[0] == "add"]
        removes = [e for e in registry.events if e[0] == "remove"]
        assert len(adds) >= 7, f"expected >=7 registered children, got {len(adds)}"
        assert len(adds) == len(removes), "every registration must be paired"

    @pytest.mark.asyncio
    async def test_the_locked_paths_register_their_helper_children_too(
        self, status_home, registry, monkeypatch
    ):
        """Stated, not stumbled on. `_compute_ahead_behind`, `_get_pull_branch`
        and `_persist_last_remote_sha` are ALSO reached from the mutating paths —
        `_conflict_response` (the 409 arm of every locked endpoint),
        `sync_to_github` and `pull_from_github`. Converting the helpers therefore
        sweep-registers three children there as well. That is desirable (#1595's
        whole point) but it must be an intended decision with a test, not a
        surprise a reviewer finds.
        """
        monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", status_home)
        registry.events.clear()

        git_mod._conflict_response(
            status_code=409,
            detail="conflict",
            conflict_type="test",
            stderr="boom",
            home_dir=status_home,
            branch="main",
        )
        assert any(e[0] == "add" for e in registry.events), (
            "_compute_ahead_behind on the 409 path must register its child"
        )

        registry.events.clear()
        git_mod._get_pull_branch("trinity/agent/abc", status_home)
        assert any(e[0] == "add" for e in registry.events)

        registry.events.clear()
        git_mod._persist_last_remote_sha("main", status_home)
        assert any(e[0] == "add" for e in registry.events)


class TestRemoteUrlRedaction:

    def test_a_tokenized_non_github_origin_is_redacted(self, status_home):
        """The old shape special-cased `@github.com` and returned everything else
        VERBATIM — so a GitLab / GHES / host-rewritten origin put a live PAT in
        the response body, proxied unmodified by `git_service.get_git_status` to
        the UI and the MCP tool."""
        _run(
            ["git", "remote", "set-url", "origin",
             "https://oauth2:ghp_SECRETVALUE123@gitlab.example.com/org/repo.git"],
            status_home,
        )
        payload = git_mod._compute_git_status(status_home)

        assert "ghp_SECRETVALUE123" not in payload["remote_url"]
        assert "oauth2" not in payload["remote_url"]
        assert "gitlab.example.com/org/repo.git" in payload["remote_url"]

    def test_a_tokenized_github_origin_is_still_redacted(self, status_home):
        _run(
            ["git", "remote", "set-url", "origin",
             "https://oauth2:ghp_SECRETVALUE123@github.com/org/repo.git"],
            status_home,
        )
        payload = git_mod._compute_git_status(status_home)
        assert "ghp_SECRETVALUE123" not in payload["remote_url"]
        assert "github.com/org/repo.git" in payload["remote_url"]


class TestSyncStateReadIsBounded:
    """`sync-state.json` is FULLY agent-authored (`merged.update(data)` merges it
    wholesale) and the backend reads every agent concurrently, once a minute."""

    def test_an_oversized_sync_state_degrades_to_defaults(self, repo, caplog):
        path = repo / ".trinity" / "sync-state.json"
        path.write_text('{"last_sync_status": "success", "pad": "'
                        + "x" * (git_mod._SYNC_STATE_MAX_BYTES + 10) + '"}')

        state = git_mod._read_sync_state_file(repo)

        assert state == dict(git_mod._SYNC_STATE_DEFAULT)
        assert state["last_sync_status"] == "never", (
            "an oversized document must not be parsed at all"
        )

    def test_a_normal_sync_state_is_still_read(self, repo):
        path = repo / ".trinity" / "sync-state.json"
        path.write_text('{"last_sync_status": "success", "consecutive_failures": 0}')
        assert git_mod._read_sync_state_file(repo)["last_sync_status"] == "success"


# ---------------------------------------------------------------------------
# AC2 — concurrent callers share ONE computation and ONE `git fetch`
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_inflight_slot():
    """The slot is module-global; every AC2 test must leave it empty."""
    git_mod._STATUS_INFLIGHT.clear()
    yield git_mod._STATUS_INFLIGHT
    assert not git_mod._STATUS_INFLIGHT, (
        "the in-flight slot leaked — a later test would be served this repo's "
        "payload for a different repo"
    )


class _BlockingFetch:
    """Stand-in for `run_registered` that blocks the FIRST `git fetch` on an
    event and counts every fetch it is asked to run."""

    def __init__(self, real, gate):
        self.real = real
        self.gate = gate
        self.fetches = 0

    def __call__(self, argv, **kwargs):
        argv = list(argv)
        if argv[:3] == ["git", "fetch", "origin"]:
            self.fetches += 1
            self.gate.wait(timeout=10)
        return self.real(argv, **kwargs)


class TestStatusIsSingleFlighted:

    @pytest.mark.asyncio
    async def test_concurrent_status_calls_share_one_fetch(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        """AC2 proper. Five concurrent callers, one `git fetch origin`.

        The fetch is held open on an Event so all five callers are genuinely
        in flight at once — without the gate the first could complete before the
        fifth arrives and the test would pass for the wrong reason.
        """
        import asyncio
        import threading

        gate = threading.Event()
        spy = _BlockingFetch(git_mod.run_registered, gate)
        monkeypatch.setattr(git_mod, "run_registered", spy)

        callers = [git_mod.get_git_status() for _ in range(5)]
        task = asyncio.gather(*callers)
        await asyncio.sleep(0.2)   # let all five reach the slot
        gate.set()
        results = await task

        assert spy.fetches == 1, f"expected 1 fetch, got {spy.fetches}"
        assert all(r is results[0] for r in results), (
            "every caller must receive the SAME payload object — a second "
            "computation would produce a second dict"
        )

    @pytest.mark.asyncio
    async def test_only_one_thread_is_used_per_inflight_computation(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        """The #2433 regression guard, and the assertion that stops the rejected
        shape from being reintroduced.

        `asyncio.to_thread` uses the loop's DEFAULT executor — 6 threads on a
        2-vCPU agent — which also carries `ctx.terminate`, auto-sync and
        pipe-close. Routing every caller through a thread (rather than
        coalescing on the loop) parks that pool and stalls execution
        termination. Five callers must cost exactly ONE `to_thread`.
        """
        import asyncio

        calls = {"n": 0}
        real_to_thread = asyncio.to_thread

        async def _counting(fn, *a, **k):
            calls["n"] += 1
            return await real_to_thread(fn, *a, **k)

        monkeypatch.setattr(git_mod.asyncio, "to_thread", _counting)

        await asyncio.gather(*[git_mod.get_git_status() for _ in range(5)])

        assert calls["n"] == 1, f"expected exactly 1 to_thread call, got {calls['n']}"

    @pytest.mark.asyncio
    async def test_follower_cancellation_does_not_kill_the_leader(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        """The `shield` test. A disconnecting or timing-out follower must not
        cancel the computation everybody else is waiting on."""
        import asyncio
        import threading

        gate = threading.Event()
        spy = _BlockingFetch(git_mod.run_registered, gate)
        monkeypatch.setattr(git_mod, "run_registered", spy)

        tasks = [asyncio.ensure_future(git_mod.get_git_status()) for _ in range(5)]
        await asyncio.sleep(0.2)

        for t in tasks[:3]:          # three clients disconnect mid-flight
            t.cancel()
        gate.set()

        survivors = await asyncio.gather(*tasks[3:])
        assert len(survivors) == 2
        assert all(s["git_enabled"] for s in survivors)
        assert spy.fetches == 1

    @pytest.mark.asyncio
    async def test_leader_exception_propagates_to_every_follower_and_clears_slot(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        """A 504 must fan out to every waiter AND the slot must clear, so the
        next poll is a fresh computation rather than a permanently wedged read."""
        import asyncio
        import threading
        from fastapi import HTTPException

        gate = threading.Event()
        real = git_mod.run_registered

        def _boom(argv, **kwargs):
            argv = list(argv)
            if argv[:3] == ["git", "fetch", "origin"]:
                gate.wait(timeout=10)
                raise subprocess.TimeoutExpired(argv, 30)
            return real(argv, **kwargs)

        monkeypatch.setattr(git_mod, "run_registered", _boom)

        tasks = [asyncio.ensure_future(git_mod.get_git_status()) for _ in range(3)]
        await asyncio.sleep(0.2)
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)

        assert len(results) == 3
        for r in results:
            assert isinstance(r, HTTPException) and r.status_code == 504

        assert not git_mod._STATUS_INFLIGHT, "slot must clear after a failure"

        # And the next call recomputes cleanly on the real runner.
        monkeypatch.setattr(git_mod, "run_registered", real)
        again = await git_mod.get_git_status()
        assert again["git_enabled"] is True

    @pytest.mark.asyncio
    async def test_follower_wait_bound_maps_to_504(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        """A follower that waits past its bound gets a 504 — and the leader is
        untouched and still completes (that is what `shield` buys)."""
        import asyncio
        import threading
        from fastapi import HTTPException

        gate = threading.Event()
        spy = _BlockingFetch(git_mod.run_registered, gate)
        monkeypatch.setattr(git_mod, "run_registered", spy)
        monkeypatch.setattr(git_mod, "_STATUS_FOLLOWER_WAIT_SECONDS", 0.1)

        leader = asyncio.ensure_future(git_mod.get_git_status())
        await asyncio.sleep(0.05)

        with pytest.raises(HTTPException) as exc:
            await git_mod.get_git_status()
        assert exc.value.status_code == 504

        gate.set()
        # The leader's own await also hit the 0.1s follower bound, so it too
        # sees a 504 — but the COMPUTATION survived and released the slot.
        with pytest.raises(HTTPException):
            await leader
        for _ in range(50):
            if not git_mod._STATUS_INFLIGHT:
                break
            await asyncio.sleep(0.05)
        assert spy.fetches == 1

    @pytest.mark.asyncio
    async def test_a_missing_git_dir_short_circuits_before_the_slot(
        self, tmp_path, clean_inflight_slot, monkeypatch
    ):
        """The early return must not claim the slot — a non-git agent polled
        every 60 s would otherwise churn futures for nothing."""
        monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", tmp_path)
        payload = await git_mod.get_git_status()
        assert payload["git_enabled"] is False
        assert not git_mod._STATUS_INFLIGHT

    @pytest.mark.asyncio
    async def test_the_payload_is_stamped_with_its_own_age(
        self, status_home, clean_inflight_slot
    ):
        """`computed_at` is how the bounded staleness of coalescing is made
        legible rather than denied."""
        from datetime import datetime, timezone
        payload = await git_mod.get_git_status()
        stamped = datetime.fromisoformat(payload["computed_at"])
        assert stamped.tzinfo is not None, "computed_at must be timezone-aware"
        assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 60

    @pytest.mark.asyncio
    async def test_the_slot_never_cross_serves_two_repos(
        self, tmp_path, clean_inflight_slot, monkeypatch
    ):
        """The R8 case, made a test rather than left implicit: the slot is a
        module global while the computation takes a parameter. Keyed on the
        resolved home path, two repos can never be served each other's payload."""
        import asyncio

        repos = []
        for name in ("one", "two"):
            local, remote = tmp_path / name, tmp_path / f"{name}-remote"
            local.mkdir()
            remote.mkdir()
            _init_repo(local, remote)
            (local / ".trinity").mkdir()
            (local / f"{name}-marker.txt").write_text("m")
            repos.append(local)

        payloads = []
        for local in repos:
            monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", local)
            payloads.append(await git_mod.get_git_status())
            # slot released between flights
            await asyncio.sleep(0)

        paths = [{c["path"] for c in p["changes"]} for p in payloads]
        assert paths[0] == {"one-marker.txt"}
        assert paths[1] == {"two-marker.txt"}
