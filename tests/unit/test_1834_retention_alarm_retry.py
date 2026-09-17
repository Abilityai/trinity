"""#1834 — a failed retention-guard alarm is retried, not permanently suppressed.

`announce_refusal` wrote its `_last_refused[key]` memo BEFORE the
`create_operator_queue_item` try. If the alarm write failed the memo already said
"seen", so every later cycle took the repeat branch and the durable,
operator-visible alarm was never written again for that (setting, window) — until
a restart or a window change.

WHAT IS LOST, PRECISELY
-----------------------
Not the refusal: that is still logged at ERROR on the cycle it happens, and the
prune is still blocked. What is lost is the half of the signal designed to
survive a log nobody is tailing. The prune then stays blocked with no queue item
explaining why — a silently stalled sweep, which is exactly the observability the
#1644 design leans on.

WHY RETRYING IS SAFE (the issue asked us to confirm this before changing it)
---------------------------------------------------------------------------
* `create_item` is `INSERT ... ON CONFLICT DO NOTHING` and `_alarm_id()` is the
  natural key, so a re-attempt is idempotent.
* The queue item AUTHORIZES NOTHING. The only thing that flips `allowed` is
  `is_acknowledged()` reading a `system_settings` row under the blocklisted
  `retention_ack_` prefix, written only by an admin-and-human-only endpoint. A
  retry can therefore neither re-authorize a prune nor wedge one. Pinned by
  `test_the_alarm_is_not_load_bearing_for_authorization`.
* "Delivered" means "the call did not raise", never "a row was inserted":
  `create_item` returns the id of the row that EXISTS, so on conflict it returns
  the pre-existing uuid. Pinned by `test_delivery_is_did_not_raise_not_rows_inserted`.

WHY THERE IS NO GIVE-UP (divergence from the #1897 precedent)
-------------------------------------------------------------
#1897 bounds its retries because its sink is an EXTERNAL webhook whose outage is
independent of the canary's own storage. This sink is the platform's own
database — the same DB the guard reads settings from — and DB incidents routinely
exceed 30 minutes. A give-up would mean: DB down 35 min, budget spent, DB
recovers, prune still refused, and NO queue item ever until a restart or a window
change. That is #1834's exact symptom shipped inside #1834's fix. Instead the
per-attempt WARNING escalates ONCE to ERROR. Pinned by
`test_retry_never_stops_and_escalates_exactly_once`.

Invocation: `cd tests && python -m pytest unit/test_1834_retention_alarm_retry.py`.
"""

import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import services.retention_guard as _RG  # noqa: E402
from services.cleanup_service import CleanupService  # noqa: E402

_CS = sys.modules[CleanupService.__module__]

_KEY = "execution_row_retention_days"


def _verdict(candidates=5000, threshold=1000, reason="over_threshold"):
    return _RG.GuardVerdict(False, candidates, threshold, reason)


# ---------------------------------------------------------------------------
# Doubles + isolation
# ---------------------------------------------------------------------------


class _DbDouble:
    """Emulates `create_operator_queue_item`'s REAL contract, which the retry
    logic depends on: `INSERT ... ON CONFLICT DO NOTHING` keyed on the item id,
    returning the id of the row that EXISTS (the pre-existing uuid on conflict),
    never a falsy "nothing inserted" sentinel."""

    def __init__(self):
        self.settings = {}
        self.rows = {}            # id -> item (the "table")
        self.create_calls = 0
        self.fail_next = 0        # how many upcoming create calls must raise
        self.raise_always = False

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def create_operator_queue_item(self, agent_name, item):
        self.create_calls += 1
        if self.raise_always or self.fail_next > 0:
            self.fail_next = max(0, self.fail_next - 1)
            raise RuntimeError("operator queue is down")
        self.rows.setdefault(item["id"], item)      # ON CONFLICT DO NOTHING
        return item["id"]                            # the row that EXISTS

    @property
    def queue_items(self):
        return list(self.rows.values())


@pytest.fixture(autouse=True)
def _reset_episodes():
    _RG.reset_transition_memo()
    yield
    _RG.reset_transition_memo()


