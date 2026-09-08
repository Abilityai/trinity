"""#2048 — `PULL_MODE_PILOT_AGENTS` can only route agent-to-agent work.

`_AUTONOMOUS_TRIGGERS` declares seven triggers autonomous, and `pull_owns_dispatch`
consulted all seven. But `capacity_manager.acquire` only offers a row to the queue
when its producer passed `overflow_policy="queue_persistent"`, and exactly one of
the three producers does:

    task_execution_service   → "reject"            scheduler/ALL cron, fan-out
    dispatch_admission_svc   → "queue_in_memory"   sequential chat, human chat
    chat_execution_service   → "queue_persistent"  POST /task              ← only

So on a cron-driven agent the pilot flag is INERT, and #1766's soak measures
nothing. Worse, it is inert *silently*: the gate is behind an
`overflow_policy == "queue_persistent"` short-circuit, so for a `schedule` row
`pull_owns_dispatch` is never called at all and the row takes the push path
indistinguishably from the flag being unset — while §9 M1 told the operator to go
check an env var that is present and correct.

Issue decision: **Option 1** — make the system honest about the reach it has,
rather than extending the reach. So these tests pin two things:

  * the predicate no longer CLAIMS reach it does not have (`PULL_REACHABLE_TRIGGERS`),
    while remaining a runtime no-op — the narrowing must not change any live
    verdict, only stop the lie; and
  * a stranded row is now DISTINGUISHABLE from an ineligible one
    (`note_unreachable_pull_trigger`), which is the acceptance criterion the
    original code failed in both directions.

**#2391 fired this file's tripwire, deliberately.** Option 2 landed: the
`reject` producer now dispatches with `overflow_policy="queue_persistent"` when
— and only when — `pull_owns_dispatch` says a pilot owns the trigger, so
`schedule`, `webhook` and `reminder` joined the reachable set. The structural
test below was re-derived rather than relaxed: it now pins BOTH policies on that
producer *and* pins that the wider one is gated, which is the property that was
never asserted before and is the one that actually matters. Everything else in
this file still holds — a stranded trigger is still stranded (`loop`, `fan_out`,
`a2a`, `operator_response`, all four blocked by a synchronous result consumer
rather than by the producer's policy), and the diagnostic still tells the
operator so.

Pure unit test — no Redis, no DB, no agent. Path bootstrap and lazy imports
follow `test_1766_pull_pilot_exclusive.py`.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_THIS = Path(__file__).resolve()
_REPO = _THIS.parent.parent.parent
_BACKEND = _REPO / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
if _BACKEND_STR not in sys.path:
    sys.path.insert(0, _BACKEND_STR)

pytestmark = pytest.mark.unit

# The triggers that are declared autonomous but cannot reach the queue.
#
# After #2391 none of these is stranded on the producer's overflow policy any
# more — that producer can queue. They are stranded on their CALLER, which reads
# the returned `TaskExecutionResult`; a row queued for a worker to claim later
# gives it nothing to read. Three structural, one a scope choice.
#
#   `fan_out`  — `fan_out_service` builds each `FanOutTaskResult` from the result
#                and `POST /fan-out` blocks on the aggregate; the async join +
#                sync edge adapter is #2524 (#1081 Phase 4).
#   `a2a`      — `routers/a2a`'s `message/send` consumes `result.response` to
#                build the JSON-RPC artifact it hands back to the remote caller
#                (ent#157). Falls out of #2524's adapter.
#   `operator_response` — out by CHOICE, not structurally: it dispatches through
#                the same producer #2391 widened, but the respond endpoint records
#                `result.status` as the dispatch receipt (audit row + #525
#                idempotency completion) and ent#329 exists because this spends
#                money on a person's answer. "queued" is not the outcome that
#                contract reports. A deliberate follow-up, not an oversight.
#
# `PULL_REACHABLE_TRIGGERS` is an explicit allow-list, so an unlisted trigger
# lands here automatically — this comment is the review the test below demands.
_STRANDED = ["fan_out", "a2a", "operator_response"]
# The six that can. `agent` + `event` arrive via `POST /task`; `schedule`,
# `webhook` and `reminder` via the scheduler's async-poll dispatch (#2391);
# `loop` since #2523 made `loop_service` terminal-driven, so its dispatch has no
# reader either.
_REACHABLE = ["agent", "event", "schedule", "webhook", "reminder", "loop"]


@pytest.fixture
def pilot(monkeypatch):
    """Make `pilot-a` a pull pilot and reset the once-per-process dedup set."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "pilot-a")
    import services.pull_pilot as pp

    pp._UNREACHABLE_NOTED.clear()
    yield pp
    pp._UNREACHABLE_NOTED.clear()


