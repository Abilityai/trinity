"""#3244 — the 429 a sync /chat answers AFTER the agent ran names its execution.

Two 429s leave `POST /api/agents/{name}/chat`. The admission refusal dispatched
nothing. The usage / rate-limit 429 is raised by `_finalize_http_failure` after
the turn ran and its row was written `failed` — and it carried nothing a caller
could read that row by, so the MCP server could only label it "agent busy,
retry", which repeats whatever the partial run already did.

Pinned here: that failure names the row in an additive `X-Trinity-Execution-Id`
response header (the #2889 shape — the body stays byte-identical), and the
admission-side callers that pass no execution keep exactly the headers they had.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import services.chat_execution_service as ces
from services.chat_execution_service import (
    ERROR_CODE_HEADER,
    EXECUTION_ID_HEADER,
    ChatDispatchError,
)

LIMIT = "Claude usage limit reached. Resets at 5pm."


def _agent_error(status: int, detail=LIMIT) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://agent-x:8000/api/chat")
    response = httpx.Response(status, json={"detail": detail}, request=request)
    return httpx.HTTPStatusError(str(status), request=request, response=response)


def _finalize(err, *, task_execution_id="exec-3244", switch_result=None):
    """Run the real finalizer + the real SUB-003 step; return (raised, db)."""
    mdb = MagicMock()
    mdb.get_execution.return_value = None
    fake_switch = SimpleNamespace(
        handle_subscription_failure=AsyncMock(return_value=switch_result),
        is_auth_failure=lambda msg: False,
    )
    with (
        patch.object(ces, "db", mdb),
        patch.object(ces, "get_staged_values", lambda: []),
        patch.object(ces, "activity_service", MagicMock(complete_activity=AsyncMock())),
        patch.dict("sys.modules", {"services.subscription_auto_switch": fake_switch}),
    ):
        with pytest.raises(ChatDispatchError) as exc:
            asyncio.run(
                ces._finalize_http_failure(
                    name="agent-x",
                    e=err,
                    task_execution_id=task_execution_id,
                    chat_activity_id="ca",
                    collaboration_activity_id=None,
                )
            )
    return exc.value, mdb


def test_header_name_is_the_documented_one():
    assert EXECUTION_ID_HEADER == "X-Trinity-Execution-Id"


def test_post_run_429_names_the_failed_row_it_just_wrote():
    raised, mdb = _finalize(_agent_error(429))

    # The turn ran: its row is written failed BEFORE the 429 is raised.
    written = mdb.update_execution_status.call_args.kwargs
    assert written["execution_id"] == "exec-3244"
    assert written["status"] == "failed"

    assert raised.status_code == 429
    assert raised.headers == {
        ERROR_CODE_HEADER: "billing",
        EXECUTION_ID_HEADER: "exec-3244",
    }
    # Additive: the body is the plain string it always was.
    assert raised.detail == LIMIT


def test_auto_switched_429_keeps_its_dict_body_and_names_the_row():
    raised, _ = _finalize(_agent_error(429), switch_result={"new_subscription": "sub-b"})

    assert raised.status_code == 429
    assert raised.headers[EXECUTION_ID_HEADER] == "exec-3244"
    assert raised.headers[ERROR_CODE_HEADER] == "billing"
    assert set(raised.detail) == {"error", "auto_switch", "message", "retry_after"}


@pytest.mark.parametrize("status,http,code", [(503, 503, "auth"), (500, 503, "agent_error")])
def test_every_post_dispatch_failure_names_its_row(status, http, code):
    raised, _ = _finalize(_agent_error(status, "boom"))
    assert raised.status_code == http
    assert raised.headers == {ERROR_CODE_HEADER: code, EXECUTION_ID_HEADER: "exec-3244"}


def test_no_row_no_header():
    """A turn with no execution row has nothing to name — the header is absent,
    never an empty or placeholder value a reader would quote back."""
    raised, mdb = _finalize(_agent_error(429), task_execution_id=None)
    assert raised.headers == {ERROR_CODE_HEADER: "billing"}
    mdb.update_execution_status.assert_not_called()


def test_router_passes_the_headers_through_unchanged():
    """`routers/chat.py` maps a ChatDispatchError 1:1; the header must survive
    FastAPI's HTTPException → response step, where a client reads it."""
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    raised, _ = _finalize(_agent_error(429))
    app = FastAPI()

    @app.post("/chat")
    def _chat():
        raise HTTPException(
            status_code=raised.status_code, detail=raised.detail, headers=raised.headers
        )

    resp = TestClient(app).post("/chat")
    assert resp.status_code == 429
    assert resp.headers["x-trinity-execution-id"] == "exec-3244"
    assert resp.headers["x-trinity-error-code"] == "billing"
    assert resp.json() == {"detail": LIMIT}
