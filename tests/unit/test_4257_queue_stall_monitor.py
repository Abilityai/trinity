"""The queue stall monitor: why a row waits, and when that is worth an alert (#4257).

trinity#4257 — an async `/task` on an idle pull pilot sat `queued` for hours.
No unit test can prove the live "starts within the poll interval" property (that
is a live-stack soak, before merge). What IS provable here, and is the mechanism
guard for the fix, is: the wait is named honestly, a head past the threshold
files exactly one alert, the same episode does not re-file, a head that moves
clears it, and one bad agent never skips the next.

Pure functions run with no engine and no Redis; `run_once` runs against stubbed
`db` / `platform_alerts` modules.
"""
from __future__ import annotations

import ast
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
sys.path.insert(0, str(Path(__file__).resolve().parent))  # `_ent695_lua`

pytestmark = pytest.mark.unit

from services import queue_stall_service as qss  # noqa: E402

_NOW = datetime(2026, 11, 5, 12, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _head(agent="alpha", minutes_ago=45, eid="e1", **extra):
    head = {
        "agent_name": agent,
        "execution_id": eid,
        "queued_at": _iso(_NOW - timedelta(minutes=minutes_ago)),
        "conversation_key": None,
        "triggered_by": "mcp",
        "redelivery_count": 0,
        "queued_count": 1,
    }
    head.update(extra)
    return head


# ---------------------------------------------------------------------------
# classify_wait — the #3244 vocabulary
# ---------------------------------------------------------------------------


def test_an_idle_pilot_waits_for_a_claim_not_because_it_is_busy():
    """The #4257 state itself: room to run, nothing running, still queued.

    #3244 reserves `agent_busy` for "the pool is full, nothing ran". Calling
    this busy — or `backlog_drain`, a backend timer that by design does not run
    on a pilot (#1766) — is what made the original stall unreadable.
    """
    assert qss.classify_wait(
        is_pilot=True, leased_running=0, max_parallel=3
    ) == qss.WAIT_WORKER_CLAIM


def test_a_full_pilot_pool_is_agent_busy():
    assert qss.classify_wait(
        is_pilot=True, leased_running=3, max_parallel=3
    ) == qss.WAIT_AGENT_BUSY


def test_a_pilot_over_its_cap_is_still_agent_busy():
    assert qss.classify_wait(
        is_pilot=True, leased_running=5, max_parallel=3
    ) == qss.WAIT_AGENT_BUSY


def test_the_2843_conversation_guard_outranks_the_pool_reading():
    """A head whose conversation is running cannot be claimed even with room,
    so reporting `worker_claim` would send an operator to the wrong half."""
    assert qss.classify_wait(
        is_pilot=True,
        leased_running=0,
        max_parallel=3,
        head_conversation_running=True,
    ) == qss.WAIT_CONVERSATION_BUSY


def test_a_push_agent_at_capacity_is_capacity_and_a_free_slot_is_a_drain():
    assert qss.classify_wait(is_pilot=False, slot_free=False) == qss.WAIT_CAPACITY
    assert qss.classify_wait(is_pilot=False, slot_free=True) == qss.WAIT_BACKLOG_DRAIN


def test_a_zero_max_parallel_cannot_read_as_idle():
    """A stray 0 must not make a running row look like room to run."""
    assert qss.classify_wait(
        is_pilot=True, leased_running=1, max_parallel=0
    ) == qss.WAIT_AGENT_BUSY


def test_every_reason_is_in_the_published_vocabulary():
    for kwargs in (
        {"is_pilot": True, "leased_running": 0, "max_parallel": 2},
        {"is_pilot": True, "leased_running": 2, "max_parallel": 2},
        {"is_pilot": True, "head_conversation_running": True},
        {"is_pilot": False, "slot_free": True},
        {"is_pilot": False, "slot_free": False},
    ):
        assert qss.classify_wait(**kwargs) in qss.WAIT_REASONS


# ---------------------------------------------------------------------------
# assess_stalls
# ---------------------------------------------------------------------------


def test_a_head_below_the_threshold_is_not_observed():
    out = qss.assess_stalls(
        [_head(minutes_ago=29)], now=_NOW, threshold_seconds=30 * 60
    )

    assert out == []


def test_a_head_past_the_threshold_is_observed_once_with_its_age():
    out = qss.assess_stalls(
        [_head(minutes_ago=45, queued_count=3)], now=_NOW, threshold_seconds=30 * 60
    )

    assert len(out) == 1
    assert out[0].agent_name == "alpha"
    assert out[0].age_seconds == 45 * 60
    assert out[0].queued_count == 3
    assert out[0].measured_at == _iso(_NOW)


def test_the_episode_marker_is_the_head_admission_so_a_re_read_is_the_same_episode():
    """#3254: a person's ending snoozes re-reads of THIS stall only. Two ticks
    over the same unmoved head must carry the same marker."""
    head = _head(minutes_ago=45)
    first = qss.assess_stalls([head], now=_NOW, threshold_seconds=1800)[0]
    later = qss.assess_stalls(
        [head], now=_NOW + timedelta(minutes=10), threshold_seconds=1800
    )[0]

    assert first.episode_started_at == later.episode_started_at
    assert later.age_seconds > first.age_seconds


def test_a_later_stall_after_a_recovery_is_a_different_episode():
    first = qss.assess_stalls([_head(minutes_ago=45, eid="e1")], now=_NOW,
                              threshold_seconds=1800)[0]
    second = qss.assess_stalls([_head(minutes_ago=31, eid="e2")], now=_NOW,
                               threshold_seconds=1800)[0]

    assert first.episode_started_at != second.episode_started_at


def test_two_agents_yield_two_observations():
    out = qss.assess_stalls(
        [_head(agent="alpha"), _head(agent="beta")],
        now=_NOW, threshold_seconds=1800,
    )

    assert {o.agent_name for o in out} == {"alpha", "beta"}


def test_an_unreadable_admission_is_skipped_not_guessed():
    out = qss.assess_stalls(
        [_head(queued_at="not-a-timestamp"), _head(agent="beta")],
        now=_NOW, threshold_seconds=1800,
    )

    assert [o.agent_name for o in out] == ["beta"]


def test_a_naive_stored_timestamp_is_read_as_utc():
    """Historical rows carry a naive string; reading one as local time would
    invent or erase hours of wait (Invariant #16)."""
    naive = (_NOW - timedelta(minutes=45)).replace(tzinfo=None).isoformat()

    out = qss.assess_stalls([_head(queued_at=naive)], now=_NOW, threshold_seconds=1800)

    assert out[0].age_seconds == 45 * 60


def test_the_context_carries_both_halves_of_the_diagnosis():
    out = qss.assess_stalls(
        [_head(minutes_ago=45, last_worker_poll_age_seconds=7,
               claim_denied_count=12, leased_running=0, max_parallel=3,
               wait_reason=qss.WAIT_WORKER_CLAIM)],
        now=_NOW, threshold_seconds=1800,
    )
    ctx = out[0].context()

    # "the pool is alive" AND "the backend is turning it away" — the pair is
    # what separates a dead pool from a refused one.
    assert ctx["last_worker_poll_age_seconds"] == 7
    assert ctx["claim_denied_count"] == 12
    assert ctx["wait_reason"] == qss.WAIT_WORKER_CLAIM
    assert ctx["oldest_execution_id"] == "e1"
    assert ctx["episode_started_at"] == out[0].queued_at


def test_the_operator_text_names_the_agent_the_reason_and_the_next_step():
    out = qss.assess_stalls([_head(minutes_ago=45)], now=_NOW, threshold_seconds=1800)
    text = qss.describe(out[0])

    assert "alpha" in text["title"] and "45" in text["title"]
    assert "e1" in text["question"]
    assert "PULL_MODE_PILOT_AGENTS" in text["question"]


# ---------------------------------------------------------------------------
# run_once wiring
# ---------------------------------------------------------------------------


_FACADE_NAMES: list = []


def _facade_names():
    if not _FACADE_NAMES:
        import database

        _FACADE_NAMES.extend(n for n in dir(database.DatabaseManager) if not n.startswith("_"))
    return _FACADE_NAMES


class _Stubs:
    """The facade `run_once` reads, stubbed.

    `db` only answers to names the real `DatabaseManager` has (`spec_set`), so
    a call the facade lacks fails here as it would in production — a bare
    MagicMock answers anything, and its `__int__` is 1. Signatures are pinned
    against the real facade in `test_4635_queue_stall_reads.py`."""

    def __init__(self, heads, *, leased=0, running_conversations=(), pending=(),
                 claimable=()):
        self.db = MagicMock(spec_set=_facade_names())
        self.claimable = list(claimable)

        def head_read(agent_names=None, *, claimable=False):
            if claimable:
                wanted = set(agent_names or ())
                return [h for h in self.claimable if h["agent_name"] in wanted]
            return list(heads)

        self.db.oldest_queued_head_per_agent.side_effect = head_read
        self.db.count_active_leased_by_agent.side_effect = (
            lambda names, unexpired_at=None: {n: leased for n in names if leased}
        )
        self.db.running_conversation_keys_by_agent.side_effect = (
            lambda names: {n: list(running_conversations) for n in names
                           if running_conversations}
        )
        self.db.list_pending_operator_queue_by_subject_prefix.return_value = list(pending)
        self.alerts = MagicMock()
        self.alerts.observe.return_value = "filed"
        self.alerts.clear.return_value = 1


def _meter(active_slots):
    """A slot service whose meter reads `active_slots` for every push agent.
    `spec_set`: reading the meter any other way raises, and so reads as an
    unreadable meter — never as a coincidental 1."""
    service = MagicMock(spec_set=["active_slot_counts", "redis"])
    service.active_slot_counts.side_effect = lambda names: {n: active_slots for n in names}
    service.redis = MagicMock(spec_set=["mget"])
    service.redis.mget.side_effect = lambda keys: [None] * len(keys)
    return service


def _import_real_modules():
    """Import every module the tick patches BEFORE `database` is stubbed. A
    module first imported inside `patch.dict(sys.modules, {"database": ...})`
    binds its own module-level `db` to the stub for the rest of the session."""
    import redis_breaker_util  # noqa: F401
    from services import (  # noqa: F401
        platform_alerts, pull_pilot, redelivery_governor, settings_service, slot_service,
    )
    from utils import helpers  # noqa: F401


def _run(stubs, *, pilot=True, threshold=30, max_parallel=3, now=_NOW,
         active_slots=None, service=None, caps=None, breaker_redis=None):
    """Drive one tick. `active_slots=None` leaves the slot meter unreadable
    (no Redis in the unit island); an int stands the meter up at that count.
    `pilot` is a bool or a per-agent callable. `caps` replaces the batched
    cap read. `breaker_redis=None` is "Redis unreachable" for the lease, which
    fails open — the lease tests pass a client."""
    _import_real_modules()
    database = MagicMock()
    database.db = stubs.db
    if service is not None:
        slots = {"return_value": service}
    elif active_slots is None:
        slots = {"side_effect": RuntimeError("no redis in the unit island")}
    else:
        slots = {"return_value": _meter(active_slots)}
    pilots = {"side_effect": pilot} if callable(pilot) else {"return_value": pilot}
    cap_read = caps or {"side_effect": lambda names: {n: max_parallel for n in names}}
    with patch.dict(sys.modules, {"database": database}), \
            patch.object(qss, "threshold_minutes", return_value=threshold), \
            patch("services.platform_alerts.observe", stubs.alerts.observe), \
            patch("services.platform_alerts.clear", stubs.alerts.clear), \
            patch("services.pull_pilot.is_pull_pilot_agent", **pilots), \
            patch("services.settings_service.get_effective_max_parallel_tasks_for",
                  **cap_read), \
            patch("services.slot_service.get_slot_service", **slots), \
            patch("redis_breaker_util.get_breaker_redis", return_value=breaker_redis):
        return qss.run_once(now=now)


def _observed(stubs):
    """`{agent: observe kwargs}` for every alert the tick filed."""
    return {c.args[0]: c.kwargs for c in stubs.alerts.observe.call_args_list}


def _cleared(stubs):
    return [c.args[0] for c in stubs.alerts.clear.call_args_list]


# --- the push-agent meter is read, not assumed (#4257 review I1) ------------


def test_a_push_agent_at_capacity_is_capacity_not_a_missing_drain():
    """A push agent with a full meter and a long turn is NOT a stalled drain.

    `max_parallel` slots all held is the ordinary shape of a long execution
    (timeouts run to hours). Calling that `backlog_drain` tells the operator
    the backend owes a dispatch it does not owe.
    """
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs, pilot=False, max_parallel=2, active_slots=2)

    ctx = stubs.alerts.observe.call_args[1]["context"]
    assert ctx["wait_reason"] == qss.WAIT_CAPACITY


