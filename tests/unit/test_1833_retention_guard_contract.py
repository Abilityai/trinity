"""#1833 — `retention_guard.evaluate` refuses instead of raising.

The guard's docstring said "NEVER raises" while `candidates <= threshold` sat
OUTSIDE the try that wraps `count_fn`, so a `count_fn` returning `None` — a very
ordinary thing to write on an error path — raised `TypeError` straight out of a
safety device whose entire job is gating destructive retention prunes.

WHY IT MATTERED EVEN THOUGH IT WAS UNREACHABLE
----------------------------------------------
A raise never caused a destructive pass: every `cleanup_service` call site invokes
the guard BEFORE its `db.prune_*` and inside a try. What it destroyed was the
SIGNAL — `announce_refusal` was never reached, so there was no operator-queue
alarm and no "REFUSED" ERROR, only a nondescript "Error pruning ...". And
`_guard_allows` is not the only consumer: `GET /api/settings/retention` calls
`evaluate` UNWRAPPED, so the same input was a 500 on the panel an operator uses
to approve the very prune that refused.

TWO FIXES IN THE SAME SIX LINES THAT THE ISSUE DID NOT NAME
-----------------------------------------------------------
* `count_negative` — `-1 <= threshold` is True and `-1` is this module's own
  "unknown" sentinel, so a `count_fn` reporting failure with the module's own
  idiom ALLOWED the prune. A fail-OPEN inside a fail-closed guard, and since the
  count does not bound the delete, that is #1638 replayed.
* `candidates` is normalised to an int before publication — it is interpolated
  into the alarm message, `json.dumps`'d into the alarm context, and returned as
  `candidate_count` by `GET /api/settings/retention`, and a non-finite float
  emits a bare `NaN` that a browser's `JSON.parse` rejects.

HOUSE RULE THIS FILE OBEYS (docs/memory/learnings.md:291)
---------------------------------------------------------
"Assert the STATE a failure produces, not that it was swallowed." No test here is
a bare `does_not_raise`: every error path asserts the refusing verdict, its
reason, and — where it is the point — the alarm the refusal produces.

Invocation: `cd tests && python -m pytest unit/test_1833_retention_guard_contract.py`.
"""

import json
import logging
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional
from unittest.mock import MagicMock

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

import services.retention_guard as _RG  # noqa: E402
from services import cleanup_service as _CS  # noqa: E402

_KEY = "execution_row_retention_days"


# ---------------------------------------------------------------------------
# Doubles + isolation
# ---------------------------------------------------------------------------


class _DbDouble:
    """In-memory stand-in for the four `db` methods this subsystem touches.

    Signatures verified against `database.py`: `get_setting_value(key, default)`,
    `set_setting(key, value)`, `delete_setting(key)`,
    `create_operator_queue_item(agent_name, item)`.
    """

    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.queue_items = []
        self.raise_on_create = False

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def create_operator_queue_item(self, agent_name, item):
        if self.raise_on_create:
            raise RuntimeError("operator queue is down")
        self.queue_items.append((agent_name, item))
        return item["id"]


@pytest.fixture(autouse=True)
def _reset_episodes():
    """`retention_guard._refusal_episodes` outlives every test in the session, and
    since #1834 each entry carries a `_clock()` stamp and a retry budget. CI runs
    the whole unit suite in one process under `pytest-randomly` with three seeds."""
    _RG.reset_transition_memo()
    yield
    _RG.reset_transition_memo()


@pytest.fixture
def guard_db(monkeypatch):
    """Patch `db` BY OBJECT on the guard module.

    `retention_guard.py` does `from database import db`, binding its own
    module-global — patching `database.db` is inert here (learnings.md:99)."""
    double = _DbDouble()
    monkeypatch.setattr(_RG, "db", double)
    return double


# ---------------------------------------------------------------------------
# A — the contract: every failure REFUSES, and nothing raises
# ---------------------------------------------------------------------------


