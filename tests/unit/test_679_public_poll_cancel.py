"""Unit tests for #679 (F2): the public async poll endpoint
(``public_execution_status`` in ``routers.public``) must surface the cancel
reason for a CANCELLED execution.

Before F2 the poller returned ``{"status":"cancelled","response":null,"error":null}``
— the row already carries the ``cancelled`` status and a "cancelled by user"
error, but the GET suppressed both because the conditions only matched
``("success","failed")`` / ``"failed"``. A public caller polling after a cancel
got no reason. F2 surfaces ``error`` (and ``response``) for cancelled too.

Pure unit test — no running backend; the four request-scoped helpers are mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

pytestmark = pytest.mark.unit


def _await(coro):
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _execution(status: str, *, response: str | None, error: str | None):
    ex = MagicMock()
    ex.id = "exec-679"
    ex.agent_name = "pub-agent"
    ex.status = status
    ex.response = response
    ex.error = error
    return ex


def _poll(execution):
    import routers.public as public_mod

    with (
        patch.object(public_mod, "_get_client_ip", return_value="1.2.3.4"),
        patch.object(public_mod, "check_public_link_rate_limit", MagicMock()),
        patch.object(public_mod, "_validate_public_link", return_value={"agent_name": "pub-agent"}),
        patch.object(public_mod, "db", MagicMock(get_execution=MagicMock(return_value=execution))),
    ):
        return _await(
            public_mod.public_execution_status(
                token="tok", execution_id="exec-679", request=MagicMock()
            )
        )


def test_cancelled_surfaces_error_and_response():
    out = _poll(_execution("cancelled", response="partial work", error="Execution cancelled by user"))
    assert out["status"] == "cancelled"
    assert out["error"] == "Execution cancelled by user"
    assert out["response"] == "partial work"


def test_failed_still_surfaces_error_regression():
    out = _poll(_execution("failed", response=None, error="boom"))
    assert out["status"] == "failed"
    assert out["error"] == "boom"


def test_running_hides_response_and_error():
    out = _poll(_execution("running", response=None, error=None))
    assert out["status"] == "running"
    assert out["response"] is None
    assert out["error"] is None