# ---------------------------------------------------------------------------
# The reach set is honest
# ---------------------------------------------------------------------------


def test_reachable_set_is_what_the_two_queueing_producers_can_actually_emit():
    """The claim behind `PULL_REACHABLE_TRIGGERS`, stated as an assertion.

    Two producers can pass `overflow_policy="queue_persistent"` since #2391, and
    the reachable set is exactly what they contribute from the autonomous set:

      * `POST /task` (`chat_execution_service`) — `_derive_task_trigger` can only
        ever produce `{self_task, agent, mcp, manual, event}`, which contributes
        `agent` + `event`.
      * `task_execution_service` — contributes the autonomous triggers with no
        synchronous result consumer: everything the scheduler dispatches
        async-and-polls (`schedule`, `webhook`, `reminder`) plus `loop`, whose
        driver became terminal-driven in #2523.

    Derived here rather than hardcoded so the constant cannot quietly disagree
    with the reasoning that justifies it.
    """
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    task_route_can_emit = {"self_task", "agent", "mcp", "manual", "event"}
    scheduler_async_polled = {"schedule", "webhook", "reminder"}
    # #2523: `loop_service` is advanced by execution terminals, so its dispatch
    # has no synchronous reader either.
    terminal_driven = {"loop"}
    assert PULL_REACHABLE_TRIGGERS == (
        (task_route_can_emit | scheduler_async_polled | terminal_driven)
        & _AUTONOMOUS_TRIGGERS
    )
    assert PULL_REACHABLE_TRIGGERS == {
        "agent", "event", "schedule", "webhook", "reminder", "loop"
    }


def test_every_reachable_trigger_from_this_producer_is_also_1083_shaped():
    """Why `{schedule, webhook, reminder}` and not "the rest of the autonomous
    set": all three reach `execute_task` from the scheduler, which dispatches
    with `async_mode=True` and then polls the DB — nobody holds a coroutine on
    the return value. That is the same property #1083 selects on for
    fire-and-forget, so its eligible set must be a SUBSET of what this producer
    can queue. If someone widens `ASYNC_DISPATCH_ELIGIBLE_TRIGGERS` to a trigger
    with a synchronous consumer, this fails and names the contradiction."""
    from config import ASYNC_DISPATCH_ELIGIBLE_TRIGGERS
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS

    assert ASYNC_DISPATCH_ELIGIBLE_TRIGGERS <= PULL_REACHABLE_TRIGGERS


def test_the_stranded_triggers_are_named_and_complete():
    """No autonomous trigger is left unclassified: every one is either reachable
    or stranded, so a trigger added to `_AUTONOMOUS_TRIGGERS` later shows up here
    as an unreviewed addition rather than silently joining the stranded set."""
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert _AUTONOMOUS_TRIGGERS - PULL_REACHABLE_TRIGGERS == set(_STRANDED)
    assert PULL_REACHABLE_TRIGGERS <= _AUTONOMOUS_TRIGGERS


@pytest.mark.parametrize("trigger", _STRANDED)
def test_pull_does_not_own_dispatch_for_a_stranded_trigger(pilot, trigger):
    """The defect, stated directly: the predicate used to answer True for these
    while dispatch could never honour it."""
    assert pilot.pull_owns_dispatch("pilot-a", trigger) is False


@pytest.mark.parametrize("trigger", _REACHABLE)
def test_pull_still_owns_dispatch_for_a_reachable_trigger(pilot, trigger):
    """#1766's actual behaviour must be untouched — this is a narrowing of a
    claim, not a narrowing of function."""
    assert pilot.pull_owns_dispatch("pilot-a", trigger) is True


def test_narrowing_is_a_runtime_no_op_on_the_only_producer_that_consults_it(pilot):
    """The safety property of Option 1.

    `pull_owns_dispatch` is only ever reached behind
    `overflow_policy == "queue_persistent"`, i.e. only for triggers `POST /task`
    can emit. Over exactly that domain the narrowed predicate must agree with
    the un-narrowed one, so this change cannot alter a single live verdict — it
    only stops the predicate from claiming reach it does not have.
    """
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS
    from services.pull_pilot import pull_owns_dispatch

    for trigger in ("self_task", "agent", "mcp", "manual", "event"):
        before = trigger in _AUTONOMOUS_TRIGGERS  # the pre-#2048 predicate body
        assert pull_owns_dispatch("pilot-a", trigger) is before, trigger


