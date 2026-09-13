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


# ---------------------------------------------------------------------------
# AC3 — a stuck lock recovers, and the recovery is observable
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_lock_sightings():
    """The sighting ledger is a module global; every test starts from empty."""
    git_mod._LOCK_SIGHTINGS.clear()
    yield
    git_mod._LOCK_SIGHTINGS.clear()


def _tick(home_dir, times=1):
    """Run the observer N times, as N successive status polls would."""
    out = None
    for _ in range(times):
        out = git_mod._index_lock_stuck(home_dir)
    return out


def _age_first_seen(path: Path, seconds: float) -> None:
    """Backdate a candidate's monotonic first-sighting so a test does not have
    to wait 15 real minutes. Reaches into the ledger deliberately: the detector's
    clock is `time.monotonic()` precisely so it CANNOT be moved from outside by
    changing the wall clock."""
    entry = git_mod._LOCK_SIGHTINGS[str(path)]
    entry["first_seen"] -= seconds


class TestStartupReapIsAnnounced:
    """The /verify-local agent stage boots `local:test-echo`, so `startup.sh`
    never runs there. The block is therefore proved by extracting it and running
    it — the standing gotcha for this file."""

    @staticmethod
    def _extract_reap_block() -> str:
        src = (_BASE_IMAGE / "startup.sh").read_text(encoding="utf-8")
        start = src.index("if [ -d /home/developer/.git ]; then")
        # The block ends at the first line that is exactly "fi" at column 0
        # after the opening `if`.
        end = src.index("\nfi\n", start) + len("\nfi\n")
        block = src[start:end]
        assert "TRINITY_REAPED_LOCKS" in block, "extracted the wrong block"
        return block

    def _run_block(self, tmp_path, home):
        """Run the SHIPPED block with /home/developer rewritten to a fixture."""
        block = self._extract_reap_block().replace("/home/developer", str(home))
        script = tmp_path / "reap.sh"
        script.write_text("#!/bin/bash\nset -u\n" + block + "\n")
        return subprocess.run(
            ["bash", str(script)], capture_output=True, text=True, timeout=60
        )

    def test_it_removes_and_announces_an_index_lock(self, tmp_path):
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)
        lock = home / ".git" / "index.lock"
        lock.write_text("")

        res = self._run_block(tmp_path, home)

        assert res.returncode == 0, res.stderr
        assert not lock.exists(), "the lock must still be removed"
        assert "reaped stale git lock at container start" in res.stdout
        assert "index.lock" in res.stdout

    def test_it_says_nothing_when_there_was_nothing_to_reap(self, tmp_path):
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)

        res = self._run_block(tmp_path, home)

        assert res.returncode == 0, res.stderr
        assert "reaped" not in res.stdout, (
            f"a clean boot must be quiet, got: {res.stdout!r}"
        )
        assert not (home / ".trinity" / "lock-recovery.json").exists()

    def test_it_drops_a_marker_the_agent_server_can_read(self, tmp_path):
        home = tmp_path / "home"
        (home / ".git").mkdir(parents=True)
        (home / ".git" / "index.lock").write_text("")

        res = self._run_block(tmp_path, home)

        marker = home / ".trinity" / "lock-recovery.json"
        assert marker.exists(), res.stdout + res.stderr
        import json as _json
        data = _json.loads(marker.read_text())
        assert "index.lock" in data["locks"]
        assert data["at"].endswith("Z")

    def test_it_reaps_submodule_and_worktree_index_locks(self, tmp_path):
        """Before #2742 these were covered by NOTHING — not the boot reap (its
        find is scoped to refs/ and logs/), not the per-cycle reaper — so a
        submodule wedge was permanent and survived every restart."""
        home = tmp_path / "home"
        sub = home / ".git" / "modules" / "vendor"
        wt = home / ".git" / "worktrees" / "feature"
        sub.mkdir(parents=True)
        wt.mkdir(parents=True)
        (sub / "index.lock").write_text("")
        (wt / "index.lock").write_text("")

        res = self._run_block(tmp_path, home)

        assert res.returncode == 0, res.stderr
        assert not (sub / "index.lock").exists()
        assert not (wt / "index.lock").exists()
        assert "modules" in res.stdout and "worktrees" in res.stdout


