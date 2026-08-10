"""#1897 — a rejected canary Slack post must not count as a delivered alert.

`CanaryAlerts.emit_transition` was annotated ``-> None`` and dropped
`post_webhook`'s `success` flag on the floor, so `_run_cycle_inner` read "did
not raise" as "delivered": it counted the transition, advanced the cycle
cursor, and continuing-red suppression then silenced that invariant on every
later cycle. The alert was unrecoverable, and `cumulative_transitions` reported
it as sent.

These tests pin the delivery contract and the retry that follows from it.

## Why this file is under ``tests/unit/`` and not beside the canary suite

The issue's AC #6 points at ``tests/test_canary_invariants.py``, which **no CI
workflow executes** — ``backend-unit-test.yml`` and ``backend-unit-nightly.yml``
both run ``cd tests && python -m pytest unit/`` (filed as #2037, and recorded
the same way by ``test_1880_canary_alert_parity.py``, ``test_1881_canary_leader
_lease.py``, ``test_1813_h01_collector_blindness.py`` and
``test_ent337_r01_zombie_dwell.py``). Worse, on this base that suite does not
even run locally: ``tests/utils`` shadows ``src/backend/utils``, so
``TestCanaryService`` / ``TestCanarySlackPayload`` / ``TestCanarySlackEmit``
all error at setup. A guard placed only there would guard nothing. The sibling
test in that suite that *asserted the bug* is fixed too, but it cannot be run.

## Style

Imports are lazy, inside test bodies: ``services/__init__.py`` eagerly imports
``docker_service``, and ``services`` is a known pytest-randomly stub-leak
target (learnings 2026-07-05), so each test seeds the stubs it needs rather
than inheriting a sibling's.

Every assertion is on the STATE a failure produces — a counter value, a field
in the pending hash, a POST count — never merely "no exception escaped".
That distinction is the whole of learnings 2026-08-02: *catching the raise is
half the fix; propagating it into the state machine is the other half, and a
test that stops at "it was caught" cannot tell the two apart.* The test this
file replaces was literally named ``..._swallowed_cycle_continues``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import types
from typing import Dict, List, Optional

import pytest


WEBHOOK = "https://hooks.slack.com/services/TEST/TEST/TEST"
PENDING_KEY = "canary:alert_pending"
CURSOR_KEY = "canary:last_cycle_at"
_BASE_EPOCH = 1786132800  # 2026-08-07T00:00:00Z — fixed, never `time.time()`


def _ts(offset_seconds: int) -> str:
    """A snapshot_time `offset_seconds` after a fixed base instant.

    The cycle clock is read from `snapshot.snapshot_time`, so driving THAT is
    how these tests move time — never by sleeping and never from the test's own
    clock, which would race the implementation's (learnings 2026-08-03 /
    #1909).
    """
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(_BASE_EPOCH + offset_seconds, tz=timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class FakeRedis:
    """String + HASH double for the canary's cycle-state side table.

    Deliberately shareable between two `CanaryService` instances — that is the
    whole point of T18 below.
    """

    def __init__(self) -> None:
        self.strings: Dict[str, str] = {}
        self.hashes: Dict[str, Dict[str, str]] = {}
        self.fail_hash_read = False
        self.fail_hash_write = False

    # STRING ---------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        return self.strings.get(key)

    def set(self, key: str, value: str, ex: Optional[int] = None) -> bool:
        self.strings[key] = str(value)
        return True

    def delete(self, key: str) -> int:
        return 1 if self.strings.pop(key, None) is not None else 0

    # HASH -----------------------------------------------------------------

    def hset(self, key: str, field: str, value: str) -> int:
        if self.fail_hash_write:
            raise RuntimeError("read-only replica")
        bucket = self.hashes.setdefault(key, {})
        added = 0 if field in bucket else 1
        bucket[field] = str(value)
        return added

    def hgetall(self, key: str) -> Dict[str, str]:
        if self.fail_hash_read:
            raise RuntimeError("redis down")
        return dict(self.hashes.get(key, {}))

    def hdel(self, key: str, *fields: str) -> int:
        if self.fail_hash_write:
            raise RuntimeError("read-only replica")
        bucket = self.hashes.get(key)
        if not bucket:
            return 0
        removed = sum(1 for f in fields if bucket.pop(f, None) is not None)
        # Real Redis drops a hash that loses its last field. Modelled so a
        # key-absence assertion cannot pass here and mean nothing in prod —
        # though the assertions below are all phrased as FIELD absence, which
        # is the property that actually matters.
        if not bucket:
            self.hashes.pop(key, None)
        return removed

    # Test conveniences ----------------------------------------------------

    def pending(self) -> Dict[str, dict]:
        """Parsed pending records, by invariant id."""
        return {
            inv: json.loads(raw)
            for inv, raw in self.hashes.get(PENDING_KEY, {}).items()
        }

    def pending_raw(self) -> Dict[str, str]:
        return dict(self.hashes.get(PENDING_KEY, {}))


class FakeDB:
    """`database.db` stand-in: remembers the latest violation per invariant."""

    def __init__(self) -> None:
        self.rows: List[tuple] = []
        self.latest: Dict[str, dict] = {}
        self.fail_latest = False
        # T12: the "lying detector" state — the read succeeds but never
        # advances, so every cycle looks like a fresh green→red flip.
        self.freeze_latest = False

    def get_latest_canary_violation_per_invariant(self) -> Dict[str, dict]:
        if self.fail_latest:
            raise RuntimeError("could not connect to server")
        if self.freeze_latest:
            return {}
        return {inv: dict(row) for inv, row in self.latest.items()}

    def insert_canary_violation(self, **kwargs) -> int:
        self.rows.append((kwargs["invariant_id"], kwargs["snapshot_time"]))
        self.latest[kwargs["invariant_id"]] = {
            "snapshot_time": kwargs["snapshot_time"]
        }
        return len(self.rows)

    def count(self, invariant_id: str) -> int:
        return sum(1 for inv, _ in self.rows if inv == invariant_id)


_INV_IN_TEXT = re.compile(r"canary ([A-Z]-\d\d)")


class SlackRecorder:
    """Records webhook POSTs and lets a test choose the outcome per call."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.fail_all = False
        self.fail_for: set = set()
        self.error = "invalid_token"
        self.raise_cancelled = False

    async def post_webhook(self, webhook_url, text, blocks=None, timeout_seconds=5.0):
        if self.raise_cancelled:
            # Not a subclass of Exception — this is the deploy-window case.
            raise asyncio.CancelledError()
        self.calls.append({"url": webhook_url, "text": text, "blocks": blocks})
        match = _INV_IN_TEXT.search(text or "")
        invariant_id = match.group(1) if match else "?"
        if self.fail_all or invariant_id in self.fail_for:
            return (False, self.error)
        return (True, None)

    def posted_invariants(self) -> List[str]:
        out = []
        for call in self.calls:
            match = _INV_IN_TEXT.search(call["text"] or "")
            out.append(match.group(1) if match else "?")
        return out

    def context_of(self, index: int) -> str:
        blocks = self.calls[index]["blocks"] or []
        ctx = [b for b in blocks if b.get("type") == "context"]
        return ctx[-1]["elements"][0]["text"] if ctx else ""