def test_a_push_agent_with_a_free_slot_is_still_a_drain_that_has_not_landed():
    """The real stall on the push side: room to run and a row still queued."""
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs, pilot=False, max_parallel=3, active_slots=2)

    ctx = stubs.alerts.observe.call_args[1]["context"]
    assert ctx["wait_reason"] == qss.WAIT_BACKLOG_DRAIN


def test_an_unreadable_meter_keeps_the_reading_it_had_before():
    """No meter is not evidence of capacity. With Redis down the dispatch path
    is down too, so the drain reading stands rather than inventing a full
    pool — and the tick still files on age alone."""
    stubs = _Stubs([_head(minutes_ago=45)])

    summary = _run(stubs, pilot=False, max_parallel=2, active_slots=None)

    assert summary["observed"] == 1
    ctx = stubs.alerts.observe.call_args[1]["context"]
    assert ctx["wait_reason"] == qss.WAIT_BACKLOG_DRAIN


def test_the_pilot_path_never_asks_the_slot_meter():
    """A pilot's queue is claimed by its own workers; the backend slot meter is
    disjoint from those rows by construction (#1766), so reading it would be a
    wrong answer, not a slow one."""
    stubs = _Stubs([_head(minutes_ago=45)])
    service = _meter(99)

    _run(stubs, pilot=True, service=service)

    service.active_slot_counts.assert_not_called()
    assert (stubs.alerts.observe.call_args[1]["context"]["wait_reason"]
            == qss.WAIT_WORKER_CLAIM)