@pytest.fixture
def guard_db(monkeypatch):
    """Patch `db` BY OBJECT on the guard module (learnings.md:99)."""
    double = _DbDouble()
    monkeypatch.setattr(_RG, "db", double)
    return double


@pytest.fixture
def fake_clock(monkeypatch):
    """Drive `_RG._clock` rather than `time.monotonic`.

    Patching the stdlib `time` module for the process is a flake factory: CI runs
    the whole unit suite in one process under `pytest-randomly` alongside
    Hypothesis. The guard binds `_clock` as a module attribute precisely so a test
    can move ONE symbol.
    """
    class _Clock:
        def __init__(self):
            self.t = 1000.0

        def __call__(self):
            return self.t

        def advance(self, seconds):
            self.t += seconds

    clock = _Clock()
    monkeypatch.setattr(_RG, "_clock", clock)
    return clock


# ---------------------------------------------------------------------------
# The bug
# ---------------------------------------------------------------------------


class TestFailedAlarmIsRetried:

    def test_a_failed_alarm_is_retried_on_the_next_cycle(self, guard_db):
        """T-B1 — the headline. FAILS on `dev`, where the memo was written before
        the attempt so the second call took the repeat branch and returned."""
        guard_db.fail_next = 1

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        assert guard_db.queue_items == [], "attempt 1 failed, so no row yet"

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 1, (
            "the next cycle must RE-ATTEMPT the alarm; on dev the memo already "
            "said 'seen' and no later cycle ever tried again"
        )

    def test_a_delivered_alarm_is_never_re_emitted(self, guard_db, caplog):
        """T-B2 — the anti-alarm-fatigue half, unchanged from today. 288 identical
        ERRORs a day is how an alert gets muted, and a muted alert IS #1638."""
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 1
        assert guard_db.create_calls == 1, "a delivered alarm must stop attempting"
        assert [r.levelno for r in caplog.records] == [logging.INFO, logging.INFO]

    def test_retry_never_stops_and_escalates_exactly_once(self, guard_db, fake_clock, caplog):
        """T-B3 — the reversal of the first draft: there is NO give-up.

        The threshold is read from the module, never hard-coded: an
        implementation that silently halved it would still pass a hard-coded
        test. Past the escalation age the WARNING becomes ONE ERROR and the
        attempts KEEP COMING — a give-up here would be #1834 shipped inside
        #1834's fix, because this sink's outages (the platform's own DB) routinely
        outlast any budget.
        """
        guard_db.raise_always = True
        cycle = 300.0
        cycles = int(_RG.ALARM_ESCALATION_AGE_SECONDS / cycle) + 6

        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            for _ in range(cycles):
                _RG.announce_refusal(_KEY, "rows", 5, _verdict())
                fake_clock.advance(cycle)

        assert guard_db.create_calls == cycles, (
            "every cycle must re-attempt for the life of the episode — "
            f"{guard_db.create_calls} attempts over {cycles} cycles"
        )
        assert guard_db.queue_items == []

        escalations = [
            r for r in caplog.records
            if "unable to write the refusal alarm" in r.getMessage()
        ]
        assert len(escalations) == 1, (
            "the escalation is a LEVEL change, once per episode — repeating it "
            "every cycle is the alarm fatigue this whole design avoids"
        )
        assert escalations[0].levelno == logging.ERROR
        assert "STILL RETRYING" in escalations[0].getMessage()

    def test_the_escalation_claims_only_what_this_worker_knows(
        self, guard_db, fake_clock, caplog
    ):
        """`cleanup_service` runs in EVERY uvicorn worker with no leader lease, so
        a sibling may have landed the row this worker could not. An escalation
        asserting "no operator-queue item exists" would send an operator hunting
        for a missing item that is present (#1897: no counter may mean two
        things, one level over)."""
        guard_db.raise_always = True
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        fake_clock.advance(_RG.ALARM_ESCALATION_AGE_SECONDS + 1)
        with caplog.at_level(logging.ERROR, logger=_RG.logger.name):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        msg = next(
            r.getMessage() for r in caplog.records
            if "unable to write the refusal alarm" in r.getMessage()
        )
        assert "this worker" in msg
        assert "another worker may have written it" in msg

    def test_the_transition_error_fires_once_per_episode_even_while_retrying(
        self, guard_db, caplog
    ):
        """T-B8 — retrying must not turn the refusal ERROR into a per-cycle one.

        Two independent log signals share this function: the green->red
        TRANSITION (ERROR once, then INFO) and the per-attempt alarm-write failure
        (WARNING). Retrying changes the second, and must not touch the first.
        """
        guard_db.raise_always = True
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            for _ in range(4):
                _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        refusals = [r for r in caplog.records if "REFUSED" in r.getMessage()]
        assert [r.levelno for r in refusals] == [
            logging.ERROR, logging.INFO, logging.INFO, logging.INFO
        ]
        warnings = [r for r in caplog.records if "could not raise alarm" in r.getMessage()]
        assert len(warnings) == 4, "each failed attempt reports itself"


    def test_an_exception_whose_str_raises_does_not_escape(self, guard_db):
        """`announce_refusal` says "Never raises", and the retry path introduced
        the first EAGERLY-formatted use of the exception (`last_error`).

        `logger.warning(..., e)` looks safe because `%s` formatting is deferred —
        but only in production, where `handleError` prints a traceback to stderr
        and carries on. pytest's `LogCaptureHandler` RE-RAISES it, which is how
        this test found the second site after the first was fixed. Both now go
        through `_describe_exception`. A function whose stated contract and actual
        behaviour disagree is #1833 itself, so it is not repeated here.
        """
        class Hostile(RuntimeError):
            def __str__(self):
                raise ValueError("even my message is broken")

        def sink(agent_name, item):
            raise Hostile()

        guard_db.create_operator_queue_item = sink

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())   # must not raise

        ep = _RG._refusal_episodes[_KEY]
        assert ep.alarm_delivered is False, "a failure must never read as delivery"
        assert ep.last_error == "Hostile", "degrade to the type name, not a crash"