class Harness:
    """A `CanaryService` with db, Redis, snapshot, invariants and sink stubbed.

    `red` is mutable so a test can flip an invariant green between cycles;
    `run(offset)` drives one cycle at a chosen snapshot_time.
    """

    def __init__(self, monkeypatch, *, webhook=True, interval=300, redis=None, db=None):
        from canary.snapshot import Snapshot, ViolationReport
        from services import canary_service as module

        self.module = module
        self.redis = redis if redis is not None else FakeRedis()
        self.db = db if db is not None else FakeDB()
        self.slack = SlackRecorder()
        self.red: List[str] = ["L-03"]

        fake_slack_module = types.ModuleType("services.slack_service")
        fake_slack_module.slack_service = self.slack
        monkeypatch.setitem(sys.modules, "services.slack_service", fake_slack_module)

        if webhook:
            monkeypatch.setenv("CANARY_SLACK_WEBHOOK_URL", WEBHOOK)
        else:
            monkeypatch.delenv("CANARY_SLACK_WEBHOOK_URL", raising=False)

        monkeypatch.setattr(module, "db", self.db)
        monkeypatch.setattr(
            module.CanaryService, "_redis", staticmethod(lambda: self.redis)
        )

        self._snapshot_time = _ts(0)

        def _collect():
            return Snapshot(snapshot_time=self._snapshot_time)

        def _run_invariants(snapshot, ids=None):
            return {
                inv: [
                    ViolationReport(
                        invariant_id=inv,
                        tier="A",
                        severity="critical",
                        observed_state={
                            "ghost_agent_name": f"ghost-{inv}",
                            "agent_name": f"agent-{inv}",
                        },
                        signal_query=f"{inv} synthetic fixture",
                    )
                ]
                for inv in self.red
            }

        monkeypatch.setattr(module, "collect_snapshot", _collect)
        monkeypatch.setattr(module, "run_invariants", _run_invariants)

        self.service = module.CanaryService(interval_seconds=interval)

    async def run(self, offset_seconds: int, red: Optional[List[str]] = None):
        if red is not None:
            self.red = list(red)
        self._snapshot_time = _ts(offset_seconds)
        return await self.service.run_cycle()