class TestEvaluateDoesNotRaise:

    def test_a_count_that_cannot_be_compared_refuses_instead_of_raising(
        self, guard_db, caplog
    ):
        """T-A1 — the #1833 headline.

        `lambda limit: None` is the issue's own example: an ordinary error-path
        return from a future `count_fn`. It must produce a REFUSING verdict with
        its own reason, an unknown-count sentinel, and an ERROR the operator can
        find — not a `TypeError` that the caller's outer `except` logs as
        "Error pruning ...".
        """
        with caplog.at_level(logging.ERROR, logger=_RG.logger.name):
            v = _RG.evaluate(_KEY, 90, lambda limit: None)

        assert v.allowed is False
        assert v.reason == "count_uninterpretable"
        assert v.candidates == -1
        assert any("REFUSING prune" in r.getMessage() for r in caplog.records), (
            "a refusal the operator cannot see is the failure mode #1833 is about"
        )

    @pytest.mark.parametrize(
        "value",
        [None, "12", "", object(), MagicMock(), {}, [], b"1", (1,), Decimal],
        ids=["none", "str", "empty-str", "object", "magicmock", "dict", "list",
             "bytes", "tuple", "type"],
    )
    def test_every_uninterpretable_count_shape_refuses(self, value, guard_db):
        """T-A2 — the shape space, not one example.

        Each of these raises `TypeError` on `<= int` today (mock's comparison
        dunders default to `NotImplemented`, and so does the reflected
        `int.__ge__`), so before #1833 every one of them escaped `evaluate`.
        """
        v = _RG.evaluate(_KEY, 90, lambda limit: value)
        assert (v.allowed, v.reason) == (False, "count_uninterpretable"), value

    def test_a_raising_count_fn_still_reports_count_failed(self, guard_db):
        """T-A3 — the two failure modes stay DISTINGUISHABLE after the try split.

        "the DB read blew up" and "the callable returned a non-number" have
        different owners and different fixes; one merged reason would put the
        wrong diagnosis in a durable operator alarm. `count_failed`'s exact string
        is also pinned by `test_1644::TestFailsClosed`.
        """
        def boom(limit):
            raise RuntimeError("db is down")

        v = _RG.evaluate(_KEY, 90, boom)
        assert (v.allowed, v.reason, v.candidates) == (False, "count_failed", -1)

    def test_numeric_and_float_counts_are_unaffected(self, guard_db):
        """T-A4 — the deliberate NON-change.

        Floats keep flowing down the comparison path. NaN refuses without an ack
        (`nan <= t` is False) and is ALLOWED with one — pinned by
        `test_1771a_retention_edges` and preserved here, because the obvious
        "just `isinstance(candidates, int)`" hardening would silently break it.
        """
        assert _RG.evaluate(_KEY, 90, lambda limit: 0).reason == "under_threshold"
        assert _RG.evaluate(_KEY, 90, lambda limit: 10**12).reason == "over_threshold"
        assert _RG.evaluate(_KEY, 90, lambda limit: 0.5).reason == "under_threshold"
        assert _RG.evaluate(_KEY, 90, lambda limit: float("inf")).reason == "over_threshold"

        nan = float("nan")
        assert _RG.evaluate(_KEY, 90, lambda limit: nan).reason == "over_threshold"
        _RG.record_acknowledgement(_KEY, 90)
        assert _RG.evaluate(_KEY, 90, lambda limit: nan).reason == "acknowledged"

    def test_a_truthy_non_bool_comparison_refuses(self, guard_db):
        """T-A5 — the real fail-open, and the reason the check is `type(...) is
        not bool` rather than `bool(...)`.

        A `bool()`-only implementation would compute `bool("under") is True` and
        return `allowed=True` — the guard AUTHORISING a prune on a count it never
        understood, a silent fail-OPEN introduced by the fix for a fail-closed
        bug. Nothing raises on this path, so no `except` catches it. This test
        FAILS against a `bool()`-only implementation, which is the only reason it
        exists.
        """
        class TruthyLe:
            def __le__(self, other):
                return "under"          # truthy, not a bool

            def __lt__(self, other):
                return False            # a real bool: only `__le__` is hostile

        class TruthyLt:
            def __le__(self, other):
                return False            # a real bool: only `__lt__` is hostile

            def __lt__(self, other):
                return "negative"       # truthy, not a bool

        for hostile in (TruthyLe(), TruthyLt()):
            v = _RG.evaluate(_KEY, 90, lambda limit, h=hostile: h)
            assert (v.allowed, v.reason) == (False, "count_uninterpretable"), hostile

    @pytest.mark.parametrize("count", [-1, -5, -(10**9)])
    def test_a_negative_count_refuses(self, count, guard_db):
        """T-A6 — replaces the old "a negative count is allowed through" pin.

        No real count is negative (all 8 accessors are `int(COUNT(*))`), so a
        negative return is an error sentinel — and `-1` is the one THIS module
        uses for "unknown", which is what made it dangerous: a `count_fn` that
        reported failure in the module's own idiom authorised an unbounded prune.
        """
        v = _RG.evaluate(_KEY, 90, lambda limit: count)
        assert (v.allowed, v.reason) == (False, "count_negative")
        assert v.candidates == -1

    def test_no_refusal_path_raises_when_the_exception_str_raises(self, guard_db):
        """The sibling of `test_1834::test_an_exception_whose_str_raises_does_not_
        escape`, for the function #1833 is actually named after.

        `evaluate`'s docstring promises "does not raise", and all three of its
        `except` branches passed the raw exception into deferred `%s`. That looks
        safe — `%s` formatting is logging's problem, and in production
        `handleError` prints to stderr and carries on — but pytest's
        `LogCaptureHandler` RE-RAISES, and measured before the fix `evaluate` raised
        straight out of a REFUSAL under a re-raising handler. `_describe_exception`
        was added for exactly this class and applied only to `announce_refusal`;
        this test is why the sibling could not stay unfixed.

        Fourth appearance of the incomplete-fix class this module keeps hitting: fix
        the site the reasoning points at, leave the one beside it.
        """
        class Hostile(RuntimeError):
            def __str__(self):
                raise ValueError("even my message is broken")

        def raising_count(limit):
            raise Hostile()

        v = _RG.evaluate(_KEY, 90, raising_count)          # must not raise
        assert (v.allowed, v.reason) == (False, "count_failed")

        def hostile_ack(setting_key, window_days):
            raise Hostile()

        import unittest.mock as _mock
        with _mock.patch.object(_RG, "is_acknowledged", hostile_ack):
            v = _RG.evaluate(_KEY, 90, lambda limit: 10**6)  # must not raise
        assert (v.allowed, v.reason) == (False, "ack_lookup_failed")

    def test_a_negative_count_with_a_hostile_repr_still_refuses(self, guard_db):
        """The `count_negative` log used to interpolate the raw foreign value.

        Reaching that branch only proves the object answered two comparisons with
        real bools — it says nothing about `__str__`, so the same deferred-`%s`
        escape applied. The value diagnoses nothing its type does not, so the log
        names the type.
        """
        class BadStr:
            def __le__(self, other):
                return True

            def __lt__(self, other):
                return True          # negative -> the count_negative branch

            def __str__(self):
                raise ValueError("nope")

            __repr__ = __str__

        v = _RG.evaluate(_KEY, 90, lambda limit: BadStr())   # must not raise
        assert (v.allowed, v.reason) == (False, "count_negative")
        assert v.candidates == -1

    def test_the_uninterpretable_log_names_which_comparison_misbehaved(
        self, guard_db, caplog
    ):
        """Reporting only `__le__`'s type produced the useless "comparison returned
        bool, expected bool" whenever it was `__lt__` that misbehaved."""
        class LeOkLtBad:
            def __le__(self, other):
                return True          # a real bool

            def __lt__(self, other):
                return "nope"        # not a bool

        with caplog.at_level(logging.ERROR):
            v = _RG.evaluate(_KEY, 90, lambda limit: LeOkLtBad())
        assert v.reason == "count_uninterpretable"
        assert "__lt__ returned str" in caplog.text, caplog.text

    def test_negative_and_uninterpretable_are_distinguishable(self, guard_db):
        """Three error reasons, three different fixes. Collapsing them would put
        "your DB is down" in the alarm for "your accessor returns a string"."""
        reasons = {
            _RG.evaluate(_KEY, 90, lambda limit: None).reason,
            _RG.evaluate(_KEY, 90, lambda limit: -1).reason,
            _RG.evaluate(
                _KEY, 90, lambda limit: (_ for _ in ()).throw(RuntimeError("x"))
            ).reason,
        }
        assert reasons == {"count_uninterpretable", "count_negative", "count_failed"}


