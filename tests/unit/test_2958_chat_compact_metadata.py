"""#2958 AC3 — a chat turn that auto-compacted is attributed on its execution row.

The field case: a 173 s chat execution, 162 s of it an auto-compaction, and the
row said only `success` with a large `duration_ms`. Callers read that as the
target agent being degraded. The task path already writes `compact_metadata`
(the column exists on both migration tracks); the chat path never did.

Also pinned: the `/chat` response's `execution.compaction` summary, so a
synchronous caller sees the one-off delay without a second call, and the #678
structured failure body carrying its compact events onto the FAILED row.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

EVENT = {
    "trigger": "auto",
    "pre_tokens": 173771,
    "post_tokens": 5600,
    "duration_ms": 162585,
    "timestamp": "2026-09-22T10:00:00.000Z",
}


def _await(coro):
    return asyncio.run(coro)


def _finalize_success(metadata: dict):
    from services import chat_execution_service as ces

    response_data = {
        "response": "done",
        "execution_log": [],
        "execution_log_simplified": [],
        "metadata": metadata,
        "session": {"context_tokens": 5600, "context_window": 200000},
        "session_id": "22222222-2222-2222-2222-222222222222",
    }
    resp = MagicMock()
    resp.json.return_value = response_data
    mdb = MagicMock()
    mdb.add_chat_message.return_value = MagicMock(id="msg-1")
    with (
        patch.object(ces, "db", mdb),
        patch.object(ces, "get_staged_values", lambda: []),
        patch.object(ces, "activity_service", MagicMock(complete_activity=AsyncMock())),
        patch.object(ces.idempotency_service, "complete", lambda *a, **k: None),
    ):
        body = _await(
            ces._finalize_chat_success(
                name="agent-x",
                response=resp,
                start_time=datetime.utcnow(),
                session=MagicMock(id="sess-1"),
                current_user=MagicMock(id=1, email="u@example.com", username="u"),
                chat_activity_id="ca",
                collaboration_activity_id=None,
                task_execution_id="exec-2958",
                _chat_subscription_id=None,
                execution=MagicMock(id="q-1"),
                queue_result="done",
                is_queued=False,
                idem="idem-1",
            )
        )
    return body, mdb.update_execution_status.call_args.kwargs


def test_success_row_carries_compact_metadata():
    body, kw = _finalize_success({"cost_usd": 0.02, "compact_events": [EVENT]})

    assert json.loads(kw["compact_metadata"]) == [EVENT]
    assert body["execution"]["compaction"] == {
        "events": 1,
        "trigger": "auto",
        "pre_tokens": 173771,
        "post_tokens": 5600,
        "duration_ms": 162585,
    }


def test_summary_spans_several_compactions():
    second = dict(
        EVENT, trigger="auto", pre_tokens=150000, post_tokens=4200, duration_ms=1000
    )
    body, _kw = _finalize_success({"compact_events": [EVENT, second]})

    summary = body["execution"]["compaction"]
    assert summary["events"] == 2
    assert summary["pre_tokens"] == 173771  # the first compaction's input
    assert summary["post_tokens"] == 4200  # the last compaction's output
    assert summary["duration_ms"] == 162585 + 1000


def test_no_compaction_writes_none():
    body, kw = _finalize_success({"cost_usd": 0.02, "compact_events": []})

    assert kw["compact_metadata"] is None
    assert body["execution"]["compaction"] is None
    assert body["execution"]["was_queued"] is False  # the existing shape is intact


def test_structured_failure_body_carries_compact_metadata_to_the_failed_row():
    """#678's structured 502 body carries the agent's metadata, compact events
    included, because the agent reads the JSONL before its error checks."""
    from services import chat_execution_service as ces

    request = httpx.Request("POST", "http://agent-x:8000/api/chat")
    agent_resp = httpx.Response(
        502,
        json={
            "detail": {
                "message": "completed without a result",
                "metadata": {"compact_events": [EVENT]},
            }
        },
        request=request,
    )
    err = httpx.HTTPStatusError("502", request=request, response=agent_resp)

    mdb = MagicMock()
    mdb.get_execution.return_value = None
    with (
        patch.object(ces, "db", mdb),
        patch.object(ces, "get_staged_values", lambda: []),
        patch.object(ces, "activity_service", MagicMock(complete_activity=AsyncMock())),
        patch.object(ces, "_apply_sub003_autoswitch", AsyncMock()),
    ):
        try:
            _await(
                ces._finalize_http_failure(
                    name="agent-x",
                    e=err,
                    task_execution_id="exec-2958",
                    chat_activity_id="ca",
                    collaboration_activity_id=None,
                )
            )
        except Exception:  # noqa: BLE001 — the finalizer's raise is not under test
            pass

    kw = mdb.update_execution_status.call_args.kwargs
    assert kw["status"] == "failed"
    assert json.loads(kw["compact_metadata"]) == [EVENT]
