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
* A re-attempt is idempotent. At #1834 that was `create_item`'s `INSERT ... ON
  CONFLICT DO NOTHING` on a natural-key id; since #3254 it is the seam's one
  pending row per subject, which a repeat reading updates.
* The queue item AUTHORIZES NOTHING. The only thing that flips `allowed` is
  `is_acknowledged()` reading a `system_settings` row under the blocklisted
  `retention_ack_` prefix, written only by an admin-and-human-only endpoint. A
  retry can therefore neither re-authorize a prune nor wedge one. Pinned by
  `test_the_alarm_is_not_load_bearing_for_authorization`.
* "Delivered" never meant "a row was inserted". At #1834 it was "the call did
  not raise" (`create_item` returned the id of the row that EXISTS); since #3254
  it is a delivered outcome of `platform_alerts.observe` (created / updated /
  snoozed), and a sibling worker's existing row is an `updated`. Pinned by
  `test_delivery_is_a_delivered_outcome_not_rows_inserted`.

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

#3254 RE-PINS (the alarm moved onto the platform-alert seam)
-------------------------------------------------------------
`announce_refusal` now reports through `platform_alerts.observe`, which writes
with `database.db.create_platform_operator_queue_item`; the subject is
`retention_refused:<setting>`, one pending row updated in place. Every asserted
outcome below is unchanged; each changed test says which class its re-pin is:
* write-observation point: the double used to stand in for
  `create_operator_queue_item` on `_RG.db`. The guard's settings stay on that
  double; the alarm's write is observed at the seam's create, passed through to
  a real database of the test's own, with the fault injection moved there (R5);
* id shape: the old id `retention-guard-<setting>-<window>` made a new window a
  second row. The subject is the setting, so a new window rewrites the one row;
* fixture shape: "delivered" is now an outcome of the seam (created / updated /
  snoozed), and a delivered alarm is re-read once per `ALARM_REFRESH_SECONDS`
  (R9, pinned in `test_3254_retention_alert.py`), so "never re-emitted" holds
  inside that period.

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
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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
    """The guard's own `db` (its settings and acks), as a dict.

    #3254 re-pin (write-observation point): the alarm no longer goes through
    this object. It is written by `platform_alerts.observe` with the seam's
    create, whose REAL contract the retry logic now depends on: one pending row
    per subject, a repeat reading updates it (`updated`, delivered), and only a
    raise inside the seam (`failed`) is undelivered. `seam_create` wraps that
    call against a real database of this file's own (`_own_db`), so the fault injection
    (`fail_next`, `raise_always`) and the attempt count move to the seam, and
    `queue_items` reads the alarm's rows back from the table."""

    def __init__(self):
        self.settings = {}
        self.create_calls = 0
        self.fail_next = 0        # how many upcoming create calls must raise
        self.raise_always = False

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def seam_create(self, real_create):
        def create(agent_name, item, **kw):
            self.create_calls += 1
            if self.raise_always or self.fail_next > 0:
                self.fail_next = max(0, self.fail_next - 1)
                raise RuntimeError("operator queue is down")
            return real_create(agent_name, item, **kw)
        return create

    @property
    def queue_items(self):
        """Every alarm row of this file's setting (the "table"), oldest first."""
        return _alarm_rows(_KEY)


def _alarm_rows(key, status=None):
    from database import db
    rows = db.list_operator_queue_items(agent_name=_RG.ALARM_AGENT_NAME, status=status, limit=500)
    rows = [r for r in rows if (r.get("context") or {}).get("setting_key") == key]
    return sorted(rows, key=lambda r: r["created_at"])


@pytest.fixture(scope="module")
def _own_db(tmp_path_factory):
    """A SQLite database of this file's own, for the whole module (#3254).

    The unit tier's default file is shared by every xdist worker (they inherit
    the controller's `TRINITY_DB_PATH`), and the cleanup cycles other files drive
    now end real `retention_refused` rows through `note_allowed`. Built with the
    real schema (`db_harness.bootstrap_schema`) once per module rather than once
    per test; the tests clear the alarm host's rows between them."""
    from db.engine import dispose_engines
    from db_harness import bootstrap_schema
    mp = pytest.MonkeyPatch()
    mp.setenv("TRINITY_DB_PATH", str(tmp_path_factory.mktemp("retention-alarm") / "trinity.db"))
    mp.delenv("DATABASE_URL", raising=False)
    dispose_engines()
    bootstrap_schema()
    yield
    mp.undo()
    dispose_engines()


def _clear_alarm_host():
    from sqlalchemy import text
    from db.engine import get_engine
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM operator_queue WHERE agent_name = :a"),
                     {"a": _RG.ALARM_AGENT_NAME})


def _route_seam(monkeypatch, wrap):
    """Wrap the seam's create on the object `observe` reads (`database.db`,
    resolved now, R6), and keep the seam's thin broadcast off the loop."""
    import database
    from services import operator_resume_service
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    real = database.db
    monkeypatch.setattr(real, "create_platform_operator_queue_item",
                        wrap(real.create_platform_operator_queue_item))


@pytest.fixture(autouse=True)
def _reset_episodes():
    _RG.reset_transition_memo()
    yield
    _RG.reset_transition_memo()


@pytest.fixture
def guard_db(monkeypatch, _own_db):
    """Patch `db` BY OBJECT on the guard module (learnings.md:99); the alarm's
    write is observed at the seam (#3254 re-pin, see `_DbDouble`), on this file's
    own database (`_own_db`), with the alarm host empty before and after."""
    double = _DbDouble()
    monkeypatch.setattr(_RG, "db", double)
    _route_seam(monkeypatch, double.seam_create)
    _clear_alarm_host()
    yield double
    _clear_alarm_host()


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
        ERRORs a day is how an alert gets muted, and a muted alert IS #1638.

        #3254 re-pin (fixture shape): "never" holds inside `ALARM_REFRESH_SECONDS`;
        R9's one re-read per period is pinned in `test_3254_retention_alert.py`.
        These three calls are milliseconds apart on the real clock."""
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
        this test found the second site after the first was fixed. Both went
        through `_describe_exception` until #3254, which moved the write onto the
        seam: `observe` now catches the exception and returns `failed`, and the
        guard stores and logs that outcome, never the exception. A function whose
        stated contract and actual behaviour disagree is #1833 itself, so it is
        not repeated here.
        """
        class Hostile(RuntimeError):
            def __str__(self):
                raise ValueError("even my message is broken")

        def sink(real_create):
            def create(agent_name, item, **kw):
                raise Hostile()
            return create

        # #3254 re-pin (write-observation point): the hostile exception is raised
        # inside the seam's create, where the alarm write now happens.
        import database
        with patch.object(database.db, "create_platform_operator_queue_item",
                          sink(None)):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())   # must not raise

        ep = _RG._refusal_episodes[_KEY]
        assert ep.alarm_delivered is False, "a failure must never read as delivery"
        # #3254 re-pin: the seam catches the exception and returns `failed`, so
        # the guard holds the outcome, never the exception or its broken `__str__`.
        assert ep.last_error == "observe returned failed", "an outcome, not a crash"


class TestEpisodeBoundaries:

    def test_a_window_change_starts_a_fresh_episode(self, guard_db, caplog):
        """T-B4 — a narrowed window is a NEW blast radius; that narrowing IS the
        #1638 event, so it must re-alarm and reset the escalation.

        #3254 re-pin (id shape): the old id carried the window, so the re-alarm
        was a second row. The subject is the setting: the re-alarm is a reading
        that reaches the seam at once and rewrites the one row to the new window."""
        _RG.announce_refusal(_KEY, "rows", 30, _verdict())
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=_RG.logger.name):
            _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert guard_db.create_calls == 2, "the new window re-alarms at once"
        [row] = guard_db.queue_items
        assert row["context"]["window_days"] == 5
        assert row["context"]["seen_count"] == 2
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

        # #3254 re-pin (id shape): the window is read from the row, not its id.
        windows = [i["context"]["window_days"] for i in guard_db.queue_items]
        assert windows == [5], (
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

    def test_delivery_is_a_delivered_outcome_not_rows_inserted(self, guard_db):
        """T-B7 — the property that stops a two-worker deployment retrying forever.

        The second worker's reading finds the first worker's pending row and
        updates it (`updated`). If "delivered" meant "a row was inserted"
        (`created`), that worker would re-attempt on every cycle, for the life of
        the refusal, in every process but the first. (#3254 renamed this test
        from `..._did_not_raise_...`: under the seam a failure does not raise
        either, it returns `failed`.)
        """
        # #3254 re-pin (write-observation point + id shape): "the row that
        # EXISTS" is now the subject's pending row, here a sibling worker's
        # reading, and finding it (`updated`) is the delivery.
        from services import platform_alerts
        platform_alerts.observe(
            _RG.ALARM_AGENT_NAME, "retention_refused", _KEY, title="t", question="q",
            priority="critical",
            context={"setting_key": _KEY, "window_days": 5, "reason": "over_threshold"})
        [existing] = guard_db.queue_items
        guard_db.create_calls = 0

        _RG.announce_refusal(_KEY, "rows", 5, _verdict())
        _RG.announce_refusal(_KEY, "rows", 5, _verdict())

        assert guard_db.create_calls == 1, (
            "the conflicting write settled the episode; it must not retry"
        )
        [row] = guard_db.queue_items
        assert row["id"] == existing["id"], "one alarm: the existing row, updated"
        assert row["context"]["seen_count"] == 2

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


def _configure_db(db):
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


def _run_cycle(svc):
    return asyncio.run(svc._run_cleanup_inner())


def test_a_later_cleanup_cycle_actually_re_attempts(monkeypatch, _own_db):
    """T-B9 — the bug is literally "no later CYCLE retries it".

    Every other test in this file calls `announce_refusal` directly, so it asserts
    the unit rather than the claim. This drives two real `_run_cleanup_inner`
    cycles: cycle 1's alarm write fails, cycle 2's succeeds, and the row must
    exist. On `dev` the memo was written before the attempt, so cycle 2 returned
    at the repeat branch and the durable alarm never appeared.
    """
    calls = {"n": 0}

    # Scoped to THIS test's own window. A cycle refuses once per guarded sweep,
    # and the set of guarded sweeps grows (ent#433 added two), so counting every
    # alarm in the cycle would make this test fail whenever an unrelated
    # retention window is added — while proving nothing extra about the retry.
    KEY = "execution_row_retention_days"

    # #3254 re-pin (write-observation point): the sink moves from the guard's
    # `db.create_operator_queue_item` to the seam's create, where the alarm is
    # now written; `rows` is read back from the table.
    def sink(real_create):
        def create(agent_name, item, *, subject, **kw):
            if subject != f"retention_refused:{KEY}":
                # Another window's alarm — irrelevant to the retry under test.
                return real_create(agent_name, item, subject=subject, **kw)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("operator queue is down")
            return real_create(agent_name, item, subject=subject, **kw)
        return create

    _route_seam(monkeypatch, sink)
    _clear_alarm_host()

    svc = _make_service()
    capacity = MagicMock()
    capacity.reclaim_stale = AsyncMock(return_value={})

    with patch.object(_CS, "db") as db, \
            patch.object(_RG, "db", new=db), \
            patch.object(_CS, "get_capacity_manager", return_value=capacity), \
            patch.object(_CS, "_read_retention_settings", return_value=(30, 90, 7, 90)), \
            patch.object(_CS, "_wal_checkpoint_truncate"):
        _configure_db(db)

        report1 = _run_cycle(svc)
        assert calls["n"] == 1, (
            "cycle 1 must actually REACH the refusal — without this the "
            "'prune not called' assertion below would pass on a sweep that "
            "never ran at all"
        )
        assert _alarm_rows(KEY) == [], "cycle 1's alarm write failed"
        assert db.prune_execution_rows.call_count == 0, (
            "the refusal must block the prune in BOTH cycles — the retry is about "
            "the alarm, never about the deletion"
        )

        report2 = _run_cycle(svc)

    assert calls["n"] == 2, "cycle 2 must re-attempt the alarm"
    assert len(_alarm_rows(KEY)) == 1, (
        "the durable operator alarm must exist after cycle 2; on dev no later "
        "cycle ever retried and the row never appeared"
    )
    assert report1.execution_rows_pruned == 0
    assert report2.execution_rows_pruned == 0
