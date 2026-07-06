"""Unit tests for #679 caller-audit: CANCELLED is treated as non-success at
every ``execute_task`` status-branching caller.

The two highest-value testable units:
  * ``routers.paid.paid_chat`` — a CANCELLED turn must NOT settle payment
    (closes the charge-on-cancel money bug — CRITICAL).
  * ``services.validation_service._parse_validation_response`` — a CANCELLED
    validation turn is a non-success ERROR, not an empty verdict.

The remaining inverse-gate sites (public sync/async, message_router, chat) are a
mechanical ``== "failed"`` → ``in ("failed", "cancelled")`` broadening; the loop
caller is covered in test_loop_service.py, and the end-to-end cancel→CANCELLED
flow is exercised by the agent-exercise stage of /verify-local.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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


# ---------------------------------------------------------------------------
# paid.py — no settle on cancel (CRITICAL money bug)
# ---------------------------------------------------------------------------
class _PaidRequest:
    def __init__(self, token: str = "tok-abc"):
        self.headers = {"payment-signature": token}
        self.base_url = "http://localhost/"


def _drive_paid(exec_status: str):
    """Drive paid_chat to the post-execute branch with a scripted result status.
    Returns (response_json, settle_mock).

    Each status uses a distinct (payment-signature, message) pair so the #1018
    idempotency key — sha256(token \\x00 body) over the REAL persistent
    ``database.db`` (idempotency_service is not mocked) — is unique per test.
    A shared key made these order-dependent: if the success case ran first it
    left a completed+settled snapshot that the cancelled/failed cases then
    replayed as ``status="success"`` (regression-diff flake under pytest-randomly).
    """
    import routers.paid as paid

    config = SimpleNamespace(
        enabled=True, nvm_environment="testnet", credits_per_request=1,
    )
    mock_db = MagicMock()
    mock_db.get_nevermined_config_with_key.return_value = {
        "config": config, "nvm_api_key": "nvm-key",
    }
    mock_db.log_nevermined_payment.return_value = None

    verify_result = SimpleNamespace(
        success=True, payer="0xpayer", agent_request_id="req-1", error=None,
    )
    settle_mock = AsyncMock(return_value=SimpleNamespace(
        success=True, tx_hash="0xtx", remaining_balance=10, error=None,
    ))
    # paid_chat settles the success path through settle_payment_once (the
    # effect-scoped idempotency wrapper, #1084), not bare settle_payment.
    payment_service = MagicMock(
        verify_payment=AsyncMock(return_value=verify_result),
        settle_payment_once=settle_mock,
    )

    exec_result = SimpleNamespace(
        status=exec_status, response="partial work", execution_id="exec-1",
    )
    task_service = MagicMock(execute_task=AsyncMock(return_value=exec_result))

    with (
        patch.object(paid, "NEVERMINED_AVAILABLE", True),
        patch.object(paid, "db", mock_db),
        patch.object(paid, "get_nevermined_payment_service", return_value=payment_service),
        patch.object(paid, "get_task_execution_service", return_value=task_service),
    ):
        resp = _await(paid.paid_chat(
            "agent-a",
            SimpleNamespace(message=f"hi-{exec_status}", session_id=None),
            _PaidRequest(token=f"tok-{exec_status}"),
        ))
    # The success path returns a plain dict; the failed/cancelled path returns a
    # JSONResponse (whose body is the JSON we want to inspect).
    if hasattr(resp, "body"):
        import json as _json
        body = _json.loads(bytes(resp.body).decode())
    else:
        body = resp
    return body, settle_mock


def test_paid_cancelled_does_not_settle():
    body, settle_mock = _drive_paid("cancelled")
    settle_mock.assert_not_awaited()  # the money bug: no charge on cancel
    assert body["status"] == "cancelled"
    assert body["payment"]["settled"] is False


def test_paid_failed_does_not_settle():
    body, settle_mock = _drive_paid("failed")
    settle_mock.assert_not_awaited()
    assert body["status"] == "failed"
    assert body["payment"]["settled"] is False


def test_paid_success_does_settle():
    """Regression: a genuine success still settles (the change didn't over-broaden)."""
    body, settle_mock = _drive_paid("success")
    settle_mock.assert_awaited_once()
    assert body["status"] == "success"
    assert body["payment"]["settled"] is True


# ---------------------------------------------------------------------------
# validation_service — cancel → ERROR (non-success), not an empty verdict
# ---------------------------------------------------------------------------
def _validation_result(status: str):
    return SimpleNamespace(status=status, response="", error="Execution cancelled by user")


def test_validation_cancelled_is_error_not_parsed():
    from services.validation_service import ValidationService
    from services.validation_service import ValidationStatus

    svc = ValidationService.__new__(ValidationService)  # no __init__ deps needed
    out = svc._parse_validation_response(_validation_result("cancelled"))
    assert out.status == ValidationStatus.ERROR
    assert "cancelled" in out.summary.lower()


def test_validation_failed_is_error():
    from services.validation_service import ValidationService, ValidationStatus

    svc = ValidationService.__new__(ValidationService)
    out = svc._parse_validation_response(_validation_result("failed"))
    assert out.status == ValidationStatus.ERROR