# ---------------------------------------------------------------------------
# A stranded row is now distinguishable from an ineligible one
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("trigger", _STRANDED)
def test_a_stranded_trigger_on_a_pilot_is_reported(pilot, trigger, caplog):
    """AC: "A pushed row for a trigger that *should* have been pulled is
    distinguishable from one that was never eligible (today both are silent)."."""
    with caplog.at_level("WARNING"):
        assert pilot.note_unreachable_pull_trigger("pilot-a", trigger) is True
    assert "#2048" in caplog.text
    assert trigger in caplog.text


def test_the_message_contradicts_the_advice_that_used_to_misfire(pilot, caplog):
    """The operator-facing point of the line. §9 M1 said a pushed autonomous row
    means "check the backend actually has PULL_MODE_PILOT_AGENTS in its env" —
    for a stranded row that sends them after a variable that is present and
    correct. The log has to say so in words, not merely fire.

    Driven with `fan_out`: `schedule` became reachable in #2391 and `loop` in
    #2523, so using either here would assert the diagnostic over a case that no
    longer exists.
    """
    with caplog.at_level("WARNING"):
        pilot.note_unreachable_pull_trigger("pilot-a", "fan_out")
    assert "flag is applied and correct" in caplog.text
    assert "topology" in caplog.text


def test_it_reports_once_per_agent_and_trigger(pilot, caplog):
    """This sits on the dispatch path of every fan-out subtask. An un-deduped
    warning would emit thousands of identical lines a day and train operators to
    filter it — a signal meant to be noticed becoming noise."""
    with caplog.at_level("WARNING"):
        first = pilot.note_unreachable_pull_trigger("pilot-a", "fan_out")
        repeats = [pilot.note_unreachable_pull_trigger("pilot-a", "fan_out") for _ in range(50)]
    assert first is True
    assert not any(repeats)
    assert caplog.text.count("#2048") == 1


def test_dedup_is_per_trigger_not_per_agent(pilot):
    """A pilot running fan-out AND serving A2A has two distinct gaps to report."""
    assert pilot.note_unreachable_pull_trigger("pilot-a", "fan_out") is True
    assert pilot.note_unreachable_pull_trigger("pilot-a", "a2a") is True


@pytest.mark.parametrize("trigger", _REACHABLE)
def test_a_reachable_trigger_is_not_reported(pilot, trigger):
    """It reached this producer only because a slot was free, not because it is
    stranded. Reporting it would be a false alarm."""
    assert pilot.note_unreachable_pull_trigger("pilot-a", trigger) is False


@pytest.mark.parametrize("trigger", ["manual", "mcp", "chat", "self_task", "voip"])
def test_an_interactive_trigger_is_not_reported(pilot, trigger):
    """Interactive turns are excluded from pull by design (Open Question 7's
    scope cut), so they are not a gap and must not be logged as one."""
    assert pilot.note_unreachable_pull_trigger("pilot-a", trigger) is False


def test_a_non_pilot_agent_is_never_reported(pilot):
    """Inertness: the whole feature is dark for an agent not in the allowlist,
    and that must include its diagnostics."""
    assert pilot.note_unreachable_pull_trigger("some-other-agent", "schedule") is False


def test_everything_is_inert_with_an_empty_allowlist(monkeypatch):
    """The default. Byte-for-byte unchanged behaviour, no new log lines."""
    monkeypatch.setenv("PULL_MODE_PILOT_AGENTS", "")
    import services.pull_pilot as pp

    pp._UNREACHABLE_NOTED.clear()
    for trigger in _STRANDED + _REACHABLE:
        assert pp.pull_owns_dispatch("pilot-a", trigger) is False
        assert pp.note_unreachable_pull_trigger("pilot-a", trigger) is False


@pytest.mark.parametrize("trigger", [None, "", 123, object()])
def test_the_diagnostic_never_raises(pilot, trigger):
    """It runs inline on the dispatch path. A bookkeeping error must never
    interfere with dispatching a real execution."""
    assert pilot.note_unreachable_pull_trigger("pilot-a", trigger) is False


