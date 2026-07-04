"""Unit tests for #1018 — settlement-ordering / honest unsettled status on the
paid x402 boundary (``routers.paid.paid_chat``).

The filed bug: a settle that fails after retries still returned HTTP 200 with a
top-level ``status: "success"`` — the paid resource released without a confirmed
transfer. This suite pins the Tier-1 fix:

  * settle-fail → top-level ``status == "success_unsettled"`` (NOT ``"success"``),
    response still delivered, ``settle_failed`` logged.
  * concurrent settle (effect-guard "already in progress") → ``success_unsettled``
    + ``settle_in_progress: true`` (NOT ``"success"``, NOT ``settle_retry_needed``).
  * ``Idempotency-Key`` wiring (Invariant #18) keyed on (token+body): in-flight
    duplicate → 409; completed *settled* snapshot → verbatim replay +
    ``X-Idempotent-Replay``; completed *unsettled* snapshot → re-drive settle,
    converge, then a THIRD request does NOT re-drive settle.
  * ``derive_payment_key`` None-safety (never a constant hash) — the CRITICAL-B
    cross-request-collision guard.
  * ``fail()`` on the 403 / execution-exception / failed-execution paths so those
    retry; ``failed`` executions no longer leak the response body.

Runs without a live backend. The parent unit conftest points ``TRINITY_DB_PATH``
at a fresh temp SQLite (with a real ``idempotency_keys`` table), so the real
``db.idempotency_*`` ops back the dedup behavior; only the Nevermined-specific db
methods + the payment/task services are mocked. Keys are unique per test (unique
tokens) so cross-test state in the shared process DB can't bleed.
"""
from __future__ import annotations

import json
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


class _Req:
    def __init__(self, token: str = "tok-abc"):
        self.headers = {"payment-signature": token} if token else {}
        self.base_url = "http://localhost/"


def _body(json_resp_or_dict):
    """Normalize a plain-dict OR JSONResponse return into (status_code, body)."""
    if hasattr(json_resp_or_dict, "body"):
        return json_resp_or_dict.status_code, json.loads(bytes(json_resp_or_dict.body).decode())
    return 200, json_resp_or_dict


def _config():
    return SimpleNamespace(
        enabled=True, nvm_environment="testnet", credits_per_request=1, nvm_plan_id="plan-1",
    )


def _settle(success, *, error=None, tx="0xtx", bal=10):
    return SimpleNamespace(
        success=success, tx_hash=tx if success else None,
        remaining_balance=bal if success else None,
        agent_request_id="req-1", error=error, payer="0xpayer",
        credits_redeemed="1",
    )


def _drive(
    *,
    token="tok-abc",
    message="hi",
    exec_status="success",
    exec_raises=False,
    settle_result=None,
    settle_side_effect=None,
    idempotency_key=None,
    verify_success=True,
    log_mock=None,
    task_mock=None,
    settle_mock=None,
):
    """Invoke ``paid.paid_chat`` with mocked payment/task services over the REAL
    idempotency table. Returns (status_code, body, mocks-dict)."""
    import routers.paid as paid

    config = _config()
    verify_result = SimpleNamespace(
        success=verify_success, payer="0xpayer", agent_request_id="req-1",
        error=None if verify_success else "bad token",
    )
    if settle_mock is None:
        if settle_side_effect is not None:
            settle_mock = AsyncMock(side_effect=settle_side_effect)
        else:
            settle_mock = AsyncMock(return_value=settle_result or _settle(True))
    payment_service = MagicMock(
        verify_payment=AsyncMock(return_value=verify_result),
        settle_payment_once=settle_mock,
    )

    if task_mock is None:
        if exec_raises:
            task_mock = AsyncMock(side_effect=RuntimeError("boom"))
        else:
            exec_result = SimpleNamespace(
                status=exec_status, response="the answer", execution_id="exec-1",
            )
            task_mock = AsyncMock(return_value=exec_result)
    task_service = MagicMock(execute_task=task_mock)

    log_mock = log_mock or MagicMock(return_value=None)

    with (
        patch.object(paid, "NEVERMINED_AVAILABLE", True),
        patch.object(paid, "get_nevermined_payment_service", return_value=payment_service),
        patch.object(paid, "get_task_execution_service", return_value=task_service),
        patch.object(paid, "build_public_channel_caller_prompt", return_value=""),
        patch.object(paid.db, "get_nevermined_config_with_key",
                     return_value={"config": config, "nvm_api_key": "nvm-key"}),
        patch.object(paid.db, "get_public_channel_model", return_value=None),
        patch.object(paid.db, "log_nevermined_payment", log_mock),
    ):
        resp = _await(paid.paid_chat(
            "agent-a",
            SimpleNamespace(message=message, session_id=None),
            _Req(token),
            idempotency_key=idempotency_key,
        ))
    status_code, body = _body(resp)
    return status_code, body, {
        "settle": settle_mock, "task": task_mock, "log": log_mock, "resp": resp,
    }


