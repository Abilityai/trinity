"""#2958 — the chat-session marker has ONE path, pinned on both sides.

The agent server writes its own /api/chat session id to a marker file; the
backend's JSONL reaper reads it over docker exec to keep that JSONL. The two
live in different images and cannot share a constant, so they are pinned here.
A divergence is silent: the reaper sees "no marker", sweeps normally, and the
chat's JSONL is deleted an hour after the chat goes idle.
"""
from __future__ import annotations


def test_agent_writer_and_backend_reader_agree_on_the_marker_path():
    from agent_server.services import chat_session_marker
    from services import session_cleanup_service

    assert chat_session_marker.DEFAULT_MARKER_PATH == session_cleanup_service.CHAT_SESSION_MARKER_PATH


def test_the_env_override_is_not_the_production_path(monkeypatch):
    """The override exists for tests only; with it unset the writer uses the
    pinned default."""
    from agent_server.services import chat_session_marker

    monkeypatch.delenv("TRINITY_CHAT_SESSION_FILE", raising=False)
    assert str(chat_session_marker.marker_path()) == chat_session_marker.DEFAULT_MARKER_PATH


def test_agent_server_startup_clears_a_leftover_marker(tmp_path, monkeypatch):
    """After a restart the in-memory chat id is gone, so a leftover marker would
    pin a JSONL nothing will resume. Runs the REAL registered startup hook.

    NOT a guard for the image's FastAPI version: the first version used
    `add_event_handler`, which the agent image's FastAPI 0.141 lacks (import
    crash) but the host test env's 0.124 has, so this test passed against it.
    Only the in-image smoke caught that."""
    import agent_server.main as agent_main

    marker = tmp_path / "chat-session.json"
    marker.write_text('{"session_id": "c4a7c4a7-2958-4958-8958-295829582958"}')
    monkeypatch.setenv("TRINITY_CHAT_SESSION_FILE", str(marker))

    hooks = [h for h in agent_main.app.router.on_startup if h.__name__ == "_clear_chat_session_marker"]
    assert len(hooks) == 1
    hooks[0]()
    assert not marker.exists()
