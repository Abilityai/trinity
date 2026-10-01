"""#2958 — the agent's own /api/chat session must be in the JSONL reaper's keep set.

The chat now resumes its own session by id (`--resume <id>`) instead of
`--continue`. That id lives in agent memory, so the agent publishes it to
`~/.trinity/chat-session.json` and the sweep reads it over docker exec. Without
it the chat's JSONL is an orphan by construction and is reaped an hour after the
chat goes idle (the fourth instance of the ent#358 / #2610 class).

The fail-closed half is the same rule as the three DB sources: a marker that
cannot be read cleanly ABORTS that agent's sweep. The marker is agent-writable,
so the log names only the failure kind, never the content.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

AGENT = "agent-chat"
CHAT_UUID = "c4a7c4a7-2958-4958-8958-295829582958"
ORPHAN_UUID = "99999999-9999-9999-9999-999999999999"
OLD = "1000000000"
LEAK = "LEAKED-MARKER-CONTENT-2958"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def sweep(monkeypatch):
    """Empty DB keep sets; the fake exec answers by command string."""
    from services import session_cleanup_service as cleanup
    from client_portal import db as portal_db
    from shared_sessions import db as rooms_db
    from database import db as core_db

    for mod in (core_db, portal_db, rooms_db):
        monkeypatch.setattr(mod, "list_active_claude_session_ids", lambda agent: [])

    state = {"marker": None, "removed": [], "listed": False, "calls": []}
    listing = "\n".join(f"{u}.jsonl {OLD}" for u in (CHAT_UUID, ORPHAN_UUID))

    async def _fake_exec(container, cmd, timeout=30, **kw):
        state["calls"].append((cmd, kw))
        if cmd.startswith("rm -f"):
            state["removed"].append(cmd)
            return {"exit_code": 0, "output": ""}
        if "chat-session.json" in cmd:
            marker = state["marker"]
            if callable(marker):
                return await marker()
            return marker
        state["listed"] = True
        return {"exit_code": 0, "output": listing}

    monkeypatch.setattr(cleanup, "execute_command_in_container", _fake_exec)

    def run():
        return _run(cleanup.SessionCleanupService()._sweep_agent(AGENT))

    state["run"] = run
    state["module"] = cleanup
    return state


def _ok(output: str) -> dict:
    return {"exit_code": 0, "output": output}


def test_the_chat_sessions_jsonl_is_kept(sweep):
    sweep["marker"] = _ok(json.dumps({"session_id": CHAT_UUID, "model": "m"}))

    per = sweep["run"]()

    assert per["deleted"] == 1
    assert any(ORPHAN_UUID in c for c in sweep["removed"])
    assert not any(
        CHAT_UUID in c for c in sweep["removed"]
    ), "the chat's own JSONL was reaped — its next turn falls back to a cold start"


def test_no_marker_is_a_normal_sweep(sweep):
    """A fresh agent, a reset, an old image, or a Codex/Gemini runtime."""
    sweep["marker"] = _ok("__NO_CHAT_SESSION__\n")

    per = sweep["run"]()

    assert per["errors"] == 0
    assert per["deleted"] == 2


def test_the_marker_is_read_bounded_and_as_developer(sweep):
    sweep["marker"] = _ok("__NO_CHAT_SESSION__\n")
    sweep["run"]()

    cmd, kw = next(c for c in sweep["calls"] if "chat-session.json" in c[0])
    assert kw.get("user") == "developer"
    assert "head -c 512" in cmd and "timeout 5" in cmd
    assert "cat " not in cmd


def _slow():
    async def _hang():
        await asyncio.sleep(5)
        return _ok("never")

    return _hang


@pytest.mark.parametrize(
    "marker, kind",
    [
        pytest.param(
            lambda: {"exit_code": 1, "output": LEAK},
            "marker_exec_failed",
            id="exec-failed",
        ),
        pytest.param(
            lambda: _ok("{not json " + LEAK), "marker_invalid_json", id="bad-json"
        ),
        pytest.param(
            lambda: _ok(json.dumps({"session_id": LEAK})),
            "marker_bad_uuid",
            id="not-a-uuid",
        ),
        pytest.param(
            lambda: _ok(json.dumps({"session_id": CHAT_UUID + "\n"})),
            "marker_bad_uuid",
            id="uuid-trailing-newline",
        ),
        pytest.param(
            lambda: _ok(json.dumps([CHAT_UUID])), "marker_bad_uuid", id="not-an-object"
        ),
        pytest.param(lambda: "timeout", "marker_timeout", id="timeout"),
    ],
)
def test_an_unreadable_marker_aborts_the_sweep(
    sweep, monkeypatch, caplog, marker, kind
):
    value = marker()
    if value == "timeout":
        monkeypatch.setattr(sweep["module"], "_CHAT_MARKER_READ_TIMEOUT_S", 0.05)
        sweep["marker"] = _slow()
    else:
        sweep["marker"] = value
    caplog.set_level(logging.WARNING)

    per = sweep["run"]()

    assert sweep["removed"] == [], "must not reap against a partial keep set"
    assert sweep["listed"] is False
    assert per["errors"] == 1
    assert per["deleted"] == 0
    assert kind in caplog.text
    assert (
        LEAK not in caplog.text
    ), "agent-writable marker content reached the backend log"


def test_an_exec_that_raises_aborts_the_sweep(sweep):
    async def _boom():
        raise RuntimeError("docker down")

    sweep["marker"] = _boom

    per = sweep["run"]()

    assert sweep["removed"] == []
    assert per["errors"] == 1