def test_run_once_files_one_alert_for_a_stalled_head():
    stubs = _Stubs([_head(minutes_ago=45)])

    summary = _run(stubs)

    assert summary["observed"] == 1
    assert stubs.alerts.observe.call_count == 1
    args, kwargs = stubs.alerts.observe.call_args
    assert args[0] == "alpha"
    assert args[1] == qss.ALERT_KIND
    assert args[2] == "alpha"
    assert kwargs["context"]["wait_reason"] == qss.WAIT_WORKER_CLAIM
    stubs.alerts.clear.assert_not_called()


def test_run_once_clears_when_the_head_is_young_again():
    stubs = _Stubs([_head(minutes_ago=2)], pending=[_pending("alpha")])

    summary = _run(stubs)

    assert summary["observed"] == 0
    stubs.alerts.observe.assert_not_called()
    stubs.alerts.clear.assert_called_once()
    assert stubs.alerts.clear.call_args[0][:3] == ("alpha", qss.ALERT_KIND, "alpha")


def test_every_clear_reason_is_one_the_platform_accepts():
    """#4578: `platform_alerts.clear` swallows the ValueError `clear_platform`
    raises for a reason outside `PLATFORM_ENDING_REASONS`, so a free-text
    reason left the stall alert pending after the queue moved on."""
    from services import ask_service

    tree = ast.parse((_BACKEND / "services" / "queue_stall_service.py").read_text())
    clears = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform_alerts"
    ]
    assert clears, "expected the stall service to clear its alert"
    for call in clears:
        for kw in call.keywords:
            if kw.arg == "reason":
                assert isinstance(kw.value, ast.Constant)
                assert kw.value.value in ask_service.PLATFORM_ENDING_REASONS


def test_run_once_reports_a_full_pilot_pool_as_busy_not_as_a_missing_claim():
    stubs = _Stubs([_head(minutes_ago=45)], leased=3)

    _run(stubs, max_parallel=3)

    ctx = stubs.alerts.observe.call_args[1]["context"]
    assert ctx["wait_reason"] == qss.WAIT_AGENT_BUSY
    assert ctx["leased_running"] == 3


def test_run_once_reads_the_2843_guard_for_the_heads_conversation():
    stubs = _Stubs(
        [_head(minutes_ago=45, conversation_key="conv-1")],
        running_conversations=("conv-1",),
    )

    _run(stubs)

    assert (stubs.alerts.observe.call_args[1]["context"]["wait_reason"]
            == qss.WAIT_CONVERSATION_BUSY)


def test_run_once_degrades_to_no_liveness_age_when_redis_is_unreachable():
    """The alert must still file on age alone; Redis is a context field."""
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs)

    ctx = stubs.alerts.observe.call_args[1]["context"]
    assert ctx["last_worker_poll_age_seconds"] is None
    assert ctx["claim_denied_count"] == 0


def test_one_failing_agent_does_not_skip_the_next():
    stubs = _Stubs([_head(agent="bad", minutes_ago=45),
                    _head(agent="good", minutes_ago=45)])

    def _pilot(agent):
        if agent == "bad":
            raise RuntimeError("could not read this agent's path")
        return True

    summary = _run(stubs, pilot=_pilot)

    assert summary["errors"] == 1
    assert [c[0][0] for c in stubs.alerts.observe.call_args_list] == ["good"]