class TestLockRecoveryReachesThePlatform:

    def test_the_marker_becomes_sync_state_without_faking_a_sync(self, repo):
        """`_write_sync_state_file` stamps `last_sync_at = now`, which would mark
        a never-synced agent as freshly synced and turn its dashboard dot green.
        The dedicated writer must not do that."""
        marker = repo / ".trinity" / "lock-recovery.json"
        marker.write_text('{"at": "2026-09-13T10:00:00Z", "locks": "index.lock"}')

        record = git_mod._record_lock_recovery(repo)

        assert record["locks"] == "index.lock"
        state = git_mod._read_sync_state_file(repo)
        assert state["last_lock_recovery"]["locks"] == "index.lock"
        assert state["last_sync_status"] == "never", "must not fake a sync"
        assert state["last_sync_at"] is None, "must not stamp last_sync_at"

    def test_the_record_is_metrics_only_over_an_existing_failure_state(self, repo):
        git_mod._write_sync_state_file(
            repo, "failed", last_error_summary="push rejected"
        )
        before = git_mod._read_sync_state_file(repo)

        (repo / ".trinity" / "lock-recovery.json").write_text(
            '{"at": "2026-09-13T10:00:00Z", "locks": "index.lock"}'
        )
        git_mod._record_lock_recovery(repo)
        after = git_mod._read_sync_state_file(repo)

        for field in ("consecutive_failures", "last_sync_status",
                      "last_sync_at", "last_error_summary"):
            assert after[field] == before[field], f"{field} must be untouched"
        assert after["last_lock_recovery"] is not None
        assert not list((repo / ".trinity").glob("*.json.tmp")), "no temp left behind"

    def test_the_marker_is_consumed_once(self, repo):
        """Reported once per episode, not every minute forever."""
        (repo / ".trinity" / "lock-recovery.json").write_text(
            '{"at": "2026-09-13T10:00:00Z", "locks": "index.lock"}'
        )
        assert git_mod._record_lock_recovery(repo) is not None
        assert git_mod._record_lock_recovery(repo) is None
        assert not (repo / ".trinity" / "lock-recovery.json").exists()

    @pytest.mark.parametrize("payload", [
        "not json at all",
        "[]",
        '{"at": {"nested": 1}, "locks": ["a", "b"]}',
        '{"at": "' + "x" * 500 + '"}',
    ])
    def test_a_malformed_marker_never_raises_and_is_cleared(self, repo, payload):
        (repo / ".trinity" / "lock-recovery.json").write_text(payload)
        git_mod._record_lock_recovery(repo)   # must not raise
        assert not (repo / ".trinity" / "lock-recovery.json").exists()

    def test_the_status_response_carries_the_recovery(self, status_home):
        (status_home / ".trinity" / "lock-recovery.json").write_text(
            '{"at": "2026-09-13T10:00:00Z", "locks": "index.lock"}'
        )
        payload = git_mod._compute_git_status(status_home)
        assert payload["lock_recovery"]["locks"] == "index.lock"
        assert payload["sync_state"]["last_lock_recovery"] is not None


