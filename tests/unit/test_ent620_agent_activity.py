"""
trinity-enterprise#620 — the agent server reports WHAT each running execution is
doing, per execution, in its heartbeat.

Before this, `session_activity.active_tool` was one slot per agent process:
a chat turn and a delegated run on the same agent overwrote each other, and
nothing left the container but the raw stream. Now `start_tool_execution`
keys a slot by execution id, `complete_tool_execution` turns it into
"between tools" (`tool: None` → the card says "Thinking"), and the heartbeat
builder emits one bounded entry per RUNNING execution, intersected with the
process registry so a finished run drops out by construction.

Loads the real agent_server package the way test_agent_heartbeat.py does.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_BASE_IMAGE = Path(__file__).resolve().parents[2] / "docker" / "base-image"

_STUBBED_MODULE_NAMES = (
    "agent_server",
    "agent_server.heartbeat",
    "agent_server.state",
    "agent_server.services",
    "agent_server.services.activity_tracking",
    "agent_server.services.process_registry",
    "agent_server.utils",
    "agent_server.utils.helpers",
)


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


# Evict ONLY when the registered `agent_server` is not the real base-image
# package (the `test_git_status_dual_ahead_behind.py` guard). An unconditional
# eviction here is a runner-killer: this file sorts after `test_drain_bounded.py`,
# which binds `_drain_bounded` at collection and patches `_drain_reader_threads`
# by dotted string at test time — a fresh module copy makes that patch land
# on the wrong object, the REAL drain runs, and its cgroup orphan sweep takes
# the CI runner down with it ("The runner has received a shutdown signal";
# the #728 regression class).
_existing = sys.modules.get("agent_server")
_real_path = str(_BASE_IMAGE / "agent_server")
if _existing is None or not any(
    _real_path in p for p in (getattr(_existing, "__path__", None) or [])
):
    for _mod in list(sys.modules):
        if _mod == "agent_server" or _mod.startswith("agent_server."):
            sys.modules.pop(_mod, None)
    _stub = types.ModuleType("agent_server")
    _stub.__path__ = [_real_path]  # type: ignore[attr-defined]
    _stub.__package__ = "agent_server"
    sys.modules["agent_server"] = _stub

from agent_server import heartbeat  # noqa: E402
from agent_server.services import activity_tracking as at  # noqa: E402
from agent_server import state as _state_mod  # noqa: E402
from agent_server.state import agent_state  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_activity(monkeypatch):
    # Every agent_server test file re-registers the package at collection
    # time, so by the time these tests RUN `sys.modules` may hold a sibling
    # file's copies. The heartbeat builder imports lazily, so pin the copies
    # this file loaded for the duration of each test — otherwise the builder
    # reads a different `agent_state` than the one the test wrote into.
    monkeypatch.setitem(sys.modules, "agent_server.state", _state_mod)
    monkeypatch.setitem(sys.modules, "agent_server.services.activity_tracking", at)
    agent_state.session_activity = agent_state._create_empty_activity()
    yield
    agent_state.session_activity = agent_state._create_empty_activity()


# ---------------------------------------------------------------------------
# Per-execution slot
# ---------------------------------------------------------------------------

def test_two_concurrent_executions_do_not_share_one_active_tool():
    at.start_tool_execution("t1", "Read", {"file_path": "/home/developer/src/backend/routers/agents.py"}, execution_id="chat-1")
    at.start_tool_execution("t2", "Bash", {"command": "pytest tests/unit -q"}, execution_id="sched-1")

    chat = at.execution_activity("chat-1")
    sched = at.execution_activity("sched-1")
    assert chat["tool"] == "Read" and chat["input_summary"] == ".../routers/agents.py"
    assert sched["tool"] == "Bash" and sched["input_summary"].startswith("pytest tests/unit")
    # The legacy single slot still exists for its old readers — last writer wins there.
    assert agent_state.session_activity["active_tool"]["name"] == "Bash"


def test_completion_turns_the_slot_into_thinking_not_a_stale_tool():
    at.start_tool_execution("t1", "Grep", {"pattern": "sync_health"}, execution_id="chat-1")
    at.complete_tool_execution("t1", True, "3 matches")
    slot = at.execution_activity("chat-1")
    assert slot["tool"] is None and slot["input_summary"] is None and slot["since"]


def test_no_execution_id_means_no_slot_but_the_legacy_tracker_still_works():
    at.start_tool_execution("t1", "Read", {"file_path": "a/b/c.py"})
    assert agent_state.session_activity["by_execution"] == {}
    assert agent_state.session_activity["active_tool"]["name"] == "Read"


def test_forget_execution_drops_the_slot():
    at.start_tool_execution("t1", "Read", {"file_path": "a/b/c.py"}, execution_id="e1")
    at.forget_execution("e1")
    assert at.execution_activity("e1") is None
    at.forget_execution("never-seen")  # idempotent


def test_delegation_and_mcp_names_carry_the_object():
    at.start_tool_execution("t1", "Task", {"subagent_type": "explore", "description": "Map the Work card"}, execution_id="e1")
    assert at.execution_activity("e1") == {"tool": "Task:explore", "input_summary": "Map the Work card", "since": at.execution_activity("e1")["since"], "tool_id": "t1"}
    at.start_tool_execution("t2", "mcp__trinity__chat_with_agent", {"agent_name": "sidekick", "message": "x" * 80}, execution_id="e2")
    assert at.execution_activity("e2")["tool"] == "mcp:trinity"
    assert at.execution_activity("e2")["input_summary"] == "agent_name: sidekick"


# ---------------------------------------------------------------------------
# Heartbeat builder
# ---------------------------------------------------------------------------

def _running(*ids):
    return [{"execution_id": i, "pid": 1, "started_at": "2026-09-16T10:00:00", "metadata": {}} for i in ids]


def test_heartbeat_reports_one_entry_per_running_execution(monkeypatch):
    monkeypatch.setattr(heartbeat, "_list_running", lambda: _running("chat-1", "sched-1", "quiet-1"))
    at.start_tool_execution("t1", "Read", {"file_path": "/x/y/z.py"}, execution_id="chat-1")
    at.start_tool_execution("t2", "WebFetch", {"url": "https://docs.example.com/a/b"}, execution_id="sched-1")

    entries = {e["execution_id"]: e for e in heartbeat._executions_activity()}
    assert entries["chat-1"]["tool"] == "Read" and entries["chat-1"]["summary"] == ".../y/z.py"
    assert entries["sched-1"]["tool"] == "WebFetch" and entries["sched-1"]["summary"] == "docs.example.com"
    # A running execution the tracker never saw a tool for: honest "between tools".
    assert entries["quiet-1"] == {"execution_id": "quiet-1", "tool": None, "summary": None, "since": "2026-09-16T10:00:00"}


def test_a_finished_run_drops_out_and_its_slot_is_pruned(monkeypatch):
    at.start_tool_execution("t1", "Read", {"file_path": "/x/y/z.py"}, execution_id="done-1")
    at.start_tool_execution("t2", "Read", {"file_path": "/x/y/w.py"}, execution_id="live-1")
    monkeypatch.setattr(heartbeat, "_list_running", lambda: _running("live-1"))

    ids = [e["execution_id"] for e in heartbeat._executions_activity()]
    assert ids == ["live-1"]
    assert at.execution_activity("done-1") is None  # pruned, never a stale line
    assert at.execution_activity("live-1") is not None


def test_heartbeat_bounds_the_list_and_the_summary(monkeypatch):
    many = [f"e{i}" for i in range(heartbeat.ACTIVITY_MAX_EXECUTIONS + 5)]
    monkeypatch.setattr(heartbeat, "_list_running", lambda: _running(*many))
    long_cmd = "x" * 500
    at.start_tool_execution("t1", "Bash", {"command": long_cmd}, execution_id="e0")
    # `get_input_summary` already cuts Bash at 50; force a long one through the
    # generic branch to exercise the builder's own ceiling.
    agent_state.session_activity["by_execution"]["e0"]["input_summary"] = "y" * 500

    entries = heartbeat._executions_activity()
    assert len(entries) == heartbeat.ACTIVITY_MAX_EXECUTIONS
    assert len(entries[0]["summary"]) == heartbeat.ACTIVITY_SUMMARY_MAX
    assert entries[0]["summary"].endswith("…")


def test_heartbeat_bounds_the_tool_name_to_the_backend_ceiling(monkeypatch):
    """An unbounded tool name (a codex `server.tool`, a model-chosen
    `Task:<type>`) past the backend's max_length would 422 the whole beat and
    read as a lost heartbeat. The builder cuts it to the same ceiling."""
    monkeypatch.setattr(heartbeat, "_list_running", lambda: _running("e0"))
    at.start_tool_execution("t1", "Read", {"file_path": "/x/y/z.py"}, execution_id="e0")
    agent_state.session_activity["by_execution"]["e0"]["tool"] = "mcp:" + "s" * 200

    entry = heartbeat._executions_activity()[0]
    assert len(entry["tool"]) == heartbeat.ACTIVITY_TOOL_MAX
    assert entry["tool"].startswith("mcp:sss") and entry["tool"].endswith("…")


def test_a_run_past_the_cap_keeps_its_slot(monkeypatch):
    """/review C1: the prune set must be every running id, not the capped
    slice — otherwise the 21st concurrent run is forgotten on every beat and
    reads "Thinking" forever while it is busy."""
    many = [f"e{i}" for i in range(heartbeat.ACTIVITY_MAX_EXECUTIONS + 3)]
    monkeypatch.setattr(heartbeat, "_list_running", lambda: _running(*many))
    last = many[-1]
    at.start_tool_execution("t-last", "Bash", {"command": "make"}, execution_id=last)
    entries = heartbeat._executions_activity()
    assert len(entries) == heartbeat.ACTIVITY_MAX_EXECUTIONS       # the wire stays bounded
    assert at.execution_activity(last)["tool"] == "Bash"             # the slot survives the beat


def test_heartbeat_activity_fails_open_to_an_empty_list(monkeypatch):
    def boom():
        raise RuntimeError("registry hiccup")
    monkeypatch.setattr(heartbeat, "_list_running", boom)
    assert heartbeat._executions_activity() == []
    payload = heartbeat._build_payload()
    assert payload["executions"] == []
