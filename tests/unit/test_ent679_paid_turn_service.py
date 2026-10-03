"""The shared paid-turn orchestrator (abilityai/trinity-enterprise#679).

`services/paid_turn_service.run_paid_turn` is the extracted x402 money
lifecycle that BOTH the paid chat door and the A2A payment gate run. The three
existing paid-door files (`test_1018_settlement_ordering`, `test_679_callers`,
`test_3114_pull_route_callers`) remain the end-to-end net over `routers/paid.py`
and are deliberately unedited; this file drives the service at ITS own layer,
one test per outcome kind, plus the orderings a renderer cannot observe:

* verify runs BEFORE the dedup gate, so a rejected token consumes no key;
* a success that could not settle is stored with `complete()`, never `fail()`;
* a failed/cancelled/raised turn is `fail()`ed and never settles;
* a client that disconnects mid-settle does not cause the LLM work to re-run.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from services import paid_turn_service as pts  # noqa: E402

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Collaborator stand-ins. Every one of these is a PARAMETER of run_paid_turn —
# the service imports none of them (decision 20), which is what this file's
# ability to drive it with plain objects demonstrates.
# ---------------------------------------------------------------------------

class FakeIdem:
    """The `idempotency_service` module surface the orchestrator uses."""

    def __init__(self, decision=None):
        self.decision = decision or SimpleNamespace(
            replay=False, in_flight=False, snapshot=None, scope="sc", key="ky",
        )
        self.begin_calls = []
        self.completed = []
        self.failed = []
        self.attached = []
        self.upgrades = []
        self.order = []

    def begin(self, scope, key):
        self.begin_calls.append((scope, key))
        self.order.append("begin")
        return self.decision

    def complete(self, decision, execution_id, snapshot):
        self.completed.append((execution_id, snapshot))
        self.order.append("complete")

    def fail(self, decision):
        self.failed.append(decision)
        self.order.append("fail")

    def attach_execution(self, decision, execution_id):
        self.attached.append(execution_id)

    def upgrade_snapshot(self, scope, key, snapshot):
        self.upgrades.append((scope, key, snapshot))
        self.order.append("upgrade")


class FakeDb:
    def __init__(self, order=None):
        self.logs = []
        self.order = order if order is not None else []

    def log_nevermined_payment(self, **kwargs):
        self.logs.append(kwargs)

    def actions(self):
        return [log["action"] for log in self.logs]


class FakePaymentService:
    def __init__(self, *, verify=None, settle=None, order=None):
        self._verify = verify or _verify_ok()
        self._settle = settle
        self.verify_calls = []
        self.settle_calls = []
        self.order = order if order is not None else []

    async def verify_payment(self, **kwargs):
        self.verify_calls.append(kwargs)
        self.order.append("verify")
        if callable(self._verify):
            return self._verify(**kwargs)
        return self._verify

    async def settle_payment_once(self, **kwargs):
        self.settle_calls.append(kwargs)
        self.order.append("settle")
        if callable(self._settle):
            return await self._settle(**kwargs)
        return self._settle


def _config(credits=1):
    return SimpleNamespace(
        agent_name="agent-a", enabled=True, nvm_environment="sandbox",
        nvm_plan_id="plan-1", nvm_agent_id="did:nv:1", credits_per_request=credits,
    )


def _verify_ok(payer="0xpayer", agent_request_id="areq-1"):
    return SimpleNamespace(success=True, payer=payer,
                           agent_request_id=agent_request_id, error=None,
                           retryable=False)


def _verify_bad(error="bad token", retryable=False):
    return SimpleNamespace(success=False, payer=None, agent_request_id=None,
                           error=error, retryable=retryable)


def _settle_ok(tx="0xtx", remaining="9"):
    return SimpleNamespace(success=True, payer="0xpayer", tx_hash=tx,
                           credits_redeemed="1", remaining_balance=remaining,
                           error=None, retryable=False)


def _settle_bad(error="facilitator exploded"):
    return SimpleNamespace(success=False, payer="0xpayer", tx_hash=None,
                           credits_redeemed=None, remaining_balance=None,
                           error=error, retryable=True)


def _exec(status="completed", response="the answer", execution_id="exec-1"):
    return SimpleNamespace(status=status, response=response, execution_id=execution_id)


async def _drive(*, payment_service=None, idem=None, db=None, execute=None,
                 config=None, **kwargs):
    order = []
    db = db or FakeDb(order)
    payment_service = payment_service or FakePaymentService(settle=_settle_ok(), order=order)
    idem = idem or FakeIdem()
    idem.order = order

    async def _default_execute():
        order.append("execute")
        return _exec()

    outcome = await pts.run_paid_turn(
        agent_name="agent-a",
        config=config or _config(),
        nvm_api_key="nvm-key",
        access_token="tok-1",
        idem_scope="agent:agent-a",
        idem_key="key-1",
        execute=execute or _default_execute,
        payment_service=payment_service,
        idem=idem,
        db=db,
        base_url="http://localhost",
        **kwargs,
    )
    return outcome, SimpleNamespace(idem=idem, db=db, payments=payment_service,
                                    order=order)


async def _until(predicate, timeout: float = 2.0):
    """Give the detached settle task its turns, bounded.

    The scope-cancelled turn has already unwound when the test resumes, so the
    settle's own bookkeeping lands on a later loop iteration. Polling the
    observable record (rather than reaching for the task object) keeps the
    assertion about behaviour.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)


