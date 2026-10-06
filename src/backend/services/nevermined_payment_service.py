"""
Nevermined x402 payment service (NVM-001).

Handles verify/settle lifecycle via the payments-py SDK.
All SDK calls are sync internally, so they are wrapped in asyncio.to_thread().
"""

import asyncio
import contextlib
import logging
import os
import weakref
from typing import Optional

from db_models import NeverminedConfig, NeverminedPaymentResult
from services import idempotency_service

logger = logging.getLogger(__name__)


#: Fleet-wide ceiling on CONCURRENT facilitator calls (#679 E8). Every verify
#: (15 s) and settle attempt (3 x 30 s) runs on the default `to_thread`
#: executor, so a slow facilitator otherwise holds backend threads for the
#: whole fleet — and a priced agent's public URL needs no credential to make us
#: dial out. Per-IP rate limiting alone does not bound that, because the bound
#: has to hold across IPs.
NEVERMINED_MAX_INFLIGHT = int(os.getenv("NEVERMINED_MAX_INFLIGHT", "8"))

#: How long a call waits for a slot before giving up. Bounded rather than
#: unbounded because the caller is holding an HTTP request open: "busy, retry"
#: is an honest answer, a queue that grows without limit is not.
NEVERMINED_FACILITATOR_WAIT_SECONDS = float(
    os.getenv("NEVERMINED_FACILITATOR_WAIT_SECONDS", "5.0")
)

#: One semaphore per event loop. Module-level `asyncio.Semaphore()` would bind
#: the first loop that contends on it, which in a test suite is whichever test
#: ran first; a WeakKeyDictionary keyed on the running loop keeps the bound
#: real in production (one loop per worker) without that cross-loop trap.
_FACILITATOR_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class FacilitatorBusy(Exception):
    """No facilitator slot became free within the wait budget."""


def _facilitator_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _FACILITATOR_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(NEVERMINED_MAX_INFLIGHT)
        _FACILITATOR_GATES[loop] = gate
    return gate


@contextlib.asynccontextmanager
async def facilitator_slot():
    """Hold one of the `NEVERMINED_MAX_INFLIGHT` slots, or raise `FacilitatorBusy`."""
    gate = _facilitator_gate()
    try:
        await asyncio.wait_for(
            gate.acquire(), timeout=NEVERMINED_FACILITATOR_WAIT_SECONDS
        )
    except asyncio.TimeoutError:
        raise FacilitatorBusy("facilitator concurrency limit reached") from None
    try:
        yield
    finally:
        gate.release()


def _resolve_endpoint(config: NeverminedConfig, base_url: str,
                      endpoint: Optional[str]) -> str:
    """The x402 `resource` URL a token is minted and verified against (#679 E2).

    Default = the paid chat door, which is what every pre-#679 caller got. The
    A2A gate passes its own door instead, because an x402 v3 token signs
    `resourceUrl` and the facilitator compares origin+path: a token minted
    against the paid URL cannot authorize a call to `/a2a/{name}`, and a
    non-Trinity client follows `resource.url` out of the 402 verbatim.
    """
    return endpoint or f"{base_url}/api/paid/{config.agent_name}/chat"


def _build_payment_required(config: NeverminedConfig, base_url: str,
                            endpoint: Optional[str]):
    """The SDK `X402PaymentRequired` for this agent's plan.

    One home for the three call sites (402 body, verify, settle) that MUST agree:
    the facilitator checks the token against this object, so a requirements
    document built differently for verify than for the 402 is a rejection the
    caller cannot act on.
    """
    network_map = {
        "sandbox": "eip155:84532",        # Base Sepolia testnet
        "staging_sandbox": "eip155:84532",
        "live": "eip155:8453",            # Base mainnet
        "staging_live": "eip155:8453",
        "custom": "eip155:84532",
    }
    return build_payment_required(
        plan_id=config.nvm_plan_id,
        endpoint=_resolve_endpoint(config, base_url, endpoint),
        agent_id=config.nvm_agent_id,
        http_verb="POST",
        network=network_map.get(config.nvm_environment, "eip155:84532"),
    )


class _SettleNotCompleted(Exception):
    """Internal control-flow signal (#1084): a non-successful settle.

    Raised inside the effect guard so the in_flight claim is RELEASED (not
    persisted as completed) — a later retry can then re-attempt. Carries the
    result so the caller still gets the settle outcome.
    """

    def __init__(self, result: NeverminedPaymentResult) -> None:
        self.result = result
        super().__init__("settle not completed")


def _settle_snapshot(result: NeverminedPaymentResult) -> dict:
    """JSON-stable, sanitized snapshot of a settle result for replay (#1084).

    Never the raw SDK object — only the fields the paid-chat response needs so a
    replayed settle reconstructs the same receipt without burning credits again.
    """
    return {
        "success": result.success,
        "payer": result.payer,
        "credits_redeemed": result.credits_redeemed,
        "remaining_balance": result.remaining_balance,
        "tx_hash": result.tx_hash,
        "error": result.error,
    }

