"""#2944 — the agent keeps a finished turn's terminal where the backend can
claim it after the connection that was waiting for it is gone.

Two halves on the agent side:

1. `services/retained_results.py` — the disk-backed store: record / get,
   overwrite on the same id (SUB-003 retry), TTL eviction, file cap, transcript
   cap, path containment, the `temp-…` id no-op, and survival across a fresh
   import (the record is on disk, not in a process).
2. `routers/chat.py` — every terminal exit of `/api/task` and `/api/chat`
   retains its envelope BEFORE the response goes out (success, cancel,
   HTTPException), and `GET /api/executions/{id}/result` serves it with the
   coded 404 the backend uses to tell "nothing retained" from an old image.

Modules under test:
    docker/base-image/agent_server/services/retained_results.py
    docker/base-image/agent_server/routers/chat.py
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

import agent_server.routers.chat as chat_mod  # noqa: E402
from agent_server.services import retained_results as rr  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(rr, "RETAINED_DIR", tmp_path / "retained")
    return rr


def _env(status="success", **over):
    base = {
        "status": status, "response": "the answer", "execution_log": [{"type": "result"}],
        "metadata": {"cost_usd": 32.0, "session_id": "sess-1"}, "session_id": "sess-1",
        "terminal_reason": "completed",
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

class TestRetainedResultsStore:
    def test_record_then_get_round_trips_the_envelope_with_a_timestamp(self, store):
        assert store.record("exec-1", _env()) is True
        rec = store.get("exec-1")
        assert rec["envelope"] == _env()
        assert rec["execution_id"] == "exec-1"
        assert isinstance(rec["retained_at"], float)
        assert rec["retained_at_iso"].endswith("+00:00")
        assert rec["truncated_log"] is False

    def test_missing_id_reads_as_none(self, store):
        assert store.get("never-written") is None

    def test_same_id_overwrites_the_last_terminal_stands(self, store):
        """A SUB-003 subscription-switch retry reuses the execution id: the
        attempt-1 FAILED(auth) must not shadow the final SUCCESS."""
        store.record("exec-1", _env("failed", error="auth", error_code="auth"))
        store.record("exec-1", _env("success"))
        assert store.get("exec-1")["envelope"]["status"] == "success"

    def test_unsafe_ids_are_refused_at_both_sinks(self, store):
        for bad in ("../../etc/passwd", "a/b", "", None, 42, "temp-1700000000.123", "x" * 129):
            assert store.record(bad, _env()) is False, bad
            assert store.get(bad) is None, bad
        assert not (store.RETAINED_DIR).exists() or list(store.RETAINED_DIR.iterdir()) == []

    def test_expired_record_reads_as_none_and_is_unlinked(self, store, monkeypatch):
        store.record("exec-1", _env())
        path = store.RETAINED_DIR / "exec-1.json"
        raw = json.loads(path.read_text())
        raw["retained_at"] = time.time() - store.RETAINED_RESULT_TTL_SECONDS - 1
        path.write_text(json.dumps(raw))
        assert store.get("exec-1") is None
        assert not path.exists()

    def test_write_evicts_expired_files_and_keeps_only_the_newest_past_the_cap(self, store, monkeypatch):
        monkeypatch.setattr(store, "RETAINED_RESULT_MAX_FILES", 3)
        for i in range(5):
            store.record(f"exec-{i}", _env())
            os.utime(store.RETAINED_DIR / f"exec-{i}.json", (time.time() + i, time.time() + i))
        store.record("exec-new", _env())
        kept = sorted(p.stem for p in store.RETAINED_DIR.glob("*.json"))
        assert "exec-new" in kept and len(kept) == 3, kept
        # an expired file goes regardless of the cap
        old = store.RETAINED_DIR / "exec-3.json"
        if old.exists():
            stale = time.time() - store.RETAINED_RESULT_TTL_SECONDS - 10
            os.utime(old, (stale, stale))
            store.record("exec-newer", _env())
            assert not old.exists()

    def test_oversized_transcript_is_dropped_but_the_result_kept(self, store, monkeypatch):
        monkeypatch.setattr(store, "RETAINED_LOG_MAX_BYTES", 50)
        store.record("exec-1", _env(execution_log=[{"type": "assistant", "text": "x" * 200}]))
        rec = store.get("exec-1")
        assert rec["truncated_log"] is True
        assert rec["envelope"]["execution_log"] is None
        assert rec["envelope"]["response"] == "the answer"
        assert rec["envelope"]["metadata"]["cost_usd"] == 32.0

    def test_garbage_file_reads_as_none_never_raises(self, store):
        store.RETAINED_DIR.mkdir(parents=True)
        (store.RETAINED_DIR / "exec-1.json").write_text("{not json")
        assert store.get("exec-1") is None
        (store.RETAINED_DIR / "exec-2.json").write_text(json.dumps({"retained_at": "soon"}))
        assert store.get("exec-2") is None

    def test_record_survives_a_fresh_import_because_it_is_on_disk(self, store, monkeypatch):
        store.record("exec-1", _env())
        monkeypatch.setenv("TRINITY_RETAINED_RESULTS_DIR", str(store.RETAINED_DIR))
        fresh = importlib.reload(rr)
        try:
            assert fresh.get("exec-1")["envelope"]["response"] == "the answer"
        finally:
            monkeypatch.delenv("TRINITY_RETAINED_RESULTS_DIR")
            importlib.reload(rr)

    def test_a_failing_write_never_raises(self, store, monkeypatch):
        monkeypatch.setattr(store.Path, "replace", MagicMock(side_effect=OSError("disk full")))
        assert store.record("exec-1", _env()) is False


# ---------------------------------------------------------------------------
# The handlers retain every terminal
# ---------------------------------------------------------------------------

def _task_req(**over):
    base = dict(
        message="do the thing", model="sonnet", allowed_tools=None, system_prompt=None,
        timeout_seconds=300, max_turns=None, execution_id="exec-1", resume_session_id=None,
        persist_session=False, images=None, async_result=False,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _metadata(session_id="sess-1"):
    md = MagicMock()
    md.model_dump.return_value = {"cost_usd": 0.5, "session_id": session_id}
    md.session_id = session_id
    md.cost_usd = 0.5
    md.output_tokens = 1
    md.input_tokens = 1
    md.context_window = 200000
    return md


def _registry(*, was_terminated=False):
    registry = MagicMock()
    registry.was_terminated.return_value = was_terminated
    return registry


def _drive_task(request, *, runtime, registry):
    with (
        patch.object(chat_mod, "get_runtime", return_value=runtime),
        patch.object(chat_mod, "get_process_registry", return_value=registry),
        patch.object(chat_mod, "agent_state", MagicMock()),
        patch.object(chat_mod.result_callback, "try_spawn_async", return_value=False),
    ):
        return asyncio.run(chat_mod.execute_task(request))


class TestTaskHandlerRetains:
    def test_success_is_retained_before_the_response_returns(self, store):
        rt = MagicMock()
        rt.execute_headless = AsyncMock(return_value=("ok", [{"type": "result"}], _metadata(), "sess-1"))
        reply = _drive_task(_task_req(), runtime=rt, registry=_registry())
        assert reply["status"] == "success"
        env = store.get("exec-1")["envelope"]
        assert env["status"] == "success"
        assert env["response"] == "ok"
        assert env["session_id"] == "sess-1"
        assert env["execution_log"] == [{"type": "result"}]
        assert env["metadata"]["cost_usd"] == 0.5
        assert env["terminal_reason"] == "completed"

    def test_cancelled_success_path_is_retained_as_cancelled(self, store):
        rt = MagicMock()
        rt.execute_headless = AsyncMock(return_value=("bye", [], _metadata(), "sess-1"))
        reply = _drive_task(_task_req(), runtime=rt, registry=_registry(was_terminated=True))
        assert reply["status"] == "cancelled"
        env = store.get("exec-1")["envelope"]
        assert env["status"] == "cancelled" and env["terminal_reason"] == "cancelled"
        assert env["error_code"] is None

    def test_http_exception_is_retained_as_the_typed_failure_and_re_raised(self, store):
        rt = MagicMock()
        rt.execute_headless = AsyncMock(side_effect=HTTPException(status_code=503, detail="auth failed"))
        with pytest.raises(HTTPException):
            _drive_task(_task_req(), runtime=rt, registry=_registry())
        env = store.get("exec-1")["envelope"]
        assert env["status"] == "failed"
        assert env["error_code"] == "auth" and env["terminal_reason"] == "auth"
        assert env["error"] == "auth failed"

    def test_http_exception_on_a_terminated_turn_is_retained_as_cancelled(self, store):
        rt = MagicMock()
        rt.execute_headless = AsyncMock(side_effect=HTTPException(status_code=504, detail="killed"))
        reply = _drive_task(_task_req(), runtime=rt, registry=_registry(was_terminated=True))
        assert reply["status"] == "cancelled"
        assert store.get("exec-1")["envelope"]["status"] == "cancelled"

    def test_a_missing_execution_id_retains_nothing(self, store):
        rt = MagicMock()
        rt.execute_headless = AsyncMock(return_value=("ok", [], _metadata(), "sess-1"))
        _drive_task(_task_req(execution_id=None), runtime=rt, registry=_registry())
        assert not store.RETAINED_DIR.exists() or list(store.RETAINED_DIR.iterdir()) == []

    def test_a_retention_failure_never_touches_the_reply(self, store, monkeypatch):
        monkeypatch.setattr(store, "record", MagicMock(side_effect=RuntimeError("boom")))
        rt = MagicMock()
        rt.execute_headless = AsyncMock(return_value=("ok", [], _metadata(), "sess-1"))
        reply = _drive_task(_task_req(), runtime=rt, registry=_registry())
        assert reply["status"] == "success" and reply["response"] == "ok"


class _Lock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _drive_chat(rt, *, execution_id="exec-chat-1"):
    state = MagicMock(
        current_model="sonnet", conversation_history=[], session_total_cost=0.0,
        session_total_output_tokens=0, session_context_tokens=0, session_context_window=200000,
    )
    request = SimpleNamespace(message="hello", model=None, stream=False, system_prompt=None,
                              execution_id=execution_id)
    with (
        patch.object(chat_mod, "get_execution_lock", return_value=_Lock()),
        patch.object(chat_mod, "get_runtime", return_value=rt),
        patch.object(chat_mod, "get_process_registry", return_value=_registry()),
        patch.object(chat_mod, "agent_state", state),
    ):
        return asyncio.run(chat_mod.chat(request))


class TestChatHandlerRetains:
    def test_success_is_retained_with_the_session_id_from_metadata(self, store):
        """`/api/chat` has no top-level session_id — the persisted one rides
        `metadata`, and the envelope must carry it or the row loses its session."""
        rt = MagicMock()
        rt.execute = AsyncMock(return_value=("hi", [], _metadata("sess-chat"), [{"type": "result"}]))
        reply = _drive_chat(rt)
        assert reply["response"] == "hi"
        env = store.get("exec-chat-1")["envelope"]
        assert env["status"] == "success" and env["response"] == "hi"
        assert env["session_id"] == "sess-chat"
        assert env["execution_log"] == [{"type": "result"}]

    def test_http_exception_is_retained_as_failure(self, store):
        rt = MagicMock()
        rt.execute = AsyncMock(side_effect=HTTPException(status_code=504, detail="timed out"))
        with pytest.raises(HTTPException):
            _drive_chat(rt)
        env = store.get("exec-chat-1")["envelope"]
        assert env["status"] == "failed" and env["terminal_reason"] == "max_duration"


# ---------------------------------------------------------------------------
# GET /api/executions/{id}/result
# ---------------------------------------------------------------------------

class TestResultRoute:
    def test_serves_the_envelope_plus_retained_at_and_truncation_flag(self, store):
        store.record("exec-1", _env())
        body = asyncio.run(chat_mod.get_execution_retained_result("exec-1"))
        assert body["status"] == "success" and body["response"] == "the answer"
        assert body["session_id"] == "sess-1"
        assert body["truncated_log"] is False
        assert body["retained_at"].endswith("+00:00")

    @pytest.mark.parametrize("eid", ["exec-nope", "../escape", "temp-1.2", ""])
    def test_missing_and_malformed_ids_answer_the_coded_404(self, store, eid):
        """The backend tells 'asked, nothing there' from an OLD image's bare
        FastAPI `{"detail": "Not Found"}` by this code — so a malformed id must
        answer it too, never a 400 and never the bare shape."""
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat_mod.get_execution_retained_result(eid))
        assert exc.value.status_code == 404
        assert exc.value.detail == {"code": "no_retained_result"}