# ---------------------------------------------------------------------------
# 1. Honest unsettled status — THE filed bug
# ---------------------------------------------------------------------------

def test_settle_fail_is_success_unsettled_not_success():
    status_code, body, m = _drive(
        token="tok-settlefail-1",
        settle_result=_settle(False, error="provider timeout"),
    )
    assert status_code == 200
    assert body["status"] == "success_unsettled"        # NOT "success" — the bug
    assert body["response"] == "the answer"             # completed work still delivered
    assert body["payment"]["settled"] is False
    assert body["payment"]["settle_retry_needed"] is True
    assert body["payment"]["error"] == "provider timeout"
    # settle_failed logged (else a later real settle is never recorded)
    actions = [c.kwargs.get("action") for c in m["log"].call_args_list]
    assert "settle_failed" in actions


# ---------------------------------------------------------------------------
# 2. Honest concurrent-settle path
# ---------------------------------------------------------------------------

def test_concurrent_settle_in_progress_is_success_unsettled():
    status_code, body, m = _drive(
        token="tok-concurrent-1",
        settle_result=_settle(False, error="settlement already in progress"),
    )
    assert status_code == 200
    assert body["status"] == "success_unsettled"        # NOT "success"
    assert body["payment"]["settled"] is False
    assert body["payment"]["settle_in_progress"] is True
    assert "settle_retry_needed" not in body["payment"]
    # The concurrently-running settle logs its own outcome — we must NOT log a
    # spurious settle_failed for the in-progress case.
    actions = [c.kwargs.get("action") for c in m["log"].call_args_list]
    assert "settle_failed" not in actions


# ---------------------------------------------------------------------------
# 3. Idempotency wiring (Invariant #18)
# ---------------------------------------------------------------------------

def test_in_flight_duplicate_returns_409():
    import routers.paid as paid
    from services import idempotency_service as idem

    token, message = "tok-inflight-1", "hi"
    scope = idem.make_agent_scope("agent-a")
    key = idem.derive_payment_key(token, message.encode())
    # Simulate a concurrent duplicate mid-flight by pre-claiming the key.
    paid.db.idempotency_claim(scope, key)

    status_code, body, _ = _drive(token=token, message=message)
    assert status_code == 409
    assert "duplicate" in body["detail"].lower()


def test_completed_settled_snapshot_replays_verbatim():
    token, message = "tok-settled-replay-1", "hi"
    # First request settles cleanly.
    sc1, b1, m1 = _drive(token=token, message=message, settle_result=_settle(True))
    assert b1["status"] == "success"
    assert b1["payment"]["settled"] is True
    assert m1["task"].await_count == 1

    # Second identical request replays the stored settled snapshot — no re-execute.
    sc2, b2, m2 = _drive(token=token, message=message, settle_result=_settle(True))
    assert sc2 == 200
    assert b2["status"] == "success"
    assert b2["payment"]["settled"] is True
    assert m2["task"].await_count == 0                  # LLM NOT re-run on replay
    assert m2["settle"].await_count == 0                # settle NOT re-driven
    assert m2["resp"].headers.get("X-Idempotent-Replay") == "true"


