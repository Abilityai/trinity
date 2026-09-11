"""The agent-side pipeline-state watcher (trinity-enterprise#533).

Mirrors tests/unit/test_agent_heartbeat.py — force-loads the real
``agent_server`` package from docker/base-image so the relative imports resolve
without booting the FastAPI app.

The file the watcher reports on is written by the agent itself, so this loop is
the only thing in the system that can know a stage advanced when it advanced.
What is proven, and why:

  * ``snapshot`` is a **bounded, forgiving** scan — a one-level ``scandir``
    with caps on both axes, invalid ids and non-JSON dropped, and a missing
    directory answering ``{}`` rather than raising. It runs once a second in
    every agent container forever; anything it can raise on, it will.
  * ``changed`` keys on ``(mtime_ns, size)``, so the canonical writer's
    non-atomic ``cp`` onto the read surface fires **again** as the file grows
    — a truncated read self-heals instead of sticking.
  * A **deletion is not a change**. Removing an instance is not a stage
    advance, and treating it as one would make a cleanup pass storm the
    backend.
  * ``read_stage`` never raises and never returns anything but a bounded
    string or ``None`` — the value goes onto a SCOPE_ALL channel.
  * The loop takes a **silent baseline**: a container restart must not replay
    every instance on disk as a fresh advance.
  * It is silent by design end to end (a failed POST is swallowed) and armed
    only when the same two env vars the #307 heartbeat needs are both present.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.unit

_BASE_IMAGE = Path(__file__).resolve().parent.parent.parent / "docker" / "base-image"
_BASE_IMAGE_STR = str(_BASE_IMAGE)
if _BASE_IMAGE_STR not in sys.path:
    sys.path.insert(0, _BASE_IMAGE_STR)

# Import-time `agent_server` namespace shim — see test_agent_heartbeat.py for
# why this is declared rather than monkeypatched (tests/lint_sys_modules.py).
_STUBBED_MODULE_NAMES = [
    "agent_server",
    "agent_server.pipeline_state_watch",
    "agent_server.config",
    "agent_server.state",
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


for _mod in list(sys.modules):
    if _mod == "agent_server" or _mod.startswith("agent_server."):
        sys.modules.pop(_mod, None)

_stub = types.ModuleType("agent_server")
_stub.__path__ = [str(_BASE_IMAGE / "agent_server")]  # type: ignore[attr-defined]
_stub.__package__ = "agent_server"
sys.modules["agent_server"] = _stub

from agent_server import pipeline_state_watch as watch  # noqa: E402


_STATE = {"instance_id": "i1", "current_stage": "synthesis", "health": "green",
          "updated_at": "2026-09-10T09:12:00Z", "escalations": []}


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    root = tmp_path / "pipeline-state"
    root.mkdir()
    monkeypatch.setattr(watch, "STATE_DIR", root)
    return root


def _write(root: Path, pipeline: str, instance: str, body=None) -> Path:
    d = root / pipeline
    d.mkdir(exist_ok=True)
    f = d / f"{instance}.json"
    f.write_text(json.dumps(body if body is not None else _STATE))
    return f


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------
def test_snapshot_finds_instances_and_keys_them_by_mtime_and_size(state_dir):
    _write(state_dir, "digest", "i1")
    _write(state_dir, "digest", "i2")
    _write(state_dir, "recon", "a")
    snap = watch.snapshot()
    assert set(snap) == {("digest", "i1"), ("digest", "i2"), ("recon", "a")}
    for value in snap.values():
        assert isinstance(value, tuple) and len(value) == 2
        assert all(isinstance(v, int) for v in value)


def test_snapshot_drops_everything_that_is_not_an_instance_file(state_dir):
    _write(state_dir, "digest", "i1")
    (state_dir / "digest" / "notes.txt").write_text("x")       # not .json
    (state_dir / "digest" / "nested").mkdir()                  # not a file
    (state_dir / "loose.json").write_text("{}")               # not under a pipeline
    _write(state_dir, "bad id", "i1")                          # invalid pipeline id
    _write(state_dir, "ok", "bad id")                          # invalid instance id
    assert set(watch.snapshot()) == {("digest", "i1")}


def test_snapshot_is_bounded_on_both_axes(state_dir, monkeypatch):
    monkeypatch.setattr(watch, "_MAX_DIRS", 2)
    monkeypatch.setattr(watch, "_MAX_FILES_PER_DIR", 2)
    for p in range(4):
        for i in range(4):
            _write(state_dir, f"p{p}", f"i{i}")
    snap = watch.snapshot()
    assert len(snap) == 4
    assert len({pid for pid, _ in snap}) == 2


def test_snapshot_on_a_missing_directory_is_empty_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(watch, "STATE_DIR", tmp_path / "nope")
    assert watch.snapshot() == {}


def test_snapshot_never_raises(state_dir, monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("filesystem gone")
    monkeypatch.setattr(watch.os, "scandir", _boom)
    assert watch.snapshot() == {}


# ---------------------------------------------------------------------------
# changed
# ---------------------------------------------------------------------------
def test_changed_reports_new_files_and_both_halves_of_the_signature():
    prev = {("p", "a"): (100, 10), ("p", "b"): (100, 10), ("p", "c"): (100, 10)}
    cur = {
        ("p", "a"): (100, 10),        # untouched
        ("p", "b"): (200, 10),        # rewritten in place (mtime moved)
        ("p", "c"): (100, 40),        # still being copied (size grew)
        ("p", "d"): (100, 10),        # brand new
    }
    assert set(watch.changed(prev, cur)) == {("p", "b"), ("p", "c"), ("p", "d")}


def test_a_deletion_is_not_a_stage_advance():
    prev = {("p", "a"): (100, 10), ("p", "b"): (100, 10)}
    cur = {("p", "a"): (100, 10)}
    assert watch.changed(prev, cur) == []


def test_a_file_still_growing_fires_again():
    """The canonical writer copies onto the read surface with a plain `cp`, so
    a reader can catch it truncated. A second notice at the larger size is what
    makes that self-healing rather than sticky."""
    first = watch.changed({}, {("p", "a"): (100, 10)})
    second = watch.changed({("p", "a"): (100, 10)}, {("p", "a"): (100, 900)})
    assert first == [("p", "a")] and second == [("p", "a")]


# ---------------------------------------------------------------------------
# read_stage
# ---------------------------------------------------------------------------
def test_read_stage_reads_the_current_stage(state_dir):
    f = _write(state_dir, "digest", "i1")
    assert watch.read_stage(f) == "synthesis"


@pytest.mark.parametrize("body", [
    {"instance_id": "i1"},                       # no current_stage
    {"current_stage": 7},                        # not a string
    {"current_stage": ""},                       # blank
    {"current_stage": "   "},
    {"current_stage": "s" * 81},                 # past the bound
    ["not", "a", "dict"],
])
def test_read_stage_returns_none_for_anything_it_cannot_vouch_for(state_dir, body):
    f = _write(state_dir, "digest", "i1", body)
    assert watch.read_stage(f) is None


def test_read_stage_survives_bad_json_a_missing_file_and_an_oversized_one(state_dir):
    d = state_dir / "digest"
    d.mkdir()
    bad = d / "i1.json"
    bad.write_text('{"current_stage": "x"')          # truncated mid-copy
    assert watch.read_stage(bad) is None
    assert watch.read_stage(d / "gone.json") is None
    huge = d / "i2.json"
    huge.write_text(" " * (watch._MAX_STAGE_READ_BYTES + 1))
    assert watch.read_stage(huge) is None


def test_read_stage_strips(state_dir):
    f = _write(state_dir, "digest", "i1", {"current_stage": "  synthesis  "})
    assert watch.read_stage(f) == "synthesis"


# ---------------------------------------------------------------------------
# _post_change_once
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code):
        self.status_code = status_code


class _RecordingClient:
    _status_code = 200

    def __init__(self):
        self.posts = []

    async def post(self, url, json=None, headers=None):
        self.posts.append((url, json, headers))
        return _FakeResp(type(self)._status_code)


def test_post_change_targets_the_agents_own_route_with_its_own_key():
    client = _RecordingClient()
    asyncio.run(watch._post_change_once(
        client, "http://backend:8000", "trinity_mcp_k", "scout", "digest", "i1", "synthesis"))
    url, body, headers = client.posts[0]
    assert url == "http://backend:8000/api/agents/scout/pipeline-state/changed"
    assert body == {"pipeline_id": "digest", "instance_id": "i1", "stage": "synthesis"}
    assert headers == {"Authorization": "Bearer trinity_mcp_k"}


def test_post_change_logs_a_non_2xx_and_does_not_raise(caplog):
    import logging

    class _Client403(_RecordingClient):
        _status_code = 403

    with caplog.at_level(logging.DEBUG, logger=watch.logger.name):
        asyncio.run(watch._post_change_once(
            _Client403(), "http://backend:8000", "k", "scout", "p", "i", None))
    assert any("backend returned 403" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------
def _drive_loop(monkeypatch, ticks: int):
    """Run the loop for `ticks` iterations, then cancel it out."""
    calls = {"n": 0}

    async def _fake_sleep(_seconds):
        calls["n"] += 1
        if calls["n"] > ticks:
            raise asyncio.CancelledError()

    monkeypatch.setattr(watch.asyncio, "sleep", _fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(watch.run_pipeline_state_watch_loop())
    return calls["n"]


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    monkeypatch.setattr(watch.agent_state, "agent_name", "scout", raising=False)
    posts = []

    async def _record(_client, backend_url, mcp_key, agent_name, pid, iid, stage):
        posts.append((backend_url, mcp_key, agent_name, pid, iid, stage))

    monkeypatch.setattr(watch, "_post_change_once", _record)
    return posts


def test_the_baseline_is_silent_so_a_restart_does_not_replay_the_disk(state_dir, armed, monkeypatch):
    _write(state_dir, "digest", "i1")
    _write(state_dir, "digest", "i2")
    _drive_loop(monkeypatch, ticks=2)
    assert armed == [], "a restart re-announced every instance already on disk"


def test_one_changed_file_is_one_post(state_dir, armed, monkeypatch):
    _write(state_dir, "digest", "i1")
    snaps = [watch.snapshot()]
    _write(state_dir, "digest", "i2")
    snaps.append(watch.snapshot())

    it = iter(snaps)
    monkeypatch.setattr(watch, "snapshot", lambda: next(it, snaps[-1]))
    _drive_loop(monkeypatch, ticks=2)

    assert len(armed) == 1
    backend_url, key, agent, pid, iid, stage = armed[0]
    assert (backend_url, key, agent) == ("http://backend:8000", "trinity_mcp_test", "scout")
    assert (pid, iid, stage) == ("digest", "i2", "synthesis")


def test_a_burst_is_capped_per_tick(state_dir, armed, monkeypatch):
    base = watch.snapshot()
    for i in range(watch._MAX_POSTS_PER_TICK + 3):
        _write(state_dir, "digest", f"i{i}")
    after = watch.snapshot()
    it = iter([base, after])
    monkeypatch.setattr(watch, "snapshot", lambda: next(it, after))
    _drive_loop(monkeypatch, ticks=2)
    assert len(armed) == watch._MAX_POSTS_PER_TICK


def test_a_failing_post_is_swallowed_and_the_loop_reaches_the_next_tick(state_dir, monkeypatch):
    monkeypatch.setenv("TRINITY_BACKEND_URL", "http://backend:8000")
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "trinity_mcp_test")
    monkeypatch.setattr(watch.agent_state, "agent_name", "scout", raising=False)
    attempts = {"n": 0}

    async def _boom(*_a, **_k):
        attempts["n"] += 1
        raise httpx.ConnectError("backend unreachable")

    monkeypatch.setattr(watch, "_post_change_once", _boom)
    base = watch.snapshot()
    _write(state_dir, "digest", "i1")
    after = watch.snapshot()
    it = iter([base, after])
    monkeypatch.setattr(watch, "snapshot", lambda: next(it, after))

    ticks = _drive_loop(monkeypatch, ticks=3)
    assert attempts["n"] == 1
    assert ticks > 2, "the transport error escaped and killed the loop"


def test_the_loop_refuses_to_run_without_both_env_vars(monkeypatch):
    monkeypatch.delenv("TRINITY_BACKEND_URL", raising=False)
    monkeypatch.setenv("TRINITY_MCP_API_KEY", "k")
    # Returns immediately: no sleep, no scan, no post.
    asyncio.run(watch.run_pipeline_state_watch_loop())


# ---------------------------------------------------------------------------
# schedule_pipeline_state_watch — the same env gate as the heartbeat
# ---------------------------------------------------------------------------
class _FakeApp:
    def __init__(self):
        self.startup = []
        self.shutdown = []

    def on_event(self, name):
        def deco(fn):
            (self.startup if name == "startup" else self.shutdown).append(fn)
            return fn
        return deco


@pytest.mark.parametrize("backend, key, armed_expected", [
    (None, "k", False),
    ("http://backend:8000", None, False),
    ("http://backend:8000", "k", True),
])
def test_schedule_is_gated_on_both_env_vars(monkeypatch, backend, key, armed_expected):
    for name, value in (("TRINITY_BACKEND_URL", backend), ("TRINITY_MCP_API_KEY", key)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    app = _FakeApp()
    watch.schedule_pipeline_state_watch(app)
    assert bool(app.startup) is armed_expected
    assert bool(app.shutdown) is armed_expected


def test_the_watcher_is_armed_in_the_agent_server(monkeypatch):
    """A loop nobody starts is a silent no-op — main.py must arm it."""
    src = (_BASE_IMAGE / "agent_server" / "main.py").read_text()
    assert "schedule_pipeline_state_watch" in src
    assert "schedule_pipeline_state_watch(app)" in src


def test_the_watcher_ships_no_new_dependency():
    """No Dockerfile change means no new pip install — the module must live on
    the stdlib plus httpx, which the heartbeat already brings."""
    src = (_BASE_IMAGE / "agent_server" / "pipeline_state_watch.py").read_text()
    third_party = {
        line.split()[1].split(".")[0]
        for line in src.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    allowed = {"__future__", "asyncio", "json", "logging", "os", "re", "time",
               "pathlib", "typing", "httpx"}
    assert third_party <= allowed, f"unexpected imports: {sorted(third_party - allowed)}"