# ---------------------------------------------------------------------------
# T10 / AC #1 — the outcome is reported at all
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_transition_returns_the_delivery_outcome(monkeypatch):
    """AC #1. Three outcomes, because SKIPPED is not a failure.

    A bool would fold "no webhook configured" — the DEFAULT on every install
    that never wired the sink — into False, arming a retry for every
    transition and ending in a give-up ERROR on a correctly-configured silent
    sink. That is alarm fatigue shipped inside the fix for alarm loss.
    """
    from canary.snapshot import ViolationReport
    from services.canary_alerts import AlertDelivery, CanaryAlerts

    recorder = SlackRecorder()
    fake = types.ModuleType("services.slack_service")
    fake.slack_service = recorder
    monkeypatch.setitem(sys.modules, "services.slack_service", fake)

    violation = ViolationReport(
        invariant_id="L-03",
        tier="A",
        severity="critical",
        observed_state={"ghost_agent_name": "ghost-1"},
        signal_query="fixture",
    )
    args = dict(
        invariant_id="L-03",
        violations=[violation],
        snapshot_time=_ts(0),
        previous_violation_at=None,
        persisted_ids=[1],
    )

    monkeypatch.delenv("CANARY_SLACK_WEBHOOK_URL", raising=False)
    assert (await CanaryAlerts.emit_transition(**args)).outcome is AlertDelivery.SKIPPED
    assert recorder.calls == [], "a silent sink must not POST"

    monkeypatch.setenv("CANARY_SLACK_WEBHOOK_URL", WEBHOOK)
    assert (await CanaryAlerts.emit_transition(**args)).outcome is AlertDelivery.DELIVERED

    recorder.fail_all = True
    result = await CanaryAlerts.emit_transition(**args)
    assert result.outcome is AlertDelivery.FAILED
    assert result.error == "invalid_token", "the give-up ERROR needs a cause"


def test_emit_transition_keeps_its_parameter_names(monkeypatch):
    """`tests/unit/test_1987_instance_label.py` calls this with keyword args.

    The RETURN type changed; the signature must not. Pinned because "signature
    unchanged" is the kind of promise that decays into "roughly unchanged".
    """
    import inspect

    from services.canary_alerts import CanaryAlerts

    params = list(inspect.signature(CanaryAlerts.emit_transition).parameters)
    assert params == [
        "invariant_id",
        "violations",
        "snapshot_time",
        "previous_violation_at",
        "persisted_ids",
    ]


# ---------------------------------------------------------------------------
# T1 / T2 — AC #2 and AC #3's cursor half
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_post_is_not_counted_as_a_transition(monkeypatch):
    """AC #2 — the headline. The row still persists; the alert does not count."""
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    result = await h.run(0)

    assert h.service.cumulative_transitions == 0, "a rejected POST is not a delivery"
    assert "L-03" not in result.transition_invariant_ids
    assert result.undelivered_invariant_ids == ["L-03"]
    # Detection is a separate fact and must not be lost to satisfy the AC.
    assert h.service.cumulative_transitions_detected == 1
    # The evidence is unaffected — it never depended on Slack.
    assert h.db.count("L-03") == 1
    assert len(h.slack.calls) == 1


