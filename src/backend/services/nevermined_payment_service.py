"""
Nevermined x402 payment service (NVM-001).

Handles verify/settle lifecycle via the payments-py SDK.
All SDK calls are sync internally, so they are wrapped in asyncio.to_thread().
"""

import asyncio
import contextlib
import logging
import os
import time
import weakref
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

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


# ---------------------------------------------------------------------------
# Plan scheme: which x402 scheme this agent's plan is actually payable with
# (abilityai/trinity#3215)
# ---------------------------------------------------------------------------

#: The crypto scheme, which is also the SDK's default and therefore what every
#: pre-#3215 402/verify/settle advertised. It stays the fallback: an install
#: whose plan lookup fails keeps today's bytes rather than guessing.
DEFAULT_SCHEME = "nvm:erc4337"

#: The fiat scheme. A card plan verified against `nvm:erc4337` requirements is
#: rejected by the facilitator, which is the #3215 defect.
CARD_SCHEME = "nvm:card-delegation"

#: Trinity's own environment -> CAIP-2 network map for `nvm:erc4337`. Kept
#: explicit rather than delegating to the SDK's `get_default_network` so a
#: future payments-py bump (#3216) cannot silently move a live agent's network;
#: `custom` is Trinity's addition (the SDK has no entry and falls back to the
#: same value). Pinned byte-for-byte by the golden test.
_ERC4337_NETWORK_BY_ENV: Dict[str, str] = {
    "sandbox": "eip155:84532",        # Base Sepolia testnet
    "staging_sandbox": "eip155:84532",
    "live": "eip155:8453",            # Base mainnet
    "staging_live": "eip155:8453",
    "custom": "eip155:84532",
}
_ERC4337_NETWORK_FALLBACK = "eip155:84532"

#: The card network when the plan names no `fiatPaymentProvider`. A frozen
#: literal of the SDK's own default (`get_default_network("nvm:card-delegation")`),
#: parity-tested rather than imported so this module keeps working when the SDK
#: is absent.
CARD_NETWORK_DEFAULT = "stripe"

#: The networks a *caller-supplied* token may name. Frozen literal of the SDK's
#: `SupportedNetworks`, parity-tested — an allow-list, never a deny-check, so a
#: network the SDK has not heard of cannot reach the facilitator through us.
_SUPPORTED_NETWORKS = frozenset({
    "eip155:8453", "eip155:84532", "stripe", "braintree", "visa",
})

#: How long a plan lookup may take. The 402 door is anonymous, so the caller is
#: holding an HTTP request open on an outbound call we do not control.
NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS = float(
    os.getenv("NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS", "5.0")
)

#: Plan lookups get their OWN small gate, never a facilitator slot: an
#: anonymous 402 flood must not be able to starve the paying verify/settle path,
#: and `asyncio.wait_for` does not cancel the worker thread, so a timed-out
#: `get_plan` (requests timeout (10, 30)) lingers after its slot is released.
NEVERMINED_PLAN_LOOKUP_MAX_INFLIGHT = int(
    os.getenv("NEVERMINED_PLAN_LOOKUP_MAX_INFLIGHT", "2")
)

#: Positive TTL — a plan's type changes only when the operator swaps it.
PLAN_SCHEME_TTL_SECONDS = 300.0

#: Negative TTL. The SDK's own resolver never caches failures, so a Nevermined
#: outage would otherwise re-dial on every single 402.
PLAN_SCHEME_NEGATIVE_TTL_SECONDS = 30.0

_PLAN_LOOKUP_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()
_PLAN_LOOKUP_INFLIGHT: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class PlanLookupBusy(Exception):
    """No plan-lookup slot became free within the wait budget."""


@dataclass(frozen=True)
class PlanScheme:
    """The `(scheme, network)` pair one x402 requirements document advertises."""

    scheme: str
    network: str


@dataclass
class _PlanCacheEntry:
    """Last-known-good plan scheme plus the negative window around a failure."""

    scheme: Optional[PlanScheme] = None
    fetched_at: float = 0.0
    failed_at: float = 0.0


#: Keyed on `(environment, plan_id)` — NOT plan_id alone (the SDK's cache is,
#: which lets a sandbox answer be served to a live request).
_PLAN_SCHEME_CACHE: Dict[Tuple[str, str], _PlanCacheEntry] = {}


