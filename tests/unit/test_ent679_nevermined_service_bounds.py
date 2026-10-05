"""`endpoint`, `retryable` and the facilitator bound (abilityai/trinity-enterprise#679).

Three properties of `services/nevermined_payment_service.py` that the A2A
payment gate depends on and that nothing previously exercised:

1. **`endpoint`** (decision 19). The 402's `resource.url` was hard-coded to the
   paid chat door. An x402 v3 token signs `resourceUrl` and the facilitator
   compares origin+path, so a token minted from the A2A door's 402 cannot
   authorize a call verified against the paid URL — and a non-Trinity client
   follows `resource.url` verbatim. The default is unchanged, which is the half
   that keeps the paid door byte-identical.
2. **`retryable`** (E7). A facilitator timeout is Trinity failing to decide, not
   the caller's token being bad. Only the second should make a client go buy
   another one.
3. **The concurrency bound** (E8). Every facilitator call occupies a thread for
   15-97 s and a priced agent's door needs no credential to make us dial out, so
   the in-flight count is bounded fleet-wide and a saturated gate answers
   "busy, retryable" instead of queueing without limit.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from services import nevermined_payment_service as nps  # noqa: E402

pytestmark = pytest.mark.asyncio

PAID_URL = "http://localhost/api/paid/agent-a/chat"
A2A_URL = "http://localhost/a2a/agent-a"


def _config():
    return SimpleNamespace(
        agent_name="agent-a", nvm_plan_id="plan-1", nvm_agent_id="did:nv:1",
        nvm_environment="sandbox", credits_per_request=1, enabled=True,
    )


class _Facilitator:
    """Stands in for `payments.facilitator` — records what it was handed."""

    def __init__(self, *, verify=None, settle=None):
        self._verify = verify
        self._settle = settle
        self.verify_args = []
        self.settle_args = []

    def verify_permissions(self, payment_required, access_token, *rest):
        self.verify_args.append(payment_required)
        if isinstance(self._verify, BaseException):
            raise self._verify
        return self._verify

    def settle_permissions(self, payment_required, access_token, max_amount=None,
                           agent_request_id=None):
        self.settle_args.append(payment_required)
        if isinstance(self._settle, BaseException):
            raise self._settle
        return self._settle


def _service(facilitator):
    svc = nps.NeverminedPaymentService()
    svc._get_payments_client = lambda *a, **k: SimpleNamespace(facilitator=facilitator)
    return svc


def _verify_reply(valid=True):
    return SimpleNamespace(is_valid=valid, payer="0xpayer", agent_request_id="areq-1",
                           invalid_reason=None if valid else "nope")


@pytest.fixture(autouse=True)
def _fresh_gate(monkeypatch):
    """A clean, per-loop semaphore for every test in this file."""
    monkeypatch.setattr(nps, "_FACILITATOR_GATES", type(nps._FACILITATOR_GATES)())
    yield


# ---------------------------------------------------------------------------
# 1. endpoint
# ---------------------------------------------------------------------------

async def test_the_402_resource_url_defaults_to_the_paid_door():
    body = nps.NeverminedPaymentService().build_402_response(_config(), "http://localhost")
    assert body["resource"]["url"] == PAID_URL


async def test_the_402_resource_url_can_be_bound_to_another_door():
    body = nps.NeverminedPaymentService().build_402_response(
        _config(), "http://localhost", endpoint=A2A_URL,
    )
    assert body["resource"]["url"] == A2A_URL
    # Everything else about the requirements is the SAME plan — "same
    # requirements as the paid door" means same plan, a different resource.
    paid = nps.NeverminedPaymentService().build_402_response(_config(), "http://localhost")
    assert body["accepts"] == paid["accepts"]


async def test_verify_is_bound_to_the_endpoint_it_was_given():
    facilitator = _Facilitator(verify=_verify_reply())
    result = await _service(facilitator).verify_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok", base_url="http://localhost", endpoint=A2A_URL,
    )
    assert result.success is True
    assert facilitator.verify_args[0].resource.url == A2A_URL


async def test_verify_without_an_endpoint_still_uses_the_paid_door():
    facilitator = _Facilitator(verify=_verify_reply())
    await _service(facilitator).verify_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok", base_url="http://localhost",
    )
    assert facilitator.verify_args[0].resource.url == PAID_URL


async def test_settle_is_bound_to_the_same_endpoint_as_verify():
    """A settle against a different resource than the verify would be rejected."""
    facilitator = _Facilitator(settle=SimpleNamespace(
        success=True, payer="0xpayer", credits_redeemed="1", remaining_balance="9",
        transaction="0xtx", error_reason=None,
    ))
    result = await _service(facilitator).settle_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok", base_url="http://localhost", endpoint=A2A_URL,
    )
    assert result.success is True
    assert facilitator.settle_args[0].resource.url == A2A_URL


# ---------------------------------------------------------------------------
# 2. retryable
# ---------------------------------------------------------------------------

async def test_a_rejected_token_is_not_retryable():
    """The facilitator DECIDED: this token is bad. Buying another is the fix."""
    result = await _service(_Facilitator(verify=_verify_reply(valid=False))).verify_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok",
    )
    assert result.success is False
    assert result.retryable is False
    assert result.error == "nope"


async def test_a_verify_timeout_is_retryable():
    """`asyncio.wait_for`'s own exception, raised from the thread it wraps."""
    result = await _service(_Facilitator(verify=asyncio.TimeoutError())).verify_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok",
    )
    assert result.success is False
    assert result.retryable is True
    assert result.error == "Payment verification timed out"