# ---------------------------------------------------------------------------
# 1. verify_failed — 403 shape, reject row, and NO key consumed
# ---------------------------------------------------------------------------

async def test_verify_failure_rejects_and_never_consumes_a_key():
    payments = FakePaymentService(verify=_verify_bad("expired"))
    outcome, ctx = await _drive(payment_service=payments)

    assert outcome.kind == pts.VERIFY_FAILED
    assert outcome.status_code == 403
    assert outcome.payload == {
        "detail": "Payment verification failed", "error": "expired",
    }
    assert ctx.db.actions() == ["reject"]
    # THE ordering invariant: begin() is never reached, so a rejected token
    # cannot burn the idempotency key a legitimate retry needs.
    assert ctx.idem.begin_calls == []
    assert payments.settle_calls == []


async def test_verify_runs_before_the_dedup_gate_on_the_success_path():
    _, ctx = await _drive()
    assert ctx.order.index("verify") < ctx.order.index("begin")


# ---------------------------------------------------------------------------
# 2. in-flight duplicate
# ---------------------------------------------------------------------------

async def test_in_flight_duplicate_is_409_and_does_not_execute():
    idem = FakeIdem(SimpleNamespace(replay=True, in_flight=True, snapshot=None,
                                    scope="sc", key="ky"))
    executed = []

    async def _execute():
        executed.append(1)
        return _exec()

    outcome, ctx = await _drive(idem=idem, execute=_execute)
    assert outcome.kind == pts.IN_FLIGHT
    assert outcome.status_code == 409
    assert executed == []
    assert ctx.payments.settle_calls == []


# ---------------------------------------------------------------------------
# 3. replay of a SETTLED snapshot — verbatim, no re-execute, no re-settle
# ---------------------------------------------------------------------------

async def test_settled_replay_is_verbatim_and_burns_nothing():
    snapshot = {
        "response": "stored", "execution_id": "exec-9", "status": "success",
        "payment": {"settled": True, "credits_burned": 1, "tx_hash": "0xold"},
    }
    idem = FakeIdem(SimpleNamespace(replay=True, in_flight=False,
                                    snapshot=snapshot, scope="sc", key="ky"))
    executed = []

    async def _execute():
        executed.append(1)
        return _exec()

    outcome, ctx = await _drive(idem=idem, execute=_execute)
    assert outcome.kind == pts.REPLAY_SETTLED
    assert outcome.payload is snapshot
    assert outcome.replayed is True
    assert outcome.settled is True
    assert executed == []
    assert ctx.payments.settle_calls == []          # no second burn
    assert "settle" not in ctx.db.actions()