def test_replay_resettle_convergence():
    """Must-fix C: a replay of an *unsettled* snapshot re-drives settle (succeeds →
    settled); a THIRD request then does NOT re-run settle (snapshot upgraded)."""
    token, message = "tok-resettle-1", "hi"

    # 1st: settle FAILS → success_unsettled, snapshot stored unsettled.
    sc1, b1, m1 = _drive(token=token, message=message, settle_result=_settle(False, error="blip"))
    assert b1["status"] == "success_unsettled"
    assert m1["task"].await_count == 1

    # 2nd: same (token+body) → completed-unsettled replay → re-drive settle (now
    # SUCCEEDS) → converge to settled. LLM must NOT re-run.
    sc2, b2, m2 = _drive(token=token, message=message, settle_result=_settle(True))
    assert sc2 == 200
    assert b2["status"] == "success"
    assert b2["payment"]["settled"] is True
    assert m2["task"].await_count == 0                  # no re-execute
    assert m2["settle"].await_count == 1                # settle re-driven once

    # 3rd: snapshot now settled → verbatim replay, settle NOT re-driven.
    sc3, b3, m3 = _drive(token=token, message=message, settle_result=_settle(True))
    assert b3["status"] == "success"
    assert b3["payment"]["settled"] is True
    assert m3["task"].await_count == 0
    assert m3["settle"].await_count == 0                # NOT re-driven — converged
    assert m3["resp"].headers.get("X-Idempotent-Replay") == "true"


# ---------------------------------------------------------------------------
# 4. derive_payment_key — None-safety (CRITICAL-B) + (token+body) keying
# ---------------------------------------------------------------------------

def test_derive_payment_key_none_safe():
    from services import idempotency_service as idem

    assert idem.derive_payment_key(None, b"x") is None
    assert idem.derive_payment_key("t", None) is None
    assert idem.derive_payment_key("", b"x") is None
    assert idem.derive_payment_key("t", b"") is None
    # Non-empty inputs → a real, prefixed key; DISTINCT falsy inputs never collide
    # into a constant hash (they are all None, so begin() disables dedup).
    k = idem.derive_payment_key("t", b"x")
    assert k and k.startswith("paid:")


def test_key_is_token_plus_body_not_agent_request_id():
    from services import idempotency_service as idem

    # Same token, different body → distinct keys.
    k_a = idem.derive_payment_key("tok", b"question A")
    k_b = idem.derive_payment_key("tok", b"question B")
    assert k_a != k_b
    # Same token+body → identical key regardless of any client-supplied header
    # (the header does not participate in derivation).
    assert idem.derive_payment_key("tok", b"same") == idem.derive_payment_key("tok", b"same")


def test_different_body_same_token_both_execute():
    token = "tok-diffbody-1"
    _, b1, m1 = _drive(token=token, message="question one", settle_result=_settle(True))
    _, b2, m2 = _drive(token=token, message="question two", settle_result=_settle(True))
    # Distinct keys → both run the LLM (no false dedup).
    assert m1["task"].await_count == 1
    assert m2["task"].await_count == 1
    assert b1["status"] == "success" and b2["status"] == "success"


def test_divergent_client_header_does_not_fork_execution():
    token, message = "tok-header-1", "hi"
    _, b1, m1 = _drive(token=token, message=message, idempotency_key="header-1",
                       settle_result=_settle(True))
    # Second request: SAME token+body, DIFFERENT client header → must still dedup.
    _, b2, m2 = _drive(token=token, message=message, idempotency_key="header-2",
                       settle_result=_settle(True))
    assert m1["task"].await_count == 1
    assert m2["task"].await_count == 0                  # header divergence ignored
    assert b2["payment"]["settled"] is True