def test_run_once_never_raises_when_the_head_read_fails():
    stubs = _Stubs([])
    stubs.db.oldest_queued_head_per_agent.side_effect = RuntimeError("db down")

    summary = _run(stubs)

    assert summary["errors"] == 1
    stubs.alerts.observe.assert_not_called()


def test_run_once_never_raises_when_the_assessment_itself_fails():
    """An import or a bug inside the assessment costs every head its reading
    this tick; drained agents are still reconciled and the drain still runs."""
    stubs = _Stubs([_head(agent="a", minutes_ago=45), _head(agent="b", minutes_ago=45)],
                   pending=[_pending("a"), _pending("drained")])

    with patch.object(qss, "_enrich_all", side_effect=ImportError("broken import")):
        summary = _run(stubs)

    assert summary["errors"] == 2
    stubs.alerts.observe.assert_not_called()
    assert _cleared(stubs) == ["drained"]


def test_run_once_logs_the_blocked_head_once_per_tick(caplog):
    stubs = _Stubs([_head(minutes_ago=45)])

    with caplog.at_level(logging.WARNING, logger=qss.__name__):
        _run(stubs)

    blocked = [r for r in caplog.records if "queue head blocked" in r.getMessage()]
    assert len(blocked) == 1
    assert "reason=worker_claim" in blocked[0].getMessage()
    assert "priority=high" in blocked[0].getMessage()


def test_run_once_with_an_empty_fleet_does_nothing():
    stubs = _Stubs([])

    summary = _run(stubs)

    assert summary == {"heads": 0, "observed": 0, "cleared": 0, "errors": 0, "ran": 1}
    stubs.alerts.observe.assert_not_called()


# ---------------------------------------------------------------------------
# #4636: the priority follows the wait
# ---------------------------------------------------------------------------

_APPROVED_BUSY = (
    "Every slot on this agent is running work, so this wait is expected; "
    "the alert ends when a slot frees and the head starts."
)
_CONVERSATION_BUSY = (
    "This conversation's previous turn is still running, so this wait is "
    "expected; the alert ends when that turn finishes and the head starts."
)

_SETUPS = {
    qss.WAIT_CAPACITY: (dict(heads=[_head(minutes_ago=45)]),
                        dict(pilot=False, max_parallel=2, active_slots=2)),
    qss.WAIT_BACKLOG_DRAIN: (dict(heads=[_head(minutes_ago=45)]),
                             dict(pilot=False, max_parallel=3, active_slots=2)),
    qss.WAIT_AGENT_BUSY: (dict(heads=[_head(minutes_ago=45)], leased=2),
                          dict(pilot=True, max_parallel=2)),
    qss.WAIT_WORKER_CLAIM: (dict(heads=[_head(minutes_ago=45)]),
                            dict(pilot=True, max_parallel=3)),
    qss.WAIT_CONVERSATION_BUSY: (
        dict(heads=[_head(minutes_ago=45, conversation_key="conv-1")],
             running_conversations=("conv-1",)),
        dict(pilot=True, max_parallel=3)),
}


def _tick_for(reason, **stub_extra):
    stub_kwargs, run_kwargs = _SETUPS[reason]
    stub_kwargs = dict(stub_kwargs)
    heads = stub_kwargs.pop("heads")
    stubs = _Stubs(heads, **{**stub_kwargs, **stub_extra})
    _run(stubs, **run_kwargs)
    return stubs


@pytest.mark.parametrize("reason, priority", [
    (qss.WAIT_CAPACITY, "medium"),
    (qss.WAIT_AGENT_BUSY, "medium"),
    (qss.WAIT_CONVERSATION_BUSY, "medium"),
    (qss.WAIT_WORKER_CLAIM, "high"),
    (qss.WAIT_BACKLOG_DRAIN, "high"),
])
def test_the_alert_files_at_the_priority_its_wait_deserves(reason, priority):
    """A push agent whose slots are all busy with real work is not the
    emergency a queue nobody consumes is (#4636)."""
    stubs = _tick_for(reason)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["wait_reason"] == reason
    assert kwargs["priority"] == priority


def test_the_priority_table_is_the_ruling_and_covers_every_reason():
    assert qss.WAIT_PRIORITY == {
        qss.WAIT_CAPACITY: "medium",
        qss.WAIT_AGENT_BUSY: "medium",
        qss.WAIT_CONVERSATION_BUSY: "medium",
        qss.WAIT_WORKER_CLAIM: "high",
        qss.WAIT_BACKLOG_DRAIN: "high",
    }
    assert set(qss.WAIT_PRIORITY) == set(qss.WAIT_REASONS)
    assert qss.BUSY_WAITS <= set(qss.WAIT_REASONS)


@pytest.mark.parametrize("reason, sentence", [
    (qss.WAIT_CAPACITY, _APPROVED_BUSY),
    (qss.WAIT_AGENT_BUSY, _APPROVED_BUSY),
    (qss.WAIT_CONVERSATION_BUSY, _CONVERSATION_BUSY),
])
def test_a_busy_wait_says_it_is_expected_and_what_ends_it(reason, sentence):
    stubs = _tick_for(reason)

    question = stubs.alerts.observe.call_args.kwargs["question"]
    assert sentence in question
    others = {_APPROVED_BUSY, _CONVERSATION_BUSY} - {sentence}
    assert not any(s in question for s in others)
    # The rest of the text stays.
    assert "PULL_MODE_PILOT_AGENTS" in question


@pytest.mark.parametrize("reason", [qss.WAIT_WORKER_CLAIM, qss.WAIT_BACKLOG_DRAIN])
def test_a_real_stall_never_says_the_wait_is_expected(reason):
    stubs = _tick_for(reason)

    question = stubs.alerts.observe.call_args.kwargs["question"]
    assert "expected" not in question


# --- the pool line names the pool the agent actually has (#4636 eyeball) -----