class TestStuckLockIsReportedNotDeleted:

    def test_a_stable_lock_is_reported_and_still_there_afterwards(self, repo):
        """The 'still there' assertion is the point of this whole design."""
        lock = repo / ".git" / "index.lock"
        lock.write_text("")

        assert _tick(repo) is None, "one sighting is never enough"
        assert _tick(repo) is None, "two sightings are never enough"
        _age_first_seen(lock, git_mod._STUCK_LOCK_MIN_AGE_SECONDS + 1)
        report = _tick(repo)

        assert report is not None
        assert report["path"] == "index.lock"
        assert report["sightings"] >= git_mod._STUCK_LOCK_MIN_SIGHTINGS
        assert lock.exists(), "the observer must NEVER remove the lock"

    def test_a_long_stable_span_alone_is_not_enough(self, repo):
        """Both gates, not either: the sighting COUNT and the span."""
        lock = repo / ".git" / "index.lock"
        lock.write_text("")
        _tick(repo)
        _age_first_seen(lock, git_mod._STUCK_LOCK_MIN_AGE_SECONDS * 10)
        assert _tick(repo) is None, (
            "two sightings must not report however old the span looks"
        )

    def test_a_busy_lock_is_never_reported(self, repo):
        """Encodes the measurement that cut the delete: a finishing git replaces
        the lock, so the inode CHANGES. A 155-second healthy `git add` under a
        clean filter must never be called a wedge, however old any single
        sighting looks."""
        lock = repo / ".git" / "index.lock"
        for round_ in range(6):
            lock.unlink(missing_ok=True)
            lock.write_text("")          # a new inode each round
            out = git_mod._index_lock_stuck(repo)
            if str(lock) in git_mod._LOCK_SIGHTINGS:
                _age_first_seen(lock, git_mod._STUCK_LOCK_MIN_AGE_SECONDS * 2)
            assert out is None, f"reported a changing candidate on round {round_}"

    def test_a_zero_byte_lock_is_not_by_itself_evidence(self, repo):
        """0 bytes is the signature of a LIVE writer for ~100% of its life (29 s
        at 60 000 files, 155 s under a clean filter). The detector must not treat
        emptiness as a signal at all."""
        lock = repo / ".git" / "index.lock"
        lock.write_text("")
        assert lock.stat().st_size == 0
        assert _tick(repo, times=git_mod._STUCK_LOCK_MIN_SIGHTINGS + 2) is None, (
            "size must not shortcut the stability requirement"
        )

    def test_a_forward_clock_step_cannot_manufacture_a_wedge(self, repo, monkeypatch):
        """A forward NTP step or a live migration ages every existing lock at
        once — the one clock direction that makes a false positive MORE likely.
        The detector's clock is `time.monotonic()`, which a wall-clock jump
        cannot move."""
        lock = repo / ".git" / "index.lock"
        lock.write_text("")
        _tick(repo)

        real_time = git_mod.time.time
        monkeypatch.setattr(git_mod.time, "time", lambda: real_time() + 86400)

        assert _tick(repo) is None
        assert _tick(repo) is None, (
            "a wall-clock jump must not satisfy the stability window"
        )

    def test_a_fresh_workspace_reports_nothing(self, repo):
        assert git_mod._index_lock_stuck(repo) is None
        assert git_mod._LOCK_SIGHTINGS == {}

    def test_the_ledger_forgets_a_lock_that_goes_away(self, repo):
        lock = repo / ".git" / "index.lock"
        lock.write_text("")
        _tick(repo)
        assert git_mod._LOCK_SIGHTINGS
        lock.unlink()
        _tick(repo)
        assert git_mod._LOCK_SIGHTINGS == {}, "the ledger must not grow"


class TestObserverResolvesTheRealGitDir:

    def test_it_follows_a_gitdir_file(self, tmp_path):
        """`.git` is a FILE for a linked worktree and for a submodule, both of
        which an agent can create with one command. Assuming a directory makes
        the observer look where the lock provably is not."""
        home = tmp_path / "wt"
        home.mkdir()
        real = tmp_path / "store" / "worktrees" / "feature"
        real.mkdir(parents=True)
        (home / ".git").write_text(f"gitdir: {real}\n")
        (real / "index.lock").write_text("")

        _tick(home)
        _tick(home)
        _age_first_seen(real / "index.lock",
                        git_mod._STUCK_LOCK_MIN_AGE_SECONDS + 1)
        report = _tick(home)

        assert report is not None
        assert report["path"] == "index.lock"

    def test_it_finds_a_submodule_lock(self, tmp_path):
        home = tmp_path / "home"
        sub = home / ".git" / "modules" / "vendor"
        sub.mkdir(parents=True)
        (sub / "index.lock").write_text("")

        _tick(home)
        _tick(home)
        _age_first_seen(sub / "index.lock",
                        git_mod._STUCK_LOCK_MIN_AGE_SECONDS + 1)
        report = _tick(home)

        assert report is not None
        assert report["path"] == "modules/vendor/index.lock"

    def test_it_finds_a_linked_worktree_lock_under_a_normal_git_dir(self, tmp_path):
        home = tmp_path / "home"
        wt = home / ".git" / "worktrees" / "feature"
        wt.mkdir(parents=True)
        (wt / "index.lock").write_text("")

        _tick(home)
        _tick(home)
        _age_first_seen(wt / "index.lock",
                        git_mod._STUCK_LOCK_MIN_AGE_SECONDS + 1)
        assert _tick(home)["path"] == "worktrees/feature/index.lock"

    def test_a_symlinked_git_is_skipped(self, tmp_path):
        """A follow-stat reasons about one file while any action would touch
        another. This code only reads, and still refuses to be in that position."""
        home = tmp_path / "home"
        home.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "index.lock").write_text("")
        (home / ".git").symlink_to(elsewhere, target_is_directory=True)

        assert _tick(home, times=5) is None
        assert (elsewhere / "index.lock").exists()

    def test_a_missing_git_dir_is_not_an_error(self, tmp_path):
        assert git_mod._index_lock_stuck(tmp_path / "nope") is None


