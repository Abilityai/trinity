"""
Regression tests for #2938 — git init rooted a NEW repo where the agent server
cannot see it.

The bug: `initialize_git_in_container` chose the repo root with a content probe
— any non-empty `/home/developer/workspace` was treated as a legacy repo root —
so an agent whose template shipped files into `workspace/` got its brand-new
repo created there. The agent server's git router reads exactly one root,
`/home/developer`, so the very next status poll answered `git_enabled: false`
and Sync / Log / the git panel all reported "not enabled" — after the init
endpoint had returned 200, because its verify ran in the directory the backend
had chosen and could only agree with itself.

Two fixes, both driven end-to-end here through the real
`initialize_git_in_container` with the container exec and the agent-server
HTTP call replaced:

1. A new repo is ALWAYS rooted at `/home/developer`, whatever `workspace/`
   holds. An existing repo is still wherever `git rev-parse --show-toplevel`
   says (a legacy workspace-rooted one included).
2. Init is verified through the agent server (`GET /api/git/status`), the
   side that answers every later call. `git_enabled: false` there fails the
   init with a named reason instead of a 200 the next poll contradicts.
"""
import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import pytest

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src/backend"))

import services.git_service as gs  # noqa: E402


class _FakeExec:
    """Stands in for `execute_command_in_container` across the whole init.

    `toplevel` is what `git rev-parse --show-toplevel` answers (None → exit
    128, "no repository at or above the probe point"). Everything else — the
    ent#615 credential seed, the setup/commit/branch commands, the backend's
    own `rev-parse --git-dir` verify — succeeds, so the only variable is where
    the repo lands and what the agent server says about it.
    """

    def __init__(self, toplevel=None):
        self.toplevel = toplevel
        self.calls: list[str] = []

    async def __call__(self, container_name, command, timeout=60, *,
                       environment=None, user="developer"):
        self.calls.append(command)
        if "rev-parse --show-toplevel" in command:
            if self.toplevel is None:
                return {"exit_code": 128, "output": ""}
            return {"exit_code": 0, "output": self.toplevel + "\n"}
        if "base64 -d" in command and "sh -s" in command:
            return {"exit_code": 0, "output": (
                "TRINITY_SCRUB_REPORT remotes_scrubbed=0 harvested=0 seeded=1 "
                "refused=0 gitmodules_hits=0 helper_ok=1 competing_helpers=0 "
                "root_readable=1 root_traversable=1 git_present=1"
            )}
        if "rev-parse --verify origin/main" in command:
            return {"exit_code": 1, "output": ""}  # empty remote
        if "mindepth" in command or "ls -A" in command:
            # The retired content probe, answered the way a POPULATED
            # `workspace/` answers it — so the test reproduces the reported
            # defect against the old code rather than passing by accident.
            return {"exit_code": 0, "output": "1\n"}
        return {"exit_code": 0, "output": ""}

    def commands_in(self, git_dir: str) -> list[str]:
        return [c for c in self.calls if f"cd {git_dir} && " in c]


class _FakeResponse:
    def __init__(self, status_code=200, body=None, raises=False):
        self.status_code = status_code
        self._body = body
        self._raises = raises

    def json(self):
        if self._raises:
            raise ValueError("not json")
        return self._body


def _agent_server(response=None, *, error=None):
    """An `agent_httpx_client` stand-in whose `.get` answers `response` or raises."""
    calls: list[str] = []

    class _Client:
        async def get(self, url):
            calls.append(url)
            if error is not None:
                raise error
            return response

    @asynccontextmanager
    async def _ctx(agent_name, timeout=None):
        yield _Client()

    _ctx.calls = calls
    return _ctx


@pytest.fixture
def run_init():
    """Drive the real `initialize_git_in_container` with both boundaries faked.

    Returns (result, exec_recorder, agent_server_stub).
    """
    def _run(*, toplevel=None, agent_server=None):
        recorder = _FakeExec(toplevel=toplevel)
        if agent_server is None:
            agent_server = _agent_server(_FakeResponse(200, {"git_enabled": True}))
        # #1028: each sibling module binds the exec at import; the git-dir
        # probe lives in `gitignore`, the init in `provisioning`.
        patches = [
            patch.object(mod, "execute_command_in_container", recorder)
            for mod in (gs.gitignore, gs.token_scrub, gs.remotes, gs.provisioning, gs.sync)
        ]
        patches.append(patch.object(gs.provisioning, "agent_httpx_client", agent_server))
        for p in patches:
            p.start()
        try:
            result = asyncio.run(gs.initialize_git_in_container(
                "a1", "acme/agent", "tok",
                create_working_branch=False, working_branch="trinity/a1/abc123",
            ))
        finally:
            for p in patches:
                p.stop()
        return result, recorder, agent_server

    return _run


# ---------------------------------------------------------------------------
# Placement: a NEW repo goes where the agent server reads