def test_a_push_agents_pool_line_reads_its_slot_meter():
    """A push card read "Pool: 0/3 leased" — a pull-pilot lease count, always
    0 on a push agent — right above "Every slot on this agent is running
    work". A push agent's pool is its slot meter."""
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs, pilot=False, max_parallel=3, active_slots=3)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert "Pool: 3/3 slots busy, " in kwargs["question"]
    assert "leased" not in kwargs["question"]
    assert kwargs["context"]["active_slots"] == 3


def test_an_unreadable_meter_says_so_instead_of_printing_zero():
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs, pilot=False, max_parallel=3, active_slots=None)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert "Pool: ?/3 slots busy (the slot meter could not be read), " in kwargs["question"]
    assert "0/3" not in kwargs["question"]
    assert kwargs["context"]["active_slots"] is None


def test_a_push_agent_with_an_unknown_cap_still_shows_its_meter():
    stubs = _Stubs([_head(minutes_ago=45)])

    _run(stubs, pilot=False, active_slots=2,
         caps={"side_effect": RuntimeError("roster unreadable")})

    assert "Pool: 2/? slots busy, " in stubs.alerts.observe.call_args.kwargs["question"]


def test_a_pilots_pool_line_still_counts_its_leases():
    stubs = _Stubs([_head(minutes_ago=45)], leased=2)

    _run(stubs, pilot=True, max_parallel=3)

    question = stubs.alerts.observe.call_args.kwargs["question"]
    assert "Pool: 2/3 leased, " in question
    assert "slots busy" not in question


# --- decision 2: a reading that worsens rewrites the row; one that does not counts


def _pending(agent="alpha", priority="medium", marker=None):
    return {"agent_name": agent, "priority": priority,
            "subject": f"{qss.ALERT_KIND}:{agent}",
            "context": {"episode_started_at": marker or _iso(_NOW - timedelta(minutes=45))}}


def test_a_higher_reading_than_the_pending_row_rewrites_it():
    """The row filed medium while every slot was busy; the same head is now
    waiting with room to run. Kept as first filed, the real stall would read
    as the expected wait."""
    stubs = _tick_for(qss.WAIT_WORKER_CLAIM, pending=[_pending(priority="medium")])

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["priority"] == "high"
    assert kwargs["keep_text"] is False


@pytest.mark.parametrize("reason, row_priority", [
    (qss.WAIT_CAPACITY, "high"),        # improved: the row keeps high
    (qss.WAIT_CAPACITY, "medium"),      # same: only counted
    (qss.WAIT_WORKER_CLAIM, "high"),    # same: only counted
    (qss.WAIT_WORKER_CLAIM, "critical"),
])
def test_a_same_or_lower_reading_only_counts(reason, row_priority):
    stubs = _tick_for(reason, pending=[_pending(priority=row_priority)])

    assert stubs.alerts.observe.call_args.kwargs["keep_text"] is True


def test_a_first_reading_files_with_its_own_text():
    stubs = _tick_for(qss.WAIT_CAPACITY)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["keep_text"] is True
    assert kwargs["priority"] == "medium"


def test_a_rewrite_of_the_same_head_keeps_its_episode_marker():
    head = _head(minutes_ago=45)
    stubs = _tick_for(qss.WAIT_WORKER_CLAIM,
                      pending=[_pending(priority="medium", marker=head["queued_at"])])

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["keep_text"] is False
    assert kwargs["context"]["episode_started_at"] == head["queued_at"]


def test_an_unreadable_pending_read_rewrites_nothing_and_clears_nothing(caplog):
    stubs = _Stubs([_head(minutes_ago=45)])
    stubs.db.list_pending_operator_queue_by_subject_prefix.side_effect = RuntimeError("down")

    with caplog.at_level(logging.WARNING, logger=qss.__name__):
        summary = _run(stubs)

    assert stubs.alerts.observe.call_args.kwargs["keep_text"] is True
    stubs.alerts.clear.assert_not_called()
    assert summary["observed"] == 1
    assert any("pending" in r.getMessage() for r in caplog.records)


def test_a_person_who_ended_a_busy_row_hears_again_when_it_turns_into_a_real_stall():
    """`_snoozed_by` lets a higher priority through a person's ending; the
    same episode at the same or a lower priority stays quiet."""
    from services import platform_alerts

    kind = platform_alerts.KINDS[qss.ALERT_KIND]
    marker = {"episode_started_at": "2026-11-05T11:15:00Z"}
    ended = {"priority": "medium", "context": dict(marker)}

    assert platform_alerts._snoozed_by(kind, ended, "high", dict(marker)) is False
    assert platform_alerts._snoozed_by(kind, ended, "medium", dict(marker)) is True


# --- G1: the reconcile reads the stall rows themselves ------------------------


def test_a_drained_agents_alert_is_ended():
    stubs = _Stubs([_head(agent="busy", minutes_ago=45)],
                   pending=[_pending("busy"), _pending("drained")])

    summary = _run(stubs)

    assert _cleared(stubs) == ["drained"]
    assert summary["cleared"] == 1


def test_an_agent_that_could_not_be_assessed_keeps_its_alert():
    """It still has queued work; a failed read is not a drained queue. Ending
    the row here filed a fresh one on the next good tick."""
    stubs = _Stubs([_head(agent="bad", minutes_ago=45),
                    _head(agent="good", minutes_ago=2)],
                   pending=[_pending("bad"), _pending("good")])

    def _pilot(agent):
        if agent == "bad":
            raise RuntimeError("could not read this agent's path")
        return True

    _run(stubs, pilot=_pilot)

    assert "bad" not in _cleared(stubs)
    assert _cleared(stubs) == ["good"]


def test_a_young_head_with_no_pending_alert_costs_no_lookup():
    """`clear` looks its row up per agent; the pending snapshot already says
    which agents have one, so a quiet fleet costs no read per agent."""
    stubs = _Stubs([_head(agent="a", minutes_ago=2), _head(agent="b", minutes_ago=3)],
                   pending=[_pending("b")])

    _run(stubs)

    assert _cleared(stubs) == ["b"]