@pytest.mark.asyncio
async def test_failed_post_does_not_hold_back_the_cycle_cursor(monkeypatch):
    """AC #3 — the retry must NOT be built on the cycle-global cursor.

    Holding it back retries nothing (the invariant's own freshly inserted row
    already post-dates it, so it still reads as a continuation) while silently
    suppressing an unrelated red→green→red flip — a fresh instance of the very
    bug this issue is about. This pins the cursor against a future "helpful"
    withhold.
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    await h.run(0)

    assert h.redis.get(CURSOR_KEY) == _ts(0)


# ---------------------------------------------------------------------------
# T3 / T4 / T7 — the retry itself
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_undelivered_alert_is_retried_on_the_next_cycle_while_still_red(
    monkeypatch,
):
    """AC #3. Cycle 1 is rejected, cycle 2 (still red) re-attempts and lands."""
    h = Harness(monkeypatch)
    h.slack.fail_all = True
    await h.run(0)
    assert h.service.cumulative_transitions == 0

    h.slack.fail_all = False
    result = await h.run(300)

    assert h.slack.posted_invariants() == ["L-03", "L-03"]
    assert result.transition_invariant_ids == ["L-03"]
    assert result.undelivered_invariant_ids == []
    assert h.service.cumulative_transitions == 1
    # Cycle 2 was a continuation, not a second flip.
    assert h.service.cumulative_transitions_detected == 1
    assert "L-03" not in h.redis.pending_raw(), "delivery must clear the entry"


@pytest.mark.asyncio
async def test_successful_post_clears_pending_and_does_not_re_post(monkeypatch):
    """The continuing-red property is untouched when delivery succeeds."""
    h = Harness(monkeypatch)

    await h.run(0)
    await h.run(300)
    await h.run(600)

    assert len(h.slack.calls) == 1, "continuing-red must POST once, not every cycle"
    # FIELD absence, never key absence: an in-memory double can leave an empty
    # dict behind where real Redis deletes the hash, so a key assertion would
    # pass against the fake and mean nothing.
    assert "L-03" not in h.redis.pending_raw()


@pytest.mark.asyncio
async def test_a_second_invariants_failure_does_not_re_alert_the_first(monkeypatch):
    """AC #3 — "without re-alerting invariants that were delivered fine"."""
    h = Harness(monkeypatch)
    h.red = ["L-03", "S-01"]
    h.slack.fail_for = {"S-01"}

    await h.run(0)
    assert sorted(h.slack.posted_invariants()) == ["L-03", "S-01"]

    result = await h.run(300)

    assert h.slack.posted_invariants()[2:] == ["S-01"], (
        "only the invariant whose alert was lost may be re-attempted"
    )
    assert result.undelivered_invariant_ids == ["S-01"]
    assert "L-03" not in h.redis.pending_raw()


# ---------------------------------------------------------------------------
# T5 / T11 / T12 — AC #4 (bounded) and AC #5 (visible)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_is_bounded_and_gives_up_loudly(monkeypatch, caplog):
    """AC #4 + #5. Past the age budget we stop, once, and say why.

    Driven at the scheduled cadence — one cycle per interval — rather than by
    jumping the clock straight past the budget, because a single leap would
    exceed the run-decay gap instead and legitimately start a FRESH window.
    The two rules are load-bearing together: the budget bounds a contiguous
    outage, the decay is what makes it contiguous.
    """
    from services.canary_service import MAX_ALERT_PENDING_AGE_SECONDS

    h = Harness(monkeypatch)
    h.slack.fail_all = True
    interval = h.service.interval
    # The last cycle here is the first whose run age exceeds the budget.
    cycles = MAX_ALERT_PENDING_AGE_SECONDS // interval + 2

    with caplog.at_level(logging.ERROR, logger="services.canary_service"):
        for n in range(cycles):
            await h.run(n * interval)

    give_ups = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(give_ups) == 1, "exactly one give-up, not one per cycle"
    message = give_ups[0].getMessage()
    assert "L-03" in message
    assert "invalid_token" in message, "the give-up must name the cause"
    assert h.service.cumulative_alerts_dropped == 1
    assert "L-03" not in h.redis.pending_raw()
    assert len(h.slack.calls) == cycles, "at most one POST per invariant per cycle"

    posts_before = len(h.slack.calls)
    await h.run(cycles * interval)
    assert len(h.slack.calls) == posts_before, "a dropped alert must stay dropped"


