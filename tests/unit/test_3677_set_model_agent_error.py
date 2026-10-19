"""#3677 — an agent's own 4xx on the model routes reaches the caller as itself.

`PUT /api/agents/{name}/model` caught every `httpx.HTTPError` in one branch and
answered 503 "Failed to set model". The agent server answers an invalid model
with a 400 that names the valid ones, so a typo read as "service unavailable",
the agent's sentence was lost, and every typo wrote an ERROR line. Two failures
were merged into one:

  * the agent answered with a 4xx  -> its status and its detail, not an ERROR
  * the agent could not be reached -> 503, as before

`GET /api/agents/{name}/model` had the same shape and gets the same split.

The agent is a real `httpx.MockTransport`, so `raise_for_status` and the
transport errors are the real ones, not stand-ins.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

pytestmark = pytest.mark.unit


def _agent(handler):
    @asynccontextmanager
    async def client(name, *a, **k):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            yield c
    return client


def _call(handler, *, method="put", model="invalid-model-xyz"):
    import routers.chat as chat
    from models import ModelChangeRequest

    running = SimpleNamespace(status="running")
    user = SimpleNamespace(id=1, username="owner", role="user")
    with patch.object(chat, "agent_httpx_client", _agent(handler)), \
            patch.object(chat, "get_agent_container", lambda name: running):
        if method == "put":
            coro = chat.set_agent_model(ModelChangeRequest(model=model), name="agent-a", current_user=user)
        else:
            coro = chat.get_agent_model(name="agent-a", current_user=user)
        return asyncio.run(coro)


def _status(handler, **kw):
    with pytest.raises(HTTPException) as exc:
        _call(handler, **kw)
    return exc.value.status_code, exc.value.detail


INVALID = "Invalid model 'invalid-model-xyz'. Valid models: sonnet, opus, haiku"


@pytest.mark.parametrize("method", ["put", "get"])
def test_an_agent_400_reaches_the_caller_with_its_own_sentence(method):
    assert _status(lambda r: httpx.Response(400, json={"detail": INVALID}), method=method) == (400, INVALID)


def test_an_agent_422_keeps_its_status_and_structured_detail():
    detail = [{"loc": ["body", "model"], "msg": "field required", "type": "missing"}]
    assert _status(lambda r: httpx.Response(422, json={"detail": detail})) == (422, detail)


def test_a_4xx_with_a_non_json_body_still_keeps_its_status():
    status, detail = _status(lambda r: httpx.Response(400, text="bad model"))
    assert status == 400 and "bad model" in detail


@pytest.mark.parametrize("method", ["put", "get"])
def test_an_agent_5xx_is_still_unavailable(method):
    status, _ = _status(lambda r: httpx.Response(500, json={"detail": "boom"}), method=method)
    assert status == 503


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow")])
@pytest.mark.parametrize("method", ["put", "get"])
def test_an_unreachable_agent_is_still_503(method, error):
    def handler(request):
        raise error
    status, _ = _status(handler, method=method)
    assert status == 503


def test_a_valid_model_answers_as_before():
    assert _call(lambda r: httpx.Response(200, json={"model": "sonnet"}), model="sonnet") == {"model": "sonnet"}


def test_a_rejected_model_is_not_an_error_log_line(caplog):
    with caplog.at_level(logging.INFO, logger="trinity.errors"):
        _status(lambda r: httpx.Response(400, json={"detail": INVALID}))
    assert not [r for r in caplog.records if r.name == "trinity.errors" and r.levelno >= logging.ERROR]


def test_an_unreachable_agent_still_logs_an_error(caplog):
    def handler(request):
        raise httpx.ConnectError("refused")
    with caplog.at_level(logging.ERROR, logger="trinity.errors"):
        _status(handler)
    assert [r for r in caplog.records if r.name == "trinity.errors" and r.levelno == logging.ERROR]
