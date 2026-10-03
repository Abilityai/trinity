"""One home for the x402 verify → dedup → execute → settle lifecycle.

`routers/paid.py` owned this inline (NVM-001, hardened by #1018, #1084 and
#679). The A2A inbound gate (abilityai/trinity-enterprise#679) must run the
IDENTICAL money logic — the same three settle branches, the same
complete-not-fail on an unsettled success, the same replay-resettle with a
snapshot upgrade — so it is extracted here rather than rebuilt from the leaf
helpers. Two copies of those branches is how a door comes to charge on a
cancelled turn again.

**Collaborators are PARAMETERS, never imports (decision 20).** `idem`, `db`,
`payment_service` and `execute` are passed in by the caller, from the caller's
own module globals. That is not style: three existing test files patch
`paid.db`, `paid.idempotency_service` and `paid.NEVERMINED_AVAILABLE`, and an
extraction that imported those names here would silently detach every one of
those patches — the money path would then be tested in a configuration nobody
runs (the 2026-09-29 guard-seam-swap class). The caller passes what it holds, so
a patch on the caller still decides what this function talks to.

**What it does NOT own**: loading the config, the 501/404 shapes, HTTP status
codes, the JSON-RPC envelope, or anything about a request. It returns a
:class:`PaidTurnOutcome` and the caller renders it — the paid door into its
historical JSON bodies, the A2A gate into a Task object. A service holding HTTP
concerns is Invariant #1.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional, Union

logger = logging.getLogger(__name__)

#: Strong references to in-flight detached settle tasks (C1). The settle must
#: outlive the request that started it, and asyncio holds a running task only
#: weakly.
_PENDING_SETTLES: set = set()


class PaidTurnAbort(Exception):
    """A caller's `pre_execute` hook refusing the turn after the dedup gate.

    Carries the payload and status the caller wants rendered. It exists so a
    door-specific input check (the paid door's #1672 resume-sentinel rejection)
    can keep running at exactly the point in the sequence it runs today —
    after `begin()`, before `execute()` — without this service knowing what the
    check is about.
    """

    def __init__(self, payload: dict, status_code: int = 400) -> None:
        self.payload = payload
        self.status_code = status_code
        super().__init__(payload.get("detail", "paid turn aborted"))


#: Outcome kinds. The caller must handle all of them; a new one is a new branch
#: at every door, which is the point of naming them rather than returning a
#: loose dict.
VERIFY_FAILED = "verify_failed"
ABORTED = "aborted"
IN_FLIGHT = "in_flight"
REPLAY_SETTLED = "replay_settled"
REPLAY_UNSETTLED = "replay_unsettled"
SETTLED = "settled"
UNSETTLED = "unsettled"
EXECUTION_ERROR = "execution_error"
EXECUTION_FAILED = "execution_failed"
EXECUTION_CANCELLED = "execution_cancelled"


@dataclass
class PaidTurnOutcome:
    """What happened, and the payload the caller should render.

    `payload` is the paid door's historical response body in every branch —
    including for the A2A gate, which rebuilds its Task FROM that dict. Keeping
    one snapshot shape is what lets the replay logic below be a single code
    path instead of one per door.
    """

    kind: str
    payload: dict = field(default_factory=dict)
    status_code: int = 200
    verify: Any = None
    settle: Any = None
    execution_id: Optional[str] = None
    replayed: bool = False

    @property
    def settled(self) -> bool:
        return bool((self.payload.get("payment") or {}).get("settled"))


def _resolve(value: Union[str, Callable[[Any], str], None], verify: Any):
    """Allow scope/key to be computed from the verify result.

    The paid door knows its scope up front (`agent:{name}`); the A2A gate's is
    namespaced by the PAYER wallet, which only exists once verify has answered.
    Taking a callable keeps the per-caller dedup namespace (FR-4) without
    verifying twice or moving verify out of this function.
    """
    return value(verify) if callable(value) else value


def _log_verify_ok(db, agent_name: str, verify: Any, error: Optional[str] = None) -> None:
    db.log_nevermined_payment(
        agent_name=agent_name,
        action="verify",
        success=True,
        subscriber_address=verify.payer,
        **({"error": error} if error is not None else {}),
    )


def finalize_settled(
    *,
    agent_name: str,
    config,
    response,
    execution_id: Optional[str],
    settle_result,
    payer: Optional[str],
    idem_decision,
    idem,
    db,
) -> dict:
    """Shared success finalizer for BOTH the fresh-settle and replay-resettle paths (#1018).

    A client retry that finally settles must still (a) log ``action="settle"`` and
    (b) converge the stored trigger snapshot unsettled→settled — otherwise the
    settle is never recorded and every replay re-drives settle forever. Kept as one
    helper so neither path can drift on the bookkeeping.
    """
    db.log_nevermined_payment(
        agent_name=agent_name,
        action="settle",
        success=True,
        execution_id=execution_id,
        subscriber_address=payer,
        credits_amount=config.credits_per_request,
        tx_hash=settle_result.tx_hash,
        remaining_balance=(
            int(settle_result.remaining_balance)
            if settle_result.remaining_balance
            else None
        ),
    )

    settled_payload = {
        "response": response,
        "execution_id": execution_id,
        "status": "success",
        "payment": {
            "settled": True,
            "credits_burned": config.credits_per_request,
            "remaining_balance": settle_result.remaining_balance,
            "tx_hash": settle_result.tx_hash,
        },
    }

    # Converge the stored trigger snapshot (#1018): on the fresh path this completes
    # the in-flight claim with the settled snapshot; on the replay-resettle path it
    # upgrades a completed-but-unsettled snapshot so a THIRD request replays
    # 'settled' and never re-drives settle. No-op when dedup is disabled.
    idem.upgrade_snapshot(idem_decision.scope, idem_decision.key, settled_payload)
    return settled_payload


async def run_paid_turn(
    *,
    agent_name: str,
    config,
    nvm_api_key: str,
    access_token: str,
    idem_scope: Union[str, Callable[[Any], str]],
    idem_key: Union[Optional[str], Callable[[Any], Optional[str]]],
    execute: Callable[[], Awaitable[Any]],
    payment_service,
    idem,
    db,
    base_url: str = "",
    endpoint: Optional[str] = None,
    pre_execute: Optional[Callable[[Any], None]] = None,
) -> PaidTurnOutcome:
    """Verify, dedup, execute, settle — once, in this order, for every paid door.

    The ORDER is the invariant, and each step is here because a previous bug put
    it here:

    * verify BEFORE the dedup gate, so a rejected token never consumes a key;
    * `complete()` — not `fail()` — on a success that could not settle, so a
      client retry re-drives settle instead of re-running the LLM (#1018);
    * `fail()` on a failed, cancelled or raised execution, so a retry re-executes
      and nothing is charged (#679: settling a cancelled turn is the money bug);
    * a replayed unsettled snapshot re-drives settle and `upgrade_snapshot`s on
      success, so a third attempt replays 'settled' and stops re-settling.

    `execute()` returns an object with `.status`, `.response` and
    `.execution_id`. Cancellation is phase-aware (#679 E5): before a result
    exists the claim is released; after the turn succeeded the settle AND its
    bookkeeping run in a detached, shielded task, so a caller that walked away
    cannot cause the LLM work to be repeated or the burn to go unrecorded — see
    `_settle_and_record` for why the bookkeeping cannot live in a cancellation
    handler (C1). The `CancelledError` is always re-raised — this function
    decides the bookkeeping, not whether the request lives.
    """
    # --- 1. verify (before any dedup key is consumed) --------------------
    verify_result = await payment_service.verify_payment(
        nvm_api_key=nvm_api_key,
        nvm_environment=config.nvm_environment,
        config=config,
        access_token=access_token,
        base_url=base_url,
        endpoint=endpoint,
    )

    if not verify_result.success:
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="reject",
            success=False,
            subscriber_address=verify_result.payer,
            error=verify_result.error,
        )
        return PaidTurnOutcome(
            kind=VERIFY_FAILED,
            status_code=403,
            payload={
                "detail": "Payment verification failed",
                "error": verify_result.error,
            },
            verify=verify_result,
        )

    _log_verify_ok(db, agent_name, verify_result)

    # --- 2. dedup gate ---------------------------------------------------
    scope = _resolve(idem_scope, verify_result)
    key = _resolve(idem_key, verify_result)
    decision = idem.begin(scope, key)

    if decision.replay:
        if decision.in_flight:
            return PaidTurnOutcome(
                kind=IN_FLIGHT,
                status_code=409,
                payload={"detail": "A duplicate paid request is still being processed."},
                verify=verify_result,
            )

        snapshot = decision.snapshot or {}
        payment_snap = snapshot.get("payment") or {}

        if payment_snap.get("settled"):
            # Already settled — replay the receipt verbatim, no re-execute/re-settle.
            return PaidTurnOutcome(
                kind=REPLAY_SETTLED,
                payload=snapshot,
                verify=verify_result,
                execution_id=snapshot.get("execution_id"),
                replayed=True,
            )

        # Completed but UNSETTLED — re-drive settle WITHOUT re-running the LLM (the
        # trigger key already deduped the execution), then converge the snapshot. This
        # is why the unsettled branch stores the claim with complete() (not fail()):
        # fail() would re-execute here. NOTE: the re-settle is NOT provider-idempotent —
        # Nevermined's agent_request_id is an observability id (fresh per verify) and the
        # facilitator burns on every successful settle_permissions call. Re-driving is
        # safe here only because the prior settle genuinely did NOT complete; a settle
        # that burned on-chain but reported failure would re-burn (at-least-once residual,
        # tracked by #1408). The payment:{agent_request_id} effect guard only dedups a
        # concurrent settle that reuses the SAME id.
        resettle = await payment_service.settle_payment_once(
            config=config,
            nvm_api_key=nvm_api_key,
            nvm_environment=config.nvm_environment,
            access_token=access_token,
            agent_request_id=verify_result.agent_request_id,
            execution_id=snapshot.get("execution_id"),
            base_url=base_url,
            endpoint=endpoint,
        )
        if resettle.success:
            return PaidTurnOutcome(
                kind=SETTLED,
                payload=finalize_settled(
                    agent_name=agent_name,
                    config=config,
                    response=snapshot.get("response"),
                    execution_id=snapshot.get("execution_id"),
                    settle_result=resettle,
                    payer=verify_result.payer,
                    idem_decision=decision,
                    idem=idem,
                    db=db,
                ),
                verify=verify_result,
                settle=resettle,
                execution_id=snapshot.get("execution_id"),
                replayed=True,
            )
        # Still unsettled — replay the stored unsettled snapshot; the claim stays
        # 'completed unsettled' so a later retry re-drives settle again.
        return PaidTurnOutcome(
            kind=REPLAY_UNSETTLED,
            payload=snapshot,
            verify=verify_result,
            settle=resettle,
            execution_id=snapshot.get("execution_id"),
            replayed=True,
        )

    # --- 3. door-specific refusal, at its historical position ------------
    if pre_execute is not None:
        try:
            pre_execute(verify_result)
        except PaidTurnAbort as abort:
            return PaidTurnOutcome(
                kind=ABORTED,
                status_code=abort.status_code,
                payload=abort.payload,
                verify=verify_result,
            )

    # --- 4. execute ------------------------------------------------------
    try:
        exec_result = await execute()
    except asyncio.CancelledError:
        # Nothing was delivered, so release the claim before unwinding — a
        # stranded in-flight claim would 409 the payer's own retry for the key's
        # whole TTL.
        idem.fail(decision)
        raise
    except Exception as e:
        logger.error(f"Task execution failed for paid request on {agent_name}: {e}")
        # Nothing dispatched — release the claim so a legitimate retry re-executes.
        idem.fail(decision)
        # Don't settle — caller keeps credits
        _log_verify_ok(db, agent_name, verify_result, error=f"Execution failed: {e}")
        return PaidTurnOutcome(
            kind=EXECUTION_ERROR,
            status_code=500,
            payload={
                "detail": "Task execution failed",
                "error": str(e),
                "payment": {"settled": False, "reason": "Execution failed — no charge"},
            },
            verify=verify_result,
        )

    if exec_result.status in ("failed", "cancelled"):
        # Don't settle — caller keeps credits. #679: a CANCELLED turn must NOT
        # settle either — settling on cancel is the charge-on-cancel money bug.
        # Release the claim so a retry re-executes (no completed work to replay).
        idem.fail(decision)
        is_cancelled = exec_result.status == "cancelled"
        payload = {
            "execution_id": exec_result.execution_id,
            "status": "cancelled" if is_cancelled else "failed",
            "payment": {
                "settled": False,
                "reason": (
                    "Execution cancelled — no charge"
                    if is_cancelled
                    else "Execution failed — no charge"
                ),
            },
        }
        # #1018 hardening: a FAILED execution may hold partial/garbled output; the
        # caller paid nothing, so don't leak the body. A CANCELLED turn keeps its
        # response (#679 — the user cancelled their own work and may want it).
        if is_cancelled:
            payload["response"] = exec_result.response
        return PaidTurnOutcome(
            kind=EXECUTION_CANCELLED if is_cancelled else EXECUTION_FAILED,
            payload=payload,
            verify=verify_result,
            execution_id=exec_result.execution_id,
        )

    # Record the execution on the claim now that it exists (best-effort).
    idem.attach_execution(decision, exec_result.execution_id)

    # --- 5. settle (success only) ----------------------------------------
    # Effect-scoped guard (#1084) so a concurrent settle reusing the SAME
    # agent_request_id is deduped locally. The terminal-turn guard above (failed
    # execution → no settle) is the outer layer and is preserved. NOTE:
    # agent_request_id is a Nevermined observability id, not a provider
    # exactly-once token — this local guard is the only settle dedup, and a
    # fresh-id retry's double-settle residual is tracked by #1408.
    #
    # Detached + shielded (#679 E5, C1): the work is DONE and the payer owes for
    # it. A client that disconnects here must not abort a settle mid-flight —
    # that strands the claim in-flight with the money unrecorded, and the retry
    # re-runs the LLM. The settle and ALL of its bookkeeping therefore run in the
    # detached task below, never in a cancellation handler.
    def _record_unsettled(settle_result) -> dict:
        """The settle-failed row + the claim, as one step.

        `complete()` — NOT `fail()` — so a client re-POST replays the completed
        work and re-drives settle (idempotent) rather than re-running the LLM
        (double cost). The snapshot stays 'unsettled' until a settle finally
        succeeds and upgrades it.
        """
        payload = _unsettled_payload(
            agent_name=agent_name,
            config=config,
            exec_result=exec_result,
            settle_result=settle_result,
            verify_result=verify_result,
            db=db,
        )
        idem.complete(decision, exec_result.execution_id, payload)
        return payload

    async def _settle_and_record():
        """Settle AND every money record it implies, in ONE detached task.

        The bookkeeping lives here rather than in the awaiting frame's
        `except CancelledError` handler because the two cancellation shapes are
        not interchangeable (C1). `asyncio.Task.cancel()` is edge-triggered: one
        `CancelledError` is delivered, so a handler may await the settle and
        then write its rows. Starlette's `StreamingResponse` — the A2A
        `message/stream` consumer — runs its body generator inside an anyio task
        group and cancels that group's cancel SCOPE on client disconnect, and
        anyio cancellation is LEVEL-triggered: every subsequent `await` inside
        the cancelled scope raises `CancelledError` again. Such a handler never
        reaches its rows, so the facilitator burns credits with no `settle` row
        and the claim strands in-flight for the key's whole 24 h TTL — the retry
        then re-runs the LLM and re-settles.

        A detached task is not inside that scope, so it completes either way.
        Returns `(settle_result, payload, exc)`; `exc` is re-raised by the
        awaiting frame if it is still alive, so a settle that RAISES keeps
        answering exactly what it answered before (the paid door's 500).
        """
        try:
            settle_result = await payment_service.settle_payment_once(
                config=config,
                nvm_api_key=nvm_api_key,
                nvm_environment=config.nvm_environment,
                access_token=access_token,
                agent_request_id=verify_result.agent_request_id,
                execution_id=exec_result.execution_id,
                base_url=base_url,
                endpoint=endpoint,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded, then handed back
            logger.warning(
                "Settle raised for %s; completing the claim as unsettled so a "
                "retry re-drives it instead of re-running the LLM: %s",
                agent_name, exc,
            )
            return None, _record_unsettled(None), exc

        if settle_result.success:
            # Logs settle + completes the idempotency claim with the settled snapshot.
            return settle_result, finalize_settled(
                agent_name=agent_name,
                config=config,
                response=exec_result.response,
                execution_id=exec_result.execution_id,
                settle_result=settle_result,
                payer=verify_result.payer,
                idem_decision=decision,
                idem=idem,
                db=db,
            ), None

        return settle_result, _record_unsettled(settle_result), None

    settle_task = asyncio.ensure_future(_settle_and_record())
    # asyncio keeps only a weak reference to a running task, so a settle whose
    # awaiter has walked away could be collected mid-flight — which is the same
    # lost burn by another route. Hold a strong reference until it finishes.
    _PENDING_SETTLES.add(settle_task)
    settle_task.add_done_callback(_PENDING_SETTLES.discard)

    try:
        settle_result, settle_payload, settle_exc = await asyncio.shield(settle_task)
    except asyncio.CancelledError:
        # Re-raise ONLY — never await here. The detached task above owns the
        # money bookkeeping and finishes it on its own; awaiting it inside a
        # level-triggered cancelled scope is precisely what C1 was.
        raise

    if settle_exc is not None:
        raise settle_exc

    if settle_result.success:
        return PaidTurnOutcome(
            kind=SETTLED,
            payload=settle_payload,
            verify=verify_result,
            settle=settle_result,
            execution_id=exec_result.execution_id,
        )

    return PaidTurnOutcome(
        kind=UNSETTLED,
        payload=settle_payload,
        verify=verify_result,
        settle=settle_result,
        execution_id=exec_result.execution_id,
    )


def _unsettled_payload(*, agent_name: str, config, exec_result, settle_result,
                       verify_result, db) -> dict:
    """The honest body for a delivered turn whose settle did not complete (#1018).

    The work WAS delivered, so deliver-then-reconcile: the caller keeps the
    response, but the status says `success_unsettled` (the filed #1018 bug: this
    used to lie with status "success"). Two sub-cases:

    * concurrent settle in-flight (effect guard) → `settle_in_progress`, NO log
      row — the concurrently-running settle logs its own outcome, once;
    * genuine failure after retries → `settle_retry_needed` + a `settle_failed`
      log row.
    """
    error = getattr(settle_result, "error", "Settlement did not complete")
    settle_in_progress = error == "settlement already in progress"
    payment_block = {"settled": False, "error": error}
    if settle_in_progress:
        payment_block["settle_in_progress"] = True
    else:
        payment_block["settle_retry_needed"] = True
        db.log_nevermined_payment(
            agent_name=agent_name,
            action="settle_failed",
            success=False,
            execution_id=exec_result.execution_id,
            subscriber_address=verify_result.payer,
            credits_amount=config.credits_per_request,
            error=error,
        )

    return {
        "response": exec_result.response,
        "execution_id": exec_result.execution_id,
        "status": "success_unsettled",
        "payment": payment_block,
    }