@pytest.mark.asyncio
async def test_budget_window_decays_between_separated_failure_runs(monkeypatch):
    """Failures more than `3 × interval` apart are separate runs.

    Without the decay a brief flap hours ago would spend the window a later
    long outage needs, and the retry would give up on its first attempt. With
    a plain elapsed-since-first-ever-failure rule this cycle would be
    3000s > 1800s and drop the alert.
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    await h.run(0)
    await h.run(300)
    assert h.redis.pending()["L-03"]["first_failed_at"] == _ts(0)
    assert h.redis.pending()["L-03"]["attempts"] == 2

    await h.run(3000)  # 2700s since the last attempt — a new run

    record = h.redis.pending()["L-03"]
    assert record["first_failed_at"] == _ts(3000), "a separated failure re-anchors"
    assert record["attempts"] == 1
    assert h.service.cumulative_alerts_dropped == 0, "the new run gets a full window"


@pytest.mark.asyncio
async def test_budget_holds_when_a_transition_is_reported_every_cycle(monkeypatch):
    """The cap engages even against a detector that claims a flip every cycle.

    Driven directly through `previous_latest` (a frozen latest-violation read)
    rather than by making `insert_canary_violation` raise. Pinning the property
    through that pre-existing defect would make this test break when the
    defect is fixed, for reasons unrelated to #1897, and the next engineer
    would delete it.
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True
    h.db.freeze_latest = True

    for cycle in range(8):
        await h.run(cycle * 300)

    # One POST per cycle — the same rate as before this change, never
    # amplified by the retry machinery, because retries never stack.
    assert len(h.slack.calls) == 8
    assert h.service.cumulative_alerts_dropped == 1, "the age budget still fires"


@pytest.mark.asyncio
async def test_first_failed_at_is_first_write_wins(monkeypatch):
    """The budget anchor is carried forward verbatim, never re-stamped.

    Re-stamping an anchor every cycle is behaviourally indistinguishable from
    a correct implementation until the thing it anchors stops working — which
    is exactly how R-01's dwell went permanently blind (learnings 2026-08-05).
    Here it would silently mean "retry forever".
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    anchors = []
    attempts = []
    for cycle in range(3):
        await h.run(cycle * 300)
        record = h.redis.pending()["L-03"]
        anchors.append(record["first_failed_at"])
        attempts.append(record["attempts"])

    assert anchors == [_ts(0), _ts(0), _ts(0)]
    assert attempts == [1, 2, 3]


@pytest.mark.asyncio
async def test_manual_cycles_cannot_burn_the_retry_budget(monkeypatch):
    """`run_cycle()` is deliberately not leader-gated, so it must be floored.

    An admin smoke-testing during a Slack blip — the single most likely reason
    anyone touches `POST /api/canary/run-cycle` — would otherwise spend the
    whole window in seconds and force a give-up before the scheduled loop
    retried once. A failure mode the retry design itself creates.
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    await h.run(0)
    assert len(h.slack.calls) == 1

    for _ in range(3):
        result = await h.run(0)
        assert result.undelivered_invariant_ids == ["L-03"]

    assert len(h.slack.calls) == 1, "one attempt per interval, on every path"
    assert h.redis.pending()["L-03"]["attempts"] == 1, "the window is not consumed"