def test_with_the_pending_rows_unreadable_every_young_head_is_still_cleared():
    stubs = _Stubs([_head(agent="a", minutes_ago=2), _head(agent="b", minutes_ago=3)])
    stubs.db.list_pending_operator_queue_by_subject_prefix.side_effect = RuntimeError("down")

    _run(stubs)

    assert sorted(_cleared(stubs)) == ["a", "b"]


def test_the_liveness_read_carries_each_agents_own_values():
    """One MGET for every agent's two markers; each agent must get its own
    poll age and refusal count back, not a neighbour's."""
    from services.pull_pilot import PULL_CLAIM_DENIED_KEY_PREFIX, PULL_POLL_KEY_PREFIX

    stubs = _Stubs([_head(agent="a", minutes_ago=45), _head(agent="b", minutes_ago=45)])
    service = _meter(2)
    service.redis.mget.side_effect = lambda keys: [
        str(_NOW.timestamp() - 7), str(_NOW.timestamp() - 300), "12", None]

    _run(stubs, service=service)

    observed = _observed(stubs)
    assert (observed["a"]["context"]["last_worker_poll_age_seconds"],
            observed["a"]["context"]["claim_denied_count"]) == (7, 12)
    assert (observed["b"]["context"]["last_worker_poll_age_seconds"],
            observed["b"]["context"]["claim_denied_count"]) == (300, 0)
    assert service.redis.mget.call_args.args[0] == [
        f"{PULL_POLL_KEY_PREFIX}a", f"{PULL_POLL_KEY_PREFIX}b",
        f"{PULL_CLAIM_DENIED_KEY_PREFIX}a", f"{PULL_CLAIM_DENIED_KEY_PREFIX}b"]


def test_the_stall_rows_are_read_once_per_tick():
    stubs = _Stubs([_head(agent="a", minutes_ago=45), _head(agent="b", minutes_ago=45)])

    _run(stubs)

    stubs.db.list_pending_operator_queue_by_subject_prefix.assert_called_once_with(
        f"{qss.ALERT_KIND}:")
    stubs.db.list_operator_queue_items.assert_not_called()


# --- G3: one read per kind of fact, not one per agent -------------------------


def test_every_per_agent_fact_is_read_once_for_the_whole_tick():
    heads = [
        _head(agent="p1", minutes_ago=45, conversation_key="conv-1"),
        _head(agent="p2", minutes_ago=45),
        _head(agent="push", minutes_ago=45),
    ]
    stubs = _Stubs(heads)
    service = _meter(2)
    caps = MagicMock(side_effect=lambda names: {n: 3 for n in names})

    _run(stubs, pilot=lambda a: a != "push", service=service,
         caps={"new": caps})

    caps.assert_called_once_with(["p1", "p2", "push"])
    leased = stubs.db.count_active_leased_by_agent
    leased.assert_called_once()
    assert leased.call_args.args[0] == ["p1", "p2"]
    assert leased.call_args.kwargs["unexpired_at"] == "2026-11-05T12:00:00.000000Z"
    stubs.db.running_conversation_keys_by_agent.assert_called_once_with(["p1"])
    service.active_slot_counts.assert_called_once_with(["push"])
    service.redis.mget.assert_called_once()
    assert len(service.redis.mget.call_args.args[0]) == 6  # poll + denied per agent
    assert {c.args[0] for c in stubs.alerts.observe.call_args_list} == {"p1", "p2", "push"}


def test_an_expired_lease_never_makes_a_dead_pool_read_full():
    """The read is asked for live leases only; a dropped `unexpired_at` would
    count the dead worker's row and file the stall as an expected wait."""
    stubs = _Stubs([_head(minutes_ago=45)])
    stubs.db.count_active_leased_by_agent.side_effect = (
        lambda names, unexpired_at=None: {} if unexpired_at else {n: 3 for n in names})

    _run(stubs, max_parallel=3)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["wait_reason"] == qss.WAIT_WORKER_CLAIM
    assert kwargs["priority"] == "high"


def test_a_failed_pilot_read_skips_the_pilots_and_files_the_push_agents():
    stubs = _Stubs([_head(agent="p1", minutes_ago=45),
                    _head(agent="push", minutes_ago=45)],
                   pending=[_pending("p1")])
    stubs.db.count_active_leased_by_agent.side_effect = RuntimeError("db blip")

    summary = _run(stubs, pilot=lambda a: a != "push", active_slots=2, max_parallel=3)

    assert list(_observed(stubs)) == ["push"]
    assert "p1" not in _cleared(stubs)
    assert summary["errors"] == 1


def test_an_unknown_cap_never_reads_as_busy():
    """A failed cap read used to fall back to 1, and one held slot then read
    as a full pool — now a medium, 'expected' alert. Unknown is not full."""
    stubs = _Stubs([_head(agent="pilot", minutes_ago=45),
                    _head(agent="push", minutes_ago=45)], leased=2)

    _run(stubs, pilot=lambda a: a == "pilot", active_slots=2,
         caps={"side_effect": RuntimeError("roster unreadable")})

    observed = _observed(stubs)
    assert observed["pilot"]["context"]["wait_reason"] == qss.WAIT_WORKER_CLAIM
    assert observed["push"]["context"]["wait_reason"] == qss.WAIT_BACKLOG_DRAIN
    assert {kw["priority"] for kw in observed.values()} == {"high"}
    assert observed["pilot"]["context"]["max_parallel"] is None
    assert "Pool: 2/? leased" in observed["pilot"]["question"]


# --- D1: a held FIFO head does not hide a stuck claimable row -----------------


def _claimable_head(minutes_ago, eid="e-free", key=None):
    return {**_head(minutes_ago=minutes_ago, eid=eid, conversation_key=key),
            "queued_count": 1}