async def test_an_sdk_error_is_retryable():
    result = await _service(_Facilitator(verify=RuntimeError("socket reset"))).verify_payment(
        nvm_api_key="k", nvm_environment="sandbox", config=_config(),
        access_token="tok",
    )
    assert result.success is False
    assert result.retryable is True


async def test_retryable_is_not_part_of_a_stored_settle_receipt():
    """A snapshot written before this field must still replay (#1084)."""
    receipt = nps.NeverminedPaymentResult(success=True, tx_hash="0xtx", retryable=True)
    snapshot = nps._settle_snapshot(receipt)
    assert "retryable" not in snapshot
    assert nps.NeverminedPaymentResult(**snapshot).retryable is False


# ---------------------------------------------------------------------------
# 3. the facilitator concurrency bound
# ---------------------------------------------------------------------------

async def test_the_gate_admits_up_to_the_limit_and_then_refuses(monkeypatch):
    monkeypatch.setattr(nps, "NEVERMINED_MAX_INFLIGHT", 1)
    monkeypatch.setattr(nps, "NEVERMINED_FACILITATOR_WAIT_SECONDS", 0.05)

    async with nps.facilitator_slot():
        with pytest.raises(nps.FacilitatorBusy):
            async with nps.facilitator_slot():
                pytest.fail("the second slot must not be granted")

    # Released again once the first call finishes.
    async with nps.facilitator_slot():
        pass


async def test_a_saturated_gate_makes_verify_busy_and_retryable_without_dialling(monkeypatch):
    monkeypatch.setattr(nps, "NEVERMINED_MAX_INFLIGHT", 1)
    monkeypatch.setattr(nps, "NEVERMINED_FACILITATOR_WAIT_SECONDS", 0.05)
    facilitator = _Facilitator(verify=_verify_reply())

    async with nps.facilitator_slot():
        result = await _service(facilitator).verify_payment(
            nvm_api_key="k", nvm_environment="sandbox", config=_config(),
            access_token="tok",
        )

    assert result.success is False
    assert result.retryable is True
    assert "busy" in result.error
    assert facilitator.verify_args == [], "no thread may be occupied when refused"


async def test_a_saturated_gate_makes_settle_fail_retryably_rather_than_burn(monkeypatch):
    monkeypatch.setattr(nps, "NEVERMINED_MAX_INFLIGHT", 1)
    monkeypatch.setattr(nps, "NEVERMINED_FACILITATOR_WAIT_SECONDS", 0.01)
    # Skip the 1 s + 2 s retry backoff; the branch under test is the refusal,
    # not the waiting. Captured first — patching `asyncio.sleep` with a lambda
    # that calls `asyncio.sleep` is infinite recursion.
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
    facilitator = _Facilitator(settle=SimpleNamespace(
        success=True, payer="0xp", credits_redeemed="1", remaining_balance="9",
        transaction="0xtx", error_reason=None,
    ))

    async with nps.facilitator_slot():
        result = await _service(facilitator).settle_payment(
            nvm_api_key="k", nvm_environment="sandbox", config=_config(),
            access_token="tok",
        )

    assert result.success is False
    assert facilitator.settle_args == []        # nothing burned
    assert "concurrency limit" in result.error  # named, not a generic failure


async def test_the_bound_is_fleet_wide_not_per_agent(monkeypatch):
    """One semaphore for every agent: the thread pool is a platform resource."""
    monkeypatch.setattr(nps, "NEVERMINED_MAX_INFLIGHT", 1)
    monkeypatch.setattr(nps, "NEVERMINED_FACILITATOR_WAIT_SECONDS", 0.05)
    other = SimpleNamespace(
        agent_name="agent-b", nvm_plan_id="plan-2", nvm_agent_id="did:nv:2",
        nvm_environment="sandbox", credits_per_request=1, enabled=True,
    )
    facilitator = _Facilitator(verify=_verify_reply())

    async with nps.facilitator_slot():
        result = await _service(facilitator).verify_payment(
            nvm_api_key="k", nvm_environment="sandbox", config=other,
            access_token="tok",
        )
    assert result.retryable is True