# ---------------------------------------------------------------------------
# T6 / T19 / T20 — staleness, cancellation, and the DB-down retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_alert_never_fires_while_the_invariant_is_green(monkeypatch):
    """A pending entry only ever acts on a cycle where its invariant is red.

    So a retry cannot alert for something that has recovered, and the payload
    is always rebuilt from the CURRENT cycle's violations rather than replayed.
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True
    await h.run(0)
    assert len(h.slack.calls) == 1

    await h.run(300, red=[])
    assert len(h.slack.calls) == 1, "a green cycle must not fire a pending alert"

    h.slack.fail_all = False
    await h.run(600, red=["L-03"])

    assert len(h.slack.calls) == 2
    assert _ts(600) in h.slack.context_of(-1), "the retry renders this cycle's state"
    assert _ts(0) not in h.slack.context_of(-1)


@pytest.mark.asyncio
async def test_cancellation_mid_post_leaves_the_alert_armed(monkeypatch):
    """The reason the entry is armed BEFORE the POST, not after it.

    `asyncio.CancelledError` is not a subclass of `Exception`, and `stop()`
    cancels a live cycle on lifespan shutdown — so a SIGTERM landing inside
    the 5s webhook await escapes the cycle's `except Exception` entirely:
    nothing would be armed, the cursor write never runs, and the next cycle
    reads the still-red invariant as a continuation. #1897 verbatim, on every
    deploy that coincides with a red cycle.
    """
    assert not issubclass(asyncio.CancelledError, Exception), (
        "the premise of this test — if this ever changes, revisit the ordering"
    )

    h = Harness(monkeypatch)
    await h.run(0, red=[])  # a green cycle, so the cursor is warm

    h.slack.raise_cancelled = True
    h.red = ["L-03"]
    with pytest.raises(asyncio.CancelledError):
        await h.run(300)

    assert "L-03" in h.redis.pending_raw(), "armed before the await, so it survives"
    # The cursor did NOT advance, and the row DID land — so on the next cycle
    # the invariant reads as a continuation and only the pending entry can
    # recover the alert.
    assert h.redis.get(CURSOR_KEY) == _ts(0)
    assert h.db.count("L-03") == 1

    h.slack.raise_cancelled = False
    result = await h.run(600)

    assert h.slack.posted_invariants() == ["L-03"]
    assert result.transition_invariant_ids == ["L-03"]


@pytest.mark.asyncio
async def test_retry_works_while_the_violation_db_read_is_failing(monkeypatch):
    """The retry keeps the `previous_violation_at` captured at detection time.

    During a DB outage `previous_latest` is empty, so recomputing would render
    the alert as a first-ever violation for something that has been red for
    hours. Storing it on the pending record is what makes the retried alert
    honest, and this is the interaction that makes that field load-bearing
    rather than decorative.
    """
    h = Harness(monkeypatch)

    await h.run(0)                 # delivered; L-03 now has history at _ts(0)
    await h.run(300, red=[])       # green
    h.slack.fail_all = True
    await h.run(600, red=["L-03"]) # red again → transition, POST rejected
    assert h.redis.pending()["L-03"]["previous_violation_at"] == _ts(0)

    h.slack.fail_all = False
    h.db.fail_latest = True        # the DB goes down before the retry
    await h.run(900)

    context = h.slack.context_of(-1)
    assert "first red" not in context, "a retry must not claim to be the first"
    assert "last red 15m ago" in context


# ---------------------------------------------------------------------------
# T8 / T9 / T14 / T15 / T16 / T21 — degradation and hostile input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_webhook_configured_is_not_a_failed_delivery(monkeypatch, caplog):
    """The default install: silent sink, and nothing to retry."""
    h = Harness(monkeypatch, webhook=False)

    with caplog.at_level(logging.ERROR, logger="services.canary_service"):
        result = await h.run(0)

    assert result.transition_invariant_ids == ["L-03"]
    assert result.undelivered_invariant_ids == []
    assert h.service.cumulative_transitions == 1
    assert h.slack.calls == []
    assert h.redis.pending_raw() == {}, "a silent sink arms nothing"
    assert [r for r in caplog.records if r.levelno == logging.ERROR] == []


@pytest.mark.asyncio
async def test_redis_unavailable_degrades_to_todays_behaviour(monkeypatch):
    """Fail-open in both directions. A retry that can wedge the harness is
    worse than the bug it fixes."""
    h = Harness(monkeypatch)
    h.redis.fail_hash_read = True
    h.redis.fail_hash_write = True

    delivered = await h.run(0)

    assert delivered.transition_invariant_ids == ["L-03"]
    assert h.service.cumulative_transitions == 1

    h.slack.fail_all = True
    failed = await h.run(300, red=["S-01"])

    # Still classified, still persisted, still not counted as delivered — the
    # only thing lost is the retry, which is exactly the pre-#1897 behaviour.
    assert failed.undelivered_invariant_ids == ["S-01"]
    assert h.service.cumulative_transitions == 1
    assert h.db.count("S-01") == 1


@pytest.mark.asyncio
async def test_emit_raising_is_treated_as_a_failed_delivery(monkeypatch):
    """A payload-builder crash is a failed delivery, not a third silent state."""
    h = Harness(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("header exceeds 150 chars")

    monkeypatch.setattr(h.module.CanaryAlerts, "emit_transition", _boom)

    result = await h.run(0)

    assert result.transition_invariant_ids == []
    assert result.undelivered_invariant_ids == ["L-03"]
    assert h.service.cumulative_transitions == 0
    assert "L-03" in h.redis.pending_raw(), "a raise must arm the retry too"


@pytest.mark.parametrize(
    "poison", ['{not json', '"a string"', '{"attempts": "abc"}', "[]"]
)
@pytest.mark.asyncio
async def test_corrupt_pending_value_cannot_break_a_cycle(monkeypatch, poison):
    """The pending record is parsed input, not something we trust because we
    wrote it. A poisoned value is treated as absent, never as a raise."""
    h = Harness(monkeypatch)
    h.redis.hashes[PENDING_KEY] = {"L-03": poison}

    result = await h.run(0)

    assert result.transition_invariant_ids == ["L-03"]
    assert h.service.cumulative_transitions == 1
    assert len(h.slack.calls) == 1


@pytest.mark.asyncio
async def test_one_corrupt_field_does_not_disable_retry_for_every_invariant(
    monkeypatch,
):
    """The whole hash arrives in ONE `HGETALL`, so parsing must be per field."""
    h = Harness(monkeypatch)
    h.slack.fail_all = True

    await h.run(0, red=["L-03", "S-01", "E-01"])
    assert len(h.slack.calls) == 3

    h.redis.hashes[PENDING_KEY]["E-01"] = "{not json"
    h.slack.fail_all = False

    await h.run(300)

    assert sorted(h.slack.posted_invariants()[3:]) == ["L-03", "S-01"], (
        "one unreadable field must not disable retry fleet-wide"
    )


@pytest.mark.asyncio
async def test_losing_the_pending_store_degrades_to_todays_behaviour(monkeypatch):
    """Redis is the least durable store here; say what happens when it blinks.

    The EVIDENCE is never at risk — it is in `canary_violations`, which is
    SQL — and the degradation is precisely the pre-#1897 behaviour, never
    worse. (An `alert_state` column on `canary_violations` would put delivery
    state in the same failure domain as the evidence; rejected on scope, not
    on merit.)
    """
    h = Harness(monkeypatch)
    h.slack.fail_all = True
    await h.run(0)
    assert "L-03" in h.redis.pending_raw()

    h.redis.hashes.clear()  # restart / eviction / FLUSHDB

    result = await h.run(300)

    assert len(h.slack.calls) == 1, "no retry — but no crash and no double-count"
    assert result.transition_invariant_ids == []
    assert h.service.cumulative_transitions == 0


# ---------------------------------------------------------------------------
# T18 — the test that distinguishes the design from a broken one
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_state_is_shared_not_per_instance(monkeypatch):
    """Prod runs `--workers 2`, and `run_cycle()` is not leader-gated.

    Every other test in this file passes against an implementation that keeps
    pending state in a per-instance `self._pending = {}`. This one does not:
    service B must honour the entry service A armed, over the same Redis.
    Same class as learnings 2026-08-05 — a double that derives the observed
    state FROM the state under test cannot witness divergence.
    """
    shared_redis = FakeRedis()
    shared_db = FakeDB()

    a = Harness(monkeypatch, redis=shared_redis, db=shared_db)
    a.slack.fail_all = True
    await a.run(0)
    assert "L-03" in shared_redis.pending_raw()

    b = Harness(monkeypatch, redis=shared_redis, db=shared_db)
    result = await b.run(300)

    assert b.slack.posted_invariants() == ["L-03"], (
        "the other worker must be able to complete A's undelivered alert"
    )
    assert result.transition_invariant_ids == ["L-03"]
    assert b.service.cumulative_transitions == 1
    assert "L-03" not in shared_redis.pending_raw()


@pytest.mark.asyncio
async def test_the_pending_key_is_the_documented_one(monkeypatch):
    """The Redis key is named in architecture.md and requirements §31.1.

    Global rather than `agent:`-keyed, so — like `canary:leader` and
    `canary:e02:*` — it is legitimately absent from #1560's
    `CLEARED_KEYSPACES`: clearing it on an agent lifecycle event would drop a
    pending fleet-level alert.
    """
    from services.canary_service import REDIS_KEY_ALERT_PENDING

    assert REDIS_KEY_ALERT_PENDING == PENDING_KEY
    assert not REDIS_KEY_ALERT_PENDING.startswith("agent:")