class TestPublishedCountIsAlwaysAnInt:

    @pytest.mark.parametrize(
        "count",
        [0, 5, 10**12, 0.5, float("nan"), float("inf"), float("-inf"),
         Decimal("3"), True, None, "12", -7],
        ids=["zero", "small", "huge", "float", "nan", "inf", "-inf", "decimal",
             "bool", "none", "str", "negative"],
    )
    def test_the_verdict_candidate_count_is_always_an_int(self, count, guard_db):
        """T-A8 — one normalisation, three surfaces.

        `verdict.candidates` is interpolated into the alarm message, serialised
        into the alarm `context`, and returned as `candidate_count` by
        `GET /api/settings/retention`. `json.dumps` emits a bare `NaN` for a
        non-finite float — accepted by Python's `json.loads`, REJECTED by a
        browser's `JSON.parse`, which would break the two surfaces the alarm
        exists to reach. `allow_nan=False` is the strict parser, standing in for
        the browser.
        """
        v = _RG.evaluate(_KEY, 90, lambda limit: count)
        assert type(v.candidates) is int, f"{count!r} -> {v.candidates!r}"

        if not v.allowed:
            _RG.announce_refusal(_KEY, "rows", 90, v)
            _agent, item = guard_db.queue_items[-1]
            json.dumps(item["context"], allow_nan=False)  # must not raise

    def test_a_bool_count_is_not_reported_as_a_count(self, guard_db):
        """`isinstance(True, int)` is True — a bool count is a defect, not a
        count, so `type(...) is int` is deliberate rather than incidental."""
        v = _RG.evaluate(_KEY, 90, lambda limit: True)
        assert v.candidates == -1


