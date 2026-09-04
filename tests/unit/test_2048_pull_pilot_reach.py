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

# **There are no stranded autonomous triggers left.** #2391 (cron/webhook/
# reminder), #2523 (loop) and #2524 (fan_out, then a2a + operator_response via
# `dispatch_and_await_terminal`) emptied the set.
#
# The narrowing MECHANISM stays, and these tests keep exercising it against a
# synthetic stranded trigger, because the mechanism is the point: a trigger added
# to `_AUTONOMOUS_TRIGGERS` later must be reviewed against dispatch topology
# rather than inheriting reach by default. A silently-stale reach set is exactly
# the bug #2048 was.
_STRANDED: list = []
# All of them, now.
_REACHABLE = ["agent", "event", "schedule", "webhook", "reminder", "loop",
              "fan_out", "a2a", "operator_response"]
# Stands in for "a trigger somebody adds without classifying it", so the
# diagnostic below is still covered now that nothing real trips it.
_SYNTHETIC_STRANDED = "agent"


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
        async-and-polls (`schedule`, `webhook`, `reminder`), plus `loop` (#2523)
        and `fan_out` (#2524), whose orchestrators stopped holding the work in a
        coroutine.

    Derived here rather than hardcoded so the constant cannot quietly disagree
    with the reasoning that justifies it.
    """
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    task_route_can_emit = {"self_task", "agent", "mcp", "manual", "event"}
    scheduler_async_polled = {"schedule", "webhook", "reminder"}
    # #2523 / #2524: `loop_service` is advanced by execution terminals and a
    # fan-out's aggregate is a query over `fan_out_id`, so neither dispatch has
    # a synchronous reader either.
    db_joined = {"loop", "fan_out"}
    # #2524: these two DO need the answer in-line, and get it from
    # `dispatch_and_await_terminal` — the sync edge adapter — rather than from
    # the dispatch's return value.
    adapter_served = {"a2a", "operator_response"}
    assert PULL_REACHABLE_TRIGGERS == (
        (task_route_can_emit | scheduler_async_polled | db_joined | adapter_served)
        & _AUTONOMOUS_TRIGGERS
    )
    assert PULL_REACHABLE_TRIGGERS == _AUTONOMOUS_TRIGGERS


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
    """No autonomous trigger is left unclassified. As of #2524 that means the
    stranded set is EMPTY — every autonomous trigger can reach the durable
    queue."""
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert _AUTONOMOUS_TRIGGERS - PULL_REACHABLE_TRIGGERS == set(_STRANDED) == set()
    assert PULL_REACHABLE_TRIGGERS <= _AUTONOMOUS_TRIGGERS


def test_the_reach_set_stays_an_explicit_allowlist_even_though_it_is_complete():
    """The guard that outlives its own findings.

    `PULL_REACHABLE_TRIGGERS` now equals `_AUTONOMOUS_TRIGGERS`, which makes
    `PULL_REACHABLE_TRIGGERS = _AUTONOMOUS_TRIGGERS` look like a tidy
    simplification. It is not: it would hand reach to the next trigger somebody
    declares autonomous, without anyone checking that dispatch can deliver it —
    which is precisely the defect #2048 filed. Pinned structurally so the
    "cleanup" fails here instead of shipping.
    """
    source = (_BACKEND / "services" / "pull_pilot.py").read_text(encoding="utf-8")
    literal = source[source.index("PULL_REACHABLE_TRIGGERS = "):]
    literal = literal[: literal.index("\n\n")]
    assert "frozenset(" in literal, "the reach set must be an enumerated literal"
    assert "_AUTONOMOUS_TRIGGERS" not in literal, (
        "PULL_REACHABLE_TRIGGERS must not be derived from _AUTONOMOUS_TRIGGERS — "
        "a new autonomous trigger has to be classified, not inherit reach (#2048)."
    )


def test_pull_does_not_own_dispatch_for_a_stranded_trigger(pilot, monkeypatch):
    """The defect, stated directly: the predicate used to answer True for a
    trigger dispatch could never honour.

    Driven against a synthetic narrowing, since #2524 left nothing really
    stranded — the behaviour under test is the narrowing itself, which is what
    protects the next trigger somebody adds."""
    narrowed = pilot.PULL_REACHABLE_TRIGGERS - {_SYNTHETIC_STRANDED}
    monkeypatch.setattr(pilot, "PULL_REACHABLE_TRIGGERS", narrowed)
    assert pilot.pull_owns_dispatch("pilot-a", _SYNTHETIC_STRANDED) is False
    # …and every other autonomous trigger is unaffected by that narrowing.
    assert pilot.pull_owns_dispatch("pilot-a", "schedule") is True


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


def test_a_stranded_trigger_on_a_pilot_is_reported(pilot, monkeypatch, caplog):
    """AC: "A pushed row for a trigger that *should* have been pulled is
    distinguishable from one that was never eligible (today both are silent)."

    Unreachable in production as of #2524 — kept, and kept tested, because it is
    the runtime half of the allow-list above."""
    narrowed = pilot.PULL_REACHABLE_TRIGGERS - {_SYNTHETIC_STRANDED}
    monkeypatch.setattr(pilot, "PULL_REACHABLE_TRIGGERS", narrowed)
    with caplog.at_level("WARNING"):
        assert pilot.note_unreachable_pull_trigger("pilot-a", _SYNTHETIC_STRANDED) is True
    assert "#2048" in caplog.text
    assert _SYNTHETIC_STRANDED in caplog.text


def test_the_message_contradicts_the_advice_that_used_to_misfire(pilot, monkeypatch, caplog):
    """The operator-facing point of the line. §9 M1 said a pushed autonomous row
    means "check the backend actually has PULL_MODE_PILOT_AGENTS in its env" —
    for a stranded row that sends them after a variable that is present and
    correct. The log has to say so in words, not merely fire.

    Driven with `a2a`: `schedule` became reachable in #2391, `loop` in #2523 and
    `fan_out` in #2524, so any of those here would assert the diagnostic over a
    case that no longer exists.
    """
    narrowed = pilot.PULL_REACHABLE_TRIGGERS - {_SYNTHETIC_STRANDED}
    monkeypatch.setattr(pilot, "PULL_REACHABLE_TRIGGERS", narrowed)
    with caplog.at_level("WARNING"):
        pilot.note_unreachable_pull_trigger("pilot-a", _SYNTHETIC_STRANDED)
    assert "flag is applied and correct" in caplog.text
    assert "topology" in caplog.text


def test_it_reports_once_per_agent_and_trigger(pilot, monkeypatch, caplog):
    """This sits on the dispatch path of every inbound A2A task. An un-deduped
    warning would emit thousands of identical lines a day and train operators to
    filter it — a signal meant to be noticed becoming noise."""
    narrowed = pilot.PULL_REACHABLE_TRIGGERS - {_SYNTHETIC_STRANDED}
    monkeypatch.setattr(pilot, "PULL_REACHABLE_TRIGGERS", narrowed)
    with caplog.at_level("WARNING"):
        first = pilot.note_unreachable_pull_trigger("pilot-a", _SYNTHETIC_STRANDED)
        repeats = [
            pilot.note_unreachable_pull_trigger("pilot-a", _SYNTHETIC_STRANDED)
            for _ in range(50)
        ]
    assert first is True
    assert not any(repeats)
    assert caplog.text.count("#2048") == 1


def test_dedup_is_per_trigger_not_per_agent(pilot, monkeypatch):
    """Two unclassified triggers are two distinct gaps to report."""
    narrowed = pilot.PULL_REACHABLE_TRIGGERS - {"agent", "event"}
    monkeypatch.setattr(pilot, "PULL_REACHABLE_TRIGGERS", narrowed)
    assert pilot.note_unreachable_pull_trigger("pilot-a", "agent") is True
    assert pilot.note_unreachable_pull_trigger("pilot-a", "event") is True


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