# ---------------------------------------------------------------------------
# 4/5. replay of an UNSETTLED snapshot — re-settle, converge or stay honest
# ---------------------------------------------------------------------------

def _unsettled_snapshot():
    return {
        "response": "stored", "execution_id": "exec-9",
        "status": "success_unsettled",
        "payment": {"settled": False, "error": "boom", "settle_retry_needed": True},
    }


async def test_unsettled_replay_resettles_and_upgrades_the_snapshot():
    idem = FakeIdem(SimpleNamespace(replay=True, in_flight=False,
                                    snapshot=_unsettled_snapshot(),
                                    scope="sc", key="ky"))
    executed = []

    async def _execute():
        executed.append(1)
        return _exec()

    outcome, ctx = await _drive(idem=idem, execute=_execute)

    assert outcome.kind == pts.SETTLED
    assert outcome.replayed is True
    assert executed == []                            # the LLM does NOT re-run
    assert len(ctx.payments.settle_calls) == 1
    assert outcome.payload["status"] == "success"
    assert outcome.payload["response"] == "stored"    # the stored work, not a new turn
    assert outcome.payload["payment"]["settled"] is True
    assert "settle" in ctx.db.actions()
    # The convergence that stops a third request re-settling forever.
    assert ctx.idem.upgrades and ctx.idem.upgrades[-1][2]["payment"]["settled"] is True


async def test_unsettled_replay_that_still_cannot_settle_replays_honestly():
    snapshot = _unsettled_snapshot()
    idem = FakeIdem(SimpleNamespace(replay=True, in_flight=False, snapshot=snapshot,
                                    scope="sc", key="ky"))
    payments = FakePaymentService(settle=_settle_bad())
    outcome, ctx = await _drive(idem=idem, payment_service=payments)

    assert outcome.kind == pts.REPLAY_UNSETTLED
    assert outcome.payload is snapshot                # unchanged, still unsettled
    assert outcome.settled is False
    assert ctx.idem.upgrades == []                    # nothing to converge
    assert ctx.idem.completed == [] and ctx.idem.failed == []


# ---------------------------------------------------------------------------
# 6. fresh settled success
# ---------------------------------------------------------------------------

async def test_settled_success_logs_the_burn_and_completes_the_claim():
    outcome, ctx = await _drive()

    assert outcome.kind == pts.SETTLED
    assert outcome.replayed is False
    assert outcome.execution_id == "exec-1"
    assert outcome.payload == {
        "response": "the answer",
        "execution_id": "exec-1",
        "status": "success",
        "payment": {
            "settled": True, "credits_burned": 1,
            "remaining_balance": "9", "tx_hash": "0xtx",
        },
    }
    assert ctx.db.actions() == ["verify", "settle"]
    assert ctx.idem.attached == ["exec-1"]
    assert ctx.idem.upgrades[-1][2] is outcome.payload
    # Settle strictly after the turn ran.
    assert ctx.order.index("execute") < ctx.order.index("settle")


async def test_settle_receives_the_verify_agent_request_id():
    """#1084: the settle effect guard keys on the id verify handed back."""
    _, ctx = await _drive()
    assert ctx.payments.settle_calls[0]["agent_request_id"] == "areq-1"


# ---------------------------------------------------------------------------
# 7/8. delivered but unsettled — THE #1018 branch
# ---------------------------------------------------------------------------