# ---------------------------------------------------------------------------
# 5. fail() paths — rejected/exception leave the boundary retryable
# ---------------------------------------------------------------------------

def test_rejected_403_leaves_no_idempotency_row():
    import routers.paid as paid
    from services import idempotency_service as idem

    token, message = "tok-403-1", "hi"
    status_code, body, _ = _drive(token=token, message=message, verify_success=False)
    assert status_code == 403
    # begin() is placed AFTER verify — a 403 must not consume a key.
    scope = idem.make_agent_scope("agent-a")
    key = idem.derive_payment_key(token, message.encode())
    assert paid.db.idempotency_claim(scope, key)["state"] == "new"  # was never claimed


def test_execution_exception_releases_claim():
    import routers.paid as paid
    from services import idempotency_service as idem

    token, message = "tok-execexc-1", "hi"
    status_code, body, _ = _drive(token=token, message=message, exec_raises=True)
    assert status_code == 500
    # The claim was released, so a retry can re-execute (claim reads as new).
    scope = idem.make_agent_scope("agent-a")
    key = idem.derive_payment_key(token, message.encode())
    assert paid.db.idempotency_claim(scope, key)["state"] == "new"


def test_failed_execution_no_body_leak_and_no_settle():
    token = "tok-failed-1"
    status_code, body, m = _drive(token=token, exec_status="failed", settle_result=_settle(True))
    assert status_code == 200
    assert body["status"] == "failed"
    assert body["payment"]["settled"] is False
    assert "response" not in body                       # #1018 — no partial body leak
    m["settle"].assert_not_awaited()                    # never settle a failed turn


def test_cancelled_execution_keeps_body_and_no_settle():
    """#679 regression: a cancelled turn keeps its response and still never settles."""
    token = "tok-cancelled-1"
    status_code, body, m = _drive(token=token, exec_status="cancelled", settle_result=_settle(True))
    assert body["status"] == "cancelled"
    assert body["response"] == "the answer"             # cancelled keeps its body
    m["settle"].assert_not_awaited()


# ---------------------------------------------------------------------------
# 6. /retry-settlement is honest — 501, not a misleading 200 "queued" stub
# ---------------------------------------------------------------------------

def _admin():
    return SimpleNamespace(role="admin", username="admin")


def _drive_retry(*, role="admin", log_entry="settle_failed"):
    """Invoke nevermined.retry_settlement with mocked db + user. Returns the raised
    HTTPException (all outcomes are exceptions)."""
    import routers.nevermined as nvm
    from fastapi import HTTPException

    entry = None
    if log_entry is not None:
        entry = SimpleNamespace(action=log_entry, agent_name="agent-a")
    mock_db = MagicMock()
    mock_db.get_nevermined_payment_log_entry.return_value = entry
    user = SimpleNamespace(role=role, username="u")

    with (
        patch.object(nvm, "NEVERMINED_AVAILABLE", True),
        patch.object(nvm, "db", mock_db),
    ):
        try:
            _await(nvm.retry_settlement("log-1", current_user=user))
        except HTTPException as e:
            return e
    raise AssertionError("expected HTTPException")


def test_retry_settlement_settle_failed_is_honest_501():
    exc = _drive_retry(log_entry="settle_failed")
    assert exc.status_code == 501                        # was a lying 200 "queued" stub
    assert "not stored" in exc.detail
    assert "payment-signature" in exc.detail             # points the caller to the real path


def test_retry_settlement_non_admin_403():
    exc = _drive_retry(role="user")
    assert exc.status_code == 403


def test_retry_settlement_missing_log_404():
    exc = _drive_retry(log_entry=None)
    assert exc.status_code == 404


def test_retry_settlement_non_failed_action_400():
    exc = _drive_retry(log_entry="settle")
    assert exc.status_code == 400
