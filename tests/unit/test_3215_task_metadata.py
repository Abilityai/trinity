"""A settled A2A Task must not report `metadata: null` (abilityai/trinity#3215,
operator scope-add 2026-10-04T11:48:59Z).

`docs/user-docs/integrations/a2a-protocol.md` promises "the reply is a normal A2A
Task whose metadata carries the payment status and a receipt". The x402 metadata
was built and placed — on `status.message.metadata`, which IS the spec location
(the SDK's own `X402Utils.get_payment_status_from_task` reads exactly there) —
but top-level `Task.metadata` was never set, so a typed client (`a2a-sdk`'s
`Task.metadata: dict | None = None`) rendered `metadata: null`, which reads as
"nothing was charged" on a turn that was.

Both halves of the ruling are pinned here: the mirror exists, AND the free path
emits no `metadata` key at all, so an unpaid Task's bytes — and the idempotency
snapshots built from them — are byte-for-byte what they were.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src" / "backend"))

from routers.a2a import _task_object  # noqa: E402
from services import a2a_payment_gate, a2a_protocol, paid_turn_service  # noqa: E402


class TestTaskObjectMirror:

    def test_a_task_with_metadata_carries_it_in_both_places(self):
        meta = {a2a_protocol.X402_STATUS_KEY: "payment-completed"}
        task = _task_object("exec-1", "completed", text="hi", metadata=meta)
        assert task["status"]["message"]["metadata"] == meta
        assert task["metadata"] == meta

    def test_the_two_locations_are_the_same_object_so_they_cannot_drift(self):
        meta = {a2a_protocol.X402_STATUS_KEY: "payment-completed"}
        task = _task_object("exec-1", "completed", metadata=meta)
        assert task["metadata"] is task["status"]["message"]["metadata"]

    def test_a_free_task_emits_no_metadata_key_at_all(self):
        task = _task_object("exec-1", "completed", text="hi")
        assert "metadata" not in task
        assert "message" not in task["status"]

    def test_an_error_task_without_metadata_stays_unchanged(self):
        task = _task_object("exec-1", "failed", error="boom")
        assert "metadata" not in task
        assert task["status"]["message"]["parts"][0]["text"] == "boom"

    @pytest.mark.parametrize("empty", [None, {}])
    def test_empty_metadata_is_not_a_reason_to_emit_the_key(self, empty):
        task = _task_object("exec-1", "completed", text="hi", metadata=empty)
        assert "metadata" not in task


class TestTheGatesRenderedTask:
    """The real producer: `task_from_paid_payload` renders every paid outcome."""

    def _outcome(self, kind, payload, verify=None):
        return SimpleNamespace(kind=kind, payload=payload, execution_id="exec-1",
                              verify=verify)

    def test_a_settled_turn_reports_its_receipt_in_both_locations(self):
        outcome = self._outcome(
            paid_turn_service.SETTLED,
            {"execution_id": "exec-1", "response": "done",
             "payment": {"tx_hash": "0xtx", "credits_burned": "1",
                         "remaining_balance": "9"}},
            verify=SimpleNamespace(payer="0xpayer"),
        )
        task = a2a_payment_gate.task_from_paid_payload(outcome, task_builder=_task_object)
        top = task["metadata"]
        inner = task["status"]["message"]["metadata"]
        assert top == inner
        assert top[a2a_protocol.X402_STATUS_KEY] == a2a_protocol.X402_STATUS_COMPLETED
        assert top[a2a_protocol.X402_RECEIPTS_KEY][0]["transaction"] == "0xtx"

    def test_an_unsettled_success_reports_why_in_both_locations(self):
        outcome = self._outcome(
            paid_turn_service.UNSETTLED,
            {"execution_id": "exec-1", "response": "done",
             "payment": {"settle_in_progress": True}},
        )
        task = a2a_payment_gate.task_from_paid_payload(outcome, task_builder=_task_object)
        assert task["metadata"] == task["status"]["message"]["metadata"]
        assert task["metadata"][a2a_protocol.X402_ERROR_KEY]["code"] == \
            "settle_in_progress"

    def test_a_failed_turn_still_says_no_charge_in_both_locations(self):
        outcome = self._outcome(
            paid_turn_service.EXECUTION_FAILED,
            {"execution_id": "exec-1", "payment": {}},
        )
        task = a2a_payment_gate.task_from_paid_payload(outcome, task_builder=_task_object)
        assert task["metadata"] == task["status"]["message"]["metadata"]
        assert task["metadata"][a2a_protocol.X402_ERROR_KEY]["reason"] == "no charge"

    def _settled_task(self):
        outcome = self._outcome(
            paid_turn_service.SETTLED,
            {"execution_id": "exec-1", "response": "done",
             "payment": {"tx_hash": "0xtx"}},
            verify=SimpleNamespace(payer="0xpayer"),
        )
        return a2a_payment_gate.task_from_paid_payload(
            outcome, task_builder=_task_object
        )

    def test_a_typed_client_no_longer_sees_metadata_null(self):
        """The reported symptom, through the client type that reported it."""
        from a2a.types import Task

        typed = Task.model_validate(self._settled_task())
        assert typed.metadata is not None
        assert typed.metadata[a2a_protocol.X402_STATUS_KEY] == \
            a2a_protocol.X402_STATUS_COMPLETED

    def test_the_sdks_own_reader_still_finds_the_spec_location(self):
        """The mirror must not have moved anything: the x402 A2A extension reads
        `status.message.metadata`, and that is what the docs name."""
        from a2a.types import Task
        from payments_py.x402.a2a import X402A2AUtils

        typed = Task.model_validate(self._settled_task())
        assert X402A2AUtils().get_payment_status_from_task(typed) == \
            a2a_protocol.X402_STATUS_COMPLETED