def test_a_stuck_claimable_row_behind_a_held_head_files_as_the_real_stall():
    """The pull claim skips a head whose conversation is running and takes the
    next row. If that row is past the threshold too, the pool is not taking
    it — the #4257 incident, which must not read as an expected wait."""
    held = _head(minutes_ago=45, eid="e-held", conversation_key="conv-1", queued_count=3)
    stubs = _Stubs([held], running_conversations=("conv-1",),
                   claimable=[_claimable_head(40, eid="e-free")])

    _run(stubs, max_parallel=3)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["oldest_execution_id"] == "e-free"
    assert kwargs["context"]["wait_reason"] == qss.WAIT_WORKER_CLAIM
    assert kwargs["priority"] == "high"
    assert kwargs["context"]["queued_count"] == 3
    # The episode is still the held head's: which row a tick names flips as
    # the row behind crosses the threshold, and the marker must not flip with it.
    assert kwargs["context"]["episode_started_at"] == held["queued_at"]
    assert kwargs["context"]["queued_at"] == _iso(_NOW - timedelta(minutes=40))
    stubs.db.oldest_queued_head_per_agent.assert_any_call(["alpha"], claimable=True)


def test_a_young_claimable_row_leaves_the_held_head_as_the_wait():
    held = _head(minutes_ago=45, eid="e-held", conversation_key="conv-1", queued_count=2)
    stubs = _Stubs([held], running_conversations=("conv-1",),
                   claimable=[_claimable_head(10, eid="e-free")])

    _run(stubs, max_parallel=3)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["oldest_execution_id"] == "e-held"
    assert kwargs["context"]["wait_reason"] == qss.WAIT_CONVERSATION_BUSY
    assert kwargs["priority"] == "medium"


def test_a_stuck_claimable_row_behind_a_full_pool_is_a_busy_wait():
    held = _head(minutes_ago=45, eid="e-held", conversation_key="conv-1", queued_count=2)
    stubs = _Stubs([held], leased=3, running_conversations=("conv-1",),
                   claimable=[_claimable_head(40, eid="e-free")])

    _run(stubs, max_parallel=3)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["oldest_execution_id"] == "e-free"
    assert kwargs["context"]["wait_reason"] == qss.WAIT_AGENT_BUSY
    assert kwargs["priority"] == "medium"


def test_the_claimable_read_is_made_only_for_held_heads_with_rows_behind():
    heads = [
        _head(agent="held", minutes_ago=45, conversation_key="conv-1", queued_count=2),
        _head(agent="alone", minutes_ago=45, conversation_key="conv-1", queued_count=1),
        _head(agent="free", minutes_ago=45, conversation_key="conv-9", queued_count=4),
    ]
    stubs = _Stubs(heads)
    stubs.db.running_conversation_keys_by_agent.side_effect = (
        lambda names: {n: ["conv-1"] for n in names})

    _run(stubs)

    claimable_calls = [c for c in stubs.db.oldest_queued_head_per_agent.call_args_list
                       if c.kwargs.get("claimable")]
    assert len(claimable_calls) == 1
    assert claimable_calls[0].args[0] == ["held"]


def test_a_failed_claimable_read_still_files_the_held_head():
    held = _head(minutes_ago=45, eid="e-held", conversation_key="conv-1", queued_count=2)
    stubs = _Stubs([held], running_conversations=("conv-1",))

    def head_read(agent_names=None, *, claimable=False):
        if claimable:
            raise RuntimeError("db blip")
        return [held]

    stubs.db.oldest_queued_head_per_agent.side_effect = head_read

    summary = _run(stubs)

    kwargs = stubs.alerts.observe.call_args.kwargs
    assert kwargs["context"]["wait_reason"] == qss.WAIT_CONVERSATION_BUSY
    assert summary["errors"] == 1


# ---------------------------------------------------------------------------
# G4: one worker runs the monitor; Redis down, every worker does
# ---------------------------------------------------------------------------


@pytest.fixture
def lease_redis():
    from _ent695_lua import LeaseRedis

    return LeaseRedis()


def test_a_worker_that_does_not_hold_the_lease_reads_nothing(lease_redis):
    lease_redis.set(qss.LEASE_KEY, "4242:other", nx=True, ex=qss.LEASE_TTL_SECONDS)
    stubs = _Stubs([_head(minutes_ago=45)], pending=[_pending("gone")])

    summary = _run(stubs, breaker_redis=lease_redis)

    assert summary["ran"] == 0
    stubs.db.oldest_queued_head_per_agent.assert_not_called()
    stubs.db.list_pending_operator_queue_by_subject_prefix.assert_not_called()
    stubs.alerts.observe.assert_not_called()
    stubs.alerts.clear.assert_not_called()


def test_a_free_lease_is_taken_and_the_holder_runs(lease_redis):
    stubs = _Stubs([_head(minutes_ago=45)])

    summary = _run(stubs, breaker_redis=lease_redis)

    assert summary["ran"] == 1
    assert summary["observed"] == 1
    assert lease_redis.store[qss.LEASE_KEY] == qss._lease_holder()
    assert lease_redis.ttls[qss.LEASE_KEY] == qss.LEASE_TTL_SECONDS


def test_two_workers_one_lease_exactly_one_runs(lease_redis):
    first = _Stubs([_head(minutes_ago=45)])
    second = _Stubs([_head(minutes_ago=45)])

    with patch.object(qss, "_lease_holder", return_value="1:aaaa"):
        a = _run(first, breaker_redis=lease_redis)
    with patch.object(qss, "_lease_holder", return_value="2:bbbb"):
        b = _run(second, breaker_redis=lease_redis)
    with patch.object(qss, "_lease_holder", return_value="1:aaaa"):
        again = _run(first, breaker_redis=lease_redis)

    assert (a["ran"], b["ran"], again["ran"]) == (1, 0, 1)
    assert second.alerts.observe.call_count == 0