def test_the_diagnostic_never_raises_on_a_hostile_agent_name(pilot):
    assert pilot.note_unreachable_pull_trigger(None, "schedule") is False


# ---------------------------------------------------------------------------
# Structural: the topology claim, and the guard against it drifting
# ---------------------------------------------------------------------------


def _overflow_policies(relative_path: str) -> set:
    """Distinct `overflow_policy="..."` literals in a module.

    A set, not a list: prose in a comment can legitimately quote the same
    literal, and the claim under test is *which policies this producer uses*,
    not how many times the string appears.
    """
    source = (_BACKEND / relative_path).read_text(encoding="utf-8")
    return set(re.findall(r"overflow_policy\s*=\s*[\"'](\w+)[\"']", source))


def test_the_producer_topology_this_fix_rests_on_still_holds():
    """The load-bearing fact, pinned as a test rather than a comment.

    #2048 pinned `task_execution_service == {"reject"}` as a tripwire for its own
    deferred Option 2. #2391 implemented Option 2, so the tripwire fired and is
    re-derived here rather than deleted: the producer now carries BOTH literals,
    which is the shape a *conditional* widening has and an unconditional one does
    not. Pair it with `test_the_wider_policy_is_gated_on_the_pull_predicate`
    below — that is the assertion #2048 could not make and the one that keeps the
    blast radius equal to the pilot allowlist.
    """
    assert _overflow_policies("services/task_execution_service.py") == {
        "reject", "queue_persistent",
    }, (
        "task_execution_service's overflow policies changed. Since #2391 it must "
        "carry exactly two: 'reject' (the default, unchanged for every non-pilot "
        "agent) and 'queue_persistent' (pilot-gated). Losing 'reject' would mean "
        "scheduled work is queued for the whole fleet — the reliability-spine "
        "change #2048 declined to make. Re-derive PULL_REACHABLE_TRIGGERS and "
        "update PULL_MIGRATION_TESTING.md §9."
    )
    assert "queue_persistent" in _overflow_policies("services/chat_execution_service.py")
    assert "queue_in_memory" in _overflow_policies("services/dispatch_admission_service.py")


def test_the_wider_policy_is_gated_on_the_pull_predicate():
    """The #2391 safety property, pinned structurally.

    `queue_persistent` on this producer is reachable ONLY through a non-None
    `pull_overflow_payload`, and the only thing that builds one is
    `build_pull_queue_payload`, whose sole widening condition is
    `pull_owns_dispatch` (false for every agent outside the allowlist). Assert
    the chain so nobody can later make the policy unconditional — which would
    change capacity-pressure semantics for the entire fleet, flag or no flag —
    without this failing.
    """
    source = (_BACKEND / "services" / "task_execution_service.py").read_text(encoding="utf-8")

    # The policy choice is a branch on the payload, not a constant.
    assert 'if pull_overflow_payload is not None:\n                overflow_policy = "queue_persistent"' in source
    assert 'overflow_policy = "reject"' in source
    # The payload builder refuses unless the pull predicate says so.
    builder = source[source.index("def build_pull_queue_payload("):]
    builder = builder[: builder.index("\ndef ")]
    assert "pull_owns_dispatch(agent_name, triggered_by)" in builder
    assert "slot_already_held or not execution_id" in builder


def test_the_reject_producer_reports_the_gap():
    """The wiring, checked structurally: the diagnostic has to sit at the
    producer that actually knows `triggered_by`. `capacity_manager` cannot do it
    — the `reject` path passes no `overflow_payload`, so the trigger is not even
    in scope there."""
    source = (_BACKEND / "services" / "task_execution_service.py").read_text(encoding="utf-8")
    assert "note_unreachable_pull_trigger(agent_name, triggered_by)" in source


def test_capacity_manager_still_short_circuits_before_the_pull_gate():
    """Why the diagnostic cannot live in `capacity_manager`: `and` short-circuits,
    so for a `reject` row `pull_owns_dispatch` is never invoked. Pinned so the
    reasoning stays true."""
    source = (_BACKEND / "services" / "capacity_manager.py").read_text(encoding="utf-8")
    gate = source[source.index("pull_exclusive = ("):]
    gate = gate[: gate.index(")")]
    assert 'overflow_policy == "queue_persistent"' in gate
    assert "pull_owns_dispatch" in gate
    assert gate.index('overflow_policy == "queue_persistent"') < gate.index("pull_owns_dispatch")