class TestANewRepoIsRootedAtHome:
    def test_a_populated_workspace_no_longer_captures_the_repo(self, run_init):
        """The reported defect: template files in `workspace/`, no repository
        anywhere. The repo must be created at `/home/developer`."""
        result, recorder, _ = run_init(toplevel=None)

        assert result.success, result.error
        assert result.git_dir == "/home/developer"
        assert any(c.endswith("git init\"") for c in recorder.commands_in("/home/developer")), (
            "git init did not run at /home/developer"
        )
        assert not recorder.commands_in("/home/developer/workspace"), (
            "a command ran in workspace/ — the repo was placed where the "
            "agent server cannot see it"
        )

    def test_no_content_probe_decides_placement(self, run_init):
        """The old heuristic — `find workspace -mindepth 1` — is gone. Only
        git's own answer is consulted before the repo is placed."""
        _, recorder, _ = run_init(toplevel=None)

        probes = [c for c in recorder.calls if "mindepth" in c or "ls -A" in c]
        assert probes == [], probes
        assert sum("rev-parse --show-toplevel" in c for c in recorder.calls) == 1

    def test_an_existing_workspace_rooted_repo_is_reinitialised_in_place_then_refused(self, run_init):
        """A genuine legacy repo (git says `workspace/`) keeps its root — that
        is a re-init of a real repository, not a placement decision. But the
        agent server reads exactly one root, `/home/developer`, so its status
        route answers `git_enabled: false` for a workspace-rooted repo (it is
        a bare `Path("/home/developer/.git").exists()` — no walk-up). The
        stub here models that real answer, so the verify step refuses the
        init with the #2938 reason instead of a 200 the panel contradicts.
        (Before #2938 this case returned 200 and stayed 'not enabled' forever.)
        """
        stub = _agent_server(_FakeResponse(200, {"git_enabled": False, "git_dir": None}))
        result, recorder, _ = run_init(toplevel="/home/developer/workspace", agent_server=stub)

        assert result.git_dir == "/home/developer/workspace"
        assert recorder.commands_in("/home/developer/workspace")
        assert not recorder.commands_in("/home/developer")
        assert result.success is False
        assert "cannot see the repository at /home/developer/workspace" in result.error
        assert "#2938" in result.error


# ---------------------------------------------------------------------------
# Verification: the agent server's verdict is the one that counts

class TestInitIsVerifiedThroughTheAgentServer:
    def test_the_agent_server_is_asked_after_the_backend_verify(self, run_init):
        result, recorder, agent_server = run_init()

        assert result.success
        assert agent_server.calls == ["http://agent-a1:8000/api/git/status"]
        # The backend's own verify ran (Step 5) — the probe is IN ADDITION, not instead.
        assert any("rev-parse --git-dir" in c for c in recorder.calls)

    def test_git_enabled_false_fails_the_init_with_a_named_reason(self, run_init):
        """The 200-then-'not enabled' contradiction becomes a failed init.
        The router turns `success=False` into a 400 and rolls the config row
        back, so the operator sees the reason instead of a green toast."""
        stub = _agent_server(_FakeResponse(200, {"git_enabled": False, "git_dir": None}))
        result, _, _ = run_init(agent_server=stub)

        assert result.success is False
        assert result.git_dir == "/home/developer"
        assert "cannot see the repository at /home/developer" in result.error
        assert "not enabled" in result.error
        assert "#2938" in result.error

    @pytest.mark.parametrize("stub", [
        pytest.param(_agent_server(error=ConnectionError("agent down")), id="transport-error"),
        pytest.param(_agent_server(_FakeResponse(503, {})), id="non-200"),
        pytest.param(_agent_server(_FakeResponse(200, None, raises=True)), id="not-json"),
        pytest.param(_agent_server(_FakeResponse(200, {"status": "ok"})), id="no-git_enabled-key"),
        pytest.param(_agent_server(_FakeResponse(200, ["nope"])), id="not-an-object"),
    ])
    def test_an_unanswerable_probe_warns_and_keeps_the_backend_verdict(self, run_init, stub, caplog):
        """Unknown is not 'no': the backend verify already passed and the
        disagreement this guards against is deterministic, so a transport blip
        must not fail a real init. It IS logged."""
        import logging
        with caplog.at_level(logging.WARNING, logger=gs.provisioning.logger.name):
            result, _, _ = run_init(agent_server=stub)

        assert result.success, result.error
        assert any("could not be confirmed through the agent server" in r.getMessage()
                   for r in caplog.records), [r.getMessage() for r in caplog.records]

    def test_the_probe_helper_maps_answers_to_the_three_verdicts(self):
        """`_agent_server_sees_repo` on its own: True / False / None."""
        async def _ask(stub):
            with patch.object(gs.provisioning, "agent_httpx_client", stub):
                return await gs.provisioning._agent_server_sees_repo("a1")

        assert asyncio.run(_ask(_agent_server(_FakeResponse(200, {"git_enabled": True})))) is True
        assert asyncio.run(_ask(_agent_server(_FakeResponse(200, {"git_enabled": False})))) is False
        assert asyncio.run(_ask(_agent_server(_FakeResponse(200, {"git_enabled": 0})))) is False
        assert asyncio.run(_ask(_agent_server(_FakeResponse(500, {"git_enabled": True})))) is None
        assert asyncio.run(_ask(_agent_server(error=OSError("boom")))) is None
