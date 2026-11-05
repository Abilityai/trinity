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


class _Stubs:
    """The three modules `run_once` imports, replaced in sys.modules."""

    def __init__(self, heads, *, leased=0, running_conversations=()):
        self.db = MagicMock()
        self.db.oldest_queued_head_per_agent.return_value = heads
        self.db.count_active_leased_by_agent.side_effect = (
            lambda names: {n: leased for n in names}
        )
        self.db.agents_with_running_conversation.return_value = list(
            running_conversations
        )
        self.db.list_operator_queue_items.return_value = []
        self.alerts = MagicMock()
        self.alerts.observe.return_value = "filed"
        self.alerts.clear.return_value = 1


def _run(stubs, *, pilot=True, threshold=30, max_parallel=3, now=_NOW):
    database = MagicMock()
    database.db = stubs.db
    with patch.dict(sys.modules, {"database": database}), \
            patch.object(qss, "threshold_minutes", return_value=threshold), \
            patch("services.platform_alerts.observe", stubs.alerts.observe), \
            patch("services.platform_alerts.clear", stubs.alerts.clear), \
            patch("services.pull_pilot.is_pull_pilot_agent", return_value=pilot), \
            patch("services.settings_service.get_effective_max_parallel_tasks",
                  return_value=max_parallel), \
            patch("services.slot_service.get_slot_service",
                  side_effect=RuntimeError("no redis in the unit island")):
        return qss.run_once(now=now)


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
    stubs = _Stubs([_head(minutes_ago=2)])

    summary = _run(stubs)

    assert summary["observed"] == 0
    stubs.alerts.observe.assert_not_called()
    stubs.alerts.clear.assert_called_once()
    assert stubs.alerts.clear.call_args[0][:3] == ("alpha", qss.ALERT_KIND, "alpha")


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
    calls = {"n": 0}

    def _boom(names):
        calls["n"] += 1
        if names == ["bad"]:
            raise RuntimeError("db blew up for this agent")
        return {n: 0 for n in names}

    stubs.db.count_active_leased_by_agent.side_effect = _boom
    summary = _run(stubs)

    assert summary["errors"] == 1
    assert [c[0][0] for c in stubs.alerts.observe.call_args_list] == ["good"]


def test_run_once_never_raises_when_the_head_read_fails():
    stubs = _Stubs([])
    stubs.db.oldest_queued_head_per_agent.side_effect = RuntimeError("db down")

    summary = _run(stubs)

    assert summary["errors"] == 1
    stubs.alerts.observe.assert_not_called()


def test_run_once_logs_the_blocked_head_once_per_tick(caplog):
    stubs = _Stubs([_head(minutes_ago=45)])

    with caplog.at_level(logging.WARNING, logger=qss.__name__):
        _run(stubs)

    blocked = [r for r in caplog.records if "queue head blocked" in r.getMessage()]
    assert len(blocked) == 1
    assert "reason=worker_claim" in blocked[0].getMessage()


def test_run_once_with_an_empty_fleet_does_nothing():
    stubs = _Stubs([])

    summary = _run(stubs)

    assert summary == {"heads": 0, "observed": 0, "cleared": 0, "errors": 0}
    stubs.alerts.observe.assert_not_called()


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


def test_the_threshold_default_matches_the_documented_30_minutes():
    from services import settings_service

    assert settings_service.QUEUE_STALL_ALERT_MINUTES_DEFAULT == 30
    assert qss.DEFAULT_STALL_MINUTES == 30