async def test_failed_settle_keeps_the_work_tells_the_truth_and_completes():
    payments = FakePaymentService(settle=_settle_bad("chain down"))
    outcome, ctx = await _drive(payment_service=payments)

    assert outcome.kind == pts.UNSETTLED
    assert outcome.payload["status"] == "success_unsettled"   # not a lie
    assert outcome.payload["response"] == "the answer"        # delivered anyway
    assert outcome.payload["payment"] == {
        "settled": False, "error": "chain down", "settle_retry_needed": True,
    }
    assert ctx.db.actions() == ["verify", "settle_failed"]
    # complete(), NOT fail() — fail() would re-run the LLM on the client's retry.
    assert ctx.idem.completed and ctx.idem.failed == []
    assert ctx.idem.completed[0][1] is outcome.payload


async def test_concurrent_settle_in_progress_is_not_logged_as_a_failure():
    """The effect guard's in-progress result: the other settle logs its own row."""
    payments = FakePaymentService(settle=_settle_bad("settlement already in progress"))
    outcome, ctx = await _drive(payment_service=payments)

    assert outcome.kind == pts.UNSETTLED
    assert outcome.payload["payment"]["settle_in_progress"] is True
    assert "settle_retry_needed" not in outcome.payload["payment"]
    assert ctx.db.actions() == ["verify"]            # no settle_failed row
    assert ctx.idem.completed and ctx.idem.failed == []


# ---------------------------------------------------------------------------
# 9/10/11. the three no-charge terminals
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,kind", [
    ("failed", pts.EXECUTION_FAILED),
    ("cancelled", pts.EXECUTION_CANCELLED),
])
async def test_failed_or_cancelled_turn_never_settles(status, kind):
    async def _execute():
        return _exec(status=status, response="partial work")

    outcome, ctx = await _drive(execute=_execute)

    assert outcome.kind == kind
    assert ctx.payments.settle_calls == []           # the #679 money bug
    assert outcome.payload["payment"]["settled"] is False
    assert ctx.idem.failed and ctx.idem.completed == []
    assert "settle" not in ctx.db.actions()


async def test_failed_turn_withholds_the_body_but_cancelled_keeps_it():
    """#1018 vs #679: garbled output is withheld; the payer's own cancel is not."""
    async def _failed():
        return _exec(status="failed", response="garbled")

    async def _cancelled():
        return _exec(status="cancelled", response="partial work")

    failed, _ = await _drive(execute=_failed)
    cancelled, _ = await _drive(execute=_cancelled)

    assert "response" not in failed.payload
    assert cancelled.payload["response"] == "partial work"


async def test_execution_exception_releases_the_claim_and_charges_nothing():
    async def _boom():
        raise RuntimeError("dispatch exploded")

    outcome, ctx = await _drive(execute=_boom)

    assert outcome.kind == pts.EXECUTION_ERROR
    assert outcome.status_code == 500
    assert outcome.payload["payment"] == {
        "settled": False, "reason": "Execution failed — no charge",
    }
    assert ctx.payments.settle_calls == []
    assert ctx.idem.failed and ctx.idem.completed == []
    # The verify row is annotated with why nothing was charged.
    assert ctx.db.logs[-1]["action"] == "verify"
    assert "dispatch exploded" in ctx.db.logs[-1]["error"]


# ---------------------------------------------------------------------------
# 12/13. cancellation is phase-aware (#679 E5)
# ---------------------------------------------------------------------------

async def test_cancellation_before_a_result_releases_the_claim():
    async def _execute():
        raise asyncio.CancelledError()

    idem = FakeIdem()
    with pytest.raises(asyncio.CancelledError):
        await _drive(execute=_execute, idem=idem)

    assert idem.failed, "a stranded in-flight claim 409s the payer's own retry"
    assert idem.completed == []