# ---------------------------------------------------------------------------
# The refusal must reach the operator, and must not prescribe an impossible fix
# ---------------------------------------------------------------------------


class TestRefusalReachesTheOperator:

    def test_an_uninterpretable_count_reaches_the_operator_queue(self, monkeypatch):
        """T-A7 — end-to-end through the real `cleanup_service` adapter.

        This is the signal a raise destroys. Before #1833 the same input raised
        out of `_guard_allows`, the sweep's outer `except` logged
        "Error pruning ...", and NO alarm row was ever written.
        """
        double = _DbDouble()
        monkeypatch.setattr(_RG, "db", double)

        allowed = _CS._guard_allows(_KEY, "execution rows", 90, lambda limit: None)

        assert allowed is False, "an uninterpretable count must never permit a prune"
        assert len(double.queue_items) == 1, (
            "the refusal must produce exactly one durable operator alarm"
        )
        _agent, item = double.queue_items[0]
        assert _agent == _RG.ALARM_AGENT_NAME
        assert item["context"]["reason"] == "count_uninterpretable"

    @pytest.mark.parametrize(
        "count_fn, reason",
        [
            (lambda limit: None, "count_uninterpretable"),
            (lambda limit: -3, "count_negative"),
            (lambda limit: (_ for _ in ()).throw(RuntimeError("x")), "count_failed"),
        ],
        ids=["uninterpretable", "negative", "failed"],
    )
    def test_an_undecidable_refusal_does_not_tell_the_operator_to_acknowledge(
        self, count_fn, reason, guard_db
    ):
        """T-A10 — the alarm must not prescribe a remedy that cannot work.

        `evaluate` returns at each of these reasons BEFORE `is_acknowledged` is
        ever consulted, so an acknowledgement CANNOT clear them. The generic
        message is false twice for an error state ("-1 candidate(s) exceeds
        threshold 1000" is nonsense as well), and it is the durable alarm's
        `question` — the single most visible place to ship the very defect #1833
        is about: a device whose stated contract and behaviour disagree.
        """
        v = _RG.evaluate(_KEY, 90, count_fn)
        assert v.reason == reason
        _RG.announce_refusal(_KEY, "execution rows", 90, v)

        _agent, item = guard_db.queue_items[0]
        question = item["question"]
        assert "could not determine the blast radius" in question
        assert "will NOT clear this" in question
        assert "/api/settings/retention/acknowledge" not in question, (
            "an ack is never read for this reason — telling the operator to send "
            "one sends them somewhere that cannot help"
        )

    def test_an_unrepresentable_over_threshold_count_is_not_reported_as_minus_one(
        self, guard_db
    ):
        """The nonsense string `_UNDECIDABLE_REASONS` was created to prevent could
        still be emitted by the branch it does NOT cover.

        A NaN / inf / Decimal count compares fine, so it refuses as an APPROVABLE
        `over_threshold` — but publication normalises it to the `-1` unknown
        sentinel, and the generic wording then rendered exactly
        "-1 candidate(s) exceeds threshold 1000", the string plan §B.4b quotes as
        its motivation.

        It gets its own wording rather than the undecidable one, because the
        undecidable text would be a FRESH lie here: an acknowledgement really does
        clear this path (asserted below), since `is_acknowledged` is reached. Drop
        the impossible number, keep the instruction that works.
        """
        v = _RG.evaluate(_KEY, 90, lambda limit: float("nan"))
        assert (v.allowed, v.reason, v.candidates) == (False, "over_threshold", -1)

        _RG.announce_refusal(_KEY, "execution rows", 90, v)
        _agent, item = guard_db.queue_items[0]
        question = item["question"]

        assert "-1 candidate(s)" not in question, question
        assert "not a whole number" in question
        # The remedy that DOES work must survive.
        assert "/api/settings/retention/acknowledge" in question
        assert "will NOT clear this" not in question, (
            "an ack does clear this path — saying otherwise would be a new lie"
        )

        # Proof of that claim, not an assumption.
        _RG.record_acknowledgement(_KEY, 90)
        after = _RG.evaluate(_KEY, 90, lambda limit: float("nan"))
        assert (after.allowed, after.reason) == (True, "acknowledged")

    def test_an_over_threshold_refusal_still_asks_for_an_acknowledgement(
        self, guard_db
    ):
        """The other half: the approvable case must keep its instruction. A fix
        that muted BOTH would be a regression dressed as a correction."""
        v = _RG.evaluate(_KEY, 90, lambda limit: 10**6)
        assert v.reason == "over_threshold"
        _RG.announce_refusal(_KEY, "execution rows", 90, v)

        _agent, item = guard_db.queue_items[0]
        assert "/api/settings/retention/acknowledge" in item["question"]
        assert "candidate(s) exceeds threshold" in item["question"]