class TestTheObserverCannotDarkenTheFeed:

    def test_a_raising_lstat_still_yields_a_status_payload(
        self, status_home, monkeypatch
    ):
        """The observer sits inside `_compute_git_status`'s `try`, whose tail is
        `HTTPException(500)` — and `_fetch_git_status` treats ANY non-200 as
        None and writes nothing. So one EACCES would silently stop the agent's
        sync-health row advancing, and `sync_failing` would never fire either."""
        real_lstat = git_mod.os.lstat

        def _boom(path, *a, **k):
            if ".git" in str(path):
                raise PermissionError(13, "Permission denied", str(path))
            return real_lstat(path, *a, **k)

        monkeypatch.setattr(git_mod.os, "lstat", _boom)

        payload = git_mod._compute_git_status(status_home)

        assert payload["git_enabled"] is True
        assert payload["index_lock_stuck"] is None

    def test_an_unwritable_sync_state_still_yields_a_status_payload(
        self, status_home, monkeypatch
    ):
        """The recovery fold is the other observability write on this path, and
        it too must be unable to fail the read — the marker lives in an
        agent-writable directory and the write can fail for any filesystem
        reason at all."""
        (status_home / ".trinity" / "lock-recovery.json").write_text(
            '{"at": "2026-09-13T10:00:00Z", "locks": "index.lock"}'
        )

        def _boom(_home, _updates):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(git_mod, "_patch_sync_state", _boom)

        payload = git_mod._compute_git_status(status_home)

        assert payload["git_enabled"] is True
        assert payload["lock_recovery"] is None

    def test_the_observer_takes_no_repo_lock(self, status_home):
        """Holding `_REPO_LOCK` across the observation would make a status poll
        a brand-new source of 409 `agent_busy` on an operator's POST /git/sync.
        An `lstat` needs no mutual exclusion, so it takes none."""
        lock = status_home / ".git" / "index.lock"
        lock.write_text("")
        acquired = git_mod._REPO_LOCK.acquire(blocking=False)
        assert acquired
        try:
            git_mod._index_lock_stuck(status_home)          # must not block
            payload = git_mod._compute_git_status(status_home)
            assert payload["git_enabled"] is True
        finally:
            git_mod._REPO_LOCK.release()
            lock.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# AC4 — an agent commit survives a concurrent poll
#
# Three phases, strongest first. Phase B is the GATE: it is deterministic AND
# genuinely concurrent, so AC4 does not rest on probability anywhere. Phase A
# (the lock-sighting sampler, above) is the property. Phase C is a behavioural
# witness that must demonstrate it can fail before it is allowed to pass.
# ---------------------------------------------------------------------------


def _bulk_repo(repo: Path, files: int) -> Path:
    """Widen the index so `git status`'s lock window is comfortably catchable."""
    bulk = repo / "bulk"
    bulk.mkdir(exist_ok=True)
    for i in range(files):
        (bulk / f"f{i}.txt").write_text(str(i))
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "bulk"], repo)
    return repo


