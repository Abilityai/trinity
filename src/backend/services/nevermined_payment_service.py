"""
Nevermined x402 payment service (NVM-001).

Handles verify/settle lifecycle via the payments-py SDK.
All SDK calls are sync internally, so they are wrapped in asyncio.to_thread().
"""

import asyncio
import logging
from typing import Optional

from db_models import NeverminedConfig, NeverminedPaymentResult
from services import idempotency_service

logger = logging.getLogger(__name__)


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

    def build_402_response(self, config: NeverminedConfig, base_url: str = "") -> dict:
        """Build the 402 Payment Required response body.

        Returns a dict suitable for JSON serialization in the 402 response.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        endpoint = f"{base_url}/api/paid/{config.agent_name}/chat"

        # Determine network from environment
        network_map = {
            "sandbox": "eip155:84532",       # Base Sepolia testnet
            "staging_sandbox": "eip155:84532",
            "live": "eip155:8453",            # Base mainnet
            "staging_live": "eip155:8453",
            "custom": "eip155:84532",
        }
        network = network_map.get(config.nvm_environment, "eip155:84532")

        payment_required = build_payment_required(
            plan_id=config.nvm_plan_id,
            endpoint=endpoint,
            agent_id=config.nvm_agent_id,
            http_verb="POST",
            network=network,
        )

        return payment_required.model_dump(by_alias=True)

    async def verify_payment(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
        access_token: str,
        base_url: str = "",
    ) -> NeverminedPaymentResult:
        """Verify a payment token before processing a request.

        Does NOT burn credits — only checks validity and balance.
        Timeout: 15 seconds.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        try:
            payments = self._get_payments_client(nvm_api_key, nvm_environment)

            endpoint = f"{base_url}/api/paid/{config.agent_name}/chat"
            network_map = {
                "sandbox": "eip155:84532",
                "staging_sandbox": "eip155:84532",
                "live": "eip155:8453",
                "staging_live": "eip155:8453",
                "custom": "eip155:84532",
            }
            network = network_map.get(config.nvm_environment, "eip155:84532")

            payment_required = build_payment_required(
                plan_id=config.nvm_plan_id,
                endpoint=endpoint,
                agent_id=config.nvm_agent_id,
                http_verb="POST",
                network=network,
            )

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
        except asyncio.TimeoutError:
            logger.error(f"Nevermined verify timeout for agent {config.agent_name}")
            return NeverminedPaymentResult(
                success=False,
                error="Payment verification timed out",
            )
        except Exception as e:
            logger.error(f"Nevermined verify error for agent {config.agent_name}: {e}")
            return NeverminedPaymentResult(
                success=False,
                error=str(e),
            )

    async def settle_payment(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
        access_token: str,
        agent_request_id: Optional[str] = None,
        base_url: str = "",
    ) -> NeverminedPaymentResult:
        """Settle a payment after successful task execution.

        Burns credits on-chain. Retries up to 3 times with exponential backoff.
        Timeout per attempt: 30 seconds.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        payments = self._get_payments_client(nvm_api_key, nvm_environment)

        endpoint = f"{base_url}/api/paid/{config.agent_name}/chat"
        network_map = {
            "sandbox": "eip155:84532",
            "staging_sandbox": "eip155:84532",
            "live": "eip155:8453",
            "staging_live": "eip155:8453",
            "custom": "eip155:84532",
        }
        network = network_map.get(config.nvm_environment, "eip155:84532")

        payment_required = build_payment_required(
            plan_id=config.nvm_plan_id,
            endpoint=endpoint,
            agent_id=config.nvm_agent_id,
            http_verb="POST",
            network=network,
        )

        last_error = None
        for attempt in range(3):
            try:
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