# ---------------------------------------------------------------------------
# The second, UNWRAPPED consumer: GET /api/settings/retention
# ---------------------------------------------------------------------------


@dataclass
class _Admin:
    """Explicit human-admin principal. NOT a MagicMock: a bare MagicMock has a
    truthy `.agent_name`, so it reads as an agent key and would exercise the
    wrong branch of `assert_admin` (#1816, re-hit in ent#293)."""
    id: int = 1
    username: str = "admin"
    email: Optional[str] = "admin@example.com"
    role: str = "admin"
    agent_name: Optional[str] = None
    connector_agent: Optional[str] = None
    mcp_scope: Optional[str] = None  # #2323


class _EndpointDb:
    """`db` for both the settings router and the guard: the endpoint reads the
    windows, the guard reads the acks, and both bind their own module global."""

    def __init__(self, agent_count):
        self._agent_count = agent_count
        self.settings = {
            "agent_soft_delete_retention_days": "180",
            "schedule_soft_delete_retention_days": "30",
        }
        self.queue_items = []

    def get_setting_value(self, key, default=None):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value

    def delete_setting(self, key):
        self.settings.pop(key, None)

    def count_soft_deleted_agents_past_retention(self, window, limit=None):
        return self._agent_count

    def count_soft_deleted_schedules_past_retention(self, window, limit=None):
        return 0

    def create_operator_queue_item(self, agent_name, item):
        self.queue_items.append((agent_name, item))
        return item["id"]