# Lazy SDK availability check
NEVERMINED_AVAILABLE = False
try:
    from payments_py.payments import Payments
    from payments_py.common.types import PaymentOptions
    from payments_py.x402.helpers import build_payment_required
    NEVERMINED_AVAILABLE = True
except ImportError:
    logger.warning("payments-py not installed — Nevermined endpoints will return 501")


class NeverminedPaymentService:
    """Handles x402 payment verification and settlement via Nevermined SDK."""

    def _get_payments_client(self, nvm_api_key: str, nvm_environment: str):
        """Create a Payments instance for the given credentials.

        The nvm_api_key must be in "env:jwt" format (e.g. "sandbox:eyJhbGci...").
        If the stored key lacks the environment prefix, it is prepended.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        # Ensure key is in "env:jwt" format
        if ":" not in nvm_api_key:
            nvm_api_key = f"{nvm_environment}:{nvm_api_key}"

        return Payments.get_instance(PaymentOptions(
            nvm_api_key=nvm_api_key,
            environment=nvm_environment,
        ))

    def build_402_response(self, config: NeverminedConfig, base_url: str = "",
                           endpoint: Optional[str] = None) -> dict:
        """Build the 402 Payment Required response body.

        Returns a dict suitable for JSON serialization in the 402 response.
        `endpoint` defaults to the paid chat door (see `_resolve_endpoint`); a
        caller serving the requirements from a different door passes its own.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        payment_required = _build_payment_required(config, base_url, endpoint)
        return payment_required.model_dump(by_alias=True)

    async def verify_payment(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
        access_token: str,
        base_url: str = "",
        endpoint: Optional[str] = None,
    ) -> NeverminedPaymentResult:
        """Verify a payment token before processing a request.

        Does NOT burn credits — only checks validity and balance.
        Timeout: 15 seconds, under the facilitator concurrency bound.

        A failure carries `retryable` (#679 E7): a timeout, an SDK error or a
        saturated facilitator gate is OUR side being unable to decide, not the
        token being bad. The paid door answers 403 either way (unchanged); the
        A2A gate tells a retryable caller to retry instead of telling a human to
        go buy another token.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        try:
            payments = self._get_payments_client(nvm_api_key, nvm_environment)
            payment_required = _build_payment_required(config, base_url, endpoint)

            async with facilitator_slot():
                result = await asyncio.wait_for(
                    asyncio.to_thread(
                        payments.facilitator.verify_permissions,
                        payment_required,
                        access_token,
                    ),
                    timeout=15.0,
                )

            return NeverminedPaymentResult(
                success=result.is_valid,
                payer=result.payer,
                agent_request_id=result.agent_request_id,
                error=result.invalid_reason if not result.is_valid else None,
            )
        except FacilitatorBusy:
            logger.warning(
                f"Nevermined verify declined for agent {config.agent_name}: "
                f"{NEVERMINED_MAX_INFLIGHT} facilitator calls already in flight"
            )
            return NeverminedPaymentResult(
                success=False,
                error="Payment verification is busy — retry shortly",
                retryable=True,
            )
        except asyncio.TimeoutError:
            logger.error(f"Nevermined verify timeout for agent {config.agent_name}")
            return NeverminedPaymentResult(
                success=False,
                error="Payment verification timed out",
                retryable=True,
            )
        except Exception as e:
            logger.error(f"Nevermined verify error for agent {config.agent_name}: {e}")
            return NeverminedPaymentResult(
                success=False,
                error=str(e),
                retryable=True,
            )

    async def settle_payment(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
        access_token: str,
        agent_request_id: Optional[str] = None,
        base_url: str = "",
        endpoint: Optional[str] = None,
    ) -> NeverminedPaymentResult:
        """Settle a payment after successful task execution.

        Burns credits on-chain. Retries up to 3 times with exponential backoff.
        Timeout per attempt: 30 seconds, under the facilitator concurrency bound
        (a saturated gate is one more retryable attempt failure, not a lost
        settle — the caller's unsettled-success path re-drives it).
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        payments = self._get_payments_client(nvm_api_key, nvm_environment)
        payment_required = _build_payment_required(config, base_url, endpoint)

        last_error = None
        for attempt in range(3):
            try:
                async with facilitator_slot():
                    result = await asyncio.wait_for(
                        asyncio.to_thread(
                            payments.facilitator.settle_permissions,
                            payment_required,
                            access_token,
                            None,  # max_amount
                            agent_request_id,
                        ),
                        timeout=30.0,
                    )

                if result.success:
                    return NeverminedPaymentResult(
                        success=True,
                        payer=result.payer,
                        credits_redeemed=result.credits_redeemed,
                        remaining_balance=result.remaining_balance,
                        tx_hash=result.transaction,
                    )
                else:
                    return NeverminedPaymentResult(
                        success=False,
                        payer=result.payer,
                        error=result.error_reason,
                    )

            except FacilitatorBusy:
                last_error = "facilitator concurrency limit reached"
                logger.warning(
                    f"Nevermined settle declined for agent {config.agent_name} "
                    f"(attempt {attempt + 1}/3): {NEVERMINED_MAX_INFLIGHT} facilitator "
                    "calls already in flight"
                )
            except asyncio.TimeoutError:
                last_error = "Settlement timed out"
                logger.warning(
                    f"Nevermined settle timeout for agent {config.agent_name} "
                    f"(attempt {attempt + 1}/3)"
                )
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Nevermined settle error for agent {config.agent_name} "
                    f"(attempt {attempt + 1}/3): {e}"
                )

            # Exponential backoff: 1s, 2s, 4s
            if attempt < 2:
                await asyncio.sleep(2 ** attempt)

        # All retries exhausted
        logger.error(
            f"Nevermined settle failed after 3 attempts for agent {config.agent_name}: {last_error}"
        )
        return NeverminedPaymentResult(
            success=False,
            error=f"Settlement failed after 3 attempts: {last_error}",
        )

    async def settle_payment_once(
        self,
        *,
        config: NeverminedConfig,
        nvm_api_key: str,
        nvm_environment: str,
        access_token: str,
        agent_request_id: Optional[str],
        execution_id: Optional[str],
        base_url: str = "",
        endpoint: Optional[str] = None,
    ) -> NeverminedPaymentResult:
        """Settle at-most-once per local ``agent_request_id`` guard claim (#1084).

        Wraps :meth:`settle_payment` in the effect guard keyed on
        ``payment:{agent_request_id}``. Nevermined's agent_request_id is an
        observability id (NOT a provider exactly-once token — the facilitator burns
        on every successful settle_permissions call), so THIS local guard is the
        dedup: a retry reusing the SAME id replays the stored receipt instead of
        burning twice. A retry that re-verifies gets a fresh id and is not deduped
        here (at-least-once residual, tracked by #1408).

        - completed replay → returns the stored receipt, no re-settle.
        - a FAILED settle → releases the claim so a later retry can re-attempt
          (only a successful settle persists as completed/replayable).
        - a concurrent in-flight settle → returns a retryable "already in
          progress" result rather than a silent skip (codex #6).
        - no ``agent_request_id`` (no native token) → fail-open, settle proceeds
          without dedup.
        """
        try:
            async with idempotency_service.effect_guard(
                "nevermined_settle",
                {"phase": "settle", "plan_id": getattr(config, "nvm_plan_id", None)},
                payment_request_id=agent_request_id,
                execution_id=execution_id,
            ) as guard:
                if guard.replay:
                    # A completed replay is terminal — NEVER re-enter the external
                    # settle path (that would double-charge). A missing snapshot
                    # (NULL/unparseable — unreachable in normal flow, but the DB
                    # contract permits it) still proves the prior settle COMPLETED,
                    # so report success without a receipt rather than re-settling (#1084 I2).
                    if guard.snapshot is None:
                        logger.warning(
                            "Nevermined settle replay for agent_request_id=%s has no stored "
                            "snapshot — reporting success without receipt (no re-settle).",
                            agent_request_id,
                        )
                        return NeverminedPaymentResult(
                            success=True, agent_request_id=agent_request_id
                        )
                    return NeverminedPaymentResult(**guard.snapshot)

                settle_result = await self.settle_payment(
                    nvm_api_key=nvm_api_key,
                    nvm_environment=nvm_environment,
                    config=config,
                    access_token=access_token,
                    agent_request_id=agent_request_id,
                    base_url=base_url,
                    endpoint=endpoint,
                )
                if not settle_result.success:
                    # Release the claim — only a SUCCESSFUL settle is replayable.
                    raise _SettleNotCompleted(settle_result)
                guard.snapshot = _settle_snapshot(settle_result)
                return settle_result
        except idempotency_service.EffectInProgressError:
            logger.warning(
                "Nevermined settle already in progress for agent_request_id=%s — "
                "returning retryable in-progress result",
                agent_request_id,
            )
            return NeverminedPaymentResult(
                success=False,
                agent_request_id=agent_request_id,
                error="settlement already in progress",
            )
        except _SettleNotCompleted as not_done:
            return not_done.result


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_nevermined_payment_service: Optional[NeverminedPaymentService] = None


def get_nevermined_payment_service() -> NeverminedPaymentService:
    """Get the global NeverminedPaymentService instance."""
    global _nevermined_payment_service
    if _nevermined_payment_service is None:
        _nevermined_payment_service = NeverminedPaymentService()
    return _nevermined_payment_service