def default_plan_scheme(nvm_environment: Optional[str]) -> PlanScheme:
    """Today's bytes: the crypto scheme on this environment's network."""
    return PlanScheme(
        DEFAULT_SCHEME,
        _ERC4337_NETWORK_BY_ENV.get(nvm_environment or "", _ERC4337_NETWORK_FALLBACK),
    )


def clear_plan_scheme_cache() -> None:
    """Drop every cached plan scheme (tests; an operator-visible swap is TTL'd)."""
    _PLAN_SCHEME_CACHE.clear()


def _plan_lookup_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _PLAN_LOOKUP_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(NEVERMINED_PLAN_LOOKUP_MAX_INFLIGHT)
        _PLAN_LOOKUP_GATES[loop] = gate
    return gate


def _plan_lookup_inflight() -> Dict[Tuple[str, str], "asyncio.Future"]:
    """Per-loop single-flight registry: one `get_plan` per key, not per caller."""
    loop = asyncio.get_running_loop()
    inflight = _PLAN_LOOKUP_INFLIGHT.get(loop)
    if inflight is None:
        inflight = {}
        _PLAN_LOOKUP_INFLIGHT[loop] = inflight
    return inflight


@contextlib.asynccontextmanager
async def plan_lookup_slot():
    """Hold one plan-lookup slot, or raise `PlanLookupBusy`."""
    gate = _plan_lookup_gate()
    try:
        await asyncio.wait_for(
            gate.acquire(), timeout=NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        raise PlanLookupBusy("plan lookup concurrency limit reached") from None
    try:
        yield
    finally:
        gate.release()


def _parse_plan(plan: Any, nvm_environment: Optional[str]) -> PlanScheme:
    """The plan's scheme + network, read with the SDK's own keys.

    Mirrors `payments_py.x402.resolve_scheme._fetch_plan_metadata` /
    `resolve_network` (parity-tested) rather than calling them, because the SDK
    swallows every failure at DEBUG and returns `nvm:erc4337` — a card plan
    silently advertised as crypto with nothing in Trinity's logs is exactly the
    reported defect wearing a new hat. Only `isCrypto is False` means card;
    a missing or malformed payload stays crypto, which is the SDK's semantics.
    """
    if not isinstance(plan, dict):
        plan = {}
    registry = plan.get("registry", {})
    price = registry.get("price", {}) if isinstance(registry, dict) else {}
    is_crypto = price.get("isCrypto") if isinstance(price, dict) else None
    # fiatPaymentProvider lives in plan.metadata.plan, not in registry.price.
    metadata = plan.get("metadata", {})
    plan_meta = metadata.get("plan", {}) if isinstance(metadata, dict) else {}
    fiat_provider = (
        plan_meta.get("fiatPaymentProvider") if isinstance(plan_meta, dict) else None
    )
    if is_crypto is False:
        network = (
            fiat_provider
            if isinstance(fiat_provider, str) and fiat_provider
            else CARD_NETWORK_DEFAULT
        )
        return PlanScheme(CARD_SCHEME, network)
    return default_plan_scheme(nvm_environment)


def _plan_scheme_from_cache(
    key: Tuple[str, str], nvm_environment: Optional[str]
) -> Optional[PlanScheme]:
    """A fresh hit, or a stale-while-revalidate answer inside the negative window."""
    entry = _PLAN_SCHEME_CACHE.get(key)
    if entry is None:
        return None
    now = time.monotonic()
    if entry.scheme is not None and (now - entry.fetched_at) < PLAN_SCHEME_TTL_SECONDS:
        return entry.scheme
    if entry.failed_at and (now - entry.failed_at) < PLAN_SCHEME_NEGATIVE_TTL_SECONDS:
        # Serve last-known-good through an outage rather than flipping a card
        # plan back to crypto the moment Nevermined blips.
        return entry.scheme or default_plan_scheme(nvm_environment)
    return None


def _record_plan_lookup_failure(
    key: Tuple[str, str], nvm_environment: Optional[str], reason: str
) -> PlanScheme:
    """Open (or extend) the negative window and return what to serve meanwhile."""
    now = time.monotonic()
    entry = _PLAN_SCHEME_CACHE.get(key)
    if entry is None:
        entry = _PlanCacheEntry()
        _PLAN_SCHEME_CACHE[key] = entry
    first_in_window = (
        not entry.failed_at
        or (now - entry.failed_at) >= PLAN_SCHEME_NEGATIVE_TTL_SECONDS
    )
    entry.failed_at = now
    served = entry.scheme or default_plan_scheme(nvm_environment)
    if first_in_window:
        # WARN, not DEBUG: a card plan quietly advertised as crypto is the
        # defect. Never logs the key or the token — plan id and environment only.
        logger.warning(
            "Nevermined plan lookup failed for plan_id=%s env=%s (%s) — "
            "serving scheme=%s network=%s for up to %.0fs",
            key[1], key[0], reason, served.scheme, served.network,
            PLAN_SCHEME_NEGATIVE_TTL_SECONDS,
        )
    return served


def _record_plan_lookup_success(key: Tuple[str, str], scheme: PlanScheme) -> PlanScheme:
    entry = _PLAN_SCHEME_CACHE.get(key)
    if entry is None:
        entry = _PlanCacheEntry()
        _PLAN_SCHEME_CACHE[key] = entry
    entry.scheme = scheme
    entry.fetched_at = time.monotonic()
    entry.failed_at = 0.0
    return scheme


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
                            endpoint: Optional[str],
                            plan_scheme: Optional[PlanScheme] = None):
    """The SDK `X402PaymentRequired` for this agent's plan.

    One home for the three call sites (402 body, verify, settle) that MUST agree:
    the facilitator checks the token against this object, so a requirements
    document built differently for verify than for the 402 is a rejection the
    caller cannot act on.

    `plan_scheme` (#3215) is the pair the plan is actually payable with. The SDK
    defaults `scheme` to `nvm:erc4337`, which is why a fiat (card) plan used to
    be unpayable: the facilitator is handed this object verbatim, so the scheme
    reaches it ONLY through `accepts[0]`. `None` keeps the pre-#3215 bytes.
    """
    resolved = plan_scheme or default_plan_scheme(config.nvm_environment)
    return build_payment_required(
        plan_id=config.nvm_plan_id,
        endpoint=_resolve_endpoint(config, base_url, endpoint),
        agent_id=config.nvm_agent_id,
        http_verb="POST",
        scheme=resolved.scheme,
        network=resolved.network,
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
    from payments_py.x402.schemes import is_valid_scheme
    from payments_py.x402.token import decode_access_token
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
                           endpoint: Optional[str] = None,
                           plan_scheme: Optional[PlanScheme] = None) -> dict:
        """Build the 402 Payment Required response body.

        Returns a dict suitable for JSON serialization in the 402 response.
        `endpoint` defaults to the paid chat door (see `_resolve_endpoint`); a
        caller serving the requirements from a different door passes its own.

        Stays SYNCHRONOUS (#3215): the plan lookup is an outbound HTTP call, so
        the caller `await`s :meth:`resolve_plan_scheme` first and hands the
        answer in. `plan_scheme=None` is the pre-#3215 document.
        """
        if not NEVERMINED_AVAILABLE:
            raise RuntimeError("payments-py SDK is not installed")

        payment_required = _build_payment_required(
            config, base_url, endpoint, plan_scheme
        )
        return payment_required.model_dump(by_alias=True)

    def scheme_from_token(
        self, access_token: Any, config: NeverminedConfig
    ) -> Optional[PlanScheme]:
        """The scheme the PRESENTED token was minted for, or None (#3215).

        This is what verify and settle use, and it is why they need no network
        call: the token body carries `accepted.{scheme,network,planId}` (the
        mint body, `x402/token_request.py`), and the SDK's own A2A server reads
        it the same way. It also makes a settle re-driven hours after its verify
        byte-stable with that verify, which a cache with a TTL cannot promise.

        Caller-supplied, therefore allow-listed rather than trusted: the scheme
        must be one the SDK knows, the network one of `SupportedNetworks`, and
        the plan id THIS agent's. Anything else returns None and the caller
        falls back to the plan-resolved scheme and then to today's default — a
        wrong guess here only changes which requirements the caller's own token
        is checked against, and the facilitator remains the authority.
        """
        if not NEVERMINED_AVAILABLE or not isinstance(access_token, str):
            return None
        try:
            decoded = decode_access_token(access_token)
        except Exception:  # noqa: BLE001 — an undecodable token is simply not a source
            return None
        if not isinstance(decoded, dict):
            return None
        accepted = decoded.get("accepted")
        if not isinstance(accepted, dict):
            return None
        scheme = accepted.get("scheme")
        network = accepted.get("network")
        if not is_valid_scheme(scheme) or network not in _SUPPORTED_NETWORKS:
            return None
        expected_plan = getattr(config, "nvm_plan_id", None)
        if expected_plan is not None and str(accepted.get("planId")) != str(expected_plan):
            return None
        return PlanScheme(str(scheme), str(network))

    async def resolve_plan_scheme(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
    ) -> PlanScheme:
        """The plan's own scheme + network, for the 402 and `/info` (#3215).

        Cached per `(environment, plan_id)`, single-flighted so a flood of cold
        402s costs one `get_plan`, bounded by its own small gate, and
        stale-while-revalidate: a lookup failure serves last-known-good (or
        today's default) and WARNs once per negative window instead of failing
        the 402. Never raises.
        """
        plan_id = getattr(config, "nvm_plan_id", None)
        if not NEVERMINED_AVAILABLE or not plan_id:
            return default_plan_scheme(nvm_environment)

        key = (nvm_environment or "", str(plan_id))
        cached = _plan_scheme_from_cache(key, nvm_environment)
        if cached is not None:
            return cached

        inflight = _plan_lookup_inflight()
        pending = inflight.get(key)
        if pending is not None:
            # Single flight: ride the in-flight lookup rather than opening a
            # second one. `shield` so our own timeout never cancels the leader.
            try:
                return await asyncio.wait_for(
                    asyncio.shield(pending),
                    timeout=NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                return _record_plan_lookup_failure(
                    key, nvm_environment, "waiting on in-flight lookup failed"
                )

        loop = asyncio.get_running_loop()
        future: "asyncio.Future" = loop.create_future()
        inflight[key] = future
        resolved: Optional[PlanScheme] = None
        try:
            resolved = await self._fetch_plan_scheme(
                nvm_api_key, nvm_environment, key, str(plan_id)
            )
            return resolved
        finally:
            inflight.pop(key, None)
            if not future.done():
                # Even on cancellation the followers get an answer rather than
                # hanging on a future nobody will ever resolve.
                future.set_result(
                    resolved
                    if resolved is not None
                    else default_plan_scheme(nvm_environment)
                )

    async def _fetch_plan_scheme(
        self,
        nvm_api_key: str,
        nvm_environment: str,
        key: Tuple[str, str],
        plan_id: str,
    ) -> PlanScheme:
        """One `plans.get_plan` under the plan-lookup gate. Never raises."""
        try:
            payments = self._get_payments_client(nvm_api_key, nvm_environment)
            async with plan_lookup_slot():
                plan = await asyncio.wait_for(
                    asyncio.to_thread(payments.plans.get_plan, plan_id),
                    timeout=NEVERMINED_PLAN_LOOKUP_TIMEOUT_SECONDS,
                )
        except PlanLookupBusy:
            return _record_plan_lookup_failure(key, nvm_environment, "lookup gate busy")
        except asyncio.TimeoutError:
            return _record_plan_lookup_failure(key, nvm_environment, "timeout")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            return _record_plan_lookup_failure(
                key, nvm_environment, type(e).__name__
            )
        return _record_plan_lookup_success(key, _parse_plan(plan, nvm_environment))

    async def _plan_scheme_for_payment(
        self,
        *,
        nvm_api_key: str,
        nvm_environment: str,
        config: NeverminedConfig,
        access_token: str,
    ) -> PlanScheme:
        """Token-derived scheme for the money path, plan-resolved as a fallback."""
        from_token = self.scheme_from_token(access_token, config)
        if from_token is not None:
            return from_token
        return await self.resolve_plan_scheme(
            nvm_api_key=nvm_api_key,
            nvm_environment=nvm_environment,
            config=config,
        )

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
            plan_scheme = await self._plan_scheme_for_payment(
                nvm_api_key=nvm_api_key,
                nvm_environment=nvm_environment,
                config=config,
                access_token=access_token,
            )
            payment_required = _build_payment_required(
                config, base_url, endpoint, plan_scheme
            )

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
        # Built ONCE, outside the retry loop, from the token the payer presented
        # (#3215): every attempt — and a settle re-driven hours after its verify
        # — checks against byte-identical requirements.
        plan_scheme = await self._plan_scheme_for_payment(
            nvm_api_key=nvm_api_key,
            nvm_environment=nvm_environment,
            config=config,
            access_token=access_token,
        )
        payment_required = _build_payment_required(
            config, base_url, endpoint, plan_scheme
        )

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