async def test_disconnect_during_settle_still_settles_and_records_it():
    """The work is done and owed for: a client walking away must not abort settle.

    Without the shield the settle is cancelled mid-flight, the claim stays
    in-flight with the money unrecorded, and the payer's retry pays for a second
    LLM run.
    """
    settle_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_settle(**kwargs):
        settle_started.set()
        await release.wait()
        return _settle_ok()

    idem = FakeIdem()
    db = FakeDb()
    payments = FakePaymentService(settle=_slow_settle)

    task = asyncio.create_task(_drive(payment_service=payments, idem=idem, db=db))
    await asyncio.wait_for(settle_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    # The settle and its bookkeeping are detached (C1), so join on the record.
    await _until(lambda: db.actions() == ["verify", "settle"])

    assert len(payments.settle_calls) == 1
    assert db.actions() == ["verify", "settle"]        # the burn IS recorded
    assert idem.upgrades, "the claim must converge to settled, not stay in-flight"
    assert idem.upgrades[-1][2]["payment"]["settled"] is True


async def test_disconnect_during_a_settle_that_fails_persists_the_unsettled_work():
    settle_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_bad_settle(**kwargs):
        settle_started.set()
        await release.wait()
        return _settle_bad("chain down")

    idem = FakeIdem()
    db = FakeDb()
    payments = FakePaymentService(settle=_slow_bad_settle)

    task = asyncio.create_task(_drive(payment_service=payments, idem=idem, db=db))
    await asyncio.wait_for(settle_started.wait(), timeout=2)
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    await _until(lambda: bool(idem.completed))

    # complete(), not fail(): the retry re-drives settle, it does not re-run the LLM.
    assert idem.completed and idem.failed == []
    assert idem.completed[0][1]["status"] == "success_unsettled"


# ---------------------------------------------------------------------------
# 13b. the cancellation shape the real consumer produces (C1)
# ---------------------------------------------------------------------------

async def test_cancel_scope_during_settle_still_records_the_burn():
    """The two tests above cancel the TASK; Starlette cancels a SCOPE.

    `asyncio.Task.cancel()` is edge-triggered: one `CancelledError` is delivered
    and every later `await` in the handler proceeds normally. `StreamingResponse`
    runs its body generator inside an anyio task group and cancels that group's
    **cancel scope** on client disconnect, and anyio cancellation is
    LEVEL-triggered — every subsequent `await` inside the cancelled scope raises
    `CancelledError` again. So a recovery handler that awaits the settle before
    writing its rows never reaches them: the facilitator burns credits, no
    `settle` row is written, and the claim stays in-flight for the key's whole
    24 h TTL (C1, abilityai/trinity-enterprise#679).

    The settle's bookkeeping therefore has to live in the DETACHED task, which is
    not inside the cancelled scope. This test is the probe for that: it cancels
    the scope, not the task.
    """
    settle_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_settle(**kwargs):
        settle_started.set()
        await release.wait()
        return _settle_ok()

    idem = FakeIdem()
    db = FakeDb()
    payments = FakePaymentService(settle=_slow_settle)

    async def _turn():
        await _drive(payment_service=payments, idem=idem, db=db)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_turn)
        await asyncio.wait_for(settle_started.wait(), timeout=2)
        tg.cancel_scope.cancel()
        release.set()

    # The settle outlives the cancelled scope and owns its own bookkeeping.
    await _until(lambda: db.actions() == ["verify", "settle"])

    assert len(payments.settle_calls) == 1
    assert db.actions() == ["verify", "settle"], "the burn must still be recorded"
    assert idem.upgrades, "the claim must converge to settled, not stay in-flight"
    assert idem.upgrades[-1][2]["payment"]["settled"] is True


async def test_cancel_scope_during_a_settle_that_fails_persists_the_unsettled_work():
    """Same scope-level cancellation, settle-failed branch.

    `complete()` — not `fail()` — so the payer's retry replays the delivered work
    and re-drives settle instead of paying for a second LLM run.
    """
    settle_started = asyncio.Event()
    release = asyncio.Event()

    async def _slow_bad_settle(**kwargs):
        settle_started.set()
        await release.wait()
        return _settle_bad("chain down")

    idem = FakeIdem()
    db = FakeDb()
    payments = FakePaymentService(settle=_slow_bad_settle)

    async def _turn():
        await _drive(payment_service=payments, idem=idem, db=db)

    async with anyio.create_task_group() as tg:
        tg.start_soon(_turn)
        await asyncio.wait_for(settle_started.wait(), timeout=2)
        tg.cancel_scope.cancel()
        release.set()

    await _until(lambda: db.actions() == ["verify", "settle_failed"])

    assert db.actions() == ["verify", "settle_failed"]
    assert idem.completed and idem.failed == []
    assert idem.completed[0][1]["status"] == "success_unsettled"


