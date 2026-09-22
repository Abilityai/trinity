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