def _get_retention(monkeypatch, agent_count):
    import asyncio

    try:
        from routers import settings as mod
    except ImportError:  # pragma: no cover
        pytest.skip("backend venv required")

    double = _EndpointDb(agent_count)
    monkeypatch.setattr(mod, "db", double)
    monkeypatch.setattr(_RG, "db", double)
    return asyncio.run(mod.get_retention_status(current_user=_Admin()))


def test_the_settings_endpoint_does_not_500_and_reports_a_blocked_sweep(monkeypatch):
    """T-A9 — the caller that actually decided #1833's direction.

    `GET /api/settings/retention` calls `evaluate` UNWRAPPED (its only try is
    inside `_ops_int`). Before #1833 an uninterpretable count was an unhandled
    500 on the operator's own remediation surface — the panel that lists
    `pending_acknowledgements` and drives `POST /retention/acknowledge` — so the
    failure mode was "the operator can no longer authorize ANY sweep".

    Refusing instead is only half the answer: the sweep is then blocked forever
    in `cleanup_service` while the panel renders a clean "nothing pending", which
    is the guard's own anti-pattern ("a guard that fails open manufactures
    confidence") relocated to the operator surface. So it must appear — under
    `blocked_sweeps`, not `pending_acknowledgements`, because an acknowledgement
    cannot clear it.
    """
    body = _get_retention(monkeypatch, agent_count=None)   # uninterpretable

    keys_pending = [p["key"] for p in body["pending_acknowledgements"]]
    assert "agent_soft_delete_retention_days" not in keys_pending, (
        "an error state is not approvable — offering an approve control for it "
        "sends the operator somewhere that cannot help"
    )

    blocked = {b["key"]: b for b in body["blocked_sweeps"]}
    assert "agent_soft_delete_retention_days" in blocked
    assert blocked["agent_soft_delete_retention_days"]["reason"] == "count_uninterpretable"
    assert blocked["agent_soft_delete_retention_days"]["window_days"] == 180


def test_a_blocked_sweep_carries_identifiers_only(monkeypatch):
    """SECURITY, same rule as the alarm payload (canary G-04): reason codes and
    identifiers, never row content or samples."""
    body = _get_retention(monkeypatch, agent_count=None)
    assert body["blocked_sweeps"], "expected the sweep to be reported"
    for entry in body["blocked_sweeps"]:
        assert set(entry) == {"key", "window_days", "reason"}
        assert isinstance(entry["window_days"], int)
        assert isinstance(entry["reason"], str)


def test_a_healthy_fleet_reports_neither_pending_nor_blocked(monkeypatch):
    """The quiet case must stay quiet: a guard that reports something on a
    healthy install is a guard that gets ignored."""
    body = _get_retention(monkeypatch, agent_count=0)
    assert body["pending_acknowledgements"] == []
    assert body["blocked_sweeps"] == []


def test_a_genuine_over_threshold_sweep_is_still_pending_not_blocked(monkeypatch):
    """The pre-existing #1709 behaviour must be untouched — `blocked_sweeps` is
    additive, not a rerouting of the approvable case."""
    body = _get_retention(monkeypatch, agent_count=5)   # FLOOR_AGENTS is 0
    keys_pending = [p["key"] for p in body["pending_acknowledgements"]]
    assert "agent_soft_delete_retention_days" in keys_pending
    assert body["blocked_sweeps"] == []