# ---------------------------------------------------------------------------
# 14. the `endpoint` thread (decision 19)
# ---------------------------------------------------------------------------

async def test_endpoint_is_threaded_to_both_facilitator_calls():
    """An x402 v3 token signs `resourceUrl`, so verify and settle must agree."""
    _, ctx = await _drive(endpoint="http://localhost/a2a/agent-a")
    assert ctx.payments.verify_calls[0]["endpoint"] == "http://localhost/a2a/agent-a"
    assert ctx.payments.settle_calls[0]["endpoint"] == "http://localhost/a2a/agent-a"


async def test_endpoint_defaults_to_none_so_the_paid_door_is_unchanged():
    _, ctx = await _drive()
    assert ctx.payments.verify_calls[0]["endpoint"] is None
    assert ctx.payments.settle_calls[0]["endpoint"] is None


# ---------------------------------------------------------------------------
# 15/16. the two injection seams the A2A gate needs
# ---------------------------------------------------------------------------

async def test_pre_execute_abort_short_circuits_after_the_gate_before_the_turn():
    executed = []

    async def _execute():
        executed.append(1)
        return _exec()

    def _refuse(verify):
        raise pts.PaidTurnAbort({"detail": "nope"}, status_code=400)

    idem = FakeIdem()
    outcome, ctx = await _drive(execute=_execute, idem=idem, pre_execute=_refuse)

    assert outcome.kind == pts.ABORTED
    assert outcome.status_code == 400
    assert outcome.payload == {"detail": "nope"}
    assert executed == []
    assert idem.begin_calls, "the refusal runs AFTER the dedup gate, as it does today"
    assert ctx.payments.settle_calls == []


async def test_pre_execute_abort_releases_the_fresh_claim():
    """I2: a refusal that keeps the claim 409s the payer's own retry for 24 h.

    `begin()` has already run when `pre_execute` refuses, so without a release
    the identical retry is answered IN_FLIGHT ("a duplicate paid request is
    still being processed") instead of the refusal that says why — for the key's
    whole TTL. Nothing was charged and nothing was delivered, so the claim must
    be released, exactly as the cancelled/failed/raised execution branches do.
    """
    def _refuse(verify):
        raise pts.PaidTurnAbort({"detail": "wallet not allowed"}, status_code=403)

    idem = FakeIdem()
    outcome, _ = await _drive(idem=idem, pre_execute=_refuse)

    assert outcome.kind == pts.ABORTED
    assert idem.failed, "the refused payer's retry must reach the refusal, not a 409"
    assert idem.completed == []


async def test_scope_may_be_derived_from_the_payer(monkeypatch):
    """The A2A gate namespaces its dedup scope by payer wallet (FR-4).

    Only verify knows the payer, so the scope has to be resolvable afterwards —
    without verifying twice.
    """
    seen = {}

    async def _execute():
        return _exec()

    idem = FakeIdem()
    payments = FakePaymentService(verify=_verify_ok(payer="0xcafe"),
                                  settle=_settle_ok())
    await pts.run_paid_turn(
        agent_name="agent-a",
        config=_config(),
        nvm_api_key="k",
        access_token="tok",
        idem_scope=lambda verify: f"a2a:agent-a:pay:{verify.payer}",
        idem_key="key-1",
        execute=_execute,
        payment_service=payments,
        idem=idem,
        db=FakeDb(),
    )
    seen["scope"], seen["key"] = idem.begin_calls[0]
    assert seen["scope"] == "a2a:agent-a:pay:0xcafe"
    assert seen["key"] == "key-1"