def test_the_lease_is_renewed_only_after_the_queue_was_read(lease_redis):
    """A holder whose reads fail must let the lease lapse, or the worker that
    could read stands down for good."""
    lease_redis.set(qss.LEASE_KEY, qss._lease_holder(), nx=True, ex=5)
    failing = _Stubs([])
    failing.db.oldest_queued_head_per_agent.side_effect = RuntimeError("db down here")

    _run(failing, breaker_redis=lease_redis)
    assert lease_redis.ttls[qss.LEASE_KEY] == 5, "renewed by a tick that read nothing"

    _run(_Stubs([]), breaker_redis=lease_redis)
    assert lease_redis.ttls[qss.LEASE_KEY] == qss.LEASE_TTL_SECONDS


def test_with_redis_unreachable_every_worker_runs_the_monitor():
    stubs = _Stubs([_head(minutes_ago=45)])

    summary = _run(stubs, breaker_redis=None)

    assert summary["ran"] == 1
    assert summary["observed"] == 1


def test_a_redis_error_at_the_lease_fails_open(lease_redis):
    lease_redis.fail = True
    stubs = _Stubs([_head(minutes_ago=45)])

    summary = _run(stubs, breaker_redis=lease_redis)

    assert summary["ran"] == 1
    assert summary["observed"] == 1


def test_release_deletes_our_own_lease_and_never_a_successors(lease_redis):
    lease_redis.set(qss.LEASE_KEY, "9:successor", nx=True, ex=qss.LEASE_TTL_SECONDS)
    with patch("redis_breaker_util.get_breaker_redis", return_value=lease_redis):
        qss.release_lease()
        assert lease_redis.store[qss.LEASE_KEY] == "9:successor"

        lease_redis.delete(qss.LEASE_KEY)
        lease_redis.set(qss.LEASE_KEY, qss._lease_holder(), nx=True, ex=qss.LEASE_TTL_SECONDS)
        qss.release_lease()
        assert qss.LEASE_KEY not in lease_redis.store


def test_release_never_raises_without_redis():
    with patch("redis_breaker_util.get_breaker_redis", return_value=None):
        qss.release_lease()


def test_the_holder_id_is_this_process_and_stable():
    import os

    holder = qss._lease_holder()
    assert holder == qss._lease_holder()
    assert holder.startswith(f"{os.getpid()}:")


def test_the_maintenance_tick_still_drains_when_another_worker_holds_the_lease(lease_redis):
    """The lease gates the monitor only: expiry, the drain, the breaker
    backstop and `canary:drain_tick_at` keep running in every worker."""
    import asyncio

    from unittest.mock import AsyncMock

    from services.capacity_manager import CapacityManager

    _import_real_modules()
    lease_redis.set(qss.LEASE_KEY, "4242:other", nx=True, ex=qss.LEASE_TTL_SECONDS)
    stubs = _Stubs([_head(minutes_ago=45)])
    cm = CapacityManager.__new__(CapacityManager)
    cm._backlog = MagicMock(expire_stale=AsyncMock(), drain_orphans_all=AsyncMock())
    cm._backstop_open_breaker_backlog = AsyncMock()
    cm._redis = MagicMock()
    database = MagicMock()
    database.db = stubs.db
    with patch.dict(sys.modules, {"database": database}), \
            patch("redis_breaker_util.get_breaker_redis", return_value=lease_redis), \
            patch("services.redelivery_governor.get_redelivery_governor",
                  return_value=MagicMock(should_hold_reaper=MagicMock(return_value=False))):
        asyncio.run(cm.run_maintenance())

    stubs.db.oldest_queued_head_per_agent.assert_not_called()
    cm._backlog.expire_stale.assert_awaited()
    cm._backlog.drain_orphans_all.assert_awaited()
    cm._backstop_open_breaker_backlog.assert_awaited()
    assert cm._redis.set.call_args.args[0] == "canary:drain_tick_at"


# ---------------------------------------------------------------------------
# Registration and wiring (the lines without which none of the above runs)
# ---------------------------------------------------------------------------


def test_the_alert_kind_is_registered():
    """`observe` refuses an unregistered kind, so the monitor would be silent."""
    from services import platform_alerts

    assert qss.ALERT_KIND in platform_alerts.KINDS
    kind = platform_alerts.KINDS[qss.ALERT_KIND]
    assert kind.prefix == "queue-stall-"
    assert kind.clears == platform_alerts.CLEARS_HOOK
    # #3254: the episode marker, so a person's ending snoozes this stall only.
    assert "episode_started_at" in kind.material_keys


def test_the_alert_prefix_is_platform_reserved():
    """Unreserved, an agent could pre-create the id of the row that says its
    own queue is stuck and suppress it through the create's ON CONFLICT."""
    from services.operator_queue_service import _RESERVED_ID_PREFIXES

    assert "queue-stall-" in _RESERVED_ID_PREFIXES


def test_the_maintenance_tick_calls_run_once():
    """The hand-off line: without it nothing ever ticks the monitor."""
    src = (_BACKEND / "services" / "capacity_manager.py").read_text()
    tree = ast.parse(src)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run_once"
        and getattr(node.func.value, "id", None) == "queue_stall_service"
    ]
    assert calls, "capacity_manager.run_maintenance no longer ticks the monitor"


def test_shutdown_releases_the_monitors_lease():
    """The hand-off line for `release_lease` (#4635): without it a restarted
    worker waits out the TTL, and every alert due in that window is late by
    it. `release_lease` itself is executed above; this pins that a shutdown
    step the lifespan awaits calls it."""
    tree = ast.parse((_BACKEND / "main.py").read_text())
    releasing = [
        fn.name for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "release_lease"
                for node in ast.walk(fn))
    ]
    assert releasing, "no shutdown step releases the queue-stall lease"
    awaited = {
        node.value.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
    }
    assert set(releasing) & awaited, f"{releasing} is never awaited by the lifespan"


def test_the_threshold_default_matches_the_documented_30_minutes():
    from services import settings_service

    assert settings_service.QUEUE_STALL_ALERT_MINUTES_DEFAULT == 30
    assert qss.DEFAULT_STALL_MINUTES == 30
