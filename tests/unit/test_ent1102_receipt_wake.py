"""ent#1102 — the receipt tells the delegating caller whether a turn is coming.

WHY THIS FILE EXISTS. `chat.ts::resolveWake` shipped green while the feature was
inert: its only live input is "did the dispatch answer carry `wake_on_ending`",
and no backend path emitted that key. Every test of the resolver handed the
policy in by hand, so the dead wiring was invisible (the #2619/#3120 class). The
delegation contract meanwhile tells every agent to read the field and to STOP
setting a reminder when it says `on_ending` — so a silent receipt is not a
missing nicety, it is the contract mis-teaching.

These tests drive the real receipt builders in `chat_execution_service` and
assert on what the caller actually receives.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

pytest.importorskip("database")  # backend venv


@pytest.fixture
def ces():
    import services.chat_execution_service as mod

    return mod


@pytest.fixture
def stub_policy(monkeypatch):
    """Point the resolver at a known verdict per caller, via the real resolver's
    own db reads — so the receipt executes `wake_policy.resolve`, not a fake."""
    from database import db as real_db

    def _set(*, wake=None, legacy=False, instance="true"):
        monkeypatch.setattr(real_db, "get_wake_on_ending", lambda n: wake, raising=False)
        monkeypatch.setattr(
            real_db, "get_operator_resume_enabled", lambda n: legacy, raising=False
        )
        monkeypatch.setattr(
            real_db,
            "get_setting_value",
            lambda key, default=None: instance if key == "wake_on_ending_default" else default,
            raising=False,
        )

    return _set


@pytest.fixture
def captured(ces, monkeypatch):
    """The receipt as the idempotency snapshot stores it — the exact body a
    replay answers with, and the same dict the route returns."""
    snapshots: list = []
    monkeypatch.setattr(
        ces,
        "idempotency_service",
        SimpleNamespace(
            complete=lambda idem, eid, payload: snapshots.append(payload),
            fail=lambda idem: None,
        ),
    )
    return snapshots


def _user():
    return SimpleNamespace(id="u1", email="ann@example.com", username="ann")


async def _dispatch(ces, monkeypatch, *, state, caller, name="callee"):
    """Run the real `_dispatch_async` receipt branch for `state`."""
    monkeypatch.setattr(
        ces,
        "_acquire_task_capacity",
        _acquire(state),
    )

    async def _noop(**kwargs):
        return None

    monkeypatch.setattr(ces, "run_async_task", _noop)
    return await ces._dispatch_async(
        request=SimpleNamespace(message="go", model=None, timeout_seconds=None, max_turns=None),
        name=name,
        current_user=_user(),
        execution_id="exec-1",
        subscription_id=None,
        collaboration_activity_id=None,
        self_task_activity_id=None,
        is_self_task=caller == name,
        triggered_by="agent_call",
        reserved_event_dispatch=False,
        image_data=[],
        idem=object(),
        x_source_agent=caller,
    )


def _acquire(state):
    async def _stub(**kwargs):
        return SimpleNamespace(state=state, pull=False), 60

    return _stub


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued_persistent", "acquired"])
async def test_a_receipt_for_a_wake_on_caller_promises_the_turn(
    ces, monkeypatch, stub_policy, captured, state
):
    """The caller is woken, so the contract's `wake: on_ending` is honest and
    the caller is right to end its turn instead of arming a reminder."""
    stub_policy(instance="true")
    payload = await _dispatch(ces, monkeypatch, state=state, caller="orchestrator")
    assert payload["wake_on_ending"] == {"effective": True, "source": "instance"}
    assert captured[-1]["wake_on_ending"] == payload["wake_on_ending"], (
        "the stored snapshot must carry the field too — a replay answers from it"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued_persistent", "acquired"])
async def test_a_receipt_for_a_wake_off_caller_says_no_turn_is_coming(
    ces, monkeypatch, stub_policy, captured, state
):
    """THE COSTLY CASE. An owner switched the wake off; the receipt has to say
    so, or the caller ends its turn waiting for a turn nobody will dispatch."""
    stub_policy(wake=0, instance="true")
    payload = await _dispatch(ces, monkeypatch, state=state, caller="orchestrator")
    assert payload["wake_on_ending"] == {"effective": False, "source": "agent"}


@pytest.mark.asyncio
async def test_a_humans_receipt_makes_no_claim_at_all(ces, monkeypatch, stub_policy, captured):
    """No caller to wake: the client stands this down itself, and a field here
    would change a result other tests pin byte-for-byte."""
    stub_policy(instance="true")
    payload = await _dispatch(ces, monkeypatch, state="acquired", caller=None)
    assert "wake_on_ending" not in payload


@pytest.mark.asyncio
async def test_a_self_task_receipt_makes_no_claim(ces, monkeypatch, stub_policy, captured):
    """The caller IS the callee; its own ending is not news to it."""
    stub_policy(instance="true")
    payload = await _dispatch(ces, monkeypatch, state="acquired", caller="callee")
    assert "wake_on_ending" not in payload


class TestTheFootnoteNeverBreaksTheReceipt:
    """A receipt is a billed dispatch's only answer. The wake verdict is a
    footnote on it, so an unreadable policy must cost silence, never the 202."""

    def test_an_unreadable_policy_leaves_the_field_off(self, ces, monkeypatch):
        from services import wake_policy

        def _boom(agent_name):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(wake_policy, "resolve", _boom)
        assert ces._wake_on_ending_receipt("orchestrator", "callee") == {}

    def test_the_source_vocabulary_is_the_resolvers_own(self, ces, stub_policy):
        """One vocabulary across the receipt, `get_agent_info` and the audit row
        — an operator reading "why was I woken" gets the layer that decided."""
        stub_policy(legacy=True, instance="false")
        assert ces._wake_on_ending_receipt("orchestrator", "callee") == {
            "wake_on_ending": {"effective": True, "source": "legacy_resume"}
        }
