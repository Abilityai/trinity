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
# `a2a` (ent#157) is stranded, and the classification is deliberate rather than
# a default. `PULL_REACHABLE_TRIGGERS` is an explicit allow-list, so an
# unlisted trigger lands here automatically — this comment is the review the
# test below demands. A2A's `message/send` consumes `result.response`
# synchronously to build the JSON-RPC artifact it returns to the remote caller;
# a pull-claimed row is dispatched by the agent later and produces no
# synchronous response, so pull dispatch structurally cannot serve this
# trigger. Same reason `fan_out` and `loop` are here.
# ent#329: `operator_response` joins the stranded set. It is dispatched by a
# direct backend call from the respond endpoint, not by `POST /task`, so
# `_derive_task_trigger` can never emit it and the pilot flag is inert for it —
# the same shape as `schedule` and `reminder`.
_STRANDED = ["schedule", "webhook", "loop", "fan_out", "reminder", "a2a",
             "operator_response"]
# The two that can.
_REACHABLE = ["agent", "event"]


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


def test_reachable_set_is_the_autonomous_triggers_post_task_can_actually_emit():
    """The claim behind `PULL_REACHABLE_TRIGGERS`, stated as an assertion.

    `POST /task` is the only `queue_persistent` producer, and its
    `_derive_task_trigger` can only ever produce these five values. Intersect
    with the autonomous set and exactly `agent` + `event` survive. Derived here
    rather than hardcoded so the constant cannot quietly disagree with the
    reasoning that justifies it.
    """
    from services.pull_pilot import PULL_REACHABLE_TRIGGERS
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    task_route_can_emit = {"self_task", "agent", "mcp", "manual", "event"}
    assert PULL_REACHABLE_TRIGGERS == (task_route_can_emit & _AUTONOMOUS_TRIGGERS)
    assert PULL_REACHABLE_TRIGGERS == {"agent", "event"}


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
    for a `schedule` row that sends them after a variable that is present and
    correct. The log has to say so in words, not merely fire."""
    with caplog.at_level("WARNING"):
        pilot.note_unreachable_pull_trigger("pilot-a", "schedule")
    assert "flag is applied and correct" in caplog.text
    assert "topology" in caplog.text


def test_it_reports_once_per_agent_and_trigger(pilot, caplog):
    """This sits on the cron dispatch path, which fires on every scheduled run.
    An un-deduped warning would emit thousands of identical lines a day and
    train operators to filter it — a signal meant to be noticed becoming noise."""
    with caplog.at_level("WARNING"):
        first = pilot.note_unreachable_pull_trigger("pilot-a", "schedule")
        repeats = [pilot.note_unreachable_pull_trigger("pilot-a", "schedule") for _ in range(50)]
    assert first is True
    assert not any(repeats)
    assert caplog.text.count("#2048") == 1


def test_dedup_is_per_trigger_not_per_agent(pilot):
    """A pilot firing cron AND webhooks has two distinct gaps to report."""
    assert pilot.note_unreachable_pull_trigger("pilot-a", "schedule") is True
    assert pilot.note_unreachable_pull_trigger("pilot-a", "webhook") is True


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

    If someone later implements the issue's Option 2 — giving
    `task_execution_service` a `queue_persistent` policy so cron can be claimed
    — this fails, which is the intent: `PULL_REACHABLE_TRIGGERS` and §9's prose
    both have to be revisited in that same change, and neither would otherwise
    announce itself. A silently-stale reach set is exactly the bug #2048 is.
    """
    assert _overflow_policies("services/task_execution_service.py") == {"reject"}, (
        "task_execution_service no longer dispatches with overflow_policy='reject' — "
        "cron may now be able to reach the durable queue. Re-derive "
        "PULL_REACHABLE_TRIGGERS and update PULL_MIGRATION_TESTING.md §9 (#2048)."
    )
    assert "queue_persistent" in _overflow_policies("services/chat_execution_service.py")
    assert "queue_in_memory" in _overflow_policies("services/dispatch_admission_service.py")


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