class TestACommitSurvivesAConcurrentPoll:

    @staticmethod
    def _freeze_status_holding_the_lock(repo: Path, argv, attempts: int = 8):
        """Start a real `git status` child and SIGSTOP it the instant it takes
        `.git/index.lock`. Returns the frozen Popen, or None if the lock never
        appeared (which is the expected outcome for the flagged argv).

        SIGSTOP is the only way to hold the window open: git runs hooks and
        filters OUTSIDE the index lock, so an fsmonitor hook sleeping 1 s
        stretches `status` wall time to 1279 ms while the lock window stays
        0.8 ms, and a pre-commit hook holds it for 0 ms. A stopped process,
        however, holds it for as long as we like.
        """
        import signal
        import time as _time

        lock = repo / ".git" / "index.lock"
        for _ in range(attempts):
            proc = subprocess.Popen(
                argv, cwd=str(repo),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            deadline = _time.monotonic() + 5.0
            while _time.monotonic() < deadline:
                if lock.exists():
                    try:
                        os.kill(proc.pid, signal.SIGSTOP)
                    except ProcessLookupError:
                        break
                    if lock.exists() and proc.poll() is None:
                        return proc
                    try:
                        os.kill(proc.pid, signal.SIGCONT)
                    except ProcessLookupError:
                        pass
                    break
                if proc.poll() is not None:
                    break
            proc.wait(timeout=10)
        return None

    @staticmethod
    def _thaw(proc):
        """SIGCONT in a `finally`, always. `--timeout-method=signal` raises
        INSIDE the test, and a leaked SIGSTOPped child would hold the index lock
        for the rest of the pytest session."""
        import signal
        if proc is None:
            return
        try:
            os.kill(proc.pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)

    def test_a_commit_fails_while_a_legacy_status_holds_the_index_lock(self, repo):
        """**The AC4 gate.** Deterministic and genuinely concurrent: a real
        `git status --porcelain` child is frozen holding the real lock, and the
        agent's own `git add` — a second real process — fails against it.

        This is the defect #2742 describes, reproduced without a race: the
        platform's poll and the agent's turn contend for one index lock.
        """
        _bulk_repo(repo, 2000)
        (repo / "agent-work.txt").write_text("the agent's own turn")

        frozen = self._freeze_status_holding_the_lock(
            repo, ["git", "status", "--porcelain"]
        )
        try:
            if frozen is None:
                pytest.skip(
                    "could not freeze a status child holding index.lock in this "
                    "environment — never pass without having reproduced it"
                )
            add = _run(["git", "add", "-A"], repo)
            assert add.returncode != 0, (
                "the agent's git add should have failed against the held lock"
            )
            assert "index.lock" in add.stderr, (
                f"expected an index.lock failure, got: {add.stderr!r}"
            )
        finally:
            self._thaw(frozen)

    def test_the_shipped_status_argv_cannot_be_caught_holding_the_lock(self, repo):
        """The other direction of the same deterministic experiment: with
        `--no-optional-locks` the lock never appears, so there is nothing to
        freeze — and the agent's concurrent `git add` succeeds."""
        _bulk_repo(repo, 2000)
        (repo / "agent-work.txt").write_text("the agent's own turn")

        frozen = self._freeze_status_holding_the_lock(
            repo, ["git", "--no-optional-locks", "status", "--porcelain"], attempts=4
        )
        try:
            assert frozen is None, (
                "the flagged argv was caught holding index.lock — the flag is "
                "not doing what AC1 claims"
            )
            add = _run(["git", "add", "-A"], repo)
            assert add.returncode == 0, add.stderr
            commit = _run(["git", "commit", "-m", "agent turn"], repo)
            assert commit.returncode == 0, commit.stderr
        finally:
            self._thaw(frozen)

    def test_the_witness_run_that_must_prove_it_can_fail_first(self, repo, monkeypatch):
        """**Phase C — the behavioural witness, self-validating and non-gating.**

        Thread A drives the real `get_git_status()` route; thread B is the agent,
        doing write / `git add` / `git commit` rounds. The CONTROL arm (the
        legacy argv, injected through the same route) must reproduce at least one
        `index.lock` failure IN THIS RUN — otherwise the test skips. It must
        never pass without having demonstrated that it can fail: measured through
        this route the duty cycle is only ~0.7 % (about 95 % of each iteration is
        `git fetch`), so the margin is roughly one failure per run and a
        scheduling change could take it away silently.

        Writer rounds write `time.time_ns()` and failures are classified by
        `"index.lock" in stderr`, never by return code: `git commit` exits 1 with
        EMPTY stderr ("nothing to commit" on stdout) whenever a round writes
        content identical to the previous one, which under `check=True` is
        indistinguishable from a lock failure.
        """
        import asyncio
        import threading
        import time as _time

        _bulk_repo(repo, 2000)
        monkeypatch.setattr(git_mod, "_STATUS_HOME_DIR", repo)
        real_run_registered = git_mod.run_registered

        def _witness(strip_flag: bool, rounds: int = 20) -> int:
            if strip_flag:
                def _legacy(argv, **kwargs):
                    argv = [a for a in argv if a != "--no-optional-locks"]
                    return real_run_registered(argv, **kwargs)
                git_mod.run_registered = _legacy
            else:
                git_mod.run_registered = real_run_registered

            git_mod._STATUS_INFLIGHT.clear()
            stop = threading.Event()
            failures = {"n": 0}

            def _reader():
                async def _loop():
                    while not stop.is_set():
                        try:
                            await git_mod.get_git_status()
                        except Exception:
                            pass
                asyncio.run(_loop())

            def _writer():
                try:
                    for _ in range(rounds):
                        (repo / "agent.txt").write_text(str(_time.time_ns()))
                        add = _run(["git", "add", "-A"], repo)
                        if "index.lock" in (add.stderr or ""):
                            failures["n"] += 1
                            continue
                        commit = _run(["git", "commit", "-m", "agent turn"], repo)
                        if "index.lock" in (commit.stderr or ""):
                            failures["n"] += 1
                finally:
                    stop.set()

            reader = threading.Thread(target=_reader, daemon=True)
            writer = threading.Thread(target=_writer, daemon=True)
            reader.start()
            writer.start()
            writer.join(timeout=180)
            stop.set()
            reader.join(timeout=60)
            return failures["n"]

        try:
            control = _witness(strip_flag=True)
            if control == 0:
                pytest.skip(
                    "the control arm did not reproduce an index.lock failure in "
                    "this run — the witness has no teeth here, so it must skip "
                    "rather than pass (the deterministic AC4 gate above is "
                    "unaffected)"
                )
            treatment = _witness(strip_flag=False)
            assert treatment == 0, (
                f"the shipped status path still collided with the agent's own "
                f"git {treatment}x (control reproduced {control})"
            )
        finally:
            git_mod.run_registered = real_run_registered
            git_mod._STATUS_INFLIGHT.clear()


class TestTheLeaderIsBounded:
    """A slow leader costs no follower THREADS on this design, but it does hold
    the in-flight slot — so every caller in that window 504s. The deadline is
    what stops one wedged computation from monopolising the read forever."""

    @pytest.mark.asyncio
    async def test_leader_deadline_releases_the_slot(
        self, status_home, clean_inflight_slot, monkeypatch
    ):
        import asyncio
        import threading
        from fastapi import HTTPException

        gate = threading.Event()
        spy = _BlockingFetch(git_mod.run_registered, gate)
        monkeypatch.setattr(git_mod, "run_registered", spy)
        monkeypatch.setattr(git_mod, "_STATUS_LEADER_DEADLINE_SECONDS", 0.2)

        with pytest.raises(HTTPException) as exc:
            await git_mod.get_git_status()
        assert exc.value.status_code == 504
        assert "deadline" in exc.value.detail

        gate.set()
        for _ in range(50):
            if not git_mod._STATUS_INFLIGHT:
                break
            await asyncio.sleep(0.05)
        assert not git_mod._STATUS_INFLIGHT, "a wedged leader must release the slot"

    def test_the_declared_leader_deadline_matches_the_real_child_budget(self):
        """AST guard so the documented arithmetic cannot silently drift.

        `_compute_git_status`'s own `run_registered` timeouts sum to 90s
        (10 rev-parse + 10 status + 10 log + 30 fetch + 10 merge-base + 10 log
        + 10 remote get-url), which is what the leader deadline is set to. Adding
        a child — or widening one — without revisiting the deadline fails here
        rather than showing up as an unexplained 504 in production. The helpers
        it calls add a further ~40s on top, which is precisely why the deadline
        exists instead of relying on the child timeouts to bound the run.
        """
        import ast

        src = (
            _BASE_IMAGE / "agent_server" / "routers" / "git.py"
        ).read_text(encoding="utf-8")
        fn = next(
            node for node in ast.walk(ast.parse(src))
            if isinstance(node, ast.FunctionDef) and node.name == "_compute_git_status"
        )
        budget = sum(
            kw.value.value
            for node in ast.walk(fn)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_registered"
            for kw in node.keywords
            if kw.arg == "timeout" and isinstance(kw.value, ast.Constant)
        )
        assert budget == git_mod._STATUS_LEADER_DEADLINE_SECONDS, (
            f"the status body's child timeouts now sum to {budget}s but the "
            f"leader deadline is still {git_mod._STATUS_LEADER_DEADLINE_SECONDS}s "
            "— revisit both together, and the flow doc's arithmetic with them"
        )

    def test_the_follower_bound_sits_above_every_real_callers_own_timeout(self):
        """Poller 10s, backend git_service 30s. A follower waiting longer than
        every caller can only produce work nobody is waiting for."""
        assert git_mod._STATUS_FOLLOWER_WAIT_SECONDS >= 30
        assert (
            git_mod._STATUS_FOLLOWER_WAIT_SECONDS
            < git_mod._STATUS_LEADER_DEADLINE_SECONDS
        ), "the caller bound must fail fast relative to the computation bound"