class TestEpisodeBoundaries:

    def test_a_window_change_starts_a_fresh_episode(self, guard_db, caplog):
        """T-B4 — a narrowed window is a NEW blast radius; that narrowing IS the
        #1638 event, so it must re-alarm and reset the escalation."""
        _RG.announce_refusal(_KEY, "rows", 30, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 2
        assert logging.ERROR in [r.levelno for r in caplog.records]

    def test_a_reason_change_at_the_same_window_starts_a_fresh_episode(
        self, guard_db, caplog
    ):
        """Freshness keys on (window, REASON), not window alone.

        An `over_threshold` sweep that degrades into `count_uninterpretable`
        (#1833's new signal) at an unchanged window is a DIFFERENT problem with a
        different remedy — one is approvable, the other is not. Under a
        window-only key it produced no fresh ERROR and no new attempt, i.e.
        #1833's new signal silently swallowed by #1834's memo.
        """
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal(
                _KEY, "rows", 5, _verdict(candidates=-1, reason="count_uninterpretable")
            )
        assert logging.ERROR in [r.levelno for r in caplog.records], (
            "a reason change is a fresh episode and must shout"
        )

    def test_note_allowed_ends_the_episode(self, guard_db, caplog):
        """T-B5 — a sweep that recovers and then degrades again must shout again,
        and must re-attempt its alarm."""
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        _RG.note_allowed(_KEY)
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert guard_db.create_calls == 2
        assert logging.ERROR in [r.levelno for r in caplog.records]

    def test_a_window_change_abandons_an_undelivered_alarm_deliberately(
        self, guard_db, fake_clock
    ):
        """T-B11 — pinned so it is a DECISION, not an accident.

        A window change while the previous window's alarm is still undelivered
        starts a fresh episode and drops the old retry. That is correct: the new
        window is a new blast radius, it alarms immediately under its own natural
        key, and the abandoned attempt described a window that is no longer in
        force. Recorded because it is the one place the retry is bounded by
        something other than delivery.
        """
        guard_db.fail_next = 1
        _RG.announce_refusal(_KEY, "rows", 30, _verdict())
        assert guard_db.queue_items == []

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        ids = [i["id"] for i in guard_db.queue_items]
        assert ids == ["retention-guard-execution_row_retention_days-5"], (
            "the new window alarms; the abandoned 30-day attempt is not resumed"
        )


class TestTheAlarmAuthorizesNothing:

    def test_the_alarm_is_not_load_bearing_for_authorization(self, guard_db):
        """T-B6 — the #1644 invariant the retry must not disturb.

        A load-bearing queue item would wedge shut permanently once it reached
        any terminal state (Clear All -> `cancelled`), and `prune_terminal_items`
        would delete the approval at 90 days — the sweep deleting its own
        authorization. Both directions are asserted: a permanently failing alarm
        cannot BLOCK an acknowledged prune, and a delivered alarm cannot GRANT an
        unacknowledged one.
        """
        guard_db.raise_always = True
        _RG.record_acknowledgement(_KEY, 90)
        v = _RG.evaluate(_KEY, 90, lambda limit: 10**6)
        assert (v.allowed, v.reason) == (True, "acknowledged"), (
            "no queue row exists at all, yet the ack still authorizes"
        )

        guard_db.raise_always = False
        _RG.consume_acknowledgement(_KEY)
        v = _RG.evaluate(_KEY, 90, lambda limit: 10**6)
        _RG.announce_refusal(_KEY, "rows", 90, v)
        assert len(guard_db.queue_items) == 1
        v2 = _RG.evaluate(_KEY, 90, lambda limit: 10**6)
        assert v2.allowed is False, "a delivered alarm grants nothing"

    def test_delivery_is_did_not_raise_not_rows_inserted(self, guard_db):
        """T-B7 — the property that stops a two-worker deployment retrying forever.

        `create_item` is ON CONFLICT DO NOTHING and returns the id of the row that
        EXISTS. If "delivered" meant "a row was inserted", the second worker —
        whose write is always a conflict no-op — would re-attempt on every cycle,
        for the life of the refusal, in every process but the first.
        """
        item_id = "retention-guard-execution_row_retention_days-5"
        guard_db.rows[item_id] = {"id": item_id, "pre": "existing"}

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert guard_db.create_calls == 1, (
            "the conflicting write settled the episode; it must not retry"
        )
        assert guard_db.rows[item_id] == {"id": item_id, "pre": "existing"}

    def test_two_workers_converge_on_one_alarm(self, guard_db):
        """T-B10 — pins the in-process design argument.

        `cleanup_service` runs in every uvicorn worker with NO leader lease, so
        both processes already call `announce_refusal` and the natural key already
        absorbs it. Two independent loops make delivery MORE likely, not less:
        worker B can land the row worker A failed to write, and A's next attempt
        then conflicts and settles. Redis would add a failure domain whose own
        outage would have to fail open (suppressing the alarm) or closed
        (blocking) — reintroducing #1834 through the fix.
        """
        # Worker A: its write fails.
        guard_db.fail_next = 1
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        worker_a = dict(_RG._refusal_episodes)
        assert guard_db.queue_items == []

        # Worker B: a separate process, so a separate (empty) episode dict.
        _RG.reset_transition_memo()
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        assert len(guard_db.queue_items) == 1, "worker B landed the row"

        # Worker A's next cycle: the write conflicts, which IS delivery.
        _RG._refusal_episodes.clear()
        _RG._refusal_episodes.update(worker_a)
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert len(guard_db.queue_items) == 1, "still exactly one durable alarm"
        assert guard_db.create_calls == 3, (
            "A(fail) + B(ok) + A(conflict=delivered); A must not keep attempting"
        )


# ---------------------------------------------------------------------------
# Through a real cleanup cycle — the claim is literally "no later CYCLE retries"
# ---------------------------------------------------------------------------


def _make_service():
    svc = CleanupService(poll_interval=300)
    svc._reconcile_orphaned_executions = AsyncMock(return_value=(0, 0, set()))
    svc._process_stale_slot_reclaims = AsyncMock(return_value=None)
    return svc


def _configure_db(db, alarm_sink):
    """Only ONE sweep refuses: execution ROWS is over threshold, everything else
    is a sub-threshold trickle, so the alarm count is unambiguous."""
    for name, value in (
        ("mark_stale_executions_failed", 0),
        ("mark_no_session_executions_failed", 0),
        ("finalize_orphaned_skipped_executions", 0),
        ("mark_stale_activities_failed", 0),
        ("get_all_execution_timeouts", {}),
        ("cleanup_old_rate_limit_events", 0),
        ("delete_expired_and_revoked_shared_files", []),
        ("prune_execution_logs", 0),
        ("prune_execution_rows", 0),
        ("scrub_terminal_backlog_metadata", 0),
        ("cleanup_old_health_records", 0),
        ("find_soft_deleted_agents_past_retention", []),
        ("find_soft_deleted_schedules_past_retention", []),
        ("idempotency_purge_expired", 0),
        ("prune_agent_reports", 0),
        ("find_expired_leases", []),
        ("prune_operator_queue_terminal_items", 0),
        ("prune_agent_reminders", 0),
        ("count_execution_log_candidates", 1),
        ("count_health_check_candidates", 1),
        ("count_agent_reports_candidates", 1),
        ("count_operator_queue_terminal_candidates", 1),
        ("count_agent_reminders_candidates", 1),
        ("count_soft_deleted_agents_past_retention", 0),
        ("count_soft_deleted_schedules_past_retention", 0),
        # THE refusing sweep.
        ("count_execution_row_candidates", 10**6),
    ):
        getattr(db, name).return_value = value

    db.get_setting_value.side_effect = lambda key, default=None: default
    db.create_operator_queue_item.side_effect = alarm_sink


def _run_cycle(svc):
    return asyncio.run(svc._run_cleanup_inner())


def test_a_later_cleanup_cycle_actually_re_attempts(monkeypatch):
    """T-B9 — the bug is literally "no later CYCLE retries it".

    Every other test in this file calls `announce_refusal` directly, so it asserts
    the unit rather than the claim. This drives two real `_run_cleanup_inner`
    cycles: cycle 1's alarm write fails, cycle 2's succeeds, and the row must
    exist. On `dev` the memo was written before the attempt, so cycle 2 returned
    at the repeat branch and the durable alarm never appeared.
    """
    rows = {}
    calls = {"n": 0}

    # Scoped to THIS test's own window. A cycle refuses once per guarded sweep,
    # and the set of guarded sweeps grows (ent#433 added two), so counting every
    # alarm in the cycle would make this test fail whenever an unrelated
    # retention window is added — while proving nothing extra about the retry.
    KEY = "execution_row_retention_days"

    def sink(agent_name, item):
        if KEY not in item["id"]:
            # Another window's alarm — irrelevant to the retry under test, and
            # deliberately not recorded so the `rows == {}` assertion below
            # keeps meaning "THIS alarm has not landed yet".
            return item["id"]
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("operator queue is down")
        rows.setdefault(item["id"], item)
        return item["id"]

    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})

    with patch.object(_CS, "db") as db, \
            patch.object(_RG, "db", new=db), \
            patch.object(_CS, "get_capacity_manager", return_value=capacity), \
            patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
            patch.object(_CS, "_wal_checkpoint_truncate"):
        _configure_db(db, sink)

        report1 = _run_cycle(svc)
        assert calls["n"] == 1, (
            "cycle 1 must actually REACH the refusal — without this the "
            "'prune not called' assertion below would pass on a sweep that "
            "never ran at all"
        )
        assert rows == {}, "cycle 1's alarm write failed"
        assert db.prune_execution_rows.call_count == 0, (
            "the refusal must block the prune in BOTH cycles — the retry is about "
            "the alarm, never about the deletion"
        )

        report2 = _run_cycle(svc)

    assert calls["n"] == 2, "cycle 2 must re-attempt the alarm"
    assert len(rows) == 1, (
        "the durable operator alarm must exist after cycle 2; on dev no later "
        "cycle ever retried and the row never appeared"
    )
    assert report1.execution_rows_pruned == 0
    assert report2.execution_rows_pruned == 0
